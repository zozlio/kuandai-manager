"""业务逻辑：日扣费、余额、提醒、统计。"""

from __future__ import annotations

import calendar
import logging
import math
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from .config import DEFAULT_AI_BASE_URL, DEFAULT_AI_MODEL, get_settings
from .models import (
    ABNORMAL_REASONS,
    Account,
    AccountGroup,
    AppSetting,
    DailyIncome,
    Reminder,
    Transaction,
    CATEGORY_ABNORMAL,
    CATEGORY_NORMAL,
    INCOME_SOURCE_AI,
    INCOME_SOURCE_MANUAL,
    STATUS_ACTIVE,
    TX_CHARGE,
    TX_RECHARGE,
    TX_ADJUST,
    REMIND_UNPAID,
    REMIND_LOW_BALANCE,
    REMIND_MANUAL,
)

CENT = Decimal("0.01")

logger = logging.getLogger("kuandai.services")

# AI 接口配置的三个键：页面设置优先，其次环境变量
AI_CONFIG_KEYS = ("ai_base_url", "ai_api_key", "ai_model")

# 视觉模型（识图）配置的四个键
VISION_CONFIG_KEYS = ("vision_base_url", "vision_api_key", "vision_model", "vision_format")

# 视觉模型默认值
DEFAULT_VISION_MODEL = "MiniMax-M3-VL-01"
DEFAULT_VISION_FORMAT = "minimax_vl"

# 七折
DISCOUNT_RATE = Decimal("0.7")

# 开号赠送金额
INITIAL_BALANCE = Decimal("500.00")

# 套餐原价
DEFAULT_MONTHLY_FEE = Decimal("399.00")

# 提醒级别
LEVEL_OK = "ok"
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_DANGER = "danger"


def money(value) -> Decimal:
    """把任何输入规整成两位小数的金额。"""
    if value is None:
        return Decimal("0.00")
    try:
        return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


def to_float(value) -> float:
    return float(money(value))


def current_period(day: date | None = None) -> str:
    return (day or date.today()).strftime("%Y-%m")


def fmt_dt(value: datetime | None) -> str:
    if not value:
        return ""
    return value.strftime("%Y-%m-%d %H:%M")


def fix_text(value: str) -> str:
    """修复个别客户端直传 UTF-8 原始字节造成的乱码。"""
    if not value:
        return value
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


# --------------------------------------------------------------------------
# 套餐与计费
# --------------------------------------------------------------------------

def effective_monthly_fee(account: Account) -> Decimal:
    """实际月费：开启七折后按原价的 70% 计。"""
    fee = money(account.monthly_fee)
    if account.discount_enabled:
        fee = money(fee * DISCOUNT_RATE)
    return fee


def days_in_month(day: date) -> int:
    return calendar.monthrange(day.year, day.month)[1]


def daily_amount(account: Account, day: date) -> Decimal:
    """某一天应扣多少钱。

    优先用账号上单独填的「每日扣费」；没填才按「月费 ÷ 当月天数」分摊；
    月末最后一天把余数补齐，保证整月扣款金额刚好等于月费，不多不少。
    """
    fixed = money(account.daily_cost)
    if fixed > 0:
        return fixed

    fee = effective_monthly_fee(account)
    if fee <= 0:
        return Decimal("0.00")

    total_days = days_in_month(day)
    already = money(account.month_deducted_amount)

    if day.day == total_days and already > 0:
        remaining = money(fee - already)
        return remaining if remaining > 0 else Decimal("0.00")

    return money(fee / total_days)


def account_flags(account: Account, today: date | None = None) -> dict:
    """账号当前状态，列表页、详情页和 AI 上下文都用它。"""
    today = today or date.today()
    period = current_period(today)
    today_str = today.strftime("%Y-%m-%d")

    fee = effective_monthly_fee(account)
    balance = money(account.balance)
    active = account.status == STATUS_ACTIVE
    daily = daily_amount(account, today)
    deducted_today = (account.last_deducted_date or "") == today_str

    low_balance = active and fee > 0 and balance < daily   # 今天就扣不动
    month_short = active and fee > 0 and balance < fee      # 撑不满本月
    gap = money(fee - balance) if month_short else Decimal("0.00")
    days_left = max(1, days_in_month(today) - today.day + 1)

    if low_balance:
        level, badge = LEVEL_DANGER, "余额不足"
    elif month_short:
        level, badge = LEVEL_WARN, "余额偏少"
    else:
        level, badge = LEVEL_OK, "正常"

    return {
        "fee": fee,
        "balance": balance,
        "daily": daily,
        "deducted_today": deducted_today,
        "low_balance": low_balance,
        "month_short": month_short,
        "gap": gap,
        "days_left": days_left,
        "level": level,
        "badge": badge,
        "period": period,
        "today": today_str,
        "discount": bool(account.discount_enabled),
    }


def search_accounts(
    db: Session, keyword: str = "", status: str = "", category: str = ""
) -> list[Account]:
    """按关键字 / 状态 / 分类筛选账号。

    category 传 "normal" 或 "abnormal" 时只出对应分类（首页两个标签页用）。
    """
    query = db.query(Account)
    keyword = fix_text(keyword.strip())
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            Account.phone.like(like)
            | Account.broadband_account.like(like)
            | Account.registration_code.like(like)
            | Account.registrant.like(like)
            | Account.sim_user.like(like)
            | Account.notes.like(like)
            | Account.install_address.like(like)
            | Account.room.like(like)
            | Account.platform.like(like)
            | Account.node_account.like(like)
        )
    if category:
        query = query.filter(Account.category == category)
    if status == "remind":
        accounts = query.order_by(Account.phone).all()
        return [a for a in accounts if account_flags(a)["level"] != LEVEL_OK]
    if status:
        query = query.filter(Account.status == status)
    return query.order_by(Account.phone).all()


# --------------------------------------------------------------------------
# 提醒
# --------------------------------------------------------------------------

def _unpaid_message(account: Account, gap: Decimal, daily: Decimal) -> str:
    return (
        f"{account.phone} 余额 {money(account.balance)} 元，"
        f"今日需扣 {daily} 元，还差 {gap} 元，请及时充值。"
    )


def _low_balance_message(account: Account, flags: dict) -> str:
    return (
        f"{account.phone} 当前余额 {flags['balance']} 元，"
        f"本月套餐 {flags['fee']} 元（每日扣 {flags['daily']} 元），"
        f"按此余额预计 {flags['days_left']} 天后用完，建议充值 {flags['gap']} 元。"
    )


def upsert_reminder(
    db: Session, account: Account, kind: str, amount: Decimal, message: str
) -> bool:
    """有同类型待处理提醒就更新，没有就新建。返回 True 表示提醒有新增或更新。"""
    existing = (
        db.query(Reminder)
        .filter(
            Reminder.account_id == account.id,
            Reminder.kind == kind,
            Reminder.status == "pending",
        )
        .order_by(Reminder.created_at.desc())
        .first()
    )
    if existing:
        changed = False
        if money(existing.amount) != money(amount):
            existing.amount = money(amount)
            existing.created_at = datetime.now()
            changed = True
        if message and existing.message != message:
            existing.message = message
            changed = True
        return changed

    db.add(
        Reminder(
            account_id=account.id,
            kind=kind,
            amount=money(amount),
            message=message,
            status="pending",
        )
    )
    return True


def has_pending_reminder(db: Session, account: Account, kind: str) -> bool:
    return (
        db.query(Reminder)
        .filter(
            Reminder.account_id == account.id,
            Reminder.kind == kind,
            Reminder.status == "pending",
        )
        .count()
        > 0
    )


def resolve_reminders_if_settled(db: Session, account: Account) -> int:
    """余额补齐后自动关掉对应的待处理提醒。"""
    flags = account_flags(account)
    count = 0
    for reminder in (
        db.query(Reminder)
        .filter(Reminder.account_id == account.id, Reminder.status == "pending")
        .all()
    ):
        if reminder.kind == REMIND_UNPAID:
            settled = flags["balance"] >= flags["daily"]
        elif reminder.kind == REMIND_LOW_BALANCE:
            settled = not flags["month_short"]
        else:
            settled = False

        if settled:
            reminder.status = "resolved"
            reminder.resolved_at = datetime.now()
            count += 1
    return count


def refresh_account_reminders(db: Session, account: Account, today: date | None = None) -> None:
    """按当前余额重新校准这个账号的提醒。

    充值、手动改余额、开号之后立刻调用，保证提醒页的金额和状态跟余额始终对得上：
    - 余额不够今天的扣款  -> 挂「待缴费」
    - 今天扣得动但撑不满本月 -> 挂「余额不足」
    - 余额够了 -> 相关提醒自动关掉
    """
    today = today or date.today()
    flags = account_flags(account, today)

    # 1. 先把已经不该挂着的提醒关掉
    resolve_reminders_if_settled(db, account)

    # 2. 再按当前状态补一条
    if account.status != STATUS_ACTIVE or flags["fee"] <= 0:
        return
    if flags["low_balance"]:
        gap = money(flags["daily"] - flags["balance"])
        upsert_reminder(
            db,
            account,
            REMIND_UNPAID,
            gap,
            _unpaid_message(account, gap, flags["daily"]),
        )
    elif flags["month_short"]:
        upsert_reminder(
            db,
            account,
            REMIND_LOW_BALANCE,
            flags["gap"],
            _low_balance_message(account, flags),
        )


# --------------------------------------------------------------------------
# 每日扣费
# --------------------------------------------------------------------------

def run_daily_billing(db: Session, today: date | None = None) -> dict:
    """每天的日扣费 + 提醒巡检。

    - 到日子还没扣过：余额够就按日扣款额扣款，不够就生成待缴费提醒
    - 扣款成功但余额撑不满本月：生成余额不足提醒，提前预警
    """
    today = today or date.today()
    period = current_period(today)
    today_str = today.strftime("%Y-%m-%d")
    stats = {"charged": 0, "skipped": 0, "reminded": 0, "amount": Decimal("0.00")}

    accounts = db.query(Account).filter(Account.status == STATUS_ACTIVE).all()

    for account in accounts:
        last = account.last_deducted_date or ""
        if last == today_str:
            continue
        if last[:7] != period:
            account.month_deducted_amount = Decimal("0.00")

        amount = daily_amount(account, today)
        if amount <= 0:
            account.last_deducted_date = today_str
            stats["skipped"] += 1
            continue

        balance = money(account.balance)
        if balance < amount:
            # 扣不动：不扣款，余额留着，挂一条待缴费提醒
            if refresh_account_reminders(db, account, today):
                stats["reminded"] += 1
            stats["skipped"] += 1
            continue

        account.balance = money(balance - amount)
        account.month_deducted_amount = money(money(account.month_deducted_amount) + amount)
        account.last_deducted_date = today_str
        account.last_charged_period = period
        db.add(
            Transaction(
                account_id=account.id,
                kind=TX_CHARGE,
                amount=amount,
                period=period,
                note=f"{today_str} 每日套餐扣费",
            )
        )
        stats["charged"] += 1
        stats["amount"] += amount

        if refresh_account_reminders(db, account, today):
            stats["reminded"] += 1

    db.commit()
    return stats


# --------------------------------------------------------------------------
# 流水
# --------------------------------------------------------------------------

def apply_transaction(
    db: Session, account: Account, kind: str, amount: Decimal, period: str, note: str
) -> Transaction:
    """记一笔流水并同步余额 / 累计充值。"""
    amount = money(amount)
    balance = money(account.balance)

    if kind == TX_RECHARGE:
        account.balance = money(balance + amount)
        account.total_recharged = money(money(account.total_recharged) + amount)
    elif kind == TX_CHARGE:
        account.balance = money(balance - amount)
        if period:
            account.last_charged_period = period
    else:  # TX_ADJUST 直接把余额设成给定值
        account.balance = amount

    transaction = Transaction(
        account_id=account.id, kind=kind, amount=amount, period=period, note=note
    )
    db.add(transaction)
    refresh_account_reminders(db, account)
    db.commit()
    return transaction


def create_manual_reminder(db: Session, account: Account, message: str = "") -> Reminder:
    """人工发起一次催缴。"""
    flags = account_flags(account)
    amount = flags["gap"] if flags["gap"] > 0 else flags["daily"]
    reminder = Reminder(
        account_id=account.id,
        kind=REMIND_MANUAL,
        amount=money(amount),
        message=message or _low_balance_message(account, flags),
        status="pending",
    )
    db.add(reminder)
    db.commit()
    return reminder


# --------------------------------------------------------------------------
# 统计
# --------------------------------------------------------------------------

def list_stats(db: Session) -> dict:
    today = date.today()
    accounts = db.query(Account).all()
    active = [a for a in accounts if a.status == STATUS_ACTIVE]

    charged_today = 0
    daily_total = Decimal("0.00")
    low_balance = 0
    month_short = 0
    for account in active:
        flags = account_flags(account, today)
        if flags["deducted_today"]:
            charged_today += 1
        daily_total += flags["daily"]
        if flags["low_balance"]:
            low_balance += 1
        if flags["month_short"]:
            month_short += 1

    return {
        "total": len(accounts),
        "active": len(active),
        "normal": sum(1 for a in accounts if a.category == CATEGORY_NORMAL),
        "abnormal": sum(1 for a in accounts if a.category == CATEGORY_ABNORMAL),
        "charged_today": charged_today,
        "daily_total": daily_total,
        "low_balance": low_balance,
        "month_short": month_short,
        "pending_reminders": db.query(Reminder).filter(Reminder.status == "pending").count(),
        "monthly_total": sum((effective_monthly_fee(a) for a in active), Decimal("0.00")),
        "balance_total": sum((money(a.balance) for a in accounts), Decimal("0.00")),
        "total_recharged": sum((money(a.total_recharged) for a in accounts), Decimal("0.00")),
        "income_today": _today_income_total(db, accounts, today),
        "period": current_period(today),
        "today": today.strftime("%Y-%m-%d"),
    }


def _today_income_total(db: Session, accounts: list[Account], today: date) -> Decimal:
    """这些账号今天的收入合计。"""
    account_ids = [a.id for a in accounts]
    if not account_ids:
        return Decimal("0.00")
    day_str = today.strftime("%Y-%m-%d")
    rows = (
        db.query(DailyIncome)
        .filter(DailyIncome.account_id.in_(account_ids), DailyIncome.day == day_str)
        .all()
    )
    return sum((money(row.amount) for row in rows), Decimal("0.00"))


# --------------------------------------------------------------------------
# 页面可改的运行配置（AI 接入）
# --------------------------------------------------------------------------

def get_stored_setting(db: Session, key: str) -> str:
    row = db.get(AppSetting, key)
    return (row.value or "").strip() if row else ""


def set_stored_setting(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = datetime.now()


def get_ai_config(db: Session) -> dict:
    """AI 接口配置：页面里存的优先，其次用环境变量兜底，最后用内置默认值。

    返回值每个字段都带 _source 标记（page / env / default），方便页面上告诉用户
    当前到底是哪来的配置。
    """
    settings = get_settings()
    config: dict = {}
    has_override = False

    for key in AI_CONFIG_KEYS:
        stored = get_stored_setting(db, key)
        env_value = (getattr(settings, key, "") or "").strip()
        if stored:
            config[key] = stored
            config[f"{key}_source"] = "page"
            has_override = True
        elif env_value:
            config[key] = env_value
            config[f"{key}_source"] = "env"
        else:
            config[key] = ""
            config[f"{key}_source"] = ""

    # 接口地址和模型名有内置默认值（MiniMax 中国版），密钥没有
    if not config["ai_base_url"]:
        config["ai_base_url"] = DEFAULT_AI_BASE_URL
        config["ai_base_url_source"] = "default"
    if not config["ai_model"]:
        config["ai_model"] = DEFAULT_AI_MODEL
        config["ai_model_source"] = "default"

    config["has_override"] = has_override
    config["configured"] = bool(config["ai_base_url"] and config["ai_api_key"] and config["ai_model"])
    return config


def save_ai_config(db: Session, base_url: str, api_key: str, model: str) -> dict:
    """保存页面里填的 AI 配置。空字符串表示这一项回落到环境变量。"""
    set_stored_setting(db, "ai_base_url", (base_url or "").strip())
    set_stored_setting(db, "ai_api_key", (api_key or "").strip())
    set_stored_setting(db, "ai_model", (model or "").strip())
    db.commit()
    return get_ai_config(db)


def clear_ai_config(db: Session) -> dict:
    """清掉页面里的覆盖，全部回落到环境变量。"""
    for key in AI_CONFIG_KEYS:
        row = db.get(AppSetting, key)
        if row is not None:
            db.delete(row)
    db.commit()
    return get_ai_config(db)


def mask_api_key(api_key: str) -> str:
    """密钥不明文回显，只留尾巴几个字符。"""
    key = (api_key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{'*' * 6}{key[-4:]}"


# --------------------------------------------------------------------------
# 视觉模型（识图）配置
# --------------------------------------------------------------------------

def get_vision_config(db: Session) -> dict:
    """视觉模型配置。

    视觉接口地址和密钥留空时，直接沿用文本模型（AI）的配置，省得填两遍。
    """
    ai = get_ai_config(db)
    config: dict = {}
    for key in VISION_CONFIG_KEYS:
        stored = get_stored_setting(db, key)
        config[key] = stored
        config[f"{key}_source"] = "page" if stored else ""

    if not config["vision_base_url"]:
        config["vision_base_url"] = ai.get("ai_base_url", "")
        config["vision_base_url_source"] = ai.get("ai_base_url_source", "")
    if not config["vision_api_key"]:
        config["vision_api_key"] = ai.get("ai_api_key", "")
        config["vision_api_key_source"] = ai.get("ai_api_key_source", "")
    if not config["vision_model"]:
        config["vision_model"] = DEFAULT_VISION_MODEL
        config["vision_model_source"] = "default"
    if not config["vision_format"]:
        config["vision_format"] = DEFAULT_VISION_FORMAT
        config["vision_format_source"] = "default"

    config["configured"] = bool(config["vision_model"] and config["vision_api_key"])
    return config


def save_vision_config(
    db: Session, base_url: str, api_key: str, model: str, fmt: str
) -> dict:
    """保存视觉模型配置。密钥留空表示保留原来那一项。"""
    if not api_key:
        api_key = get_stored_setting(db, "vision_api_key")
    set_stored_setting(db, "vision_base_url", (base_url or "").strip())
    set_stored_setting(db, "vision_api_key", (api_key or "").strip())
    set_stored_setting(db, "vision_model", (model or "").strip())
    set_stored_setting(db, "vision_format", (fmt or "").strip())
    db.commit()
    return get_vision_config(db)


# --------------------------------------------------------------------------
# AI 识图结果的两级匹配：先找宽带账号，再核对节点号
# --------------------------------------------------------------------------

def _norm(value) -> str:
    return re.sub(r"[\s\-_]", "", str(value or "")).lower()


def match_recognized_items(db: Session, items: list[dict]) -> list[dict]:
    """把识别出来的每一行跟台账对上。

    第一级用「宽带账号」找账号（手机号 / 宽带账号 / 注册码），
    第二级核对「节点号」跟这个账号登记的是不是同一个。
    对不上的行原样返回，标记清楚，让人来决定怎么处置。
    """
    accounts = db.query(Account).all()

    result = []
    for item in items:
        row = dict(item)
        broadband = _norm(item.get("broadband"))
        node_text = _norm(item.get("node_account") or item.get("node"))

        account: Account | None = None
        account_reason = ""

        digits = re.findall(r"\d{11}", broadband)
        for digit in digits:
            account = next((a for a in accounts if _norm(a.phone).endswith(digit)), None)
            if account is not None:
                account_reason = "手机号"
                break

        if account is None and broadband:
            for candidate in accounts:
                if _norm(candidate.broadband_account) == broadband:
                    account, account_reason = candidate, "宽带账号"
                    break

        if account is None and broadband:
            for candidate in accounts:
                code = _norm(candidate.registration_code)
                if code and (code in broadband or broadband in code):
                    account, account_reason = candidate, "注册码"
                    break

        if account is None and broadband:
            for candidate in accounts:
                code = _norm(candidate.broadband_account)
                if code and (code in broadband or broadband in code):
                    account, account_reason = candidate, "宽带账号（近似）"
                    break

        if account is None and digits:
            for candidate in accounts:
                if _norm(candidate.phone) and _norm(candidate.phone) in broadband:
                    account, account_reason = candidate, "手机号（近似）"
                    break

        # 第二级：核对节点号。账号与节点号一一对应，对不上要提醒人确认。
        node_reason = ""
        if account is not None:
            saved_node = _norm(account.node_account)
            if node_text and saved_node:
                if saved_node == node_text:
                    node_reason = "节点号一致"
                elif saved_node in node_text or node_text in saved_node:
                    node_reason = "节点号（近似）"
                else:
                    node_reason = "节点号不一致"
            elif node_text and not saved_node:
                node_reason = "账号还没登记节点号"

        if account is None:
            status = "no_account"
        elif node_reason == "节点号不一致":
            status = "no_node"
        else:
            status = "matched"

        row.update(
            {
                "status": status,
                "account_id": account.id if account else None,
                "account_phone": account.phone if account else "",
                "account_broadband": account.broadband_account if account else "",
                "account_reason": account_reason,
                "node_account": str(item.get("node_account") or item.get("node") or ""),
                "saved_node_account": account.node_account if account else "",
                "node_reason": node_reason,
            }
        )
        result.append(row)
    return result


# --------------------------------------------------------------------------
# 图表：纯 SVG 画好再塞进模板，不依赖任何外网资源
# --------------------------------------------------------------------------

CHART_PALETTE = [
    "#3b82f6",
    "#10b981",
    "#f59e0b",
    "#8b5cf6",
    "#ef4444",
    "#06b6d4",
    "#ec4899",
    "#84cc16",
    "#f97316",
    "#6366f1",
]


def bar_chart_svg(
    daily: list[dict], color: str = "#3b82f6", width: int = 680, height: int = 200
) -> str:
    """每天收入柱状图。daily = [{"day": "2026-11-08", "amount": Decimal}]"""
    pad_l, pad_r, pad_t, pad_b = 54, 12, 14, 26
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    count = len(daily) or 1

    amounts = [float(row["amount"]) for row in daily] or [0.0]
    top = max(amounts)
    if top <= 0:
        top = 1.0

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="每日收入柱状图">'
    ]

    for index in range(3):
        value = top * index / 2
        y = pad_t + plot_h - plot_h * (value / top)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
            f'class="chart-grid"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="chart-axis" '
            f'text-anchor="end">{value:,.0f}</text>'
        )

    slot = plot_w / count
    bar_w = max(4.0, min(34.0, slot * 0.6))
    label_step = max(1, count // 7)

    for index, row in enumerate(daily):
        amount = float(row["amount"])
        bar_h = (amount / top) * plot_h
        x = pad_l + slot * index + (slot - bar_w) / 2
        y = pad_t + plot_h - bar_h
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
            f'height="{max(bar_h, 1.5):.1f}" rx="3" fill="{color}">'
            f"<title>{row['day']} ¥{amount:,.2f}</title></rect>"
        )
        if index % label_step == 0 or index == count - 1:
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{height - 8}" class="chart-axis" '
                f'text-anchor="middle">{row["day"][5:]}</text>'
            )

    parts.append(
        f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{width - pad_r}" '
        f'y2="{pad_t + plot_h}" class="chart-base"/>'
    )
    parts.append("</svg>")
    return "".join(parts)


def donut_svg(
    platforms: list[dict], days: int = 14, size: int = 200
) -> str:
    """平台收入占比环形图。"""
    radius = 68
    center = size / 2
    circumference = 2 * math.pi * radius
    total = sum(float(row["amount"]) for row in platforms)

    parts = [
        f'<svg class="chart donut" viewBox="0 0 {size} {size}" role="img" '
        f'aria-label="平台收入占比图">'
    ]
    parts.append(
        f'<circle cx="{center}" cy="{center}" r="{radius}" fill="none" '
        f'stroke="var(--border)" stroke-width="26"/>'
    )

    offset = 0.0
    for index, row in enumerate(platforms):
        amount = float(row["amount"])
        if amount <= 0:
            continue
        segment = circumference * (amount / total) if total > 0 else 0
        color = CHART_PALETTE[index % len(CHART_PALETTE)]
        parts.append(
            f'<circle cx="{center}" cy="{center}" r="{radius}" fill="none" '
            f'stroke="{color}" stroke-width="26" '
            f'stroke-dasharray="{segment:.2f} {circumference - segment:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" '
            f'transform="rotate(-90 {center} {center})">'
            f"<title>{row['platform']} ¥{amount:,.2f}</title></circle>"
        )
        offset += segment

    parts.append(
        f'<text x="{center}" y="{center - 2}" class="donut-value" '
        f'text-anchor="middle">¥{total:,.0f}</text>'
    )
    parts.append(
        f'<text x="{center}" y="{center + 16}" class="donut-label" '
        f'text-anchor="middle">近 {days} 天</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------
# 分类：正常账号 / 异常账号
# --------------------------------------------------------------------------

def mark_abnormal(db: Session, account: Account, reason: str, note: str = "") -> Account:
    """把账号移进异常分类，并记下异常原因。"""
    if reason not in ABNORMAL_REASONS:
        raise ValueError("未知的异常原因")
    account.category = CATEGORY_ABNORMAL
    account.abnormal_reason = reason
    account.abnormal_at = datetime.now()
    account.abnormal_note = fix_text(note)
    db.commit()
    logger.info("账号 %s 标记为异常（%s）", account.phone, ABNORMAL_REASONS[reason])
    return account


def restore_account(db: Session, account: Account) -> Account:
    """异常账号恢复正常，回到正常分类。"""
    account.category = CATEGORY_NORMAL
    account.abnormal_reason = ""
    account.abnormal_at = None
    account.abnormal_note = ""
    db.commit()
    logger.info("账号 %s 恢复正常", account.phone)
    return account


# --------------------------------------------------------------------------
# 分组
# --------------------------------------------------------------------------

def list_groups(db: Session) -> list[AccountGroup]:
    return db.query(AccountGroup).order_by(AccountGroup.name, AccountGroup.id).all()


def group_account_count(db: Session, group_id: int) -> int:
    return db.query(Account).filter(Account.group_id == group_id).count()


def delete_group(db: Session, group: AccountGroup) -> None:
    """删分组时，组里的账号退回「未分组」，不会被一起删掉。"""
    for account in group.accounts:
        account.group_id = None
    db.delete(group)
    db.commit()


# --------------------------------------------------------------------------
# 机房 / 平台 / 节点号 / 每日收入
# --------------------------------------------------------------------------

def account_income_on(db: Session, account: Account, day: str) -> Decimal:
    """某个账号某天的收入（账号与节点号一一对应，收入直接记在账号上）。"""
    row = (
        db.query(DailyIncome)
        .filter(DailyIncome.account_id == account.id, DailyIncome.day == day)
        .first()
    )
    return money(row.amount) if row else Decimal("0.00")


def upsert_income(
    db: Session,
    account_id: int,
    day: str,
    amount,
    source: str = INCOME_SOURCE_MANUAL,
    note: str = "",
) -> DailyIncome:
    """记一笔某天的收入。同一条宽带同一天重复录入会覆盖，不会累加成两条。"""
    value = money(amount)
    row = (
        db.query(DailyIncome)
        .filter(DailyIncome.account_id == account_id, DailyIncome.day == day)
        .first()
    )
    if row is None:
        row = DailyIncome(
            account_id=account_id,
            day=day,
            amount=value,
            source=source,
            note=fix_text(note),
        )
        db.add(row)
    else:
        row.amount = value
        row.source = source
        if fix_text(note):
            row.note = fix_text(note)
    db.commit()
    return row


def delete_income(db: Session, income: DailyIncome) -> None:
    db.delete(income)
    db.commit()


# --------------------------------------------------------------------------
# 首页看板统计
# --------------------------------------------------------------------------

def income_dashboard(
    db: Session,
    category: str | None = None,
    days: int = 14,
    today: date | None = None,
) -> dict:
    """首页看板的数据。

    口径跟着当前分类走：分类是 normal 就只算正常账号的收入和扣费，
    abnormal 就只算异常账号的，两边互不掺和。

    - today_income : 今日收入合计
    - total_income : 累计收入合计
    - today_cost   : 今日所有账号的每日扣费合计
    - today_profit : 今日收入 - 今日扣费
    - daily        : 近 N 天每天的收入（画柱状图）
    - platforms    : 近 N 天各节点平台的收入和占比（画占比图）
    """
    today = today or date.today()
    today_str = today.strftime("%Y-%m-%d")

    query = db.query(Account)
    if category:
        query = query.filter(Account.category == category)
    accounts = query.all()
    account_ids = [a.id for a in accounts]

    # 平台归属跟着账号走
    account_platform: dict[int, str] = {
        a.id: (fix_text(a.platform) or "未填平台") for a in accounts
    }

    rows: list[DailyIncome] = []
    if account_ids:
        rows = db.query(DailyIncome).filter(DailyIncome.account_id.in_(account_ids)).all()

    today_income = Decimal("0.00")
    total_income = Decimal("0.00")
    window_start = (today - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    daily_map: dict[str, Decimal] = {}
    platform_map: dict[str, Decimal] = {}

    for row in rows:
        amount = money(row.amount)
        total_income += amount
        if row.day == today_str:
            today_income += amount
        if row.day >= window_start:
            daily_map[row.day] = daily_map.get(row.day, Decimal("0.00")) + amount
            platform = account_platform.get(row.account_id, "未填平台")
            platform_map[platform] = platform_map.get(platform, Decimal("0.00")) + amount

    daily = []
    for offset in range(days):
        day_str = (today - timedelta(days=days - 1 - offset)).strftime("%Y-%m-%d")
        daily.append({"day": day_str, "amount": daily_map.get(day_str, Decimal("0.00"))})

    platform_total = sum(platform_map.values(), Decimal("0.00"))
    platforms = []
    for name, amount in sorted(platform_map.items(), key=lambda kv: -kv[1]):
        ratio = float(amount / platform_total * 100) if platform_total > 0 else 0.0
        platforms.append({"platform": name, "amount": amount, "ratio": ratio})

    today_cost = sum((daily_amount(a, today) for a in accounts), Decimal("0.00"))
    today_profit = money(today_income - today_cost)

    # 剩余余额：账面上的钱跟正常/异常分类无关，所以统计全部账号
    all_accounts = db.query(Account).all()
    total_balance = sum((money(a.balance) for a in all_accounts), Decimal("0.00"))
    normal_balance = sum(
        (money(a.balance) for a in all_accounts if a.category == CATEGORY_NORMAL),
        Decimal("0.00"),
    )
    abnormal_balance = money(total_balance - normal_balance)

    return {
        "today_income": today_income,
        "total_income": total_income,
        "today_cost": today_cost,
        "today_profit": today_profit,
        "daily": daily,
        "platforms": platforms,
        "window_total": platform_total,
        "account_count": len(accounts),
        "node_count": len([a for a in accounts if fix_text(a.node_account)]),
        "days": days,
        "today": today_str,
        "total_balance": total_balance,
        "normal_balance": normal_balance,
        "abnormal_balance": abnormal_balance,
        "all_account_count": len(all_accounts),
    }


def today_account_incomes(db: Session, accounts: list[Account], today: date | None = None) -> dict:
    """一次算出所有账号的今日收入，避免列表页里一条条查数据库。"""
    today = today or date.today()
    day_str = today.strftime("%Y-%m-%d")
    account_ids = [a.id for a in accounts]
    if not account_ids:
        return {}

    result: dict[int, Decimal] = {account_id: Decimal("0.00") for account_id in account_ids}
    rows = (
        db.query(DailyIncome)
        .filter(DailyIncome.account_id.in_(account_ids), DailyIncome.day == day_str)
        .all()
    )
    for row in rows:
        result[row.account_id] = money(row.amount)
    return result
