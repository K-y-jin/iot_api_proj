"""환경 설정 로더 (DESIGN.md §14).

- Aqara API 인증값(APPID/KEYID/APPKEY)과 경로·시크릿을 환경변수 → .env 순으로 로드.
- 코드에 비밀값을 하드코딩하지 않으며, 누락 시 명확한 에러를 던진다.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# .env 파일 로드 (없어도 무방, 환경변수가 이미 설정되어 있다면 그대로 사용).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env(key: str, default: str | None = None, required: bool = False) -> str:
    """환경변수 조회. required=True 인 경우 누락 시 RuntimeError."""
    value = os.getenv(key, default)
    if required and (value is None or value == ""):
        raise RuntimeError(f"필수 환경변수 누락: {key} (.env.example 참고)")
    return value or ""


# ─────────────────────────── Aqara API ───────────────────────────
AQARA_API_URL: str = _env("AQARA_API_URL", "https://open-kr.aqara.com/v3.0/open/api")
AQARA_APPID: str = _env("AQARA_APPID")
AQARA_KEYID: str = _env("AQARA_KEYID")
AQARA_APPKEY: str = _env("AQARA_APPKEY")

# ─────────────────────── 관리자/세션 ───────────────────────
ADMIN_INITIAL_PASSWORD: str = _env("ADMIN_INITIAL_PASSWORD", "changeme")
SESSION_SECRET: str = _env("SESSION_SECRET", "dev-only-secret-change-in-prod")
SESSION_MAX_AGE_SECONDS: int = 8 * 60 * 60  # 8시간 (DESIGN.md §6.3)

# ─────────────────────── 파일/디렉토리 경로 ───────────────────────
DATA_DIR: Path = Path(_env("DATA_DIR", str(PROJECT_ROOT / "data")))
DB_PATH: Path = Path(_env("DB_PATH", str(PROJECT_ROOT / "app.db")))
TOKENS_PATH: Path = Path(_env("TOKENS_PATH", str(PROJECT_ROOT / "tokens.json")))
# SmartThings OAuth 토큰(access/refresh) 저장 파일 (DESIGN.md §15.2).
# Aqara tokens 와 분리해 schema 충돌 회피.
SMARTTHINGS_TOKEN_PATH: Path = Path(_env(
    "SMARTTHINGS_TOKEN_PATH", str(PROJECT_ROOT / "tokens_smartthings.json")
))
# admin 페이지에서 직접 입력한 SmartThings PAT 저장 파일 (선택 stopgap).
# 환경변수 SMARTTHINGS_PAT 가 최우선이고, 그 다음 이 파일, 그 다음 OAuth 순.
SMARTTHINGS_PAT_PATH: Path = Path(_env(
    "SMARTTHINGS_PAT_PATH", str(PROJECT_ROOT / "tokens_smartthings_pat.json")
))

# ─────────────────────── SmartThings API ───────────────────────
SMARTTHINGS_API_URL: str = _env("SMARTTHINGS_API_URL", "https://api.smartthings.com/v1")

# SmartThings OAuth 2.0 Authorization Code flow (DESIGN.md §15.2).
# client_id/secret 은 SmartThings Developer Workspace 에서 OAuth-In 클라이언트 생성 후 발급.
# redirect_uri 는 그 클라이언트에 등록한 값과 정확히 일치해야 한다 (앱의 콜백 라우트).
SMARTTHINGS_CLIENT_ID: str = _env("SMARTTHINGS_CLIENT_ID", "")
SMARTTHINGS_CLIENT_SECRET: str = _env("SMARTTHINGS_CLIENT_SECRET", "")
SMARTTHINGS_OAUTH_REDIRECT_URI: str = _env(
    "SMARTTHINGS_OAUTH_REDIRECT_URI",
    "http://localhost:8000/admin/smartthings/oauth/callback",
)
# 공백 구분 scope. 디바이스·장소 조회 + history CLI 호출에 필요한 권한.
SMARTTHINGS_OAUTH_SCOPE: str = _env(
    "SMARTTHINGS_OAUTH_SCOPE", "r:devices:* r:locations:* x:devices:*"
)
SMARTTHINGS_OAUTH_AUTHORIZE_URL: str = _env(
    "SMARTTHINGS_OAUTH_AUTHORIZE_URL", "https://api.smartthings.com/oauth/authorize"
)
SMARTTHINGS_OAUTH_TOKEN_URL: str = _env(
    "SMARTTHINGS_OAUTH_TOKEN_URL", "https://auth-global.api.smartthings.com/oauth/token"
)

# [임시 stopgap] OAuth 클라이언트 준비 전 PAT(Personal Access Token) 로 즉시 운영하고 싶을 때 사용.
# 이 값이 설정돼 있으면 OAuth 흐름을 *완전히 우회* 하고 PAT 를 Bearer 토큰으로 그대로 사용한다.
# refresh 흐름 없음 — 만료/거부 시 admin 이 새 PAT 발급해 .env 를 갱신해야 함.
# OAuth 가 정상화되면 이 변수를 비워 자연스럽게 OAuth 경로로 복귀.
SMARTTHINGS_PAT: str = _env("SMARTTHINGS_PAT", "")

# CLI 실행 파일 절대 경로 (선택). 비어 있으면 PATH 의 'smartthings' 자동 탐색.
# 예) Windows 기본 설치 경로: C:\Program Files\SmartThings\smartthings.exe
SMARTTHINGS_CLI_PATH: str = _env("SMARTTHINGS_CLI_PATH", "")
# CLI history 호출 옵션 (samsung/export_history_throttled.py 패턴 흡수)
SMARTTHINGS_CLI_PAGE_SIZE: int = int(_env("SMARTTHINGS_CLI_PAGE_SIZE", "1000"))
SMARTTHINGS_CLI_MAX_RPM: int = int(_env("SMARTTHINGS_CLI_MAX_RPM", "200"))
SMARTTHINGS_CLI_MAX_PAGES: int = int(_env("SMARTTHINGS_CLI_MAX_PAGES", "50"))  # 한 디바이스 1일 안전 가드
SMARTTHINGS_CLI_INTER_DEVICE_SLEEP_SEC: float = float(_env(
    "SMARTTHINGS_CLI_INTER_DEVICE_SLEEP_SEC", "0.3"))

# ─────────────────────── 시간대 / 스케줄 ───────────────────────
KST_TZ = "Asia/Seoul"
COLLECT_CRON_HOUR = 9
COLLECT_CRON_MINUTE = 0                 # 매일 09:00 KST 일일 수집 (DESIGN.md §6.1)
TOKEN_PROACTIVE_REFRESH_HOUR = 3        # 매일 03:00 KST 선제 갱신 (DESIGN.md §6.2)
HEALTHCHECK_INTERVAL_MINUTES = 60       # 매시간 누락분 보충 (DESIGN.md §6.1)
HEALTHCHECK_LOOKBACK_DAYS = 7           # Aqara 7일 제한 (DESIGN.md §10)
JOB_HISTORY_RETENTION_DAYS = 28         # collection_jobs 보관 일수 (4주). 매일 03:30 cron 자동 정리.


def ensure_dirs() -> None:
    """런타임 디렉토리 자동 생성 (수집 CSV 저장 루트 등)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
