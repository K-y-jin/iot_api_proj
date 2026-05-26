"""SmartThings API 클라이언트 (DESIGN.md §15.5).

두 가지 호출 경로:
1. REST `api.smartthings.com/v1` 직접 호출 (디바이스/장소 목록)
   - PAT Bearer 헤더 인증, requests 모듈 사용
2. **`smartthings` CLI subprocess** (history) — 공식 REST history 엔드포인트가 없어 CLI 의존
   - `smartthings devices:history <id> -L <N> -U -j -B <epoch_ms> --token <PAT>`
   - 응답은 시간 내림차순, 1페이지 최대 N건. 페이지네이션은 `-B` 갱신으로 처리.

Rate limit / 토큰 만료는 system_alerts 로 표면화 (Aqara 와 동일 패턴).
Aqara 와 달리 PAT refresh 흐름이 없어 401 시 admin 이 직접 재발급해야 한다.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from . import alerts, config, token_manager
from .devices import Bundle


# CLI stderr 에 인증 실패가 *정확히* 표현된 경우만 매치.
# - 'unauthorized' / 'forbidden' 단어
# - 'HTTP 401', 'status 403', 'code: 401' 등 HTTP 상태 prefix 와 함께 등장하는 401/403
# 단순히 숫자 substring (예: device id 12401, 길이 5403ms) 만으로는 매치되지 않게 단어 경계 사용.
_AUTH_KEYWORD_RE = re.compile(r"\b(unauthorized|forbidden|invalid[\s_-]?token|token\s+expired)\b", re.IGNORECASE)
_AUTH_HTTP_CODE_RE = re.compile(
    r"\b(?:http|status|code|response)\b[^a-z0-9]{0,12}\b(?:401|403)\b",
    re.IGNORECASE,
)


def _looks_like_auth_error(err: str) -> bool:
    """SmartThings CLI 의 stderr/stdout 이 토큰 거부를 의미하는지 정확히 판정.

    false-positive 를 줄이기 위해:
      - 'unauthorized'/'forbidden' 단어, 또는
      - 'HTTP 401', 'status: 403' 같이 HTTP 상태 코드 prefix 와 함께 등장하는 401/403
    만 인증 오류로 분류. 그 외 (단순한 숫자 매치, 네트워크 오류, device 권한 부족 등)는
    일반 수집 실패로 처리 (DESIGN.md §15.2).
    """
    if not err:
        return False
    if _AUTH_KEYWORD_RE.search(err):
        return True
    if _AUTH_HTTP_CODE_RE.search(err):
        return True
    return False


class SmartThingsAPIError(RuntimeError):
    """SmartThings 호출 실패 (HTTP non-2xx, CLI 비정상 종료, JSON 파싱 실패 등)."""

    def __init__(self, message: str, payload: dict | None = None):
        super().__init__(message)
        self.payload = payload or {}


# ─────────────────────── OAuth 2.0 Authorization Code flow (DESIGN.md §15.2) ───────────────────────

def oauth_authorize_url(state: str) -> str:
    """SmartThings OAuth authorize URL 생성 (admin 을 이 주소로 리다이렉트).

    state 는 CSRF 방지용 — 콜백에서 세션 값과 대조한다.
    """
    from urllib.parse import urlencode
    if not config.SMARTTHINGS_CLIENT_ID:
        raise SmartThingsAPIError("SMARTTHINGS_CLIENT_ID 가 설정되지 않았습니다 (.env 확인).")
    params = {
        "client_id": config.SMARTTHINGS_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": config.SMARTTHINGS_OAUTH_REDIRECT_URI,
        "scope": config.SMARTTHINGS_OAUTH_SCOPE,
        "state": state,
    }
    return f"{config.SMARTTHINGS_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


def _post_token_request(data: dict) -> dict:
    """SmartThings OAuth token 엔드포인트 POST 공통 처리.

    client_id/secret 은 HTTP Basic 인증으로 전달 (SmartThings 표준).
    실패 시 SmartThingsAPIError. 호출자(code 교환 / refresh)가 응답을 저장 형식으로 변환.
    """
    if not (config.SMARTTHINGS_CLIENT_ID and config.SMARTTHINGS_CLIENT_SECRET):
        raise SmartThingsAPIError("SMARTTHINGS_CLIENT_ID/SECRET 미설정 (.env 확인).")
    resp = requests.post(
        config.SMARTTHINGS_OAUTH_TOKEN_URL,
        data=data,
        auth=(config.SMARTTHINGS_CLIENT_ID, config.SMARTTHINGS_CLIENT_SECRET),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise SmartThingsAPIError(
            f"OAuth token 요청 실패 (HTTP {resp.status_code}): {resp.text[:300]}",
            {"http_status": resp.status_code, "body": resp.text[:1000]},
        )
    try:
        body = resp.json()
    except ValueError as e:
        raise SmartThingsAPIError(f"OAuth token 응답 JSON 파싱 실패: {resp.text[:300]}") from e
    if "access_token" not in body or "refresh_token" not in body:
        raise SmartThingsAPIError(f"OAuth token 응답에 토큰 누락: {body}")
    return body


def exchange_code_for_tokens(code: str) -> None:
    """authorization code → access/refresh 토큰 교환 후 저장 (OAuth 콜백에서 호출)."""
    body = _post_token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.SMARTTHINGS_OAUTH_REDIRECT_URI,
    })
    token_manager.save_smartthings_tokens(token_manager.build_smartthings_record(body))
    # 토큰이 새로 발급됐으므로 기존 인증 관련 alert 해제.
    alerts.resolve_by_code(["smartthings_token_missing", "smartthings_token_invalid"])


def refresh_smartthings_tokens() -> None:
    """저장된 refresh_token 으로 새 access/refresh 토큰 발급 후 저장.

    SmartThings refresh 응답은 refresh_token 도 회전되므로 새 값으로 덮어쓴다.
    refresh 실패(만료/거부)는 alert 등록 후 예외 — admin 이 재연결 필요.
    """
    try:
        rec = token_manager.load_smartthings_tokens()
    except token_manager.SmartThingsTokenNotFoundError as e:
        raise SmartThingsAPIError(str(e)) from e
    try:
        body = _post_token_request({
            "grant_type": "refresh_token",
            "refresh_token": rec["refresh_token"],
        })
    except SmartThingsAPIError as e:
        alerts.raise_alert(
            code="smartthings_token_invalid",
            level="error",
            message="SmartThings refresh token 갱신 실패. /admin/token 에서 재연결이 필요합니다.",
            details=str(e),
        )
        raise
    token_manager.save_smartthings_tokens(token_manager.build_smartthings_record(body))
    alerts.resolve_by_code(["smartthings_token_missing", "smartthings_token_invalid"])


def get_access_token() -> str:
    """현재 유효한 SmartThings access_token 반환.

    우선순위 (먼저 매치되는 값을 그대로 사용):
      1. **환경변수 PAT** — `.env` 의 `SMARTTHINGS_PAT`. 운영자가 잠근 stopgap.
      2. **파일 PAT** — admin 이 `/admin/token` 페이지에서 직접 입력해 저장한 PAT.
      3. **OAuth (정식)** — 저장된 access_token, 만료 임박 시 refresh_token 으로 자동 갱신.

    PAT 경로(1·2)는 refresh 흐름이 없다 — 만료/거부 시 admin 이 새 PAT 로 교체.
    REST/CLI 호출 직전 매번 호출. 모두 미등록이면 alert + 예외.
    """
    pat_env = (config.SMARTTHINGS_PAT or "").strip()
    if pat_env:
        return pat_env
    pat_file = token_manager.load_smartthings_pat_file()
    if pat_file:
        return pat_file
    try:
        token_manager.load_smartthings_tokens()
    except token_manager.SmartThingsTokenNotFoundError as e:
        alerts.raise_alert(
            code="smartthings_token_missing",
            level="error",
            message="SmartThings 미연결. /admin/token 에서 'SmartThings 연결' 또는 PAT 입력을 진행하세요.",
            details=str(e),
        )
        raise SmartThingsAPIError(str(e)) from e
    if token_manager.smartthings_is_expired():
        refresh_smartthings_tokens()
    return token_manager.load_smartthings_tokens()["access_token"]


# ─────────────────────────── 토큰 / 헤더 ───────────────────────────

def _bearer_headers() -> dict[str, str]:
    """REST 호출용 Authorization 헤더. 만료 임박 시 get_access_token 이 자동 refresh."""
    return {"Authorization": f"Bearer {get_access_token()}", "Accept": "application/json"}


# ─────────────────────────── 시각 변환 헬퍼 ───────────────────────────

_KST = timezone(timedelta(hours=9))


def _utc_iso_to_kst_str(utc_iso: str) -> str:
    """'2026-05-08T02:51:26.000+00:00' → '2026-05-08 11:51:26' (KST). 형식 오류는 빈 문자열."""
    if not utc_iso:
        return ""
    try:
        dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return dt.astimezone(_KST).strftime("%Y-%m-%d %H:%M:%S")


def _kst_str_to_epoch_ms(kst_str: str) -> int:
    """'YYYY-MM-DD HH:MM:SS' (KST naive) → UTC epoch milliseconds (CLI -B 입력용)."""
    dt = datetime.strptime(kst_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_KST)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


# ─────────────────────────── REST: list_devices / list_locations ───────────────────────────

def _rest_get(path: str) -> dict:
    """단일 GET. HTTP 429 → Retry-After 한 번 대기 후 재시도. 401/403 → 토큰 알림."""
    url = f"{config.SMARTTHINGS_API_URL}{path}"
    headers = _bearer_headers()
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code == 429:
        wait = int(resp.headers.get("Retry-After", "10"))
        time.sleep(wait)
        resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code in (401, 403):
        alerts.raise_alert(
            code="smartthings_token_invalid",
            level="error",
            message=f"SmartThings PAT 가 거부되었습니다 (HTTP {resp.status_code}). 재발급 필요.",
            details=resp.text[:500],
        )
        raise SmartThingsAPIError(f"HTTP {resp.status_code}: token rejected", {"body": resp.text[:1000]})
    if resp.status_code != 200:
        raise SmartThingsAPIError(
            f"HTTP {resp.status_code}: {resp.text[:300]}",
            {"http_status": resp.status_code, "body": resp.text[:1000]},
        )
    return resp.json()


def list_devices() -> list[dict]:
    """토큰 권한 내 모든 디바이스 (id/label/type/locationId 등)."""
    return _rest_get("/devices").get("items", [])


def list_locations() -> list[dict]:
    """장소 목록 (locationId/name/timezone 등)."""
    return _rest_get("/locations").get("items", [])


# ─────────────────────────── CLI history 호출 ───────────────────────────

class _RateLimiter:
    """1분 sliding-window 안에서 max_rpm 회까지 허용. CLI 호출 직전 acquire() 차단.

    samsung/export_history_throttled.py 의 RateLimiter 패턴 흡수.
    프로세스 내 단일 인스턴스 사용 (collector 가 직렬 호출하므로 충분).
    """

    def __init__(self, max_rpm: int):
        self.max_rpm = max_rpm
        self.window = 60.0
        self.timestamps: deque[float] = deque()

    def acquire(self) -> None:
        while True:
            now = time.monotonic()
            while self.timestamps and now - self.timestamps[0] >= self.window:
                self.timestamps.popleft()
            if len(self.timestamps) < self.max_rpm:
                self.timestamps.append(now)
                return
            wait = self.timestamps[0] + self.window - now
            if wait > 0:
                time.sleep(wait + 0.05)


_rate_limiter = _RateLimiter(config.SMARTTHINGS_CLI_MAX_RPM)


def _is_rate_limited(text: str) -> bool:
    """CLI stderr/stdout 에서 rate limit 표지를 발견."""
    t = (text or "").lower()
    return ("429" in t) or ("rate limit" in t) or ("too many requests" in t)


def _resolve_cli() -> str | None:
    """smartthings CLI 실행 파일 경로 해결.

    우선순위:
      1. `SMARTTHINGS_CLI_PATH` 설정값이 비어있지 않고 파일이 존재하면 그 경로 사용
         — 서버 PATH 에 등록되지 않은 절대 경로(예: 'C:\\Program Files\\SmartThings\\smartthings.exe')도 사용 가능.
      2. `shutil.which("smartthings")` — PATH 에 있는 첫 번째 매치.
      3. 둘 다 실패하면 None (호출자는 alert 발생).
    """
    configured = (config.SMARTTHINGS_CLI_PATH or "").strip()
    if configured and os.path.isfile(configured):
        return configured
    return shutil.which("smartthings")


def _extract_json(stdout: str) -> Any:
    """CLI stdout 에서 JSON 영역만 추출 (CLI 가 가끔 진행 메시지를 앞에 붙임).

    `[` 또는 `{` 가 처음 등장하는 위치부터 끝까지 json.loads.
    """
    text = (stdout or "").strip()
    if not text:
        return []
    candidates = [i for i in (text.find("["), text.find("{")) if i != -1]
    if not candidates:
        raise SmartThingsAPIError("CLI 출력에서 JSON 시작 위치를 찾지 못했습니다.", {"stdout_head": text[:300]})
    return json.loads(text[min(candidates):])


def _run_cli_history(device_id: str, before_ms: int, limit: int, max_retries: int = 3) -> list[dict]:
    """`smartthings devices:history` 1회 호출 → 이벤트 리스트 반환.

    - `-A` (after) 는 일부 기기에서 'Cannot read properties of undefined' 발생 → 사용 안 함.
      시작점 필터는 호출자가 KST 비교로 처리.
    - 429/"rate limit" 감지 시 60/120/180s 점진 백오프 후 재시도.
    - CLI 미설치 / 비정상 종료 / 토큰 401 → SmartThingsAPIError + system_alerts.
    """
    cli_path = _resolve_cli()
    if not cli_path:
        alerts.raise_alert(
            code="smartthings_cli_missing",
            level="error",
            message="smartthings CLI 바이너리를 찾을 수 없습니다. SmartThings 수집 비활성화.",
            details=(
                "(1) PATH 에 'smartthings' 추가, 또는 "
                "(2) 환경변수 SMARTTHINGS_CLI_PATH 에 절대 경로 지정 "
                "(예: C:\\Program Files\\SmartThings\\smartthings.exe). "
                "CLI 설치: winget install SmartThings.SmartThingsCLI "
                "또는 https://github.com/SmartThingsCommunity/smartthings-cli"
            ),
        )
        raise SmartThingsAPIError("smartthings CLI binary not found (PATH/SMARTTHINGS_CLI_PATH)")

    # 토큰 prefetch — get_access_token 이 미등록 시 alert + 예외, 만료 임박 시 자동 refresh.
    # OAuth access_token 도 CLI 의 --token 에 bearer 로 그대로 전달 가능.
    token = get_access_token()
    cmd = [
        cli_path, "devices:history", device_id,
        "-L", str(limit),
        "-U",                   # UTC ISO 시각 출력
        "-j",                   # JSON
        "-B", str(before_ms),   # before 시각 (이 이전 이벤트만)
        "--token", token,
    ]

    for attempt in range(max_retries + 1):
        _rate_limiter.acquire()
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            data = _extract_json(result.stdout)
            if isinstance(data, dict):
                return data.get("items", []) or []
            return data or []

        err = (result.stderr or result.stdout or "").strip()
        # 토큰 거부 *정확한* 감지 (DESIGN.md §15.2). 과거 'unauthorized/401/403' 부분문자열을
        # 그대로 매치해 false-positive (예: device id 의 '12401' 안에 '401' 포함) 가 빈번했음.
        # → 단어 경계로 한정 + HTTP 코드는 명시적 prefix(예: 'HTTP 401', 'status 403',
        # 'code: 401') 와 함께 등장할 때만 인증 오류로 분류한다. 그 외 모든 CLI 오류는
        # 일반 수집 실패로 처리해 token alert 를 띄우지 않는다.
        if _looks_like_auth_error(err):
            alerts.raise_alert(
                code="smartthings_token_invalid",
                level="error",
                message="SmartThings PAT 가 거부되었습니다. 재발급 필요.",
                details=err[:500],
            )
            raise SmartThingsAPIError(f"smartthings CLI auth error: {err[:300]}")
        # rate limit → 점진 백오프 후 재시도
        if _is_rate_limited(err) and attempt < max_retries:
            backoff = 60 * (attempt + 1)
            time.sleep(backoff)
            continue
        raise SmartThingsAPIError(f"smartthings CLI failed: {err[:300]}")

    raise SmartThingsAPIError("smartthings CLI retry exhausted")


# ─────────────────────────── 1일치 페이지네이션 수집 ───────────────────────────

def fetch_device_history_for_date(
    device_id: str,
    target_date_kst: str,
    page_size: int | None = None,
    max_pages: int | None = None,
) -> list[dict]:
    """target_date(KST 'YYYY-MM-DD') 의 모든 이벤트 수집 (DESIGN.md §15.6.1).

    원리:
    - `-B` 를 (target_date + 1일) 00:00 KST 의 epoch ms 로 시작.
    - CLI 응답은 시간 내림차순. batch 의 가장 오래된 이벤트가 target_date 시작 이전이면 종료.
    - 그 외에는 `-B` 를 batch 의 가장 오래된 이벤트 epoch ms 로 갱신해 다음 페이지 호출
      (CLI 의 -B 는 exclusive 동작 가정 — 동일 경계 중복 방지).

    반환: SmartThings CLI 원본 이벤트 dict 의 리스트 (target_date 안의 이벤트만 필터링 완료).
    """
    if page_size is None:
        page_size = config.SMARTTHINGS_CLI_PAGE_SIZE
    if max_pages is None:
        max_pages = config.SMARTTHINGS_CLI_MAX_PAGES

    start_dt = datetime.strptime(target_date_kst, "%Y-%m-%d").replace(tzinfo=_KST)
    end_dt = start_dt + timedelta(days=1)
    cursor_before_ms = int(end_dt.astimezone(timezone.utc).timestamp() * 1000)

    collected: list[dict] = []
    for _ in range(max_pages):
        batch = _run_cli_history(device_id, cursor_before_ms, page_size)
        if not batch:
            break

        # target_date 안의 이벤트만 보존
        in_range: list[dict] = []
        oldest_dt: datetime | None = None
        for ev in batch:
            ts = ev.get("time")
            if not ts:
                continue
            try:
                ev_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if oldest_dt is None or ev_dt < oldest_dt:
                oldest_dt = ev_dt
            if start_dt <= ev_dt < end_dt:
                in_range.append(ev)
        collected.extend(in_range)

        # 종료 조건
        if oldest_dt is None:
            break
        if oldest_dt < start_dt:
            break
        if len(batch) < page_size:
            break
        # 다음 페이지: -B 를 가장 오래된 이벤트 epoch ms 로 갱신
        new_cursor = int(oldest_dt.astimezone(timezone.utc).timestamp() * 1000)
        if new_cursor >= cursor_before_ms:
            # 진전이 없으면 (CLI 이상) 안전 종료
            break
        cursor_before_ms = new_cursor

    return collected


# ─────────────────────────── Bundle 매칭 ───────────────────────────

def fetch_history_for_bundle(
    device_id: str,
    bundle: Bundle,
    target_date_kst: str,
) -> dict[str, dict[str, str]]:
    """단일 디바이스 × bundle 의 (capability, attribute) 별 (kst_time → value) 매핑 반환.

    collector.run_one_bundle 이 기대하는 형태:
        {resource_name: {kst_str: value_str}}

    한 디바이스의 CLI 응답은 capability/attribute 가 섞여 있으므로 Python 에서 분리.
    """
    events = fetch_device_history_for_date(device_id, target_date_kst)

    # (capability, attribute) → resource_name 매핑
    by_cap: dict[tuple[str, str], str] = {}
    for r in bundle.resources:
        cap, _, attr = r.id.partition(".")
        by_cap[(cap, attr)] = r.name

    per_resource: dict[str, dict[str, str]] = {r.name: {} for r in bundle.resources}
    for ev in events:
        key = (ev.get("capability") or "", ev.get("attribute") or "")
        name = by_cap.get(key)
        if name is None:
            continue  # bundle 정의 외 capability 는 무시
        ts_kst = _utc_iso_to_kst_str(ev.get("time") or "")
        if not ts_kst:
            continue
        # 동일 timestamp 중복은 마지막 값 (Aqara dict 패턴과 동일)
        v = ev.get("value")
        per_resource[name][ts_kst] = "" if v is None else str(v)

    # 성공 호출 시 관련 알림 자동 해제
    alerts.resolve_by_code(["smartthings_token_missing", "smartthings_token_invalid", "smartthings_cli_missing"])
    return per_resource
