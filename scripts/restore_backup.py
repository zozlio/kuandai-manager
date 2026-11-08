#!/usr/bin/env python3
"""从全量备份 JSON 恢复 kuandai-manager 的数据。

用法：

    python3 scripts/restore_backup.py 备份文件.json --yes

行为：
1. 先把当前数据库改名成 kuandai.db.备份前时间，**原来的数据不会丢**；
2. 再把备份里的数据写进新库；
3. 万一中途出错，可以把备份前的库改回原名继续用。

要在项目根目录（或能 import 到 app 包的地方）运行。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 表名 → 模型
TABLE_MODELS = {
    "accounts": "Account",
    "account_groups": "AccountGroup",
    "nodes": "Node",
    "daily_incomes": "DailyIncome",
    "transactions": "Transaction",
    "reminders": "Reminder",
    "users": "User",
    "app_settings": "AppSetting",
}


def _coerce_row(model, row: dict) -> dict:
    """把备份里的字符串日期转回 datetime，SQLAlchemy 才认。"""
    from sqlalchemy import Date, DateTime

    out = {}
    for column in model.__table__.columns:
        if column.name not in row:
            continue
        value = row[column.name]
        if isinstance(value, str) and value:
            if isinstance(column.type, DateTime):
                try:
                    value = datetime.strptime(value, "%Y-%m-%d %H:%M")
                except ValueError:
                    try:
                        value = datetime.strptime(value, "%Y-%m-%d")
                    except ValueError:
                        value = None
            elif isinstance(column.type, Date):
                try:
                    value = datetime.strptime(value, "%Y-%m-%d").date()
                except ValueError:
                    value = None
        out[column.name] = value
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="从备份 JSON 恢复数据")
    parser.add_argument("backup", help="备份 JSON 文件路径")
    parser.add_argument(
        "--yes", action="store_true", help="确认要覆盖当前数据（不加这个参数只会看看不动手）"
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("DATA_DIR", "/data"),
        help="数据目录，默认取环境变量 DATA_DIR",
    )
    args = parser.parse_args()

    backup_path = Path(args.backup)
    if not backup_path.is_file():
        print(f"找不到备份文件：{backup_path}")
        return 1

    payload = json.loads(backup_path.read_text(encoding="utf-8"))
    if payload.get("format") != "kuandai-manager-backup":
        print("这个文件不是 kuandai-manager 的备份文件")
        return 1

    tables = payload.get("tables") or {}
    print(f"备份生成时间：{payload.get('exported_at', '未知')}")
    for name, rows in tables.items():
        print(f"  {name}: {len(rows)} 行")

    data_dir = Path(args.data_dir)
    db_path = data_dir / "kuandai.db"

    if not args.yes:
        print("\n这是演练模式，没有动任何数据。")
        print(f"要真正恢复，请加上 --yes（目标库：{db_path}）")
        return 0

    # 1. 老的库先改名保命
    backup_copy = None
    if db_path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_copy = db_path.with_name(f"kuandai.db.恢复前_{stamp}")
        shutil.copy2(db_path, backup_copy)
        print(f"\n当前数据库已另存为：{backup_copy}")

    # 2. 写入备份内容
    from app.database import Base, init_engine  # noqa: E402  延迟导入
    from app import models  # noqa: E402,F401

    engine = init_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        for table_name, rows in tables.items():
            attr = TABLE_MODELS.get(table_name)
            if not attr:
                print(f"  跳过不认识的表：{table_name}")
                continue
            model = getattr(models, attr)
            for row in rows:
                session.add(model(**_coerce_row(model, row)))
            print(f"  写入 {table_name}：{len(rows)} 行")
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        session.close()
        print(f"\n恢复失败：{exc}")
        # 出错了就把原来的库放回去，别让人对着空库发愣
        if backup_copy is not None:
            shutil.copy2(backup_copy, db_path)
            print(f"已经自动还原成原来的数据：{db_path}")
            print(f"（另存的那份也还在：{backup_copy}）")
        return 1
    finally:
        session.close()

    print(f"\n恢复完成 → {db_path}")
    print("建议重启一下服务，让它重新加载数据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
