"""数据库连接与初始化（SQLite）。"""

from __future__ import annotations

from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

_engine = None
_session_factory: sessionmaker | None = None

# 新增列的迁移清单：SQLite 不支持 ADD COLUMN IF NOT EXISTS，这里手工补齐
COLUMN_MIGRATIONS = [
    ("accounts", "total_recharged", "total_recharged NUMERIC(12, 2) DEFAULT 0"),
    ("accounts", "discount_enabled", "discount_enabled BOOLEAN DEFAULT 0"),
    ("accounts", "discount_enabled_at", "discount_enabled_at DATETIME"),
    ("accounts", "last_deducted_date", "last_deducted_date VARCHAR(16) DEFAULT ''"),
    ("accounts", "month_deducted_amount", "month_deducted_amount NUMERIC(12, 2) DEFAULT 0"),
    ("accounts", "sim_user", "sim_user VARCHAR(128) DEFAULT ''"),
    # 分类 / 分组 / 安装地址 / 每日扣费
    ("accounts", "category", "category VARCHAR(16) DEFAULT 'normal'"),
    ("accounts", "abnormal_reason", "abnormal_reason VARCHAR(32) DEFAULT ''"),
    ("accounts", "abnormal_at", "abnormal_at DATETIME"),
    ("accounts", "abnormal_note", "abnormal_note TEXT DEFAULT ''"),
    ("accounts", "group_id", "group_id INTEGER"),
    ("accounts", "install_address", "install_address VARCHAR(255) DEFAULT ''"),
    ("accounts", "daily_cost", "daily_cost NUMERIC(12, 2) DEFAULT 0"),
    # 机房 / 节点平台 / 节点号（账号与节点号一一对应）
    ("accounts", "room", "room VARCHAR(128) DEFAULT ''"),
    ("accounts", "platform", "platform VARCHAR(128) DEFAULT ''"),
    ("accounts", "node_account", "node_account VARCHAR(128) DEFAULT ''"),
    # 每日收入改为记在账号上
    ("daily_incomes", "account_id", "account_id INTEGER"),
]


class Base(DeclarativeBase):
    pass


def _apply_column_migrations(engine) -> None:
    """给已经存在的库补齐新版本新增的列。"""
    with engine.begin() as conn:
        for table, column, ddl in COLUMN_MIGRATIONS:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            if not rows:
                continue  # 表还不存在，create_all 会直接建出带新列的表
            existing = {row[1] for row in rows}
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))


def _migrate_rooms_to_accounts(engine) -> None:
    """把老版本「账号下面挂机房」的写法，搬成「账号自带机房/平台/节点号」。

    老结构：accounts 1 ── N nodes（name=机房名, platform=平台, node_account=节点号）
            daily_incomes.node_id → nodes.id
    新结构：accounts 1 ── 1 节点号，机房/平台/节点号都是账号自己的字段
            daily_incomes.account_id → accounts.id

    老库的 daily_incomes 里 node_id 是 NOT NULL，得整张表重建才能去掉这个约束。
    这段只在老库里还有 nodes 表时才动，跑过一遍就不会再跑。
    """
    with engine.begin() as conn:
        if not conn.execute(text("PRAGMA table_info(nodes)")).fetchall():
            return  # 新库没有这张表，不用搬

        # 老节点搬到它所属的账号上：机房名 → 机房，平台 → 平台，节点账号 → 节点号
        conn.execute(
            text(
                """
                UPDATE accounts
                SET room = COALESCE(NULLIF(room, ''),
                        (SELECT n.name FROM nodes n WHERE n.account_id = accounts.id LIMIT 1)),
                    platform = COALESCE(NULLIF(platform, ''),
                        (SELECT n.platform FROM nodes n WHERE n.account_id = accounts.id LIMIT 1)),
                    node_account = COALESCE(NULLIF(node_account, ''),
                        (SELECT n.node_account FROM nodes n WHERE n.account_id = accounts.id LIMIT 1))
                WHERE EXISTS (SELECT 1 FROM nodes n WHERE n.account_id = accounts.id)
                """
            )
        )

        # 老收入表整张重建：去掉 node_id 的 NOT NULL，换成 account_id
        income_cols = {
            r[1] for r in conn.execute(text("PRAGMA table_info(daily_incomes)")).fetchall()
        }
        if income_cols and "node_id" in income_cols:
            conn.execute(text("DROP TABLE IF EXISTS daily_incomes_new"))
            conn.execute(
                text(
                    """
                    CREATE TABLE daily_incomes_new (
                        id INTEGER NOT NULL PRIMARY KEY,
                        account_id INTEGER,
                        day VARCHAR(10),
                        amount NUMERIC(12, 2),
                        source VARCHAR(16),
                        note TEXT,
                        created_at DATETIME
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO daily_incomes_new
                        (id, account_id, day, amount, source, note, created_at)
                    SELECT d.id,
                           (SELECT n.account_id FROM nodes n WHERE n.id = d.node_id),
                           d.day, d.amount, d.source, d.note, d.created_at
                    FROM daily_incomes d
                    """
                )
            )
            conn.execute(text("DROP TABLE daily_incomes"))
            conn.execute(text("ALTER TABLE daily_incomes_new RENAME TO daily_incomes"))

        # 索引：重建表会把索引一起带走，这里补回来
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_daily_incomes_account_day "
                "ON daily_incomes (account_id, day)"
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_daily_incomes_day ON daily_incomes (day)")
        )

        # 数据搬完了，老的 nodes 表就没用了，删掉免得以后重复搬
        conn.execute(text("DROP TABLE IF EXISTS nodes"))


def init_engine():
    """惰性创建引擎，首次调用时建表。"""
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        connect_args = {"check_same_thread": False} if settings.resolved_database_url.startswith("sqlite") else {}
        _engine = create_engine(
            settings.resolved_database_url,
            connect_args=connect_args,
            future=True,
        )
        _session_factory = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
        from . import models  # noqa: F401  确保模型已注册

        Base.metadata.create_all(_engine)
        _apply_column_migrations(_engine)
        _migrate_rooms_to_accounts(_engine)
        # 迁移可能重建过表，再建一次把索引补上
        Base.metadata.create_all(_engine)
    return _engine


def get_db() -> Generator[Session, None, None]:
    init_engine()
    assert _session_factory is not None
    db = _session_factory()
    try:
        yield db
    finally:
        db.close()
