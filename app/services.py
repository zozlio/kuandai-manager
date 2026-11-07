"""业务逻辑：日扣费、余额、提醒、统计。"""

from __future__ import annotations

import calendar
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from .config import DEFAULT_AI_BASE_URL, DEFAULT_AI_MODEL, get_settings
from .models import (
    Account,
    AppSetting,
    Reminder,
    Transaction,
    STATUS_ACTIVE,
    TX_CHARGE,
    TX_RECHARGE,
    TX_ADJUST,
    REMIND_UNPAID,
    REMIND_LOW_BALANCE,
    REMIND_MANUAL,
)

CENT = Decimal("0.01")

# AI 接口配置的三个键：页面设置优先，其次环境变量
AI_CONFIG_KEYS = ("ai_base_url", "ai_api_key", "ai_model")

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

    常规按「月费 ÷ 当月天数」分摊；月末最后一天把余数补齐，
    保证整月扣款金额刚好等于月费，不多不少。
    """
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


def search_accounts(db: Session, keyword: str = "", status: str = "") -> list[Account]:
    """按关键字（手机号/宽带账号/注册码/注册人）和状态筛选。"""
    query = db.query(Account)
    if keyword:
        like = f"%{keyword.strip()}%"
        query = query.filter(
            Account.phone.like(like)
            | Account.broadband_account.like(like)
            | Account.registration_code.like(like)
            | Account.registrant.like(like)
            | Account.sim_user.like(like)
            | Account.notes.like(like)
        )
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
        "charged_today": charged_today,
        "daily_total": daily_total,
        "low_balance": low_balance,
        "month_short": month_short,
        "pending_reminders": db.query(Reminder).filter(Reminder.status == "pending").count(),
        "monthly_total": sum((effective_monthly_fee(a) for a in active), Decimal("0.00")),
        "balance_total": sum((money(a.balance) for a in accounts), Decimal("0.00")),
        "total_recharged": sum((money(a.total_recharged) for a in accounts), Decimal("0.00")),
        "period": current_period(today),
        "today": today.strftime("%Y-%m-%d"),
    }


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
