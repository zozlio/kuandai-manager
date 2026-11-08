"""宽带账号台账 —— 回归测试。

跑法（项目根目录）：

    .venv/bin/python -m unittest discover -s tests -v

用的是标准库 unittest + starlette 自带的 TestClient，不需要额外装 pytest。
测试用的是独立的临时 SQLite 库，不会碰真实数据。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal

# 必须在 import app.* 之前设好：配置是第一次读环境变量时才生效的
_TMP_DIR = tempfile.mkdtemp(prefix="kd_test_")
os.environ["DATA_DIR"] = _TMP_DIR
os.environ["ENABLE_SCHEDULER"] = "false"

import sys  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import database, services, vision  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    ABNORMAL_REASONS,
    Account,
    AppSetting,
    DailyIncome,
    Reminder,
    Transaction,
    User,
)


def tearDownModule() -> None:
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


# ==========================================================================
# 一、纯函数层
# ==========================================================================


class TestAmountAndDailyCost(unittest.TestCase):
    """每日扣费的计算口径。"""

    def _account(self, **kw):
        base = dict(
            phone="13800000000",
            monthly_fee=Decimal("300"),
            balance=Decimal("0"),
            total_recharged=Decimal("0"),
            daily_cost=Decimal("0"),
        )
        base.update(kw)
        return Account(**base)

    def test_daily_cost_wins_over_monthly_fee(self):
        """填了「每日扣费」就用它，不再按月费分摊。"""
        account = self._account(daily_cost=Decimal("12.50"))
        self.assertEqual(services.daily_amount(account, date(2026, 11, 8)), Decimal("12.50"))

    def test_falls_back_to_monthly_fee(self):
        """没填每日扣费，按月费 ÷ 当月天数分摊。"""
        account = self._account(monthly_fee=Decimal("300"))
        # 2026 年 11 月 30 天 → 300 / 30 = 10
        self.assertEqual(services.daily_amount(account, date(2026, 11, 8)), Decimal("10.00"))

    def test_zero_monthly_fee_is_zero(self):
        account = self._account(monthly_fee=Decimal("0"))
        self.assertEqual(services.daily_amount(account, date(2026, 11, 8)), Decimal("0.00"))


class TestVisionParsing(unittest.TestCase):
    """AI 返回内容的解析要够宽容。"""

    def test_parse_fenced_json(self):
        text = '```json\n[{"broadband":"13400001111","node":"香港01","amount":88.5}]\n```'
        items = vision.parse_items(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["node"], "香港01")

    def test_parse_plain_array(self):
        items = vision.parse_items('[{"amount": 12.5}]')
        self.assertEqual(len(items), 1)

    def test_parse_wrapped_object(self):
        items = vision.parse_items('{"items": [{"amount": 3}]}')
        self.assertEqual(len(items), 1)

    def test_parse_garbage_returns_empty(self):
        self.assertEqual(vision.parse_items("完全不是 JSON"), [])
        self.assertEqual(vision.parse_items(""), [])

    def test_clean_amount(self):
        self.assertEqual(vision._clean_amount("¥1,234.50"), 1234.5)
        self.assertEqual(vision._clean_amount("88.5元"), 88.5)
        self.assertEqual(vision._clean_amount("-12"), -12.0)
        self.assertEqual(vision._clean_amount(""), 0.0)
        self.assertEqual(vision._clean_amount(7.25), 7.25)

    def test_normalize_aliases(self):
        """模型可能用「宽带账号」「节点」「收益」等别名，normalize 要统一。"""
        out = vision.normalize_items(
            [{"宽带账号": "13400001111", "节点号": "ND-001", "收益": "66.5"}]
        )
        self.assertEqual(out[0]["broadband"], "13400001111")
        self.assertEqual(out[0]["node_account"], "ND-001")
        self.assertEqual(out[0]["amount"], 66.5)

    def test_vision_configured(self):
        self.assertFalse(vision.vision_configured({"vision_model": "x"}))
        self.assertTrue(
            vision.vision_configured({"vision_model": "x", "vision_api_key": "k"})
        )


class TestChartSvg(unittest.TestCase):
    """图表是纯 SVG 生成的，不依赖外网。"""

    def test_bar_chart_svg(self):
        svg = services.bar_chart_svg(
            [{"day": "2026-11-08", "amount": Decimal("99.9")}], "#3b82f6"
        )
        self.assertTrue(svg.strip().startswith("<svg"))
        self.assertIn("</svg>", svg)
        self.assertIn("2026-11-08", svg)

    def test_donut_svg(self):
        svg = services.donut_svg(
            [{"platform": "某某云", "amount": Decimal("99.9"), "ratio": 100.0}], 14
        )
        self.assertTrue(svg.strip().startswith("<svg"))
        self.assertIn("某某云", svg)


class TestColumnMigration(unittest.TestCase):
    """老库加新列的迁移脚本。"""

    def test_adds_new_columns(self):
        from sqlalchemy import create_engine, text

        path = os.path.join(_TMP_DIR, "migrate_case.db")
        engine = create_engine(f"sqlite:///{path}")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE accounts (id INTEGER PRIMARY KEY, phone VARCHAR(20))"))

        database._apply_column_migrations(engine)

        with engine.begin() as conn:
            cols = {r[1] for r in conn.execute(text("PRAGMA table_info(accounts)")).fetchall()}
        engine.dispose()

        for col in (
            "category",
            "abnormal_reason",
            "abnormal_at",
            "abnormal_note",
            "group_id",
            "install_address",
            "daily_cost",
        ):
            self.assertIn(col, cols)


# ==========================================================================
# 二、带数据库的服务层
# ==========================================================================


class ServiceLayerTest(unittest.TestCase):
    """需要真实数据库的服务层用例。"""

    def setUp(self):
        database.init_engine()
        self.db = database._session_factory()
        # 清干净
        self.db.query(DailyIncome).delete()
        self.db.query(Account).delete()
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _make_account(self, phone="13400001111", **kw):
        account = Account(
            phone=phone,
            broadband_account=kw.pop("broadband_account", "93711111111"),
            registration_code=kw.pop("registration_code", "REG001"),
            registrant=kw.pop("registrant", "张三"),
            monthly_fee=kw.pop("monthly_fee", Decimal("399")),
            balance=kw.pop("balance", Decimal("500")),
            total_recharged=kw.pop("total_recharged", Decimal("500")),
            daily_cost=kw.pop("daily_cost", Decimal("12.50")),
            install_address=kw.pop("install_address", "罗山县二里湾小区"),
            **kw,
        )
        self.db.add(account)
        self.db.commit()
        self.db.refresh(account)
        return account

    def _setup_node(self, account, room="香港机房", platform="某某云", node_account="srv001"):
        """机房 / 节点平台 / 节点号都是账号自己的字段。"""
        account.room = room
        account.platform = platform
        account.node_account = node_account
        self.db.commit()
        self.db.refresh(account)
        return account

    # -- 收入 ----------------------------------------------------------

    def test_upsert_income_overwrites_same_day(self):
        """同一天再记一笔是覆盖，不是累加。"""
        account = self._make_account()
        today = date.today().strftime("%Y-%m-%d")
        services.upsert_income(self.db, account.id, today, Decimal("88.5"), "manual")
        services.upsert_income(self.db, account.id, today, Decimal("99.9"), "manual")
        self.db.commit()
        rows = self.db.query(DailyIncome).filter(DailyIncome.account_id == account.id).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            services.account_income_on(self.db, account, today), Decimal("99.90")
        )

    def test_dashboard_numbers(self):
        account = self._make_account(daily_cost=Decimal("12.50"))
        today = date.today().strftime("%Y-%m-%d")
        services.upsert_income(self.db, account.id, today, Decimal("99.90"), "manual")
        self.db.commit()

        dash = services.income_dashboard(self.db, category="normal", days=14)
        self.assertEqual(dash["today_income"], Decimal("99.90"))
        self.assertEqual(dash["today_cost"], Decimal("12.50"))
        self.assertEqual(dash["today_profit"], Decimal("87.40"))
        self.assertEqual(dash["total_income"], Decimal("99.90"))

    def test_dashboard_respects_category(self):
        """正常页和异常页的数字互不掺和。"""
        normal = self._make_account(phone="13400001111", daily_cost=Decimal("10"))
        abnormal = self._make_account(phone="13400002222", daily_cost=Decimal("20"))
        self._setup_node(normal, platform="云A", node_account="ND-A")
        self._setup_node(abnormal, platform="云B", node_account="ND-B")
        today = date.today().strftime("%Y-%m-%d")
        services.upsert_income(self.db, normal.id, today, Decimal("100"), "manual")
        services.upsert_income(self.db, abnormal.id, today, Decimal("50"), "manual")
        services.mark_abnormal(self.db, abnormal, "machine_room")
        self.db.commit()

        normal_dash = services.income_dashboard(self.db, category="normal", days=14)
        abnormal_dash = services.income_dashboard(self.db, category="abnormal", days=14)
        self.assertEqual(normal_dash["today_income"], Decimal("100.00"))
        self.assertEqual(normal_dash["today_cost"], Decimal("10.00"))
        self.assertEqual(abnormal_dash["today_income"], Decimal("50.00"))
        self.assertEqual(abnormal_dash["today_cost"], Decimal("20.00"))

    def test_platform_ratio(self):
        a1 = self._make_account(phone="13400001111")
        a2 = self._make_account(phone="13400002222")
        self._setup_node(a1, platform="云A", node_account="ND-A")
        self._setup_node(a2, platform="云B", node_account="ND-B")
        today = date.today().strftime("%Y-%m-%d")
        services.upsert_income(self.db, a1.id, today, Decimal("75"), "manual")
        services.upsert_income(self.db, a2.id, today, Decimal("25"), "manual")
        self.db.commit()

        dash = services.income_dashboard(self.db, category="normal", days=14)
        ratio = {p["platform"]: p["ratio"] for p in dash["platforms"]}
        self.assertAlmostEqual(ratio["云A"], 75.0, places=2)
        self.assertAlmostEqual(ratio["云B"], 25.0, places=2)

    def test_daily_series_length_matches_window(self):
        dash = services.income_dashboard(self.db, category="normal", days=7)
        self.assertEqual(len(dash["daily"]), 7)
        self.assertEqual(dash["daily"][-1]["day"], date.today().strftime("%Y-%m-%d"))

    # -- 两级匹配 ------------------------------------------------------

    def test_match_by_phone_then_node_account(self):
        """先认宽带账号，再核对节点号。"""
        account = self._make_account()
        self._setup_node(account, node_account="srv001")
        out = services.match_recognized_items(
            self.db,
            [{"broadband": "13400001111", "node_account": "srv001", "amount": 88.5}],
        )
        self.assertEqual(out[0]["status"], "matched")
        self.assertEqual(out[0]["account_id"], account.id)
        self.assertEqual(out[0]["account_reason"], "手机号")
        self.assertEqual(out[0]["node_reason"], "节点号一致")

    def test_match_node_account_mismatch(self):
        """账号认出来了但节点号对不上，要标出来让人确认。"""
        account = self._make_account()
        self._setup_node(account, node_account="srv001")
        out = services.match_recognized_items(
            self.db,
            [{"broadband": "13400001111", "node_account": "另一个号", "amount": 88.5}],
        )
        self.assertEqual(out[0]["account_id"], account.id)
        self.assertEqual(out[0]["status"], "no_node")
        self.assertEqual(out[0]["node_reason"], "节点号不一致")

    def test_match_by_broadband_account(self):
        account = self._make_account()
        out = services.match_recognized_items(
            self.db, [{"broadband": "93711111111", "node_account": "ND-1", "amount": 1}]
        )
        self.assertEqual(out[0]["account_id"], account.id)
        self.assertEqual(out[0]["account_reason"], "宽带账号")
        # 账号还没登记节点号，节点号对不上但账号是对的
        self.assertEqual(out[0]["status"], "matched")

    def test_match_unknown_account(self):
        self._make_account()
        out = services.match_recognized_items(
            self.db, [{"broadband": "19999999999", "node_account": "某节点", "amount": 1}]
        )
        self.assertEqual(out[0]["status"], "no_account")
        self.assertIsNone(out[0]["account_id"])

    # -- 分类 ----------------------------------------------------------

    def test_mark_and_restore(self):
        account = self._make_account()
        services.mark_abnormal(self.db, account, "throttle", "晚上卡")
        self.assertEqual(account.category, "abnormal")
        self.assertEqual(account.abnormal_reason, "throttle")
        self.assertIsNotNone(account.abnormal_at)

        services.restore_account(self.db, account)
        self.assertEqual(account.category, "normal")
        self.assertEqual(account.abnormal_reason, "")
        self.assertIsNone(account.abnormal_at)

    def test_all_six_reasons_allowed(self):
        """六个异常原因，一个都不能少。"""
        self.assertEqual(
            set(ABNORMAL_REASONS.values()),
            {"机房", "当前电费", "管理线", "限速", "注销", "欠费"},
        )
        self.assertEqual(len(ABNORMAL_REASONS), 6)

    def test_search_by_install_address(self):
        self._make_account(phone="13400001111", install_address="罗山县二里湾小区")
        self._make_account(phone="13400002222", install_address="光山县五楼村")
        found = services.search_accounts(self.db, "二里湾", "", "normal")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].phone, "13400001111")

    def test_search_by_category(self):
        a = self._make_account(phone="13400001111")
        b = self._make_account(phone="13400002222")
        services.mark_abnormal(self.db, b, "arrears")
        normal = services.search_accounts(self.db, "", "", "normal")
        abnormal = services.search_accounts(self.db, "", "", "abnormal")
        self.assertEqual([x.phone for x in normal], [a.phone])
        self.assertEqual([x.phone for x in abnormal], [b.phone])


# ==========================================================================
# 三、HTTP 接口层
# ==========================================================================


class HttpTest(unittest.TestCase):
    """走真实路由的接口测试。"""

    def setUp(self):
        database.init_engine()
        self.db = database._session_factory()
        # 清干净，保证每个用例都从零开始（用户也要清，否则第二次注册会失败）
        self.db.query(DailyIncome).delete()
        self.db.query(Account).delete()
        self.db.query(User).delete()
        self.db.query(AppSetting).filter(AppSetting.key.like("vision_%")).delete()
        self.db.commit()
        self.db.close()

        self.client = TestClient(app)
        # 首次访问会走注册页，注册后自动登录（TestClient 默认跟随重定向）
        self.client.post("/register", data={"username": "tester", "password": "test1234"})
        # 确认真的登录上了（cookie 生效），没登录的话首页会显示登录页
        home = self.client.get("/")
        self.assertIn("正常账号", home.text)
        self.assertNotIn("登录后才能查看台账", home.text)

    def tearDown(self):
        self.client.close()

    # -- 登录态 --------------------------------------------------------

    def test_homepage_requires_login(self):
        anon = TestClient(app)
        try:
            resp = anon.get("/")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("登录", resp.text)
        finally:
            anon.close()

    def test_all_pages_ok(self):
        for path in (
            "/",
            "/?tab=abnormal",
            "/?days=7",
            "/?days=30",
            "/nodes",
            "/groups",
            "/recognize",
            "/settings",
            "/reminders",
            "/chat",
            "/accounts/new",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    # -- 账号 ----------------------------------------------------------

    def test_create_account_with_new_fields(self):
        resp = self.client.post(
            "/accounts/new",
            data={
                "phone": "13400001111",
                "broadband_account": "93711111111",
                "registrant": "张三",
                "monthly_fee": "399",
                "balance": "500",
                "install_address": "罗山县二里湾小区",
                "daily_cost": "12.5",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        account_id = int(resp.headers["location"].rstrip("/").split("/")[-1])

        detail = self.client.get(f"/accounts/{account_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("罗山县二里湾小区", detail.text)

        db = database._session_factory()
        try:
            account = db.get(Account, account_id)
            self.assertEqual(account.install_address, "罗山县二里湾小区")
            self.assertEqual(account.daily_cost, Decimal("12.50"))
            self.assertEqual(account.category, "normal")
        finally:
            db.close()

    def test_install_address_is_last_column(self):
        """安装地址要放在列表最后一列。"""
        self.client.post(
            "/accounts/new",
            data={"phone": "13400001111", "install_address": "罗山县二里湾小区"},
            follow_redirects=False,
        )
        home = self.client.get("/")
        self.assertIn("安装地址", home.text)
        # 最后一列表头就是「安装地址」
        head = home.text.split("<thead")[1].split("</thead>")[0]
        last_th = head.split("<th")[-1]
        self.assertIn("安装地址", last_th)

    # -- 节点与收入 ----------------------------------------------------

    def _new_account(self, phone="13400001111"):
        resp = self.client.post(
            "/accounts/new", data={"phone": phone}, follow_redirects=False
        )
        return int(resp.headers["location"].rstrip("/").split("/")[-1])

    def test_account_with_room_platform_and_node_account(self):
        """账号自带机房 / 节点平台 / 节点号。"""
        resp = self.client.post(
            "/accounts/new",
            data={
                "phone": "13400001111",
                "room": "香港机房",
                "platform": "某某云",
                "node_account": "srv001",
            },
            follow_redirects=False,
        )
        account_id = int(resp.headers["location"].rstrip("/").split("/")[-1])

        db = database._session_factory()
        try:
            account = db.get(Account, account_id)
            self.assertEqual(account.room, "香港机房")
            self.assertEqual(account.platform, "某某云")
            self.assertEqual(account.node_account, "srv001")
        finally:
            db.close()

        rooms_page = self.client.get("/rooms")
        self.assertIn("香港机房", rooms_page.text)
        self.assertIn("某某云", rooms_page.text)
        self.assertIn("srv001", rooms_page.text)

    def test_income_flow(self):
        """记收入：同一天重复记是覆盖。"""
        account_id = self._new_account()
        today = date.today().strftime("%Y-%m-%d")
        r1 = self.client.post(
            f"/api/accounts/{account_id}/income",
            data={"day": today, "amount": "88.5"},
        )
        self.assertEqual(r1.status_code, 200)
        self.assertAlmostEqual(r1.json()["account_income"], 88.5, places=2)

        # 同一天再记 → 覆盖
        r2 = self.client.post(
            f"/api/accounts/{account_id}/income",
            data={"day": today, "amount": "99.9"},
        )
        self.assertAlmostEqual(r2.json()["account_income"], 99.9, places=2)

        db = database._session_factory()
        try:
            rows = db.query(DailyIncome).filter(DailyIncome.account_id == account_id).all()
            self.assertEqual(len(rows), 1)
        finally:
            db.close()

    def test_income_bad_day_rejected(self):
        account_id = self._new_account()
        resp = self.client.post(
            f"/api/accounts/{account_id}/income", data={"day": "2026/11/08", "amount": "1"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_homepage_shows_income_and_profit(self):
        account_id = self._new_account()
        today = date.today().strftime("%Y-%m-%d")
        self.client.post(
            f"/api/accounts/{account_id}/income", data={"day": today, "amount": "99.9"}
        )
        home = self.client.get("/")
        self.assertIn("99.90", home.text)
        self.assertIn("今日平台收入", home.text)
        self.assertIn("净利润", home.text)

    # -- 分组 ----------------------------------------------------------

    def test_group_create_and_assign(self):
        account_id = self._new_account()
        resp = self.client.post(
            "/groups",
            data={
                "name": "机房A",
                "install_location": "罗山县城东",
                "manage_phone": "13900001111",
                "manage_account": "GD8899",
                "manage_expire_at": "2026-12-31",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)

        groups_page = self.client.get("/groups")
        self.assertIn("机房A", groups_page.text)
        self.assertIn("罗山县城东", groups_page.text)
        self.assertIn("13900001111", groups_page.text)

        db = database._session_factory()
        try:
            group = db.query(services.AccountGroup).filter(
                services.AccountGroup.name == "机房A"
            ).one()
            group_id = group.id
        finally:
            db.close()

        assign = self.client.post(
            f"/accounts/{account_id}/group",
            data={"group_id": str(group_id)},
            follow_redirects=False,
        )
        self.assertEqual(assign.status_code, 303)

        home = self.client.get("/")
        self.assertIn("机房A", home.text)

    def test_delete_group_keeps_account(self):
        account_id = self._new_account()
        self.client.post(
            "/groups", data={"name": "临时组"}, follow_redirects=False
        )
        db = database._session_factory()
        try:
            group = db.query(services.AccountGroup).filter(
                services.AccountGroup.name == "临时组"
            ).one()
            group_id = group.id
            self.client.post(
                f"/accounts/{account_id}/group", data={"group_id": str(group_id)}
            )
            self.client.post(f"/groups/{group_id}/delete", follow_redirects=False)
            account = db.get(Account, account_id)
            self.assertIsNotNone(account)
            self.assertIsNone(account.group_id)
        finally:
            db.close()

    # -- 异常 ----------------------------------------------------------

    def test_mark_abnormal_moves_to_abnormal_tab(self):
        account_id = self._new_account()
        resp = self.client.post(
            f"/accounts/{account_id}/abnormal",
            data={"reason": "machine_room", "note": "停电"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(resp.json()["reason_label"], "机房")
        self.assertEqual(resp.json()["reason"], "machine_room")

        normal_page = self.client.get("/")
        self.assertNotIn("13400001111", normal_page.text)

        abnormal_page = self.client.get("/?tab=abnormal")
        self.assertIn("13400001111", abnormal_page.text)

        restore = self.client.post(f"/accounts/{account_id}/restore")
        self.assertTrue(restore.json()["ok"])
        self.assertIn("13400001111", self.client.get("/").text)

    def test_invalid_abnormal_reason_rejected(self):
        account_id = self._new_account()
        resp = self.client.post(
            f"/accounts/{account_id}/abnormal", data={"reason": "随便写的"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_every_reason_accepted(self):
        account_id = self._new_account()
        for reason in ABNORMAL_REASONS:
            with self.subTest(reason=reason):
                resp = self.client.post(
                    f"/accounts/{account_id}/abnormal", data={"reason": reason}
                )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["reason"], reason)
                self.client.post(f"/accounts/{account_id}/restore")

    # -- AI 识图 -------------------------------------------------------

    def test_recognize_without_config_returns_503(self):
        # 1x1 透明 PNG
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        resp = self.client.post(
            "/api/recognize", files={"image": ("shot.png", png, "image/png")}
        )
        self.assertEqual(resp.status_code, 503)
        self.assertIn("视觉模型", resp.json()["error"])

    def test_recognize_apply_writes_income(self):
        account_id = self._new_account()
        today = date.today().strftime("%Y-%m-%d")
        payload = json.dumps(
            [
                {
                    "use": True,
                    "account_id": account_id,
                    "node_account": "ND-001",
                    "amount": 66.5,
                    "day": today,
                },
                {
                    "use": False,
                    "account_id": account_id,
                    "amount": 999,
                    "day": today,
                },
            ]
        )
        resp = self.client.post("/api/recognize/apply", data={"payload": payload})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["saved"], 1)

        db = database._session_factory()
        try:
            rows = db.query(DailyIncome).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].amount, Decimal("66.50"))
            self.assertEqual(rows[0].source, "ai")
        finally:
            db.close()

    def test_recognize_apply_updates_node_account(self):
        """回填时顺手把节点号 / 平台更新到账号上。"""
        account_id = self._new_account()
        payload = json.dumps(
            [
                {
                    "use": True,
                    "account_id": account_id,
                    "node_account": "ND-NEW",
                    "platform": "新平台",
                    "amount": 30,
                    "day": date.today().strftime("%Y-%m-%d"),
                }
            ]
        )
        resp = self.client.post("/api/recognize/apply", data={"payload": payload})
        self.assertEqual(resp.json()["saved"], 1)
        self.assertEqual(resp.json()["updated_nodes"], 1)

        db = database._session_factory()
        try:
            account = db.get(Account, account_id)
            self.assertEqual(account.node_account, "ND-NEW")
            self.assertEqual(account.platform, "新平台")
        finally:
            db.close()

    def test_recognize_apply_bad_payload(self):
        resp = self.client.post("/api/recognize/apply", data={"payload": "不是 JSON"})
        self.assertEqual(resp.status_code, 400)

    # -- 视觉配置 ------------------------------------------------------

    def test_save_vision_settings(self):
        resp = self.client.post(
            "/settings/vision",
            data={
                "vision_base_url": "https://api.minimaxi.com/v1",
                "vision_api_key": "sk-test-key",
                "vision_model": "MiniMax-M3-VL-01",
                "vision_format": "minimax_vl",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)

        settings_page = self.client.get("/settings")
        self.assertIn("MiniMax-M3-VL-01", settings_page.text)

        recognize = self.client.get("/recognize")
        self.assertNotIn("视觉模型还没配置", recognize.text)

    def test_vision_settings_keep_key_when_blank(self):
        """密钥留空表示保留原来的，不能被清掉。"""
        self.client.post(
            "/settings/vision",
            data={"vision_api_key": "sk-first", "vision_model": "M-VL"},
            follow_redirects=False,
        )
        self.client.post(
            "/settings/vision",
            data={"vision_model": "M-VL-2", "vision_api_key": ""},
            follow_redirects=False,
        )
        db = database._session_factory()
        try:
            config = services.get_vision_config(db)
            self.assertEqual(config["vision_api_key"], "sk-first")
            self.assertEqual(config["vision_model"], "M-VL-2")
        finally:
            db.close()


    # -- 机房（界面上叫机房，数据库里还是 nodes 表）-------------------

    def test_rooms_page_uses_room_wording(self):
        """界面上统一叫「机房」。"""
        resp = self.client.get("/rooms")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("机房与收入", resp.text)
        self.assertIn("节点号", resp.text)   # 节点号是要的
        self.assertNotIn("新增节点", resp.text)
        self.assertNotIn("节点账号", resp.text)

    def test_nav_uses_room_wording(self):
        home = self.client.get("/")
        self.assertIn(">机房<", home.text)
        self.assertNotIn(">节点<", home.text)

    def test_rooms_page_groups_by_room_and_platform(self):
        """机房页：按机房分组，组内按平台分组，平台下是宽带账号和节点号。"""
        self.client.post(
            "/accounts/new",
            data={
                "phone": "13400001111",
                "room": "香港机房",
                "platform": "某某云",
                "node_account": "srv001",
            },
            follow_redirects=False,
        )
        self.client.post(
            "/accounts/new",
            data={
                "phone": "13400002222",
                "room": "香港机房",
                "platform": "另一平台",
                "node_account": "srv002",
            },
            follow_redirects=False,
        )
        self.client.post(
            "/accounts/new",
            data={
                "phone": "13400003333",
                "room": "郑州机房",
                "platform": "某某云",
                "node_account": "srv003",
            },
            follow_redirects=False,
        )

        page = self.client.get("/rooms")
        self.assertEqual(page.status_code, 200)
        self.assertIn("香港机房", page.text)
        self.assertIn("郑州机房", page.text)
        self.assertIn("某某云", page.text)
        self.assertIn("另一平台", page.text)
        self.assertIn("srv001", page.text)

    def test_node_account_and_platform_are_editable(self):
        """节点号和节点平台随时可以改。"""
        resp = self.client.post(
            "/accounts/new",
            data={
                "phone": "13400001111",
                "room": "香港机房",
                "platform": "某某云",
                "node_account": "srv001",
            },
            follow_redirects=False,
        )
        account_id = int(resp.headers["location"].rstrip("/").split("/")[-1])

        self.client.post(
            f"/accounts/{account_id}/edit",
            data={
                "phone": "13400001111",
                "room": "郑州机房",
                "platform": "新平台",
                "node_account": "srv999",
            },
            follow_redirects=False,
        )
        db = database._session_factory()
        try:
            account = db.get(Account, account_id)
            self.assertEqual(account.room, "郑州机房")
            self.assertEqual(account.platform, "新平台")
            self.assertEqual(account.node_account, "srv999")
        finally:
            db.close()

    def test_accounts_csv_has_room_columns(self):
        self.client.post(
            "/accounts/new",
            data={
                "phone": "13400001111",
                "room": "香港机房",
                "platform": "某某云",
                "node_account": "srv001",
            },
            follow_redirects=False,
        )
        resp = self.client.get("/export/accounts.csv")
        self.assertEqual(resp.status_code, 200)
        text = resp.content.decode("utf-8")
        header = text.strip().splitlines()[0]
        self.assertIn("机房", header)
        self.assertIn("节点平台", header)
        self.assertIn("节点号", header)
        self.assertIn("香港机房", text)
        self.assertIn("srv001", text)

    # -- 宽带账号锁定 / 运营商下拉 ------------------------------------

    def test_broadband_account_cannot_be_changed(self):
        """宽带账号是身份标识：绕过页面直接 POST 改也不生效。"""
        resp = self.client.post(
            "/accounts/new",
            data={"phone": "13400001111", "broadband_account": "TEST-BB-0001"},
            follow_redirects=False,
        )
        account_id = int(resp.headers["location"].rstrip("/").split("/")[-1])

        # 试着改成别的
        self.client.post(
            f"/accounts/{account_id}/edit",
            data={"phone": "13400001111", "broadband_account": "偷偷改的账号"},
            follow_redirects=False,
        )
        db = database._session_factory()
        try:
            account = db.get(Account, account_id)
            self.assertEqual(account.broadband_account, "TEST-BB-0001")  # 没被改掉
        finally:
            db.close()

    def test_broadband_account_can_be_set_on_create(self):
        """新建时当然可以填宽带账号。"""
        resp = self.client.post(
            "/accounts/new",
            data={"phone": "13400002222", "broadband_account": "NEW-BB-002"},
            follow_redirects=False,
        )
        account_id = int(resp.headers["location"].rstrip("/").split("/")[-1])
        db = database._session_factory()
        try:
            account = db.get(Account, account_id)
            self.assertEqual(account.broadband_account, "NEW-BB-002")
        finally:
            db.close()

    def test_provider_is_dropdown(self):
        """运营商用下拉框选，常见几家都在。"""
        form = self.client.get("/accounts/new")
        self.assertIn('<select name="provider">', form.text)
        for name in ("中国电信", "中国联通", "中国移动"):
            self.assertIn(f"<option value=\"{name}\"", form.text)

    def test_copy_buttons_present(self):
        """列表和详情页都要有复制按钮。"""
        self.client.post(
            "/accounts/new",
            data={
                "phone": "13400001111",
                "broadband_account": "TEST-BB-0001",
                "node_account": "ND-001",
            },
            follow_redirects=False,
        )
        home = self.client.get("/")
        self.assertIn("copyText(", home.text)
        # 手机号 / 宽带账号 / 节点号 三样都能复制
        self.assertIn("复制手机号", home.text)
        self.assertIn("复制宽带账号", home.text)
        self.assertIn("复制节点号", home.text)

        detail = self.client.get("/accounts/1")
        self.assertIn("copyText(", detail.text)
        self.assertIn("复制宽带账号", detail.text)
        self.assertIn("复制节点号", detail.text)

    def test_rooms_page_has_copy_buttons(self):
        """机房页也要能复制宽带账号和节点号。"""
        self.client.post(
            "/accounts/new",
            data={
                "phone": "13400001111",
                "room": "香港机房",
                "platform": "某某云",
                "node_account": "srv001",
                "broadband_account": "TEST-BB-0001",
            },
            follow_redirects=False,
        )
        page = self.client.get("/rooms")
        self.assertIn("复制手机号", page.text)
        self.assertIn("复制宽带账号", page.text)
        self.assertIn("复制节点号", page.text)

    # -- 数据导出 ------------------------------------------------------

    def test_export_page_ok(self):
        resp = self.client.get("/export")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("数据导出", resp.text)
        self.assertIn("全量备份", resp.text)

    def test_accounts_csv_has_header_and_data(self):
        account_id = self._new_account()
        resp = self.client.get("/export/accounts.csv")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp.headers.get("content-disposition", ""))
        text = resp.content.decode("utf-8")
        self.assertTrue(text.startswith("﻿"))  # BOM，Excel 才不乱码
        lines = text.strip().splitlines()
        self.assertIn("手机号", lines[0])
        self.assertIn("每日扣费", lines[0])
        self.assertIn("安装地址", lines[0])
        self.assertTrue(any("13400001111" in line for line in lines[1:]))

    def test_export_csv_files_all_ok(self):
        for path in (
            "/export/accounts.csv",
            "/export/nodes.csv",
            "/export/incomes.csv",
            "/export/groups.csv",
            "/export/transactions.csv",
        ):
            with self.subTest(path=path):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.content)

    def test_backup_json_contains_all_tables(self):
        account_id = self._new_account()
        resp = self.client.get("/export/backup.json")
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.content.decode("utf-8"))
        self.assertEqual(payload["format"], "kuandai-manager-backup")
        for table in (
            "accounts",
            "account_groups",
            "daily_incomes",
            "transactions",
            "users",
            "app_settings",
        ):
            self.assertIn(table, payload["tables"])
        phones = [row["phone"] for row in payload["tables"]["accounts"]]
        self.assertIn("13400001111", phones)

    def test_backup_json_is_restorable(self):
        """备份里的账号信息要够完整，才能还原回去。"""
        self._new_account()
        resp = self.client.get("/export/backup.json")
        payload = json.loads(resp.content.decode("utf-8"))
        row = payload["tables"]["accounts"][0]
        for field in (
            "phone",
            "broadband_account",
            "monthly_fee",
            "balance",
            "total_recharged",
            "category",
            "install_address",
            "daily_cost",
        ):
            self.assertIn(field, row)

    # -- 余额与扣费展示 ------------------------------------------------

    def test_dashboard_has_total_balance(self):
        """看板要显示剩余余额合计。"""
        self._new_account()
        db = database._session_factory()
        try:
            account = db.query(Account).one()
            account.balance = Decimal("123.45")
            db.commit()
        finally:
            db.close()

        dash = services.income_dashboard(db, category="normal", days=14)
        self.assertEqual(dash["total_balance"], Decimal("123.45"))
        home = self.client.get("/")
        self.assertIn("剩余余额", home.text)
        self.assertIn("123.45", home.text)

    def test_daily_cost_blank_uses_monthly_fee(self):
        """每日扣费留空时，七折后按月费分摊：399 × 0.7 ÷ 30 = 9.31。"""
        account_id = self._new_account()
        db = database._session_factory()
        try:
            account = db.get(Account, account_id)
            account.monthly_fee = Decimal("399")
            account.discount_enabled = True
            account.daily_cost = Decimal("0")
            db.commit()
            daily = services.daily_amount(account, date(2026, 11, 8))
            self.assertEqual(daily, Decimal("9.31"))
        finally:
            db.close()

    def test_daily_cost_fixed_overrides_discount(self):
        """填了每日扣费就以填的为准，七折不参与。"""
        account_id = self._new_account()
        db = database._session_factory()
        try:
            account = db.get(Account, account_id)
            account.monthly_fee = Decimal("399")
            account.discount_enabled = True
            account.daily_cost = Decimal("12.50")
            db.commit()
            self.assertEqual(
                services.daily_amount(account, date(2026, 11, 8)), Decimal("12.50")
            )
        finally:
            db.close()

    def test_auto_billing_deducts_and_records(self):
        """自动扣费：扣余额 + 记一笔扣费流水。"""
        account_id = self._new_account()
        db = database._session_factory()
        try:
            account = db.get(Account, account_id)
            account.balance = Decimal("100")
            account.monthly_fee = Decimal("399")
            account.discount_enabled = True
            account.daily_cost = Decimal("0")
            account.status = "active"
            db.commit()

            stats = services.run_daily_billing(db, date(2026, 11, 8))
            db.refresh(account)
            self.assertEqual(stats["charged"], 1)
            self.assertEqual(account.balance, Decimal("90.69"))  # 100 - 9.31
            self.assertEqual(account.last_deducted_date, "2026-11-08")
            tx = (
                db.query(Transaction)
                .filter(Transaction.account_id == account_id, Transaction.kind == "charge")
                .one()
            )
            self.assertEqual(tx.kind, "charge")
            self.assertEqual(tx.amount, Decimal("9.31"))
        finally:
            db.close()

    def test_auto_billing_skips_when_balance_short(self):
        """余额不够就不扣，挂待缴费提醒。"""
        account_id = self._new_account()
        db = database._session_factory()
        try:
            account = db.get(Account, account_id)
            account.balance = Decimal("1")
            account.monthly_fee = Decimal("399")
            account.daily_cost = Decimal("9.31")
            account.status = "active"
            db.commit()

            stats = services.run_daily_billing(db, date(2026, 11, 8))
            db.refresh(account)
            self.assertEqual(stats["charged"], 0)
            self.assertEqual(account.balance, Decimal("1.00"))  # 余额没动
            self.assertTrue(db.query(Reminder).filter(Reminder.account_id == account_id).count())
        finally:
            db.close()

    def test_auto_billing_runs_once_per_day(self):
        """同一天不会重复扣。"""
        account_id = self._new_account()
        db = database._session_factory()
        try:
            account = db.get(Account, account_id)
            account.balance = Decimal("100")
            account.daily_cost = Decimal("10")
            account.status = "active"
            db.commit()

            services.run_daily_billing(db, date(2026, 11, 8))
            db.refresh(account)
            self.assertEqual(account.balance, Decimal("90.00"))

            stats = services.run_daily_billing(db, date(2026, 11, 8))
            db.refresh(account)
            self.assertEqual(stats["charged"], 0)
            self.assertEqual(account.balance, Decimal("90.00"))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
