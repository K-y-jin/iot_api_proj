"""인증·세션 (DESIGN.md §6.3).

- 비밀번호: passlib bcrypt 해시
- 세션: Starlette SessionMiddleware (서명 쿠키), 만료 8시간
- 권한: 공개(비로그인) / 로그인 / admin 3단계 (DESIGN.md §6.3 권한 매트릭스)
"""

from __future__ import annotations

from typing import Optional, TypedDict

from fastapi import Depends, HTTPException, Request, status
from passlib.context import CryptContext

from . import config
from .db import get_connection, now_kst_iso


# bcrypt cost 12 (DESIGN.md §14).
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


class CurrentUser(TypedDict):
    id: int
    username: str
    is_admin: bool


# ─────────────────────────── 비밀번호 ───────────────────────────

def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ─────────────────────────── 사용자 CRUD ───────────────────────────

def get_user_by_username(username: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_user(username: str, plain_password: str, is_admin: bool = False) -> int:
    """신규 계정 생성. UNIQUE 위반은 호출자가 IntegrityError 처리."""
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO users(username, password_hash, is_admin, created_at)
                    VALUES (?, ?, ?, ?)""",
            (username, hash_password(plain_password), 1 if is_admin else 0, now_kst_iso()),
        )
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_users() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, username, is_admin, created_at FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_user(user_id: int) -> bool:
    """admin 본인은 삭제 불가 등 정책은 호출자에서. 여기서는 DB 행 제거만."""
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        return cur.rowcount > 0
    finally:
        conn.close()


def seed_admin_if_missing() -> None:
    """앱 첫 기동 시 admin 계정이 없으면 ADMIN_INITIAL_PASSWORD로 자동 생성 (DESIGN.md §6.3)."""
    if get_user_by_username("admin"):
        return
    create_user("admin", config.ADMIN_INITIAL_PASSWORD, is_admin=True)


# ─────────────────────────── 세션 / 의존성 ───────────────────────────

def login_session(request: Request, user: dict) -> None:
    """세션 쿠키에 user 정보 기록 (SessionMiddleware 사용)."""
    request.session["user_id"] = int(user["id"])
    request.session["username"] = user["username"]
    request.session["is_admin"] = bool(user["is_admin"])


def logout_session(request: Request) -> None:
    request.session.clear()


def current_user(request: Request) -> Optional[CurrentUser]:
    """비로그인 시 None을 반환하는 의존성 (공개 라우트에서 사용)."""
    uid = request.session.get("user_id")
    if uid is None:
        return None
    return {
        "id": int(uid),
        "username": str(request.session.get("username", "")),
        "is_admin": bool(request.session.get("is_admin", False)),
    }


def require_login(request: Request) -> CurrentUser:
    """로그인 필수 라우트용 의존성. 비로그인 시 401."""
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="로그인이 필요합니다.")
    return user


def require_admin(user: CurrentUser = Depends(require_login)) -> CurrentUser:
    """admin 전용 라우트용 의존성. 403."""
    if not user["is_admin"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="관리자 권한이 필요합니다.")
    return user
