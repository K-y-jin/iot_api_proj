"""Aqara access/refresh 토큰 관리 (DESIGN.md §6.2).

- tokens.json 파일에 저장. 메모리 캐시는 호출자 측에서 매번 load() 호출.
- refresh 성공 시 즉시 덮어쓰기 (atomic write 권장이지만 단일 프로세스 가정).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

from . import config


class Tokens(TypedDict, total=False):
    access_token: str
    refresh_token: str
    refreshed_at: str   # KST ISO8601
    expires_at: str     # KST ISO8601 (refreshed_at + expiresIn)


class TokenNotFoundError(RuntimeError):
    """tokens.json이 존재하지 않거나 비어있을 때 (admin이 초기 입력 필요)."""


# ─────────────────────────── 파일 I/O ───────────────────────────

def load(path: Path | None = None) -> Tokens:
    """현재 저장된 토큰 로드. 파일 부재 시 명확한 예외."""
    p = path or config.TOKENS_PATH
    if not p.exists():
        raise TokenNotFoundError(
            f"tokens.json 미존재: {p}. /admin/token 화면에서 refresh_token을 등록하세요."
        )
    data = json.loads(p.read_text(encoding="utf-8"))
    if "access_token" not in data or "refresh_token" not in data:
        raise TokenNotFoundError(f"tokens.json 형식 오류: {p}")
    return data


def save(tokens: Tokens, path: Path | None = None) -> None:
    """토큰 저장 (덮어쓰기). 임시 파일 → rename으로 partial write 방지."""
    p = path or config.TOKENS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(tokens, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def get_access_token() -> str:
    """현재 access token 즉시 반환 (호출자가 매번 fresh load)."""
    return load()["access_token"]


def get_refresh_token() -> str:
    return load()["refresh_token"]


# ───────────────────── refresh API 응답 → 저장 포맷 변환 ─────────────────────

def build_record(api_result: dict) -> Tokens:
    """Aqara refreshToken intent 응답을 tokens.json 저장 형식으로 변환.

    응답 예시: {"accessToken": "...", "refreshToken": "...", "expiresIn": "86400", "openId": "..."}
    """
    now_utc = datetime.now(tz=timezone.utc)
    kst = timezone(timedelta(hours=9))
    refreshed = now_utc.astimezone(kst).strftime("%Y-%m-%d %H:%M:%S")
    try:
        expires_sec = int(api_result.get("expiresIn", "0"))
    except (TypeError, ValueError):
        expires_sec = 0
    expires_dt = now_utc + timedelta(seconds=expires_sec) if expires_sec else now_utc
    return {
        "access_token": api_result["accessToken"],
        "refresh_token": api_result["refreshToken"],
        "refreshed_at": refreshed,
        "expires_at": expires_dt.astimezone(kst).strftime("%Y-%m-%d %H:%M:%S"),
    }


def should_refresh_proactively(now: datetime | None = None) -> bool:
    """선제 갱신(03:00 cron)에서 호출 — expires_at - now < 24h 이면 True."""
    try:
        rec = load()
    except TokenNotFoundError:
        return False
    try:
        kst = timezone(timedelta(hours=9))
        exp = datetime.strptime(rec["expires_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=kst)
    except (KeyError, ValueError):
        return False
    if now is None:
        now = datetime.now(tz=timezone.utc)
    return (exp - now) < timedelta(hours=24)


# ─────────────────────── SmartThings OAuth 토큰 (DESIGN.md §15.2) ───────────────────────
# PAT 방식에서 OAuth 2.0 Authorization Code flow + refresh token 으로 전환.
# access_token 은 단기(약 24h), refresh_token 으로 자동 갱신. tokens_smartthings.json 에 저장.

class SmartThingsTokens(TypedDict, total=False):
    access_token: str
    refresh_token: str
    refreshed_at: str   # KST ISO8601 (토큰 발급/갱신 시각)
    expires_at: str     # KST ISO8601 (refreshed_at + expires_in)


class SmartThingsTokenNotFoundError(RuntimeError):
    """SmartThings OAuth 토큰 미등록. admin 이 /admin/token 화면에서 'SmartThings 연결' 필요."""


def load_smartthings_tokens() -> SmartThingsTokens:
    """저장된 SmartThings OAuth 토큰 로드. 미등록·빈 파일·손상·형식 오류는 모두
    SmartThingsTokenNotFoundError 로 통일 (호출자가 '미연결'로 일관 처리)."""
    p = config.SMARTTHINGS_TOKEN_PATH
    if not p.exists():
        raise SmartThingsTokenNotFoundError(
            f"SmartThings OAuth 토큰 미등록: {p}. /admin/token 에서 'SmartThings 연결'을 진행하세요."
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise SmartThingsTokenNotFoundError(f"SmartThings 토큰 파일 손상: {p}") from e
    if not isinstance(data, dict) or "access_token" not in data or "refresh_token" not in data:
        raise SmartThingsTokenNotFoundError(f"SmartThings 토큰 파일 형식 오류: {p}")
    return data


def save_smartthings_tokens(tokens: SmartThingsTokens) -> None:
    """SmartThings OAuth 토큰 저장 (덮어쓰기). 임시 파일 → rename 으로 partial write 방지."""
    p = config.SMARTTHINGS_TOKEN_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(tokens, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def build_smartthings_record(api_result: dict) -> SmartThingsTokens:
    """SmartThings OAuth token 엔드포인트 응답을 저장 형식으로 변환.

    응답 예시: {"access_token": "...", "refresh_token": "...", "expires_in": 86400,
                "token_type": "bearer", "scope": "r:devices:*"}
    """
    now_utc = datetime.now(tz=timezone.utc)
    kst = timezone(timedelta(hours=9))
    refreshed = now_utc.astimezone(kst).strftime("%Y-%m-%d %H:%M:%S")
    try:
        expires_sec = int(api_result.get("expires_in", 0))
    except (TypeError, ValueError):
        expires_sec = 0
    expires_dt = now_utc + timedelta(seconds=expires_sec) if expires_sec else now_utc
    return {
        "access_token": api_result["access_token"],
        "refresh_token": api_result["refresh_token"],
        "refreshed_at": refreshed,
        "expires_at": expires_dt.astimezone(kst).strftime("%Y-%m-%d %H:%M:%S"),
    }


def smartthings_is_expired(now: datetime | None = None, skew_sec: int = 300) -> bool:
    """현재 access_token 이 만료(또는 skew_sec 이내 임박) 상태인지.

    매 호출 직전 검사용. expires_at 파싱 실패 시 안전하게 만료로 간주(refresh 유도).
    """
    try:
        rec = load_smartthings_tokens()
    except SmartThingsTokenNotFoundError:
        return False  # 미등록은 별도 예외 경로 — 여기서는 만료 아님으로 처리
    try:
        kst = timezone(timedelta(hours=9))
        exp = datetime.strptime(rec["expires_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=kst)
    except (KeyError, ValueError):
        return True
    if now is None:
        now = datetime.now(tz=timezone.utc)
    return (exp - now) < timedelta(seconds=skew_sec)


def smartthings_should_refresh_proactively(now: datetime | None = None) -> bool:
    """선제 갱신(03:00 cron)에서 호출 — expires_at - now < 6h 이면 True.

    access_token 수명이 약 24h 라 Aqara(24h 임계)보다 짧은 6h 임계를 쓴다.
    """
    try:
        rec = load_smartthings_tokens()
    except SmartThingsTokenNotFoundError:
        return False
    try:
        kst = timezone(timedelta(hours=9))
        exp = datetime.strptime(rec["expires_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=kst)
    except (KeyError, ValueError):
        return False
    if now is None:
        now = datetime.now(tz=timezone.utc)
    return (exp - now) < timedelta(hours=6)


# ─────────────────────── SmartThings PAT 파일 (stopgap, admin 입력) ───────────────────────
# 환경변수 SMARTTHINGS_PAT (config.SMARTTHINGS_PAT) 가 최우선. 그 다음 admin 이 /admin/token
# 페이지에서 직접 입력한 이 파일의 PAT. 그 다음 OAuth 토큰. OAuth 가 준비되면 이 파일은
# admin 페이지의 "PAT 삭제" 또는 직접 파일 제거로 비활성.


def load_smartthings_pat_file() -> Optional[str]:
    """admin 이 저장한 SmartThings PAT 파일 읽기. 없거나 손상이면 None."""
    p = config.SMARTTHINGS_PAT_PATH
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    token = (data.get("pat") or "").strip()
    return token or None


def save_smartthings_pat_file(pat: str) -> None:
    """admin 입력 PAT 저장 (덮어쓰기). 빈 문자열은 거부."""
    s = (pat or "").strip()
    if not s:
        raise ValueError("SmartThings PAT 가 비어 있습니다.")
    p = config.SMARTTHINGS_PAT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    kst = timezone(timedelta(hours=9))
    rec = {
        "pat": s,
        "saved_at": datetime.now(tz=kst).strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def clear_smartthings_pat_file() -> bool:
    """저장된 PAT 파일 제거. 존재했으면 True."""
    p = config.SMARTTHINGS_PAT_PATH
    if p.exists():
        try:
            p.unlink()
            return True
        except OSError:
            return False
    return False


def _read_smartthings_pat_file_meta() -> Optional[str]:
    """PAT 파일의 saved_at (없으면 None)."""
    p = config.SMARTTHINGS_PAT_PATH
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("saved_at")
    except (json.JSONDecodeError, OSError):
        return None


def smartthings_token_status() -> dict:
    """관리자 화면 표시용 — 현재 활성 인증 source + 마스킹 정보.

    우선순위: env-PAT > file-PAT > OAuth > none. source 필드로 어느 것이 활성인지 노출.
    OAuth 토큰이 따로 저장돼 있어도 PAT 가 우선되면 PAT 정보만 반환.
    """
    # 1) 환경변수 PAT (최우선)
    pat_env = (config.SMARTTHINGS_PAT or "").strip()
    if pat_env:
        return {
            "present": True,
            "source": "pat-env",
            "access_token_masked": (pat_env[:6] + "…" + pat_env[-4:]) if len(pat_env) > 12 else "…",
            "refreshed_at": None,
            "expires_at": None,
            "expired": False,
            "saved_at": None,
        }
    # 2) 파일 PAT (admin 입력)
    pat_file = load_smartthings_pat_file()
    if pat_file:
        return {
            "present": True,
            "source": "pat-file",
            "access_token_masked": (pat_file[:6] + "…" + pat_file[-4:]) if len(pat_file) > 12 else "…",
            "refreshed_at": None,
            "expires_at": None,
            "expired": False,
            "saved_at": _read_smartthings_pat_file_meta(),
        }
    # 3) OAuth 토큰
    try:
        rec = load_smartthings_tokens()
    except SmartThingsTokenNotFoundError:
        return {"present": False}
    access = rec.get("access_token") or ""
    return {
        "present": True,
        "source": "oauth",
        "access_token_masked": (access[:6] + "…" + access[-4:]) if len(access) > 12 else "…",
        "refreshed_at": rec.get("refreshed_at"),
        "expires_at": rec.get("expires_at"),
        "expired": smartthings_is_expired(),
    }
