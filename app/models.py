"""数据表定义。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

# 账号状态
STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_CLOSED = "closed"

STATUS_LABELS = {
    STATUS_ACTIVE: "正常",
    STATUS_SUSPENDED: "已暂停",
    STATUS_CLOSED: "已注销",
}

# 流水类型
TX_CHARGE = "charge"      # 月度套餐费扣费
TX_RECHARGE = "recharge"  # 充值
TX_ADJUST = "adjust"      # 手动调整

TX_LABELS = {
    TX_CHARGE: "扣费",
    TX_RECHARGE: "充值",
    TX_ADJUST: "调整",
}

# 提醒类型
REMIND_UNPAID = "unpaid"          # 余额不足以扣费
REMIND_LOW_BALANCE = "low_balance"  # 余额不足以覆盖月费，需要充值
REMIND_MANUAL = "manual"          # 人工发起的催缴

REMIND_LABELS = {
    REMIND_UNPAID: "待缴费",
    REMIND_LOW_BALANCE: "余额不足",
    REMIND_MANUAL: "人工催缴",
}


class Account(Base):
    """宽带账号台账：一条记录 = 一个电话号码绑定的宽带。"""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # --- 核心字段 ---
    phone: Mapped[str] = mapped_column(String(32), index=True, comment="绑定的电话号码")
    broadband_account: Mapped[str] = mapped_column(String(128), index=True, default="", comment="宽带账号")
    registration_code: Mapped[str] = mapped_column(String(128), default="", comment="宽带注册码")
    registrant: Mapped[str] = mapped_column(String(128), default="", comment="注册人")
    sim_user: Mapped[str] = mapped_column(String(128), default="", comment="手机卡使用人")
    registered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="注册时间")
    monthly_fee: Mapped[float] = mapped_column(Numeric(12, 2), default=399, comment="套餐原价")
    balance: Mapped[float] = mapped_column(Numeric(12, 2), default=500, comment="剩余金额")

    # --- 辅助字段 ---
    provider: Mapped[str] = mapped_column(String(64), default="", comment="运营商")
    status: Mapped[str] = mapped_column(String(16), default=STATUS_ACTIVE, comment="账号状态")
    notes: Mapped[str] = mapped_column(Text, default="", comment="备注")

    # --- 优惠与计费 ---
    discount_enabled: Mapped[bool] = mapped_column(Boolean, default=False, comment="七折开关（开启后不可关闭）")
    discount_enabled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="七折开启时间"
    )
    total_recharged: Mapped[float] = mapped_column(
        Numeric(12, 2), default=0, comment="累计充值金额"
    )
    last_deducted_date: Mapped[str] = mapped_column(
        String(16), default="", comment="最近一次成功扣费日期 YYYY-MM-DD"
    )
    month_deducted_amount: Mapped[float] = mapped_column(
        Numeric(12, 2), default=0, comment="本月已扣金额"
    )
    last_charged_period: Mapped[str] = mapped_column(String(8), default="", comment="最近扣费月份 YYYY-MM")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    reminders: Mapped[list["Reminder"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_accounts_phone_status", "phone", "status"),)


class User(Base):
    """登录账户：谁能进来看台账。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(128), index=True, comment="账户名")
    password_hash: Mapped[str] = mapped_column(String(256), default="", comment="密码哈希")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否管理员")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("ix_users_username_ci", "username"),)


class AppSetting(Base):
    """页面里可改的运行配置（目前是 AI 接口接入信息）。

    存在数据库里，跟着 /data 一起备份迁移；环境变量作为兜底。
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class Transaction(Base):
    """收支流水：每月扣费、充值、手动调整都记一笔。"""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)

    kind: Mapped[str] = mapped_column(String(16), default=TX_RECHARGE, comment="流水类型")
    amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0, comment="金额（正数）")
    period: Mapped[str] = mapped_column(String(8), default="", comment="归属月份 YYYY-MM")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    account: Mapped["Account"] = relationship(back_populates="transactions")


class Reminder(Base):
    """催缴提醒：列表里显示待办，可附 AI 生成的催缴话术。"""

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)

    kind: Mapped[str] = mapped_column(String(16), default=REMIND_LOW_BALANCE, comment="提醒类型")
    amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0, comment="待缴金额")
    message: Mapped[str] = mapped_column(Text, default="", comment="催缴话术")
    status: Mapped[str] = mapped_column(String(16), default="pending", comment="pending / resolved")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    account: Mapped["Account"] = relationship(back_populates="reminders")

    __table_args__ = (Index("ix_reminders_status_kind", "status", "kind"),)
