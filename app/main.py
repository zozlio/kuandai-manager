"""FastAPI 应用入口：台账、提醒、AI 助手。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote as _url_quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import ai, auth, services
from .config import get_settings
from .database import get_db, init_engine
from .models import (
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
templates.env.globals["TX_LABELS"] = TX_LABELS
templates.env.globals["REMIND_LABELS"] = REMIND_LABELS


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
    db: Session = Depends(get_db),
):
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

    accounts = services.search_accounts(db, services.fix_text(keyword.strip()), status)
    rows = [
        {"account": account, "flags": services.account_flags(account)}
        for account in accounts
    ]
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
        base_context(request, db=db, account=None, form={}, stats=services.list_stats(db)),
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
        base_context(request, db=db, account=account, form={}, stats=services.list_stats(db)),
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
    db: Session = Depends(get_db),
):
    account = get_account_or_404(db, account_id)
    account.phone = services.fix_text(phone.strip()) or account.phone
    account.broadband_account = services.fix_text(broadband_account.strip())
    account.registration_code = services.fix_text(registration_code.strip())
    account.registrant = services.fix_text(registrant.strip())
    account.sim_user = services.fix_text(sim_user.strip())
    account.registered_at = _parse_datetime(registered_at)
    account.monthly_fee = services.money(monthly_fee) or services.DEFAULT_MONTHLY_FEE
    account.provider = services.fix_text(provider.strip())
    account.status = status if status in STATUS_LABELS else STATUS_ACTIVE
    account.notes = services.fix_text(notes.strip())

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
    return templates.TemplateResponse(
        request,
        "settings.html",
        base_context(
            request,
            db=db,
            ai_config=config,
            masked_key=services.mask_api_key(config.get("ai_api_key", "")),
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
