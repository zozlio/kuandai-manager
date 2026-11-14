#!/usr/bin/env python3
"""往本地演示库里灌一批假数据，方便看界面效果。

    .venv/bin/python scripts/seed_demo.py

只往本地 .localdata 库写，绝不碰 NAS 上的正式数据。
账号号码统一用 1340000xxxx 假号段。
"""

from __future__ import annotations

import os
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", str(ROOT / ".localdata"))

from app import database  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    DailyIncome,
    Transaction,
    TX_RECHARGE,
)
from app.services import money  # noqa: E402

random.seed(20261108)

# 演示账号：手机号 / 宽带账号 / 注册码 / 注册人 / 手机卡使用人 / 机房 / 平台 / 节点号 / 月费 / 七折 / 余额
ACCOUNTS = [
    ("13400001111", "TEST-BB-0001", "6051111111", "张三", "张三", "香港机房", "某某云", "ND-HK-001", 399, True, 320.00),
    ("13400002222", "TEST-BB-0002", "6052222222", "李四", "李四", "香港机房", "某某云", "ND-HK-002", 399, True, 180.50),
    ("13400003333", "TEST-BB-0003", "6053333333", "王五", "王五", "香港机房", "另一云", "ND-HK-003", 399, False, 95.00),
    ("13400004444", "TEST-BB-0004", "6054444444", "赵六", "赵六", "香港机房", "另一云", "ND-HK-004", 399, False, 12.00),
    ("13400005555", "TEST-BB-0005", "6055555555", "钱七", "钱七", "郑州机房", "某某云", "ND-ZZ-001", 399, True, 640.00),
    ("13400006666", "TEST-BB-0006", "6056666666", "孙八", "孙八", "郑州机房", "某某云", "ND-ZZ-002", 399, False, 45.00),
    ("13400007777", "TEST-BB-0007", "6057777777", "周九", "周九", "郑州机房", "第三云", "ND-ZZ-003", 399, False, 0.00),
    ("13400008888", "TEST-BB-0008", "6058888888", "吴十", "吴十", "美西机房", "另一云", "ND-US-001", 399, False, 260.00),
]

# 这两条标成异常（原因 code）
ABNORMAL = {
    "13400007777": ("arrears", "欠费三天了"),
    "13400008888": ("machine_room", "机房检修"),
}


def main() -> int:
    database.init_engine()
    db = database._session_factory()
    try:
        # 清掉旧的演示数据（手机号 1340000 开头的）
        for a in db.query(Account).filter(Account.phone.like("1340000%")).all():
            db.delete(a)
        db.commit()

        today = date.today()
        for index, row in enumerate(ACCOUNTS):
            (
                phone, bb, code, registrant, sim_user, room, platform, node_account,
                fee, discount, balance,
            ) = row
            account = Account(
                phone=phone,
                broadband_account=bb,
                registration_code=code,
                registrant=registrant,
                sim_user=sim_user,
                registered_at=datetime.now() - timedelta(days=200 - index * 7),
                monthly_fee=fee,
                balance=money(balance),
                provider=random.choice(["中国电信", "中国联通", "中国移动"]),
                status="active",
                discount_enabled=discount,
                discount_enabled_at=datetime.now() - timedelta(days=30) if discount else None,
                total_recharged=money(balance + random.choice([0, 100, 200, 500])),
                room=room,
                platform=platform,
                node_account=node_account,
                notes="演示数据",
            )
            db.add(account)
            db.flush()

            # 开号流水
            db.add(
                Transaction(
                    account_id=account.id,
                    kind=TX_RECHARGE,
                    amount=money(balance),
                    period=today.strftime("%Y-%m"),
                    note="开号初始金额",
                )
            )

            # 近 14 天的每日收入
            for days_ago in range(14, -1, -1):
                day = (today - timedelta(days=days_ago)).strftime("%Y-%m-%d")
                base = random.uniform(45, 130)
                if platform == "另一云":
                    base *= 1.35
                if platform == "第三云":
                    base *= 0.6
                amount = money(round(base, 2))
                db.add(
                    DailyIncome(
                        account_id=account.id,
                        day=day,
                        amount=amount,
                        source="ai" if days_ago % 3 == 0 else "manual",
                        note="演示数据",
                    )
                )

            # 异常账号
            if phone in ABNORMAL:
                reason, note = ABNORMAL[phone]
                account.category = "abnormal"
                account.abnormal_reason = reason
                account.abnormal_note = note
                account.abnormal_at = datetime.now() - timedelta(days=2)

        db.commit()

        print(f"演示数据已灌好：{len(ACCOUNTS)} 条账号")
        print("  其中 2 条标成了异常账号")
        print("  每条账号带近 15 天的每日收入")
        print(f"  库位置：{ROOT / '.localdata' / 'kuandai.db'}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
