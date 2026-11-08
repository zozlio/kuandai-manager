"""FastAPI 应用入口：台账、提醒、AI 助手。"""

from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional
from urllib.parse import quote as _url_quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import ai, auth, exporting, services, vision
from .config import get_settings
from .database import get_db, init_engine
from .models import (
    ABNORMAL_REASONS,
    CATEGORY_ABNORMAL,
    CATEGORY_LABELS,
    CATEGORY_NORMAL,
    INCOME_SOURCE_AI,
    INCOME_SOURCE_LABELS,
    INCOME_SOURCE_MANUAL,
    PROVIDERS,
    REMIND_LABELS,
    STATUS_ACTIVE,
    STATUS_CLOSED,
    STATUS_LABELS,
    STATUS_SUSPENDED,
    TX_ADJUST,
    TX_CHARGE,
    TX_LABELS,
    TX_RECHARGE,
    Account,
    AccountGroup,
    DailyIncome,
    Reminder,
    Transaction,
    User,
)
from .scheduler import start_scheduler

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger("kuandai")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR.parent / "static"
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

templates.env.filters["money"] = lambda value: f"{services.to_float(value):,.2f}"
templates.env.filters["datetime"] = services.fmt_dt
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
templates.env.globals["PROVIDERS"] = PROVIDERS
templates.env.globals["TX_LABELS"] = TX_LABELS
templates.env.globals["REMIND_LABELS"] = REMIND_LABELS
templates.env.globals["ABNORMAL_REASONS"] = ABNORMAL_REASONS
templates.env.globals["CATEGORY_LABELS"] = CATEGORY_LABELS
templates.env.globals["INCOME_SOURCE_LABELS"] = INCOME_SOURCE_LABELS
templates.env.globals["VISION_FORMAT_LABELS"] = vision.VISION_FORMAT_LABELS


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_engine()
    start_scheduler()
    logger.info("kuandai-manager 启动完成")
    yield


app = FastAPI(title="kuandai-manager", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 不用登录就能访问的路径
PUBLIC_PATHS = {"/", "/login", "/register", "/healthz"}


def current_user(request: Request, db: Session) -> Optional[User]:
    """从 cookie 里取当前登录的用户，取不到就是没登录。"""
    user_id = auth.read_session_cookie(request.cookies.get(auth.SESSION_COOKIE, ""), db)
    if not user_id:
        return None
    return db.get(User, user_id)


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """没登录的一律拦下来，页面跳回登录页，接口返回 401。"""
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)

    db = next(get_db())
    try:
        if current_user(request, db) is None:
            if path.startswith("/api/"):
                return JSONResponse({"error": "请先登录"}, status_code=401)
            return RedirectResponse("/", status_code=303)
    finally:
        db.close()

    return await call_next(request)


def base_context(request: Request, db: Session | None = None, **extra) -> dict:
    context = {
        "request": request,
        "ai": ai.ai_status(services.get_ai_config(db) if db is not None else {}),
        "now": datetime.now(),
        "user": current_user(request, db) if db is not None else None,
    }
    context.update(extra)
    return context


def get_account_or_404(db: Session, account_id: int) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return account


# --------------------------------------------------------------------------
# 登录 / 注册
# --------------------------------------------------------------------------

def _set_login_cookie(response: Response, user_id: int, db: Session) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.make_session_cookie(user_id, db),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
        secure=get_settings().session_cookie_secure,
    )


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    keyword: str = "",
    status: str = "",
    tab: str = CATEGORY_NORMAL,
    days: int = 14,
    db: Session = Depends(get_db),
):
    """首页：正常账号 / 异常账号两个标签 + 收入看板。

    看板的数字跟着当前标签走：正常标签只算正常账号的机房和扣费，
    异常标签只算异常账号的。
    """
    # 没登录就显示登录 / 注册页
    if current_user(request, db) is None:
        return templates.TemplateResponse(
            request,
            "auth.html",
            {
                "request": request,
                "first_run": auth.is_first_run(db),
                "user_count": auth.user_count(db),
                "login_error": request.query_params.get("login_error", ""),
                "register_error": request.query_params.get("register_error", ""),
                "now": datetime.now(),
            },
        )

    category = CATEGORY_ABNORMAL if tab == CATEGORY_ABNORMAL else CATEGORY_NORMAL
    days = days if days in (7, 14, 30) else 14

    accounts = services.search_accounts(
        db, services.fix_text(keyword.strip()), status, category
    )
    incomes = services.today_account_incomes(db, accounts)
    rows = []
    for account in accounts:
        rows.append(
            {
                "account": account,
                "flags": services.account_flags(account),
                "income": incomes.get(account.id, Decimal("0.00")),
                "group": account.group,
            }
        )

    dashboard = services.income_dashboard(db, category=category, days=days)
    chart_color = "#ef4444" if category == CATEGORY_ABNORMAL else "#3b82f6"
    return templates.TemplateResponse(
        request,
        "index.html",
        base_context(
            request,
            db=db,
            rows=rows,
            stats=services.list_stats(db),
            keyword=keyword,
            status=status,
            tab=category,
            days=days,
            dashboard=dashboard,
            groups=services.list_groups(db),
            bar_svg=services.bar_chart_svg(dashboard["daily"], chart_color),
            donut_svg=services.donut_svg(dashboard["platforms"], days),
            palette=services.CHART_PALETTE,
        ),
    )


@app.post("/login")
def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    """登录。账户名和密码都不做格式限制，能对上就进。"""
    user = auth.authenticate(db, username, password)
    if user is None:
        return RedirectResponse(
            f"/?login_error={_url_quote('账户名或密码不对')}", status_code=303
        )
    logger.info("用户 %s 登录", user.username)
    response = RedirectResponse("/", status_code=303)
    _set_login_cookie(response, user.id, db)
    return response


@app.post("/register")
def register(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    """注册新账户。第一次注册的这个自动成为管理员。"""
    first = auth.is_first_run(db)
    try:
        user = auth.create_user(db, username, password, is_admin=first)
    except ValueError as exc:
        return RedirectResponse(
            f"/?register_error={_url_quote(str(exc))}", status_code=303
        )
    logger.info("新用户 %s 注册成功（管理员=%s）", user.username, user.is_admin)
    response = RedirectResponse("/", status_code=303)
    _set_login_cookie(response, user.id, db)
    return response


@app.post("/logout")
def logout(request: Request):
    """退出登录。"""
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


# --------------------------------------------------------------------------
# 新增 / 编辑
# --------------------------------------------------------------------------

@app.get("/accounts/new", response_class=HTMLResponse)
def new_account_form(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "account_form.html",
        base_context(
            request,
            db=db,
            account=None,
            form={},
            stats=services.list_stats(db),
            groups=services.list_groups(db),
        ),
    )


@app.post("/accounts/new")
def create_account(
    request: Request,
    phone: str = Form(...),
    broadband_account: str = Form(""),
    registration_code: str = Form(""),
    registrant: str = Form(""),
    sim_user: str = Form(""),
    registered_at: str = Form(""),
    monthly_fee: str = Form("399"),
    balance: str = Form("500"),
    provider: str = Form(""),
    status: str = Form(STATUS_ACTIVE),
    notes: str = Form(""),
    discount_enabled: str = Form(""),
    install_address: str = Form(""),
    daily_cost: str = Form(""),
    group_id: str = Form(""),
    room: str = Form(""),
    platform: str = Form(""),
    node_account: str = Form(""),
    db: Session = Depends(get_db),
):
    phone = services.fix_text(phone.strip())
    if not phone:
        return RedirectResponse("/accounts/new?error=请填写电话号码", status_code=303)

    fee = services.money(monthly_fee) or services.DEFAULT_MONTHLY_FEE
    opening_balance = services.money(balance)

    account = Account(
        phone=phone,
        broadband_account=services.fix_text(broadband_account.strip()),
        registration_code=services.fix_text(registration_code.strip()),
        registrant=services.fix_text(registrant.strip()),
        sim_user=services.fix_text(sim_user.strip()),
        registered_at=_parse_datetime(registered_at),
        monthly_fee=fee,
        balance=opening_balance,
        provider=services.fix_text(provider.strip()),
        status=status if status in STATUS_LABELS else STATUS_ACTIVE,
        notes=services.fix_text(notes.strip()),
        discount_enabled=bool(discount_enabled),
        discount_enabled_at=datetime.now() if discount_enabled else None,
        total_recharged=opening_balance,
        install_address=services.fix_text(install_address.strip()),
        daily_cost=services.money(daily_cost),
        group_id=_parse_group_id(group_id),
        room=services.fix_text(room.strip()),
        platform=services.fix_text(platform.strip()),
        node_account=services.fix_text(node_account.strip()),
    )
    db.add(account)
    db.flush()

    # 开号赠送的金额记一笔流水，累计充值从这里开始算
    if opening_balance > 0:
        db.add(
            Transaction(
                account_id=account.id,
                kind=TX_RECHARGE,
                amount=opening_balance,
                period=services.current_period(),
                note="开号初始金额",
            )
        )

    db.commit()
    services.refresh_account_reminders(db, account)
    db.commit()
    logger.info("新增账号 %s，开号金额 %s", phone, opening_balance)
    return RedirectResponse(f"/accounts/{account.id}", status_code=303)


@app.get("/accounts/{account_id}/edit", response_class=HTMLResponse)
def edit_account_form(
    request: Request, account_id: int, db: Session = Depends(get_db)
):
    account = get_account_or_404(db, account_id)
    return templates.TemplateResponse(
        request,
        "account_form.html",
        base_context(
            request,
            db=db,
            account=account,
            form={},
            stats=services.list_stats(db),
            groups=services.list_groups(db),
        ),
    )


@app.post("/accounts/{account_id}/edit")
def update_account(
    request: Request,
    account_id: int,
    phone: str = Form(...),
    broadband_account: str = Form(""),
    registration_code: str = Form(""),
    registrant: str = Form(""),
    sim_user: str = Form(""),
    registered_at: str = Form(""),
    monthly_fee: str = Form("399"),
    balance: str = Form("500"),
    provider: str = Form(""),
    status: str = Form(STATUS_ACTIVE),
    notes: str = Form(""),
    discount_enabled: str = Form(""),
    install_address: str = Form(""),
    daily_cost: str = Form(""),
    group_id: str = Form(""),
    room: str = Form(""),
    platform: str = Form(""),
    node_account: str = Form(""),
    db: Session = Depends(get_db),
):
    account = get_account_or_404(db, account_id)
    account.phone = services.fix_text(phone.strip()) or account.phone
    # --- 宽带账号是身份标识，建好后锁定：即使有人绕过页面直接 POST 也不改 ---
    incoming_bb = services.fix_text(broadband_account.strip())
    if incoming_bb != account.broadband_account:
        logger.info(
            "忽略账号 %s 的宽带账号改动（原值 %s）", account.phone, account.broadband_account
        )
    account.registration_code = services.fix_text(registration_code.strip())
    account.registrant = services.fix_text(registrant.strip())
    account.sim_user = services.fix_text(sim_user.strip())
    account.registered_at = _parse_datetime(registered_at)
    account.monthly_fee = services.money(monthly_fee) or services.DEFAULT_MONTHLY_FEE
    account.provider = services.fix_text(provider.strip())
    account.status = status if status in STATUS_LABELS else STATUS_ACTIVE
    account.notes = services.fix_text(notes.strip())
    account.install_address = services.fix_text(install_address.strip())
    account.daily_cost = services.money(daily_cost)
    # --- 机房 / 节点平台 / 节点号：这三个是可以随时改的 ---
    account.room = services.fix_text(room.strip())
    account.platform = services.fix_text(platform.strip())
    account.node_account = services.fix_text(node_account.strip())
    new_group_id = _parse_group_id(group_id)
    if new_group_id != account.group_id:
        account.group_id = new_group_id

    # --- 七折开关：只能开启，不能关闭 ---
    if account.discount_enabled:
        account.discount_enabled = True
        if account.discount_enabled_at is None:
            account.discount_enabled_at = datetime.now()
    elif discount_enabled:
        account.discount_enabled = True
        account.discount_enabled_at = datetime.now()
        logger.info("账号 %s 开启七折优惠", account.phone)

    # --- 余额改动记一笔调整流水，保证收支可追溯 ---
    new_balance = services.money(balance)
    old_balance = services.money(account.balance)
    if new_balance != old_balance:
        account.balance = new_balance
        db.add(
            Transaction(
                account_id=account.id,
                kind=TX_ADJUST,
                amount=new_balance,
                period=services.current_period(),
                note=f"手动调整余额 {old_balance} → {new_balance}",
            )
        )

    db.commit()
    services.refresh_account_reminders(db, account)
    db.commit()
    logger.info("更新账号 %s", account.phone)
    return RedirectResponse(f"/accounts/{account.id}", status_code=303)


@app.post("/accounts/{account_id}/delete")
def delete_account(
    request: Request, account_id: int, db: Session = Depends(get_db)
):
    account = get_account_or_404(db, account_id)
    phone = account.phone
    db.delete(account)
    db.commit()
    logger.info("删除账号 %s", phone)
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------
# 账号详情 + 流水
# --------------------------------------------------------------------------

@app.get("/accounts/{account_id}", response_class=HTMLResponse)
def account_detail(
    request: Request, account_id: int, db: Session = Depends(get_db)
):
    account = get_account_or_404(db, account_id)
    transactions = (
        db.query(Transaction)
        .filter(Transaction.account_id == account.id)
        .order_by(Transaction.created_at.desc())
        .all()
    )
    reminders = (
        db.query(Reminder)
        .filter(Reminder.account_id == account.id)
        .order_by(Reminder.created_at.desc())
        .all()
    )
    today = date.today().strftime("%Y-%m-%d")
    incomes = (
        db.query(DailyIncome)
        .filter(DailyIncome.account_id == account.id)
        .order_by(DailyIncome.day.desc())
        .limit(30)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "account_detail.html",
        base_context(
            request,
            db=db,
            account=account,
            flags=services.account_flags(account),
            transactions=transactions,
            reminders=reminders,
            incomes=incomes,
            today=today,
            income_total=services.account_income_on(db, account, today),
            groups=services.list_groups(db),
            stats=services.list_stats(db),
        ),
    )


@app.post("/accounts/{account_id}/ledger")
def add_transaction(
    request: Request,
    account_id: int,
    kind: str = Form(...),
    amount: str = Form(...),
    period: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    account = get_account_or_404(db, account_id)
    value = services.money(amount)
    if value <= 0:
        return RedirectResponse(
            f"/accounts/{account_id}?error=金额必须大于0", status_code=303
        )
    if kind not in (TX_CHARGE, TX_RECHARGE, TX_ADJUST):
        kind = TX_RECHARGE

    services.apply_transaction(
        db,
        account,
        kind,
        value,
        services.fix_text(period.strip()),
        services.fix_text(note.strip()),
    )
    logger.info("账号 %s 记一笔 %s %s", account.phone, kind, value)
    return RedirectResponse(f"/accounts/{account.id}", status_code=303)


# --------------------------------------------------------------------------
# 提醒
# --------------------------------------------------------------------------

@app.get("/reminders", response_class=HTMLResponse)
def reminders_page(
    request: Request,
    scope: str = "pending",
    db: Session = Depends(get_db),
):
    query = db.query(Reminder).join(Account, Reminder.account_id == Account.id)
    if scope == "resolved":
        query = query.filter(Reminder.status == "resolved")
    else:
        query = query.filter(Reminder.status == "pending")

    reminders = query.order_by(Reminder.created_at.desc()).all()
    rows = []
    for reminder in reminders:
        rows.append(
            {
                "reminder": reminder,
                "account": reminder.account,
                "flags": services.account_flags(reminder.account),
            }
        )
    return templates.TemplateResponse(
        request,
        "reminders.html",
        base_context(
            request, db=db, rows=rows, scope=scope, stats=services.list_stats(db)
        ),
    )


@app.post("/reminders/{reminder_id}/resolve")
def resolve_reminder(
    request: Request, reminder_id: int, db: Session = Depends(get_db)
):
    reminder = db.get(Reminder, reminder_id)
    if reminder is None:
        raise HTTPException(status_code=404, detail="提醒不存在")
    reminder.status = "resolved"
    reminder.resolved_at = datetime.now()
    db.commit()
    return RedirectResponse("/reminders", status_code=303)


@app.post("/reminders/{reminder_id}/generate")
async def generate_reminder_text(
    request: Request, reminder_id: int, db: Session = Depends(get_db)
):
    """用 AI 生成催缴话术并写回提醒记录。"""
    reminder = db.get(Reminder, reminder_id)
    if reminder is None:
        raise HTTPException(status_code=404, detail="提醒不存在")
    config = services.get_ai_config(db)
    if not ai.ai_configured(config):
        return JSONResponse({"error": "AI 未配置，请到「设置」页填写接口地址、密钥和模型名"}, status_code=503)

    try:
        text = await run_in_threadpool(ai.generate_reminder_message, reminder.account, config)
    except Exception as exc:  # noqa: BLE001
        logger.exception("生成催缴话术失败")
        return JSONResponse({"error": str(exc)}, status_code=502)

    reminder.message = text
    db.commit()
    return JSONResponse({"message": text})


@app.post("/api/collection-advice")
async def collection_advice(db: Session = Depends(get_db)):
    """批量催缴建议。"""
    config = services.get_ai_config(db)
    if not ai.ai_configured(config):
        return JSONResponse({"error": "AI 未配置，请到「设置」页填写接口地址、密钥和模型名"}, status_code=503)
    accounts = db.query(Account).all()
    try:
        text = await run_in_threadpool(ai.generate_collection_advice, accounts, config)
    except Exception as exc:  # noqa: BLE001
        logger.exception("生成批量建议失败")
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"advice": text})


@app.post("/accounts/{account_id}/remind")
def manual_reminder(
    request: Request,
    account_id: int,
    message: str = Form(""),
    db: Session = Depends(get_db),
):
    account = get_account_or_404(db, account_id)
    services.create_manual_reminder(db, account, services.fix_text(message.strip()))
    return RedirectResponse(f"/accounts/{account.id}", status_code=303)


# --------------------------------------------------------------------------
# AI 对话
# --------------------------------------------------------------------------

@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "chat.html", base_context(request, db=db, stats=services.list_stats(db))
    )


@app.post("/api/chat")
async def chat_api(
    request: Request, question: str = Form(...), db: Session = Depends(get_db)
):
    question = question.strip()
    if not question:
        return JSONResponse({"error": "请输入问题"}, status_code=400)
    if not ai.ai_configured(services.get_ai_config(db)):
        return JSONResponse({"error": "AI 未配置，请到「设置」页填写接口地址、密钥和模型名"}, status_code=503)

    accounts = db.query(Account).all()
    try:
        result = await run_in_threadpool(
            ai.answer_question, question, accounts, services.get_ai_config(db)
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("AI 问答失败")
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse(result)


# --------------------------------------------------------------------------
# 设置页（AI 接入）
# --------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    """AI 接入设置：页面里改完立刻生效，不用重启容器。"""
    config = services.get_ai_config(db)
    vision_config = services.get_vision_config(db)
    return templates.TemplateResponse(
        request,
        "settings.html",
        base_context(
            request,
            db=db,
            ai_config=config,
            masked_key=services.mask_api_key(config.get("ai_api_key", "")),
            vision_config=vision_config,
            vision_masked_key=services.mask_api_key(vision_config.get("vision_api_key", "")),
            stats=services.list_stats(db),
        ),
    )


@app.post("/settings/ai")
def save_ai_settings(
    request: Request,
    ai_base_url: str = Form(""),
    ai_api_key: str = Form(""),
    ai_model: str = Form(""),
    db: Session = Depends(get_db),
):
    """保存 AI 配置。密钥留空表示保留原来那一项，不动它。"""
    base_url = services.fix_text(ai_base_url.strip())
    model = services.fix_text(ai_model.strip())
    api_key = (ai_api_key or "").strip()

    if not api_key:
        # 页面不回显密钥，留空就沿用原来存的那份
        api_key = services.get_stored_setting(db, "ai_api_key")

    services.save_ai_config(db, base_url, api_key, model)
    logger.info("更新 AI 配置：base_url=%s model=%s", base_url or "(空)", model or "(空)")
    return RedirectResponse("/settings?ok=AI 配置已保存，立即生效", status_code=303)


@app.post("/settings/ai/test")
async def test_ai_settings(
    request: Request,
    ai_base_url: str = Form(""),
    ai_api_key: str = Form(""),
    ai_model: str = Form(""),
    db: Session = Depends(get_db),
):
    """测试连接：按填的内容真发一次请求，不改动已保存的配置。"""
    base_url = services.fix_text(ai_base_url.strip())
    model = services.fix_text(ai_model.strip())
    api_key = (ai_api_key or "").strip()
    if not api_key:
        api_key = services.get_ai_config(db).get("ai_api_key", "")

    config = {"ai_base_url": base_url, "ai_api_key": api_key, "ai_model": model}
    if not ai.ai_configured(config):
        return RedirectResponse(
            "/settings?error=接口地址、密钥、模型名三项都要填才能测试", status_code=303
        )

    try:
        reply = await run_in_threadpool(ai.test_connection, config)
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI 连接测试失败：%s", exc)
        return RedirectResponse(
            f"/settings?error={_url_quote('连接失败：' + str(exc)[:200])}", status_code=303
        )

    return RedirectResponse(
        f"/settings?ok={_url_quote('连接正常，接口回复：' + reply[:60])}", status_code=303
    )


@app.post("/settings/ai/reset")
def reset_ai_settings(request: Request, db: Session = Depends(get_db)):
    """清掉页面里的覆盖，AI 配置回落到环境变量。"""
    services.clear_ai_config(db)
    logger.info("清除页面 AI 配置，回落到环境变量")
    return RedirectResponse("/settings?ok=已恢复使用容器的环境变量配置", status_code=303)


# --------------------------------------------------------------------------
# 分类：正常账号 / 异常账号
# --------------------------------------------------------------------------

@app.post("/accounts/{account_id}/abnormal")
def set_abnormal(
    request: Request,
    account_id: int,
    reason: str = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    """把账号移进异常分类，并记下原因。"""
    account = get_account_or_404(db, account_id)
    if reason not in ABNORMAL_REASONS:
        return JSONResponse({"error": "异常原因不合法"}, status_code=400)
    services.mark_abnormal(db, account, reason, note)
    return JSONResponse(
        {
            "ok": True,
            "category": account.category,
            "reason": account.abnormal_reason,
            "reason_label": ABNORMAL_REASONS[account.abnormal_reason],
        }
    )


@app.post("/accounts/{account_id}/restore")
def restore_normal(
    request: Request,
    account_id: int,
    db: Session = Depends(get_db),
):
    """异常账号恢复正常。"""
    account = get_account_or_404(db, account_id)
    services.restore_account(db, account)
    return JSONResponse({"ok": True, "category": account.category})


# --------------------------------------------------------------------------
# 机房总览：按机房分组 → 平台 → 宽带账号 + 节点号
# --------------------------------------------------------------------------

@app.get("/rooms", response_class=HTMLResponse)
@app.get("/nodes", response_class=HTMLResponse)  # 老地址保留
def rooms_page(request: Request, day: str = "", db: Session = Depends(get_db)):
    """机房总览：按机房分组，组内按平台分组，平台下面是宽带账号和节点号。"""
    day = day or date.today().strftime("%Y-%m-%d")

    accounts = db.query(Account).order_by(Account.room, Account.platform, Account.phone).all()
    incomes = {
        row.account_id: row
        for row in db.query(DailyIncome)
        .filter(DailyIncome.day == day)
        .all()
    }

    # 先按机房分组，再按平台分组
    rooms: dict[str, dict[str, list[dict]]] = {}
    for account in accounts:
        room_name = services.fix_text(account.room) or "未分机房"
        platform_name = services.fix_text(account.platform) or "未填平台"
        row = {
            "account": account,
            "income": services.money(incomes[account.id].amount) if account.id in incomes else Decimal("0.00"),
            "incomes": (
                db.query(DailyIncome)
                .filter(DailyIncome.account_id == account.id)
                .order_by(DailyIncome.day.desc())
                .limit(7)
                .all()
            ),
        }
        rooms.setdefault(room_name, {}).setdefault(platform_name, []).append(row)

    total_income = sum((r["income"] for r in _iter_room_rows(rooms)), Decimal("0.00"))
    today_cost = sum(
        (services.daily_amount(a, date.today()) for a in accounts),
        Decimal("0.00"),
    )
    return templates.TemplateResponse(
        request,
        "rooms.html",
        base_context(
            request,
            db=db,
            rooms=rooms,
            day=day,
            today_income=total_income,
            today_cost=today_cost,
            today_profit=services.money(total_income - today_cost),
            accounts=accounts,
            stats=services.list_stats(db),
        ),
    )


def _iter_room_rows(rooms: dict):
    """把「机房 → 平台 → 账号」摊平成一串，方便统计。"""
    for platforms in rooms.values():
        for rows in platforms.values():
            for row in rows:
                yield row


@app.post("/api/accounts/{account_id}/income")
async def record_income(
    request: Request,
    account_id: int,
    day: str = Form(...),
    amount: str = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    """记一笔账号某天的收入。同一天重复记会覆盖。"""
    account = get_account_or_404(db, account_id)
    value = services.money(amount)
    day = (day or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        return JSONResponse({"error": "日期格式应为 YYYY-MM-DD"}, status_code=400)

    services.upsert_income(
        db, account.id, day, value, INCOME_SOURCE_MANUAL, services.fix_text(note.strip())
    )
    return JSONResponse(
        {
            "ok": True,
            "day": day,
            "amount": services.to_float(value),
            "account_income": services.to_float(
                services.account_income_on(db, account, day)
            ),
        }
    )


@app.post("/api/incomes/{income_id}/delete")
def remove_income(request: Request, income_id: int, db: Session = Depends(get_db)):
    income = db.get(DailyIncome, income_id)
    if income is None:
        return JSONResponse({"error": "记录不存在"}, status_code=404)
    services.delete_income(db, income)
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------
# 分组
# --------------------------------------------------------------------------

@app.get("/groups", response_class=HTMLResponse)
def groups_page(request: Request, db: Session = Depends(get_db)):
    groups = services.list_groups(db)
    rows = []
    for group in groups:
        rows.append(
            {
                "group": group,
                "count": services.group_account_count(db, group.id),
                "accounts": sorted(group.accounts, key=lambda a: a.phone),
            }
        )
    return templates.TemplateResponse(
        request,
        "groups.html",
        base_context(
            request,
            db=db,
            rows=rows,
            accounts=db.query(Account).order_by(Account.phone).all(),
            stats=services.list_stats(db),
        ),
    )


@app.post("/groups")
def create_group(
    request: Request,
    name: str = Form(...),
    install_location: str = Form(""),
    manage_phone: str = Form(""),
    manage_account: str = Form(""),
    manage_expire_at: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    """新建分组。"""
    group = AccountGroup(
        name=services.fix_text(name.strip()),
        install_location=services.fix_text(install_location.strip()),
        manage_phone=services.fix_text(manage_phone.strip()),
        manage_account=services.fix_text(manage_account.strip()),
        manage_expire_at=_parse_datetime(manage_expire_at),
        note=services.fix_text(note.strip()),
    )
    db.add(group)
    db.commit()
    logger.info("新建分组 %s", group.name)
    return RedirectResponse("/groups", status_code=303)


@app.post("/groups/{group_id}/edit")
def update_group(
    request: Request,
    group_id: int,
    name: str = Form(...),
    install_location: str = Form(""),
    manage_phone: str = Form(""),
    manage_account: str = Form(""),
    manage_expire_at: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    group = db.get(AccountGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="分组不存在")
    group.name = services.fix_text(name.strip())
    group.install_location = services.fix_text(install_location.strip())
    group.manage_phone = services.fix_text(manage_phone.strip())
    group.manage_account = services.fix_text(manage_account.strip())
    group.manage_expire_at = _parse_datetime(manage_expire_at)
    group.note = services.fix_text(note.strip())
    db.commit()
    return RedirectResponse("/groups", status_code=303)


@app.post("/groups/{group_id}/delete")
def remove_group(request: Request, group_id: int, db: Session = Depends(get_db)):
    group = db.get(AccountGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="分组不存在")
    services.delete_group(db, group)
    return RedirectResponse("/groups", status_code=303)


@app.post("/accounts/{account_id}/group")
def assign_group(
    request: Request,
    account_id: int,
    group_id: str = Form(""),
    db: Session = Depends(get_db),
):
    """把账号归到某个分组（留空 = 移出分组）。"""
    account = get_account_or_404(db, account_id)
    group_id = (group_id or "").strip()
    if not group_id or group_id == "0":
        account.group_id = None
    else:
        group = db.get(AccountGroup, int(group_id))
        if group is None:
            raise HTTPException(status_code=404, detail="分组不存在")
        account.group_id = group.id
    db.commit()
    return RedirectResponse(f"/accounts/{account.id}", status_code=303)


# --------------------------------------------------------------------------
# AI 识图：从截图里读机房收益
# --------------------------------------------------------------------------

@app.get("/recognize", response_class=HTMLResponse)
def recognize_page(request: Request, db: Session = Depends(get_db)):
    """AI 识图页面：上传截图 → 识别 → 人工确认 → 回填。"""
    config = services.get_vision_config(db)
    return templates.TemplateResponse(
        request,
        "recognize.html",
        base_context(
            request,
            db=db,
            vision_config=config,
            vision_status=vision.vision_status(config),
            accounts=db.query(Account).order_by(Account.phone).all(),
            stats=services.list_stats(db),
        ),
    )


@app.post("/api/recognize")
async def api_recognize(
    request: Request,
    image: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """上传截图，调用视觉模型识别出每个机房的收益。"""
    config = services.get_vision_config(db)
    if not vision.vision_configured(config):
        return JSONResponse(
            {"error": "视觉模型未配置，请到「设置」页填写视觉模型和密钥"}, status_code=503
        )

    content = await image.read()
    if not content:
        return JSONResponse({"error": "图片是空的"}, status_code=400)
    if len(content) > 12 * 1024 * 1024:
        return JSONResponse({"error": "图片太大了（上限 12MB）"}, status_code=400)

    try:
        items = await run_in_threadpool(
            vision.recognize_income, content, image.filename or "shot.png", config
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("AI 识图失败")
        return JSONResponse({"error": str(exc)}, status_code=502)

    matched = services.match_recognized_items(db, items)
    logger.info("AI 识图识别到 %d 行", len(matched))
    return JSONResponse({"items": matched, "count": len(matched)})


@app.post("/api/recognize/apply")
async def api_recognize_apply(
    request: Request,
    payload: str = Form(...),
    db: Session = Depends(get_db),
):
    """把人工确认过的识别结果写进每日收入。"""
    try:
        rows = json.loads(payload or "[]")
    except json.JSONDecodeError:
        return JSONResponse({"error": "提交的数据格式不对"}, status_code=400)
    if not isinstance(rows, list):
        return JSONResponse({"error": "提交的数据格式不对"}, status_code=400)

    saved = 0
    updated_nodes = 0
    for row in rows:
        if not row.get("use"):
            continue
        account_id = row.get("account_id")
        if not account_id:
            continue
        account = db.get(Account, int(account_id))
        if account is None:
            continue

        # 人工确认过了：顺手把节点号 / 平台更新到账号上（这两个字段本来就是可以改的）
        new_node = services.fix_text(str(row.get("node_account") or "").strip())
        if new_node and new_node != account.node_account:
            account.node_account = new_node
            updated_nodes += 1
        new_platform = services.fix_text(str(row.get("platform") or "").strip())
        if new_platform and new_platform != account.platform:
            account.platform = new_platform

        day = str(row.get("day") or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
            day = date.today().strftime("%Y-%m-%d")
        services.upsert_income(
            db,
            int(account_id),
            day,
            services.money(row.get("amount")),
            INCOME_SOURCE_AI,
            "AI 识图",
        )
        saved += 1
    db.commit()

    logger.info("AI 识图回填 %d 条收入，更新节点号 %d 个", saved, updated_nodes)
    return JSONResponse({"ok": True, "saved": saved, "updated_nodes": updated_nodes})


@app.post("/settings/vision")
def save_vision_settings(
    request: Request,
    vision_base_url: str = Form(""),
    vision_api_key: str = Form(""),
    vision_model: str = Form(""),
    vision_format: str = Form(""),
    db: Session = Depends(get_db),
):
    """保存视觉模型配置。密钥留空表示保留原来那一项。"""
    fmt = (vision_format or "").strip()
    if fmt and fmt not in vision.VISION_FORMAT_LABELS:
        fmt = ""
    services.save_vision_config(
        db,
        services.fix_text(vision_base_url.strip()),
        (vision_api_key or "").strip(),
        services.fix_text(vision_model.strip()),
        fmt,
    )
    logger.info("更新视觉模型配置：model=%s format=%s", vision_model or "(空)", fmt or "(空)")
    return RedirectResponse("/settings?ok=视觉模型配置已保存", status_code=303)


@app.post("/settings/vision/test")
async def test_vision_settings(
    request: Request,
    vision_base_url: str = Form(""),
    vision_api_key: str = Form(""),
    vision_model: str = Form(""),
    vision_format: str = Form(""),
    db: Session = Depends(get_db),
):
    """测试视觉接口：拿一张 1x1 的小图真发一次请求。"""
    config = services.get_vision_config(db)
    base_url = services.fix_text(vision_base_url.strip()) or config.get("vision_base_url", "")
    api_key = (vision_api_key or "").strip() or config.get("vision_api_key", "")
    model = services.fix_text(vision_model.strip()) or config.get("vision_model", "")
    fmt = (vision_format or "").strip() or config.get("vision_format", "")

    test_config = {
        "vision_base_url": base_url,
        "vision_api_key": api_key,
        "vision_model": model,
        "vision_format": fmt,
        "ai_base_url": config.get("ai_base_url", ""),
        "ai_api_key": config.get("ai_api_key", ""),
    }
    if not vision.vision_configured(test_config):
        return RedirectResponse(
            "/settings?error=接口地址、模型名、密钥都要填才能测试", status_code=303
        )

    # 一张 1x1 的透明 PNG，只用来验证通道通不通
    tiny_png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6300010000050001"
        "0d0a2db40000000049454e44ae426082"
    )
    try:
        await run_in_threadpool(
            vision.recognize_income, tiny_png, "test.png", test_config
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("视觉接口测试失败：%s", exc)
        return RedirectResponse(
            f"/settings?error={_url_quote('视觉接口测试失败：' + str(exc)[:200])}",
            status_code=303,
        )

    return RedirectResponse(
        f"/settings?ok={_url_quote('视觉接口通道正常，可以去「识图」页上传截图试')}",
        status_code=303,
    )


# --------------------------------------------------------------------------
# 数据导出（备份）
# --------------------------------------------------------------------------
def _download(content: str, filename: str, media_type: str) -> Response:
    """做成浏览器直接下载的文件。

    文件名带中文，所以按 RFC 5987 的写法同时给一个纯英文名和中文名，
    各种浏览器 / 系统下都不会乱码或下载成乱码名字。
    """
    ascii_name = re.sub(r"[^A-Za-z0-9_.-]", "_", filename) or "export"
    quoted = _url_quote(filename)
    return Response(
        content=content.encode("utf-8"),
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"
            )
        },
    )


@app.get("/export", response_class=HTMLResponse)
def export_page(request: Request, db: Session = Depends(get_db)):
    """导出中心：列出所有可以下载的文件。"""
    stamp = date.today().strftime("%Y%m%d")
    files = [
        {
            "name": "台账导出",
            "desc": "所有宽带账号：余额、累计充值、每日扣费、分组、安装地址、异常原因",
            "url": f"/export/accounts.csv?d={stamp}",
            "size": db.query(Account).count(),
            "unit": "条账号",
        },
        {
            "name": "每日收入导出",
            "desc": "机房按天的收益金额，含来源（手工录入 / AI 识图）",
            "url": f"/export/incomes.csv?d={stamp}",
            "size": db.query(DailyIncome).count(),
            "unit": "条收入",
        },
        {
            "name": "分组导出",
            "desc": "分组名称、安装地点、管理线信息（手机号 / 宽带账号 / 到期时间）",
            "url": f"/export/groups.csv?d={stamp}",
            "size": db.query(AccountGroup).count(),
            "unit": "个分组",
        },
        {
            "name": "收支流水导出",
            "desc": "所有充值、扣费、调整记录",
            "url": f"/export/transactions.csv?d={stamp}",
            "size": db.query(Transaction).count(),
            "unit": "条流水",
        },
        {
            "name": "全量备份（JSON）",
            "desc": "整个数据库的完整快照，可用恢复脚本一键还原。含密码哈希，请当敏感文件保管",
            "url": f"/export/backup.json?d={stamp}",
            "size": None,
            "unit": "",
            "danger": True,
        },
    ]
    return templates.TemplateResponse(
        request,
        "export.html",
        base_context(request, db=db, files=files, stats=services.list_stats(db)),
    )


@app.get("/export/accounts.csv")
def export_accounts_csv(db: Session = Depends(get_db)):
    stamp = date.today().strftime("%Y%m%d")
    return _download(
        exporting.accounts_csv(db), f"台账导出_{stamp}.csv", "text/csv; charset=utf-8"
    )


# 老路径保留一份，别的地方存过书签的话还能用
@app.get("/export/rooms.csv")
@app.get("/export/nodes.csv")
def export_rooms_csv(db: Session = Depends(get_db)):
    """机房信息现在是账号的字段，跟着台账一起导出。"""
    stamp = date.today().strftime("%Y%m%d")
    return _download(
        exporting.accounts_csv(db), f"台账导出_{stamp}.csv", "text/csv; charset=utf-8"
    )


@app.get("/export/incomes.csv")
def export_incomes_csv(db: Session = Depends(get_db)):
    stamp = date.today().strftime("%Y%m%d")
    return _download(
        exporting.incomes_csv(db), f"每日收入导出_{stamp}.csv", "text/csv; charset=utf-8"
    )


@app.get("/export/groups.csv")
def export_groups_csv(db: Session = Depends(get_db)):
    stamp = date.today().strftime("%Y%m%d")
    return _download(
        exporting.groups_csv(db), f"分组导出_{stamp}.csv", "text/csv; charset=utf-8"
    )


@app.get("/export/transactions.csv")
def export_transactions_csv(db: Session = Depends(get_db)):
    stamp = date.today().strftime("%Y%m%d")
    return _download(
        exporting.transactions_csv(db), f"收支流水导出_{stamp}.csv", "text/csv; charset=utf-8"
    )


@app.get("/export/backup.json")
def export_backup_json(db: Session = Depends(get_db)):
    stamp = date.today().strftime("%Y%m%d_%H%M")
    return _download(
        exporting.backup_json(db), f"全量备份_{stamp}.json", "application/json; charset=utf-8"
    )


# --------------------------------------------------------------------------
# 运维
# --------------------------------------------------------------------------
@app.post("/admin/run-billing")
def run_billing_now(request: Request, db: Session = Depends(get_db)):
    """手动触发一次日扣费与提醒巡检。"""
    stats = services.run_daily_billing(db)
    logger.info("手动执行巡检：%s", stats)
    return RedirectResponse("/?ran=1", status_code=303)


@app.post("/api/accounts/{account_id}/recharge")
async def api_recharge(
    request: Request,
    account_id: int,
    amount: str = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    """充值接口：返回最新余额和累计充值金额。"""
    account = get_account_or_404(db, account_id)
    value = services.money(amount)
    if value <= 0:
        return JSONResponse({"error": "充值金额必须大于 0"}, status_code=400)

    services.apply_transaction(
        db,
        account,
        TX_RECHARGE,
        value,
        services.current_period(),
        services.fix_text(note.strip()) or "页面充值",
    )
    logger.info("账号 %s 充值 %s", account.phone, value)
    return JSONResponse(
        {
            "ok": True,
            "amount": services.to_float(value),
            "balance": services.to_float(account.balance),
            "total_recharged": services.to_float(account.total_recharged),
        }
    )


@app.get("/api/accounts")
def api_accounts(db: Session = Depends(get_db)):
    accounts = db.query(Account).order_by(Account.phone).all()
    return JSONResponse(
        [
            {
                "phone": a.phone,
                "broadband_account": a.broadband_account,
                "registration_code": a.registration_code,
                "registrant": a.registrant,
                "sim_user": a.sim_user,
                "monthly_fee": services.to_float(a.monthly_fee),
                "balance": services.to_float(a.balance),
                "status": a.status,
                "flags": services.account_flags(a)["badge"],
            }
            for a in accounts
        ]
    )


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    return {"status": "ok", "ai": ai.ai_status(services.get_ai_config(db))["configured"]}


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def _parse_group_id(value: str) -> Optional[int]:
    """表单里的分组下拉值，空或 0 表示不分组。"""
    text = (value or "").strip()
    if not text or text == "0":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _parse_datetime(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None
