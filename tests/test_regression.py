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
from app import __version__ as APP_VERSION  # noqa: E402
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


class _FakeResponse:
    """伪造 httpx.Response，不真正发网络请求。"""

    def __init__(self, status_code, json_data, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        return self._json


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

    def test_415_error_points_to_format_setting(self):
        """415 要能指路：提示里必须带「视觉格式」和「OpenAI 兼容」，原文只进日志。"""
        resp = _FakeResponse(415, {}, text='{"error":{"message":"Unsupported Media Type: Content-Type must be application/json"}}')
        with self.assertRaisesRegex(RuntimeError, "视觉格式") as ctx:
            vision._read_reply(resp)
        msg = str(ctx.exception)
        self.assertIn("OpenAI 兼容", msg)
        self.assertIn("415", msg)
        # 端点英文原文不要直接甩给用户
        self.assertNotIn("Unsupported Media Type: Content-Type", msg)

    def test_truncated_reply_raises(self):
        """finish_reason == length 说明生成被截断，不能把半截 JSON 给解析器。"""
        resp = _FakeResponse(200, {
            "choices": [{
                "message": {"content": "[{\"amount\": 1.5}"},  # 半截
                "finish_reason": "length",
            }]
        })
        with self.assertRaisesRegex(RuntimeError, "截断"):
            vision._read_reply(resp)

    def test_ok_reply_reads_content(self):
        resp = _FakeResponse(200, {
            "choices": [{
                "message": {"content": '[{"amount": 1.5}]'},
                "finish_reason": "stop",
            }]
        })
        self.assertEqual(vision._read_reply(resp), '[{"amount": 1.5}]')

    def test_max_tokens_falls_back_to_default(self):
        """设置项没配时用默认 8192，不写死在别处。"""
        self.assertEqual(vision._max_tokens({}), 8192)
        self.assertEqual(vision._max_tokens({"ai_max_tokens": "4096"}), 4096)
        self.assertEqual(vision._max_tokens({"ai_max_tokens": ""}), 8192)

    def test_retry_on_429_then_ok(self):
        """429 限流要重试，重试通了就正常返回。"""
        import httpx
        responses = [
            _FakeResponse(429, {}, text="too many"),
            _FakeResponse(200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}),
        ]
        calls = []
        payloads = []

        class _Client:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json=None, files=None, headers=None):
                calls.append(url)
                payloads.append(json)
                return responses.pop(0)

        original = vision.httpx.Client
        vision.httpx.Client = _Client
        try:
            out = vision._call_openai(
                {"vision_base_url": "http://x/v1", "vision_api_key": "k", "vision_model": "m"},
                b"img", "a.png",
            )
        finally:
            vision.httpx.Client = original
        self.assertEqual(out, "ok")
        self.assertEqual(len(calls), 2)  # 429 一次 + 重试成功一次
        # 请求体里必须带 max_tokens（默认 8192）
        self.assertEqual(payloads[0]["max_tokens"], 8192)

    def test_vision_timeout_falls_back_to_default(self):
        """视觉超时走设置项，没配时用默认 300，不写死在别处。"""
        self.assertEqual(vision._vision_timeout({}), 300.0)
        self.assertEqual(vision._vision_timeout({"vision_timeout": "600"}), 600.0)
        self.assertEqual(vision._vision_timeout({"vision_timeout": ""}), 300.0)
        self.assertEqual(vision._vision_timeout({"vision_timeout": "abc"}), 300.0)

    def test_read_timeout_gives_chinese_message(self):
        """读超时要转成可自救的中文提示，httpx 的英文原文不能给用户看。"""
        import httpx

        class _Client:
            def __init__(self, *a, **kw):
                raise httpx.ReadTimeout("timed out")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        original = vision.httpx.Client
        vision.httpx.Client = _Client
        try:
            with self.assertRaises(RuntimeError) as ctx:
                vision._call_openai(
                    {
                        "vision_base_url": "http://x/v1",
                        "vision_api_key": "k",
                        "vision_model": "m",
                        "vision_timeout": "300",
                    },
                    b"img", "a.png",
                )
        finally:
            vision.httpx.Client = original
        msg = str(ctx.exception)
        self.assertIn("识别超时", msg)
        self.assertIn("300", msg)
        self.assertIn("换更快的模型", msg)
        self.assertNotIn("timed out", msg)


class TestChartSvg(unittest.TestCase):
    """图表是纯 SVG 生成的，不依赖外网。"""

    def test_bar_chart_svg(self):
        svg = services.bar_chart_svg(
            [{"day": "2026-11-08", "amount": Decimal("99.9")}], "#3b82f6"
        )
        self.assertTrue(svg.strip().startswith("<svg"))
        self.assertIn("</svg>", svg)
        self.assertIn("2026-11-08", svg)

    def test_bar_chart_value_labels(self):
        """柱顶数值标签：与 X 轴日期同频抽样，金额走 |money 口径，不压进柱子。"""
        import re

        daily = [
            {"day": f"2026-11-{d:02d}", "amount": Decimal(f"{d}.50")}
            for d in range(1, 15)
        ]
        svg = services.bar_chart_svg(daily, "#3b82f6")

        pattern = (
            r'<text x="[\d.]+" y="([\d.]+)" class="chart-value"[^>]*>'
            r"([\d,]+\.\d{2})</text>"
        )

        # 14 天 label_step=2：抽样第 0,2,4,6,8,10,12 根 + 最后 1 根 = 8 个标签
        labels = re.findall(pattern, svg)
        self.assertEqual(
            [amount for _, amount in labels],
            ["1.50", "3.50", "5.50", "7.50", "9.50", "11.50", "13.50", "14.50"],
        )

        # 每个标签都落在对应柱顶上方 0–4px，不压进柱子里
        rect_ys = [
            float(y)
            for y in re.findall(r'<rect x="[\d.]+" y="([\d.]+)"', svg)
        ]
        sampled = [0, 2, 4, 6, 8, 10, 12, 13]
        for (label_y, _), bar_index in zip(labels, sampled):
            label_y = float(label_y)
            rect_y = rect_ys[bar_index]
            self.assertTrue(
                0 <= rect_y - label_y <= 4, (label_y, rect_y)
            )

        # hover tooltip（<title>）保留，是兜底
        self.assertIn("<title>2026-11-01 ¥1.50</title>", svg)

        # 7 天窗口 label_step=1：每根柱子都标
        svg7 = services.bar_chart_svg(daily[:7])
        self.assertEqual(len(re.findall(pattern, svg7)), 7)

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

    def test_match_by_node_account_first(self):
        """节点号是第一主键：截图里几乎都有节点号，唯一命中直接落账号。"""
        account = self._make_account()
        self._setup_node(account, node_account="srv001")
        out = services.match_recognized_items(
            self.db,
            [{"broadband": "13400001111", "node_account": "srv001", "amount": 88.5}],
        )
        self.assertEqual(out[0]["status"], "matched")
        self.assertEqual(out[0]["account_id"], account.id)
        self.assertEqual(out[0]["account_reason"], "节点号")
        self.assertEqual(out[0]["node_reason"], "节点号一致")

    def test_match_by_phone_when_no_node(self):
        """台账没登记节点号时，回落到按手机号认。"""
        account = self._make_account()
        self._setup_node(account, node_account="")  # 台账里节点号是空的
        out = services.match_recognized_items(
            self.db,
            [{"broadband": "13400001111", "node_account": "srv999", "amount": 88.5}],
        )
        self.assertEqual(out[0]["status"], "matched")
        self.assertEqual(out[0]["account_id"], account.id)
        self.assertEqual(out[0]["account_reason"], "手机号")

    def test_match_multi_candidate_node(self):
        """同一节点号挂在多个账号上：不许静默选一条，要列候选让人挑。"""
        a1 = self._make_account(phone="13400001111")
        a2 = self._make_account(phone="13400002222")
        self._setup_node(a1, node_account="shared")
        self._setup_node(a2, node_account="shared")
        self.db.commit()
        out = services.match_recognized_items(
            self.db,
            [{"broadband": "", "node_account": "shared", "amount": 10.0}],
        )
        self.assertEqual(out[0]["status"], "multi_candidate")
        self.assertEqual(out[0]["account_id"], None)
        cand = out[0]["candidates"]
        self.assertEqual(len(cand), 2)
        self.assertEqual({c["id"] for c in cand}, {a1.id, a2.id})

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
            "/recognize",
            "/settings",
            "/reminders",
            "/chat",
            "/accounts/new",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_manual_remind_entrance_removed(self):
        """「发起催缴」手动入口已下线：详情页不再有发起催缴表单/路由。"""
        account_id = self._new_account()
        detail = self.client.get(f"/accounts/{account_id}")
        self.assertNotIn("发起催缴", detail.text)
        self.assertNotIn("加入提醒列表", detail.text)
        # 手动催缴路由已删，POST 应 404
        resp = self.client.post(
            f"/accounts/{account_id}/remind",
            data={"message": "测试"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 404)
        # 自动催缴提醒展示还在（路由保留）
        reminders = self.client.get("/reminders")
        self.assertIn("催缴提醒", reminders.text)

    def test_main_pages_render_with_content(self):
        """渲染级回归：主要页面不仅 200，关键内容必须真的渲染出来。

        教训：Jinja 模板语法错（元组解包）会让页面 500，
        只验 status_code 的路由测试抓不到，所以这里断言关键 DOM。
        """
        import datetime as _dt

        resp = self.client.post(
            "/accounts/new",
            data={
                "phone": "13400001111",
                "room": "测试机房",
                "platform": "测试云",
                "node_account": "ND-TEST-01",
                "monthly_fee": "399",
                "balance": "500",
            },
            follow_redirects=False,
        )
        account_id = int(resp.headers["location"].rstrip("/").split("/")[-1])

        db = database._session_factory()
        try:
            today = _dt.date.today()
            for i in range(7):
                db.add(DailyIncome(
                    account_id=account_id,
                    day=(today - _dt.timedelta(days=i)).strftime("%Y-%m-%d"),
                    amount=10.0 + i,
                ))
            db.commit()
        finally:
            db.close()

        # 首页：统计卡 + 主表格都在
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("今日平台收入", home.text)
        self.assertIn("13400001111", home.text)

        # 机房总览：sparkline SVG 真渲染出来（修 Jinja 元组 500 的回归点）
        rooms = self.client.get("/rooms")
        self.assertEqual(rooms.status_code, 200)
        self.assertIn('<svg class="spark"', rooms.text)
        self.assertIn("测试机房", rooms.text)

        # 账号详情：meta 两行网格 + 收入表
        detail = self.client.get(f"/accounts/{account_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("meta-grid", detail.text)
        self.assertIn("收入", detail.text)

        # 其余主要页面：200 + 各自的页面骨架
        for path, marker in (
            ("/reminders", "提醒"),
            ("/recognize", "识图"),
            ("/chat", "AI"),
            ("/export", "导出"),
            ("/settings", "设置"),
        ):
            with self.subTest(path=path):
                page = self.client.get(path)
                self.assertEqual(page.status_code, 200)
                self.assertIn(marker, page.text)

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
                "daily_cost": "12.5",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        account_id = int(resp.headers["location"].rstrip("/").split("/")[-1])

        detail = self.client.get(f"/accounts/{account_id}")
        self.assertEqual(detail.status_code, 200)

        db = database._session_factory()
        try:
            account = db.get(Account, account_id)
            self.assertEqual(account.daily_cost, Decimal("12.50"))
            self.assertEqual(account.category, "normal")
        finally:
            db.close()

    def test_install_address_column_kept_in_db(self):
        """install_address 数据库列保留（防误删 PVE 已有数据），只是界面/导出入口下线。"""
        self._new_account()
        db = database._session_factory()
        try:
            account = db.query(Account).one()
            self.assertIsNotNone(account)
            # 模型字段还在，默认空串（不写入了）
            self.assertEqual(account.install_address, "")
        finally:
            db.close()

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

    def test_legacy_vision_format_migrated_to_openai(self):
        """DB 存旧格式 minimax_vl + 非 MiniMax 端点 → 自动迁移成 openai，并留日志。"""
        import logging

        db = database._session_factory()
        try:
            db.add(AppSetting(key="vision_format", value="minimax_vl"))
            db.add(AppSetting(key="vision_base_url", value="https://developer.amd.com.cn/radeon/api/v1"))
            db.commit()

            with self.assertLogs("kuandai.services", level="INFO") as log_ctx:
                config = services.get_vision_config(db)

            self.assertEqual(config["vision_format"], "openai")
            self.assertIn("视觉格式自动迁移", "\n".join(log_ctx.output))
            # 迁完再读还是 openai，不会反复迁
            self.assertEqual(services.get_vision_config(db)["vision_format"], "openai")
        finally:
            db.close()

    def test_legacy_vision_format_kept_for_minimax_endpoint(self):
        """端点就是 MiniMax 官方的，旧格式继续有效，不迁。"""
        db = database._session_factory()
        try:
            db.add(AppSetting(key="vision_format", value="minimax_vl"))
            db.add(AppSetting(key="vision_base_url", value="https://api.minimaxi.com/v1"))
            db.commit()
            config = services.get_vision_config(db)
            self.assertEqual(config["vision_format"], "minimax_vl")
        finally:
            db.close()

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

    def test_save_vision_timeout(self):
        """视觉超时存库、DB 优先；留空表示不改，设置页要回显。"""
        self.client.post(
            "/settings/vision",
            data={
                "vision_api_key": "sk-t",
                "vision_model": "M-VL",
                "vision_timeout": "600",
            },
            follow_redirects=False,
        )
        db = database._session_factory()
        try:
            config = services.get_vision_config(db)
            self.assertEqual(config["vision_timeout"], "600")
        finally:
            db.close()

        # 留空表示不改，不能把 600 清掉
        self.client.post(
            "/settings/vision",
            data={"vision_model": "M-VL-2"},
            follow_redirects=False,
        )
        db = database._session_factory()
        try:
            config = services.get_vision_config(db)
            self.assertEqual(config["vision_timeout"], "600")
        finally:
            db.close()

        page = self.client.get("/settings")
        self.assertIn('value="600"', page.text)


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

    def test_broadband_account_can_be_changed(self):
        """宽带账号现在可改（此前锁定不可改，用户要求放开）。"""
        resp = self.client.post(
            "/accounts/new",
            data={"phone": "13400001111", "broadband_account": "TEST-BB-0001"},
            follow_redirects=False,
        )
        account_id = int(resp.headers["location"].rstrip("/").split("/")[-1])

        # 改成新的
        self.client.post(
            f"/accounts/{account_id}/edit",
            data={"phone": "13400001111", "broadband_account": "NEW-BB-0002"},
            follow_redirects=False,
        )
        db = database._session_factory()
        try:
            account = db.get(Account, account_id)
            self.assertEqual(account.broadband_account, "NEW-BB-0002")  # 改生效
        finally:
            db.close()

    def test_broadband_account_duplicate_rejected(self):
        """宽带账号不能改成别的账号已占用的值。"""
        self.client.post(
            "/accounts/new",
            data={"phone": "13400001111", "broadband_account": "DUP-BB-0001"},
            follow_redirects=False,
        )
        resp2 = self.client.post(
            "/accounts/new",
            data={"phone": "13400002222", "broadband_account": "DUP-BB-0002"},
            follow_redirects=False,
        )
        account2_id = int(resp2.headers["location"].rstrip("/").split("/")[-1])

        # 把账号2改成账号1已占用的值 → 应被拒
        self.client.post(
            f"/accounts/{account2_id}/edit",
            data={"phone": "13400002222", "broadband_account": "DUP-BB-0001"},
            follow_redirects=False,
        )
        db = database._session_factory()
        try:
            account2 = db.get(Account, account2_id)
            self.assertEqual(account2.broadband_account, "DUP-BB-0002")  # 没被改成重复值
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
        self.assertNotIn("安装地址", lines[0])  # 该列已下线，不再导出
        self.assertTrue(any("13400001111" in line for line in lines[1:]))

    def test_export_csv_files_all_ok(self):
        for path in (
            "/export/accounts.csv",
            "/export/nodes.csv",
            "/export/incomes.csv",
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


class TestVersion(unittest.TestCase):
    """版本号要真的露出来：/healthz 报版本，页面页脚显示版本。"""

    def setUp(self):
        db = database._session_factory()
        try:
            db.query(DailyIncome).delete()
            db.query(Account).delete()
            db.query(User).delete()
            db.commit()
        finally:
            db.close()
        self.client = TestClient(app)
        self.client.post("/register", data={"username": "tester", "password": "test1234"})

    def tearDown(self):
        self.client.close()

    def test_healthz_reports_version(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("version"), APP_VERSION)

    def test_footer_shows_version(self):
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn(f"kuandai-manager v{APP_VERSION}", home.text)

    def test_changelog_documents_current_version(self):
        """更新日志里要有当前版本的条目，升级说明不能漏。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        changelog = os.path.join(root, "CHANGELOG.md")
        self.assertTrue(os.path.exists(changelog), "缺少 CHANGELOG.md")
        with open(changelog, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn(f"v{APP_VERSION}", text)
        self.assertIn("升级步骤", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
