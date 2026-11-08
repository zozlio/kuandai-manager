"""AI 识图：从截图里读出每个机房的每日收益金额。

支持三种调用格式（设置页里选）：

1. ``minimax_vl``    —— MiniMax 官方 VL 接口，multipart 提交，图片单独一个字段
2. ``minimax_vl_json`` —— MiniMax VL 的 JSON 写法，图片放 body 的 image 字段
3. ``openai``        —— 通用 OpenAI 兼容多模态写法，content 数组里塞 image_url

不管哪种格式，返回给上层的都是同一个结构：一组「识别出来的行」。
"""

from __future__ import annotations

import base64
import json
import re

import httpx

from .config import get_settings

# 视觉模型配置：页面设置优先，其次环境变量
VISION_CONFIG_KEYS = ("vision_base_url", "vision_api_key", "vision_model", "vision_format")

DEFAULT_VISION_MODEL = "MiniMax-M3-VL-01"
DEFAULT_VISION_FORMAT = "minimax_vl"

VISION_FORMAT_LABELS = {
    "minimax_vl": "MiniMax VL（multipart）",
    "minimax_vl_json": "MiniMax VL（JSON）",
    "openai": "OpenAI 兼容多模态",
}

MIME_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
}

# 识图提示词：要求只吐 JSON 数组，并明确两级识别顺序
RECOGNIZE_PROMPT = """你是一个截图表格识别助手。请读取这张截图，提取出其中每一行「宽带的每日收益金额」。

识别顺序（非常重要）：
1. 第一识别目标：宽带账号。每行都要先确定它属于哪一条宽带账号（可能是手机号，也可能是宽带账号/业务账号）。
2. 第二识别目标：节点号。确定宽带账号后，找出这一行对应的节点号（服务器上的账号）。

请只输出一个 JSON 数组，不要任何解释文字、不要 markdown 代码块。每个元素包含：
- broadband：这一行所属的宽带账号标识（原样抄写截图里的内容，例如手机号或宽带账号）
- node_account：节点号（原样抄写截图里的节点号）
- platform：节点所在的平台名称（截图里没有就填空字符串）
- amount：这一行的收益金额（只写数字，不要带货币符号和千分位）
- day：日期，格式 YYYY-MM-DD（截图里没有日期就填空字符串）
- raw：这一行的原文，照抄截图里那一行的文字，方便人工核对

注意：
- 金额要准，不要四舍五入丢小数；负数保留负号。
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
    return (config.get("vision_model") or DEFAULT_VISION_MODEL).strip()


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
    with httpx.Client(timeout=get_settings().ai_timeout) as client:
        response = client.post(url, files=files, headers=headers)
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
    with httpx.Client(timeout=get_settings().ai_timeout) as client:
        response = client.post(url, json=payload, headers=headers)
    return _read_reply(response)


def _call_openai(config: dict, image_bytes: bytes, filename: str) -> str:
    """通用 OpenAI 兼容多模态写法。"""
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
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {_api_key(config)}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=get_settings().ai_timeout) as client:
        response = client.post(url, json=payload, headers=headers)
    return _read_reply(response)


CALLERS = {
    "minimax_vl": _call_minimax_vl,
    "minimax_vl_json": _call_minimax_vl_json,
    "openai": _call_openai,
}


def _read_reply(response: httpx.Response) -> str:
    if response.status_code >= 400:
        raise RuntimeError(
            f"视觉接口返回 {response.status_code}：{response.text[:300]}"
        )
    data = response.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(
            f"视觉接口返回格式异常：{json.dumps(data, ensure_ascii=False)[:300]}"
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
