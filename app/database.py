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
    return _engine


def get_db() -> Generator[Session, None, None]:
    init_engine()
    assert _session_factory is not None
    db = _session_factory()
    try:
        yield db
    finally:
        db.close()
