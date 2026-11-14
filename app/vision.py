"""AI 识图：从截图里读出每个节点的每日收益金额。

支持三种调用格式（设置页里选）：

1. ``openai``            —— 通用 OpenAI 兼容多模态写法，content 数组里塞 image_url（默认）
2. ``minimax_vl``        —— MiniMax 官方 VL 接口，multipart 提交，图片单独一个字段
3. ``minimax_vl_json``   —— MiniMax VL 的 JSON 写法，图片放 body 的 image 字段

不管哪种格式，返回给上层的都是同一个结构：一组「识别出来的行」。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time

import httpx

from .config import get_settings
from .services import DEFAULT_VISION_FORMAT

logger = logging.getLogger("kuandai.vision")

# 视觉模型配置：页面设置优先，其次环境变量
VISION_CONFIG_KEYS = ("vision_base_url", "vision_api_key", "vision_model", "vision_format")

# 默认模型留空：识图必须用「视觉模型」，文本模型认不了图，
# 所以默认值不替用户猜，没填就提示去设置页填。
DEFAULT_VISION_MODEL = ""

VISION_FORMAT_LABELS = {
    "openai": "OpenAI 兼容多模态（通用，推荐）",
    "minimax_vl": "MiniMax VL（multipart）",
    "minimax_vl_json": "MiniMax VL（JSON）",
}

MIME_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
}

# 只对这些错误重试：限流 / 过载 / 网络断连。
# 400（请求本身有问题）和超时不重试——重试超时等于让用户多等一个超时窗口。
RETRY_STATUS = {429, 503}
RETRY_MAX = 2
RETRY_BACKOFFS = (2, 8)

# 识图提示词：要求只吐 JSON 数组，节点号是第一识别目标
RECOGNIZE_PROMPT = """你是一个截图表格识别助手。请读取这张截图，提取出其中每一行「节点的每日收益」。

识别顺序（非常重要）：
1. 第一识别目标：节点号（node_account，服务器上的账号编号，通常是一长串字母数字）。截图里几乎每行都有它，务必逐字符抄准。
2. 第二识别目标：宽带账号（broadband，可能是手机号或业务账号）。截图里没有这一列就留空，不要猜。

请只输出一个 JSON 数组，不要任何解释文字、不要 markdown 代码块。每个元素包含：
- node_account：节点号（原样抄写，逐字符，不要增删）
- broadband：这一行所属的宽带账号（截图里没有就填空字符串）
- platform：节点所在的平台名称（截图里没有就填空字符串）
- amount：这一行的收益金额（只写数字，不要带货币符号、+/- 号和千分位；金额旁边带百分号的是跑满率不是金额）
- day：日期，格式 YYYY-MM-DD（截图里没有日期就填空字符串）
- raw：这一行的原文，照抄截图里那一行的文字，方便人工核对

注意：
- 金额要准，不要四舍五入丢小数；负数保留负号。
- 「昨日跑满率 / 实时利用率」这类百分比不要当成金额输出。
- 看不清的字段就留空字符串，不要猜。
- 截图里有几行就输出几个元素。"""


def vision_configured(config: dict | None = None) -> bool:
    config = config or {}
    return bool(
        config.get("vision_model")
        and (config.get("vision_api_key") or config.get("ai_api_key"))
    )


def vision_status(config: dict | None = None) -> dict:
    config = config or {}
    return {
        "configured": vision_configured(config),
        "model": config.get("vision_model") or DEFAULT_VISION_MODEL,
        "base_url": config.get("vision_base_url") or "未配置",
        "format": config.get("vision_format") or DEFAULT_VISION_FORMAT,
    }


def _mime_of(filename: str) -> str:
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    return MIME_TYPES.get(ext, "image/png")


def _data_url(image_bytes: bytes, filename: str) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{_mime_of(filename)};base64,{encoded}"


def _base_url(config: dict) -> str:
    base = (
        config.get("vision_base_url")
        or config.get("ai_base_url")
        or ""
    ).strip().rstrip("/")
    if not base:
        raise RuntimeError("视觉模型未配置：请在「设置」页填写视觉接口地址、模型名和密钥")
    if "/v1" not in base:
        base = f"{base}/v1"
    return base


def _api_key(config: dict) -> str:
    key = (config.get("vision_api_key") or config.get("ai_api_key") or "").strip()
    if not key:
        raise RuntimeError("视觉模型未配置密钥")
    return key


def _model(config: dict) -> str:
    return (config.get("vision_model") or DEFAULT_VISION_MODEL or "").strip()


def _max_tokens(config: dict) -> int:
    """单次生成 token 上限。走设置项（数据库 ai_max_tokens 优先），
    其次环境变量 AI_MAX_TOKENS，默认 8192。不写死，因为不同端点
    的模型上限不同，写死会撞 400。"""
    for value in (config.get("ai_max_tokens"), get_settings().ai_max_tokens):
        try:
            number = int(str(value or "").strip())
            if number > 0:
                return number
        except ValueError:
            continue
    return 8192


def _vision_timeout(config: dict) -> float:
    """视觉接口超时（秒）。设置项（数据库 vision_timeout）优先，
    其次环境变量 VISION_TIMEOUT，默认 300。和 ai_max_tokens 同构：
    用户的端点快慢不同，必须给自救旋钮。"""
    for value in (config.get("vision_timeout"), get_settings().vision_timeout):
        try:
            number = float(str(value or "").strip())
            if number > 0:
                return number
        except ValueError:
            continue
    return 300.0


def _request_with_retry(url: str, config: dict, *, files=None, json_body=None,
                        headers=None) -> httpx.Response:
    """发一次视觉请求。只对 429/503/连接类错误重试（最多 2 次，退避 2s→8s）；
    400 和超时不重试——视觉一次超时就是几百秒，再重试等于让用户多等一轮。

    超时（ReadTimeout / WriteTimeout 等）和连不上都转成中文提示，
    httpx 的原始英文异常（如 'timed out'）只进服务端日志，不给前端。"""
    timeout = _vision_timeout(config)
    last_error: Exception | None = None
    for attempt in range(RETRY_MAX + 1):
        try:
            with httpx.Client(timeout=timeout) as client:
                if files is not None:
                    response = client.post(url, files=files, headers=headers)
                else:
                    response = client.post(url, json=json_body, headers=headers)
        except httpx.ConnectError as exc:
            last_error = exc
            if attempt >= RETRY_MAX:
                raise RuntimeError("网络连不上视觉接口，请检查接口地址或稍后再试") from exc
            time.sleep(RETRY_BACKOFFS[min(attempt, len(RETRY_BACKOFFS) - 1)])
            continue
        except httpx.TimeoutException as exc:
            # 读超时是视觉最常见的失败（图信息量大、端点慢），不再重试，
            # 直接给明确的自救提示
            raise RuntimeError(
                f"识别超时（超过 {timeout:g} 秒），这张图信息量太大或接口较慢。"
                "可换更快的模型、在「设置」里调大视觉超时，或裁剪截图只留收益表"
            ) from exc
        if response.status_code in RETRY_STATUS and attempt < RETRY_MAX:
            time.sleep(RETRY_BACKOFFS[min(attempt, len(RETRY_BACKOFFS) - 1)])
            continue
        return response
    # 理论上到不了这里（最后一次 attempt 一定 return），兜底
    if last_error is not None:
        raise RuntimeError("网络连不上视觉接口，请检查接口地址或稍后再试") from last_error
    raise RuntimeError("视觉接口持续不可用，请稍后再试")


# --------------------------------------------------------------------------
# 三种调用格式
# --------------------------------------------------------------------------

def _call_minimax_vl(config: dict, image_bytes: bytes, filename: str) -> str:
    """MiniMax 官方 VL 接口：multipart 提交，image 字段单独放图片。"""
    url = f"{_base_url(config)}/vl/chat/completions"
    messages = [
        {"role": "user", "content": f"<image>{RECOGNIZE_PROMPT}"},
    ]
    files = {
        "model": (None, _model(config)),
        "messages": (None, json.dumps(messages, ensure_ascii=False)),
        "image": (None, _data_url(image_bytes, filename)),
    }
    headers = {"Authorization": f"Bearer {_api_key(config)}"}
    response = _request_with_retry(url, config, files=files, headers=headers)
    return _read_reply(response)


def _call_minimax_vl_json(config: dict, image_bytes: bytes, filename: str) -> str:
    """MiniMax VL 的 JSON 写法：图片放 body 的 image 字段。"""
    url = f"{_base_url(config)}/vl/chat/completions"
    payload = {
        "model": _model(config),
        "messages": [{"role": "user", "content": f"<image>{RECOGNIZE_PROMPT}"}],
        "image": _data_url(image_bytes, filename),
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {_api_key(config)}",
        "Content-Type": "application/json",
    }
    response = _request_with_retry(url, config, json_body=payload, headers=headers)
    return _read_reply(response)


def _call_openai(config: dict, image_bytes: bytes, filename: str) -> str:
    """通用 OpenAI 兼容多模态写法（默认）。"""
    url = f"{_base_url(config)}/chat/completions"
    payload = {
        "model": _model(config),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": RECOGNIZE_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": _data_url(image_bytes, filename)},
                    },
                ],
            }
        ],
        "max_tokens": _max_tokens(config),
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {_api_key(config)}",
        "Content-Type": "application/json",
    }
    response = _request_with_retry(url, config, json_body=payload, headers=headers)
    return _read_reply(response)


CALLERS = {
    "minimax_vl": _call_minimax_vl,
    "minimax_vl_json": _call_minimax_vl_json,
    "openai": _call_openai,
}


def _read_reply(response: httpx.Response) -> str:
    if response.status_code >= 400:
        detail = response.text[:300]
        if response.status_code == 415:
            # 415 Unsupported Media Type：端点只认一种请求格式，而当前
            # 「视觉格式」和它不匹配（最常见：通用端点收到旧版
            # MiniMax multipart 写法）。原文进日志，给用户指路。
            logger.warning("视觉接口返回 415，原文：%s", detail)
            raise RuntimeError(
                "当前端点不接受这种请求格式（415 Unsupported Media Type）。"
                "请到「设置」页把「视觉格式」改成「OpenAI 兼容（通用）」再试——"
                "旧的 MiniMax multipart 格式只对 MiniMax 官方端点有效，"
                "和通用端点不匹配。"
            )
        if response.status_code == 400:
            # 请求本身有问题（模型名错 / 参数不支持 / 图片格式不对）
            raise RuntimeError(f"视觉接口拒绝了请求：{detail}")
        raise RuntimeError(
            f"视觉接口返回 {response.status_code}，请稍后再试。"
        )
    data = response.json()
    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content")
    if content is None:
        raise RuntimeError(
            f"视觉接口返回格式异常：{json.dumps(data, ensure_ascii=False)[:300]}"
        )
    # 输出被截断：模型生成到 max_tokens 上限就停了，JSON 多半是半截的，
    # 不能塞进解析器，直接报错让用户调大 max_tokens 或换模型。
    if choice.get("finish_reason") == "length":
        raise RuntimeError(
            "输出被截断，请调大「设置」里的最大生成 token 数（ai_max_tokens），或换更快的模型"
        )
    return (content or "").strip()


# --------------------------------------------------------------------------
# 解析模型返回的 JSON
# --------------------------------------------------------------------------

def parse_items(text: str) -> list[dict]:
    """从模型返回的文本里抠出 JSON 数组。模型爱加解释，这里尽量宽容。"""
    if not text:
        return []

    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()

    candidates = [text]
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            data = data.get("items") or data.get("rows") or data.get("data") or []
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def _clean_amount(value) -> float:
    """把模型给的各种金额写法变成数字。"""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    text = text.replace(",", "").replace("，", "")
    text = re.sub(r"[¥￥$元\s]", "", text)
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return 0.0
    try:
        return float(match.group(0))
    except ValueError:
        return 0.0


def normalize_items(items: list[dict]) -> list[dict]:
    """把模型输出的字段名统一成内部结构。"""
    result = []
    for index, item in enumerate(items):
        broadband = str(
            item.get("broadband")
            or item.get("account")
            or item.get("宽带账号")
            or item.get("宽带")
            or ""
        ).strip()
        node_account = str(
            item.get("node_account")
            or item.get("node")
            or item.get("node_name")
            or item.get("节点号")
            or item.get("节点")
            or item.get("机房")
            or ""
        ).strip()
        platform = str(
            item.get("platform") or item.get("平台") or item.get("平台名称") or ""
        ).strip()
        day = str(item.get("day") or item.get("date") or item.get("日期") or "").strip()
        day = day.replace("/", "-").replace(".", "-")
        if day and not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
            day = ""
        amount = _clean_amount(
            item.get("amount")
            if item.get("amount") is not None
            else item.get("income")
            if item.get("income") is not None
            else item.get("收益")
            if item.get("收益") is not None
            else item.get("金额")
        )
        raw = str(item.get("raw") or item.get("text") or "").strip()
        result.append(
            {
                "index": index,
                "broadband": broadband,
                "node_account": node_account,
                "platform": platform,
                "amount": amount,
                "day": day,
                "raw": raw,
            }
        )
    return result


def recognize_income(
    image_bytes: bytes, filename: str, config: dict
) -> list[dict]:
    """识图主入口：返回识别出来的每一行。"""
    fmt = (config.get("vision_format") or DEFAULT_VISION_FORMAT).strip()
    caller = CALLERS.get(fmt)
    if caller is None:
        raise RuntimeError(f"未知的视觉接口格式：{fmt}")

    reply = caller(config, image_bytes, filename)
    return normalize_items(parse_items(reply))
