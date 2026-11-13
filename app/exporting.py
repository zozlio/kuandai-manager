"""数据导出：给人看的 CSV，和给机器用的全量备份 JSON。

CSV 前面加 BOM，Excel / WPS 双击打开不会乱码。
JSON 备份包含所有表，恢复脚本 scripts/restore_backup.py 用它还原。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable

from sqlalchemy.orm import Session

from .models import (
    ABNORMAL_REASONS,
    Account,
    AccountGroup,
    AppSetting,
    DailyIncome,
    Reminder,
    STATUS_LABELS,
    Transaction,
    User,
    CATEGORY_LABELS,
)
from .services import effective_monthly_fee, money

DATETIME_FMT = "%Y-%m-%d %H:%M"
DATE_FMT = "%Y-%m-%d"


def _fmt_dt(value: datetime | None) -> str:
    return value.strftime(DATETIME_FMT) if value else ""


def _fmt_date(value) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime(DATE_FMT)
    if isinstance(value, date):
        return value.strftime(DATE_FMT)
    return str(value)


def _money(value) -> str:
    return f"{money(value):.2f}"


def to_csv(header: list[str], rows: Iterable[list]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return "\ufeff" + buf.getvalue()


def accounts_csv(db: Session) -> str:
    header = [
        "手机号",
        "宽带账号",
        "宽带注册码",
        "注册人",
        "手机卡使用人",
        "注册时间",
        "套餐原价",
        "是否七折",
        "实际月费",
        "每日扣费",
        "余额",
        "累计充值",
        "运营商",
        "账号状态",
        "分类",
        "异常原因",
        "异常备注",
        "标记异常时间",
        "机房",
        "节点平台",
        "节点号",
        "本月已扣",
        "最近扣费日期",
        "备注",
    ]
    rows = []
    for a in db.query(Account).order_by(Account.phone).all():
        rows.append(
            [
                a.phone,
                a.broadband_account,
                a.registration_code,
                a.registrant,
                a.sim_user,
                _fmt_dt(a.registered_at),
                _money(a.monthly_fee),
                "是" if a.discount_enabled else "否",
                _money(effective_monthly_fee(a)),
                _money(a.daily_cost),
                _money(a.balance),
                _money(a.total_recharged),
                a.provider,
                STATUS_LABELS.get(a.status, a.status),
                CATEGORY_LABELS.get(a.category, a.category),
                ABNORMAL_REASONS.get(a.abnormal_reason, a.abnormal_reason),
                a.abnormal_note,
                _fmt_dt(a.abnormal_at),
                a.room,
                a.platform,
                a.node_account,
                _money(a.month_deducted_amount),
                a.last_deducted_date,
                a.notes,
            ]
        )
    return to_csv(header, rows)


def incomes_csv(db: Session) -> str:
    header = [
        "日期",
        "手机号",
        "宽带账号",
        "机房",
        "节点平台",
        "节点号",
        "收入金额",
        "来源",
        "备注",
    ]
    rows = []
    for inc in db.query(DailyIncome).order_by(DailyIncome.day.desc(), DailyIncome.id).all():
        account = db.get(Account, inc.account_id)
        rows.append(
            [
                inc.day,
                account.phone if account else "",
                account.broadband_account if account else "",
                account.room if account else "",
                account.platform if account else "",
                account.node_account if account else "",
                _money(inc.amount),
                "AI 识图" if inc.source == "ai" else "手工录入",
                inc.note,
            ]
        )
    return to_csv(header, rows)


def transactions_csv(db: Session) -> str:
    header = ["时间", "手机号", "类型", "金额", "归属月份", "备注"]
    rows = []
    for tx in db.query(Transaction).order_by(Transaction.id.desc()).all():
        account = db.get(Account, tx.account_id)
        rows.append(
            [
                _fmt_dt(tx.created_at),
                account.phone if account else "",
                {"charge": "扣费", "recharge": "充值", "adjust": "调整"}.get(tx.kind, tx.kind),
                _money(tx.amount),
                tx.period,
                tx.note,
            ]
        )
    return to_csv(header, rows)


def _dump_model_rows(db: Session, model) -> list[dict]:
    rows = []
    for obj in db.query(model).all():
        row = {}
        for column in model.__table__.columns:
            value = getattr(obj, column.name, None)
            if isinstance(value, Decimal):
                value = float(value)
            elif isinstance(value, datetime):
                value = value.strftime(DATETIME_FMT)
            elif isinstance(value, date):
                value = value.strftime(DATE_FMT)
            elif isinstance(value, bool):
                value = 1 if value else 0
            row[column.name] = value
        rows.append(row)
    return rows


def backup_json(db: Session) -> str:
    payload = {
        "format": "kuandai-manager-backup",
        "version": 1,
        "exported_at": datetime.now().strftime(DATETIME_FMT),
        "tables": {
            "accounts": _dump_model_rows(db, Account),
            "account_groups": _dump_model_rows(db, AccountGroup),
            "daily_incomes": _dump_model_rows(db, DailyIncome),
            "transactions": _dump_model_rows(db, Transaction),
            "reminders": _dump_model_rows(db, Reminder),
            "users": _dump_model_rows(db, User),
            "app_settings": _dump_model_rows(db, AppSetting),
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
