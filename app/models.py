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

# 常见运营商：账号表单里用下拉框选，不用手打
PROVIDERS = [
    "中国电信",
    "中国联通",
    "中国移动",
    "中国广电",
    "铁通",
    "长城宽带",
    "鹏博士",
    "其他",
]

# ---------------------------------------------------------------------------
# 账号分类：正常账号 / 异常账号
# ---------------------------------------------------------------------------
CATEGORY_NORMAL = "normal"
CATEGORY_ABNORMAL = "abnormal"

CATEGORY_LABELS = {
    CATEGORY_NORMAL: "正常账号",
    CATEGORY_ABNORMAL: "异常账号",
}

# 异常原因（标记异常时六选一）
ABNORMAL_REASONS = {
    "machine_room": "机房",
    "power": "当前电费",
    "manage_line": "管理线",
    "throttle": "限速",
    "cancel": "注销",
    "arrears": "欠费",
}

# ---------------------------------------------------------------------------
# 每日收入来源
# ---------------------------------------------------------------------------
INCOME_SOURCE_MANUAL = "manual"
INCOME_SOURCE_AI = "ai"

INCOME_SOURCE_LABELS = {
    INCOME_SOURCE_MANUAL: "手工录入",
    INCOME_SOURCE_AI: "AI 识图",
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

REMIND_LABELS = {
    REMIND_UNPAID: "待缴费",
    REMIND_LOW_BALANCE: "余额不足",
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

    # --- 分类 / 分组 / 安装地址 ---
    category: Mapped[str] = mapped_column(
        String(16), default=CATEGORY_NORMAL, index=True, comment="正常账号 / 异常账号"
    )
    abnormal_reason: Mapped[str] = mapped_column(String(32), default="", comment="异常原因")
    abnormal_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="标记异常的时间"
    )
    abnormal_note: Mapped[str] = mapped_column(Text, default="", comment="异常说明")
    group_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True, comment="所属分组"
    )
    install_address: Mapped[str] = mapped_column(
        String(255), default="", comment="宽带安装地址"
    )
    daily_cost: Mapped[float] = mapped_column(
        Numeric(12, 2), default=0, comment="每日扣费（0 表示按月费分摊）"
    )

    # --- 机房 / 节点平台 / 节点号 ---
    # 层级：机房 → 节点平台 → 宽带账号 ＋ 节点号（账号与节点号一一对应）
    room: Mapped[str] = mapped_column(
        String(128), index=True, default="", comment="所在机房"
    )
    platform: Mapped[str] = mapped_column(
        String(128), index=True, default="", comment="节点平台"
    )
    node_account: Mapped[str] = mapped_column(
        String(128), index=True, default="", comment="节点号（服务器上的账号）"
    )

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
    incomes: Mapped[list["DailyIncome"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    group: Mapped[Optional["AccountGroup"]] = relationship(
        back_populates="accounts",
        primaryjoin="Account.group_id == AccountGroup.id",
        foreign_keys="Account.group_id",
        viewonly=True,
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


class AccountGroup(Base):
    """正常账号分组。

    分组本身是标签，组上可以记安装地点和管理线信息
    （管理线信息 = 注册手机号 / 宽带账号 / 到期时间）。
    """

    __tablename__ = "account_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True, comment="分组名称")
    install_location: Mapped[str] = mapped_column(
        String(255), default="", comment="安装地点"
    )
    manage_phone: Mapped[str] = mapped_column(
        String(32), default="", comment="管理线注册手机号"
    )
    manage_account: Mapped[str] = mapped_column(
        String(128), default="", comment="管理线宽带账号"
    )
    manage_expire_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="管理线到期时间"
    )
    note: Mapped[str] = mapped_column(Text, default="", comment="备注")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    accounts: Mapped[list["Account"]] = relationship(
        back_populates="group",
        primaryjoin="Account.group_id == AccountGroup.id",
        foreign_keys="Account.group_id",
        viewonly=True,
    )


class DailyIncome(Base):
    """宽带账号某一天的收益金额。

    账号与节点号一一对应，所以收益直接记在账号上：
    同一条宽带同一天只保留一条（重复录入会覆盖）。
    """

    __tablename__ = "daily_incomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )

    day: Mapped[str] = mapped_column(String(10), index=True, comment="日期 YYYY-MM-DD")
    amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0, comment="当天收入")
    source: Mapped[str] = mapped_column(
        String(16), default=INCOME_SOURCE_MANUAL, comment="来源：manual / ai"
    )
    note: Mapped[str] = mapped_column(Text, default="", comment="备注")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    account: Mapped["Account"] = relationship(back_populates="incomes")

    __table_args__ = (Index("ix_daily_incomes_account_day", "account_id", "day"),)
