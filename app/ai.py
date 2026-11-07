"""AI 能力：自然语言查询 + 催缴话术生成。

对接 OpenAI 兼容协议（/chat/completions），换 AI_BASE_URL 即可接入
MiniMax、DeepSeek、通义千问、OpenAI 等任意兼容服务。
"""

from __future__ import annotations

import json
import re

import httpx

from .config import get_settings
from .models import Account
from .services import account_flags, fmt_dt, money

PHONE_RE = re.compile(r"\d{11}")


def ai_configured(config: dict | None = None) -> bool:
    """三个参数都配齐了才算启用。"""
    config = config or {}
    return bool(
        config.get("ai_base_url") and config.get("ai_api_key") and config.get("ai_model")
    )


def ai_status(config: dict | None = None) -> dict:
    config = config or {}
    return {
        "configured": bool(
            config.get("ai_base_url")
            and config.get("ai_api_key")
            and config.get("ai_model")
        ),
        "model": config.get("ai_model") or "未配置",
        "base_url": config.get("ai_base_url") or "未配置",
    }


def _base_url(config: dict) -> str:
    base = (config.get("ai_base_url") or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("AI 功能未配置：请在「设置」页填写接口地址、密钥和模型名")
    if "/v1" not in base:
        base = f"{base}/v1"
    return base


def chat(messages: list[dict], config: dict, temperature: float | None = None) -> str:
    """调用一次 chat/completions，返回文本内容。"""
    settings = get_settings()
    if not ai_configured(config):
        raise RuntimeError("AI 功能未配置：请在「设置」页填写接口地址、密钥和模型名")

    payload = {
        "model": config["ai_model"],
        "messages": messages,
        "temperature": temperature if temperature is not None else settings.ai_temperature,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {config['ai_api_key']}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=settings.ai_timeout) as client:
        response = client.post(
            f"{_base_url(config)}/chat/completions", json=payload, headers=headers
        )
        if response.status_code >= 400:
            raise RuntimeError(f"AI 接口返回 {response.status_code}：{response.text[:300]}")
        data = response.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"AI 返回格式异常：{json.dumps(data, ensure_ascii=False)[:300]}")
    return (content or "").strip()


# --------------------------------------------------------------------------
# 上下文构造
# --------------------------------------------------------------------------

def account_context(accounts: list[Account]) -> str:
    """把账号记录拼成给模型看的纯文本，避免模型看到原始数据库。"""
    if not accounts:
        return "（当前没有任何账号记录）"
    lines = []
    for account in accounts:
        flags = account_flags(account)
        parts = [
            f"手机号 {account.phone}",
            f"宽带账号 {account.broadband_account or '无'}",
            f"注册码 {account.registration_code or '无'}",
            f"注册人 {account.registrant or '无'}",
            f"手机卡使用人 {account.sim_user or '无'}",
            f"注册时间 {fmt_dt(account.registered_at) or '未记录'}",
            f"运营商 {account.provider or '未记录'}",
            f"套餐原价 {money(account.monthly_fee)} 元",
            f"实际月费 {flags['fee']} 元"
            + ("（七折）" if flags["discount"] else ""),
            f"每日扣款 {flags['daily']} 元",
            f"余额 {flags['balance']} 元",
            f"累计充值 {money(account.total_recharged)} 元",
            f"本月已扣 {money(account.month_deducted_amount)} 元",
            f"状态 {flags['badge']}",
        ]
        if account.notes:
            parts.append(f"备注 {account.notes}")
        lines.append("- " + " | ".join(parts))
    return "\n".join(lines)


def match_accounts(question: str, accounts: list[Account]) -> list[Account]:
    """先按手机号/宽带账号定位相关记录，减少无关上下文。"""
    compact = re.sub(r"[\s\-]", "", question)
    phones = set(PHONE_RE.findall(compact))
    if phones:
        matched = [a for a in accounts if re.sub(r"[\s\-]", "", a.phone or "") in phones]
        if matched:
            return matched

    lowered = question.lower()
    fuzzy = [
        a for a in accounts if a.broadband_account and a.broadband_account.lower() in lowered
    ]
    if fuzzy:
        return fuzzy

    # 按人名找：注册人 / 手机卡使用人
    by_name = [
        a
        for a in accounts
        if (a.registrant and a.registrant in question)
        or (a.sim_user and a.sim_user in question)
    ]
    if by_name:
        return by_name
    return []


# --------------------------------------------------------------------------
# 对外的两个能力
# --------------------------------------------------------------------------

def answer_question(question: str, accounts: list[Account], config: dict) -> dict:
    """自然语言查询。

    有明确对象时只把相关记录交给模型；问的是整体情况（例如"哪些要催缴"）
    时把全部记录交给模型。
    """
    matched = match_accounts(question, accounts)
    scope = matched if matched else accounts
    scope_note = (
        f"用户问到了具体账号，以下 {len(scope)} 条是与问题相关的记录。"
        if matched
        else f"以下是全部 {len(scope)} 条账号记录。"
    )

    system = (
        "你是一个宽带账号管理助手，负责帮运营人员查询台账、统计应缴金额、"
        "找出需要催缴的账号并给出建议。"
        "规则：1) 只能使用下面给出的记录，不要编造账号、金额或时间；"
        "2) 金额一律直接引用记录里的数字；"
        "3) 回答用简洁的中文，可以用简短的分点；"
        "4) 如果记录里没有相关信息，直接说明没有。"
    )
    user = (
        f"{scope_note}\n\n账号记录：\n{account_context(scope)}\n\n"
        f"用户问题：{question}"
    )
    reply = chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        config,
        temperature=get_settings().ai_temperature,
    )
    return {"answer": reply, "scope": len(scope), "focused": bool(matched)}


def generate_reminder_message(account: Account, config: dict) -> str:
    """给单个账号生成一条催缴话术。"""
    flags = account_flags(account)
    gap = flags["gap"] if flags["gap"] > 0 else flags["fee"]

    system = (
        "你是宽带业务客服，要写一条发给客户本人的催缴短信。"
        "要求：中文、80字以内、语气礼貌但明确，"
        "必须包含建议充值金额，不要编造账号或具体日期。"
    )
    user = (
        f"客户手机号：{account.phone}\n"
        f"宽带账号：{account.broadband_account or '无'}\n"
        f"每月套餐费：{flags['fee']} 元\n"
        f"当前余额：{flags['balance']} 元\n"
        f"建议充值金额：{gap} 元\n"
        f"备注：{account.notes or '无'}"
    )
    return chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        config,
        temperature=0.7,
    )


def generate_collection_advice(accounts: list[Account], config: dict) -> str:
    """批量催缴建议：按优先级给运营人员一个处理顺序。"""
    targets = [a for a in accounts if account_flags(a)["level"] != "ok"]
    if not targets:
        return "当前没有需要催缴的账号，全部正常。"

    system = (
        "你是宽带业务运营助手。依据给出的待催缴账号，按紧急程度排出处理顺序，"
        "并为每个账号给一句处理建议。简洁中文分点，不要编造数据。"
    )
    user = f"待催缴账号：\n{account_context(targets)}"
    return chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        config,
        temperature=0.4,
    )


def test_connection(config: dict) -> str:
    """发一条最短的消息，确认接口地址 / 密钥 / 模型名是对的。"""
    reply = chat(
        [
            {"role": "system", "content": "你是一个连接测试助手，只回复两个字：可用"},
            {"role": "user", "content": "连接测试"},
        ],
        config,
        temperature=0.0,
    )
    return reply or "（接口返回了空内容）"
