"""登录账户：建号、验密码、会话。

密码用 PBKDF2-HMAC-SHA256 加随机盐保存，数据库里看不到明文。
账户名和密码都不做格式限制，只要非空就行。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime
from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from .models import AppSetting, User

# PBKDF2 迭代次数，越大越安全也越慢
ITERATIONS = 240_000
SALT_BYTES = 16

# 登录状态 cookie
SESSION_COOKIE = "kd_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 天

_signer: Optional[URLSafeTimedSerializer] = None


# --------------------------------------------------------------------------
# 密码
# --------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """生成 `pbkdf2_sha256$迭代次数$盐$哈希` 格式的密码串。"""
    salt = os.urandom(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"), salt, ITERATIONS
    )
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """核对密码是否正确。"""
    if not stored:
        return False
    try:
        algo, iterations, salt_hex, digest_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256",
            (password or "").encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected.hex(), digest_hex)


# --------------------------------------------------------------------------
# 账户
# --------------------------------------------------------------------------

def user_count(db: Session) -> int:
    return db.query(User).count()


def is_first_run(db: Session) -> bool:
    """一个账户都还没有 = 首次使用，要先创建管理员。"""
    return user_count(db) == 0


def find_user(db: Session, username: str) -> Optional[User]:
    """按账户名找用户。忽略大小写和首尾空格，中文名也能用。"""
    target = (username or "").strip().casefold()
    if not target:
        return None
    for user in db.query(User).all():
        if (user.username or "").strip().casefold() == target:
            return user
    return None


def create_user(db: Session, username: str, password: str, is_admin: bool = False) -> User:
    """新建账户。账户名重复会抛 ValueError。"""
    name = (username or "").strip()
    if not name:
        raise ValueError("账户名不能为空")
    if not (password or ""):
        raise ValueError("密码不能为空")
    if find_user(db, name) is not None:
        raise ValueError("这个账户名已经被用了")

    user = User(
        username=name,
        password_hash=hash_password(password),
        is_admin=is_admin,
        created_at=datetime.now(),
    )
    db.add(user)
    db.commit()
    return user


def authenticate(db: Session, username: str, password: str) -> Optional[User]:
    """校验账户名密码，成功返回用户，失败返回 None。"""
    user = find_user(db, username)
    if user is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login_at = datetime.now()
    db.commit()
    return user


# --------------------------------------------------------------------------
# 会话签名密钥
# --------------------------------------------------------------------------

def get_session_secret(db: Session) -> str:
    """会话 cookie 的签名密钥。

    第一次启动时随机生成一个并存进数据库，这样容器重启后用户不会被强制登出，
    也不用手工配置环境变量。
    """
    row = db.get(AppSetting, "session_secret")
    if row is not None and row.value:
        return row.value
    secret = secrets.token_hex(32)
    if row is None:
        db.add(AppSetting(key="session_secret", value=secret))
    else:
        row.value = secret
    db.commit()
    return secret


def _get_signer(db: Session) -> URLSafeTimedSerializer:
    global _signer
    if _signer is None:
        _signer = URLSafeTimedSerializer(get_session_secret(db))
    return _signer


def make_session_cookie(user_id: int, db: Session) -> str:
    """给用户签一个带有效期的登录 cookie。"""
    return _get_signer(db).dumps({"uid": user_id})


def read_session_cookie(value: str, db: Session) -> Optional[int]:
    """校验 cookie，返回用户 id；没登录 / 过期 / 被改过都返回 None。"""
    if not value:
        return None
    try:
        data = _get_signer(db).loads(value, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    except Exception:  # noqa: BLE001  兜底，任何解析异常都当没登录
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    try:
        return int(uid)
    except (TypeError, ValueError):
        return None
