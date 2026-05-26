"""HTML 페이지 라우트 (DESIGN.md §7.1).

대부분 라우트는 공개(비로그인 조회 허용). 로그인 시에만 액션 버튼이 렌더링되며,
서버측에서도 변경 라우트(api.py)에 require_login 의존성을 적용해 이중 방어한다.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import alerts, auth, config, scheduler, token_manager
from ..auth import current_user
from ..db import get_connection
from ..devices import DEVICE_TYPES


router = APIRouter()
templates = Jinja2Templates(directory=str(config.PROJECT_ROOT / "app" / "templates"))


def _human_bytes(n: int | None) -> str:
    """바이트 수를 사용자 친화 단위(B/KB/MB/GB)로 환산 (DESIGN.md §7.3 "총 용량(KB·MB 환산)").

    1024 base, 소수 1자리. B 단위는 정수. n이 None이면 빈 문자열을 돌려
    템플릿에서 별도 분기 없이도 미수집/실패 행을 자연스럽게 빈칸 처리한다.
    """
    if n is None:
        return ""
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(n)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    if idx == 0:
        return f"{int(size):,} B"
    return f"{size:,.1f} {units[idx]}"


templates.env.filters["human_bytes"] = _human_bytes


def _ctx(request: Request, **extra) -> dict:
    """공통 템플릿 컨텍스트 (current_user + active alerts)."""
    return {
        "request": request,
        "user": current_user(request),
        "active_alerts": alerts.list_active(),
        **extra,
    }


# ─────────────────────────── 로그인 / 로그아웃 ───────────────────────────

@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", _ctx(request))


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    user = auth.get_user_by_username(username)
    if user is None or not auth.verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            request,
            "login.html",
            _ctx(request, error="아이디 또는 비밀번호가 올바르지 않습니다."),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    auth.login_session(request, user)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
def logout(request: Request):
    auth.logout_session(request)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


# ─────────────────────────── 대시보드 / 장치 / 데이터 / 작업 ───────────────────────────

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    """대시보드: 전체 활성 기기 수, 어제 success/failed, 누적 용량 (DESIGN.md §7.1)."""
    from ..db import yesterday_kst
    conn = get_connection()
    try:
        active_count = conn.execute(
            "SELECT COUNT(*) AS c FROM devices WHERE deleted_at IS NULL AND enabled=1"
        ).fetchone()["c"]
        y = yesterday_kst()
        ok = conn.execute(
            "SELECT COUNT(*) AS c FROM collection_jobs WHERE target_date=? AND status='success'",
            (y,),
        ).fetchone()["c"]
        ng = conn.execute(
            "SELECT COUNT(*) AS c FROM collection_jobs WHERE target_date=? AND status='failed'",
            (y,),
        ).fetchone()["c"]
        total_bytes = conn.execute(
            "SELECT COALESCE(SUM(file_size_bytes), 0) AS s FROM collection_jobs WHERE status='success'"
        ).fetchone()["s"]
    finally:
        conn.close()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _ctx(request,
             active_count=active_count, yesterday=y,
             yesterday_ok=ok, yesterday_fail=ng,
             total_bytes=total_bytes,
             jobs=scheduler.list_jobs()),
    )


@router.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request):
    """활성/변경·삭제 이력 두 탭 + 그룹 관리 미니 섹션 (DESIGN.md §7.4).

    비로그인 시 추가/편집/삭제/그룹 변경 버튼은 미렌더링.
    그룹은 혼합 device_type 허용이므로 드롭다운에 모든 그룹을 노출 (DISPLAY.md §4.8).
    "변경/삭제 이력" 은 device_history(change_type='update') 와 devices.deleted_at 행을
    같은 컬럼 셋으로 합쳐 시간 역순으로 렌더한다 (편집 적용 직전 스냅샷 + 삭제 이벤트).
    """
    conn = get_connection()
    try:
        active = conn.execute(
            "SELECT * FROM devices WHERE deleted_at IS NULL ORDER BY id"
        ).fetchall()
        # 변경 이력 (update 스냅샷) — devices 와 LEFT JOIN 해 등록자/등록일 메타 결합.
        updates = conn.execute(
            """SELECT 'update'          AS change_type,
                      h.id               AS history_id,
                      h.device_pk        AS device_pk,
                      h.changed_at       AS changed_at,
                      h.changed_by_name  AS changed_by_name,
                      h.changed_fields   AS changed_fields,
                      h.device_id, h.device_id_upper, h.device_type, h.hub,
                      h.install_location, h.install_date, h.alias, h.enabled, h.group_id,
                      d.created_by_name, d.created_at
                 FROM device_history h
            LEFT JOIN devices d ON d.id = h.device_pk
                WHERE h.change_type = 'update'"""
        ).fetchall()
        # 삭제 이력 (기존과 동일 의미) — change_type='delete' 로 통일된 키 셋 노출.
        deletes = conn.execute(
            """SELECT 'delete'           AS change_type,
                      NULL               AS history_id,
                      id                 AS device_pk,
                      deleted_at         AS changed_at,
                      deleted_by_name    AS changed_by_name,
                      NULL               AS changed_fields,
                      device_id, device_id_upper, device_type, hub,
                      install_location, install_date, alias, enabled, group_id,
                      created_by_name, created_at
                 FROM devices
                WHERE deleted_at IS NOT NULL"""
        ).fetchall()
        history = [dict(r) for r in (*updates, *deletes)]
        history.sort(key=lambda x: x.get("changed_at") or "", reverse=True)

        groups = conn.execute(
            """SELECT g.id, g.name, g.device_type, g.description, g.created_by_name, g.created_at,
                      (SELECT COUNT(*) FROM devices d
                        WHERE d.group_id=g.id AND d.deleted_at IS NULL) AS member_count
                 FROM device_groups g
                ORDER BY g.id"""
        ).fetchall()
    finally:
        conn.close()
    group_name_by_id = {g["id"]: g["name"] for g in groups}
    return templates.TemplateResponse(
        request,
        "devices.html",
        _ctx(request,
             active_devices=[dict(r) for r in active],
             device_history=history,
             groups=[dict(g) for g in groups],
             group_name_by_id=group_name_by_id,
             device_types=DEVICE_TYPES),
    )


@router.get("/data", response_class=HTMLResponse)
def data_page(request: Request):
    """데이터 현황 화면 (DESIGN.md §7.3).

    파일시스템 walk로 data/{bundle_key}/{device_id}/ 하위 모든 CSV를 집계한다.
    자동 수집뿐 아니라 수동 import한 파일(예: 과거 데이터 변환분)도 자동 노출.
    devices 테이블의 active 행 메타데이터로 enrich (alias/종류/설치 위치).
    """
    from .. import display_extract

    summary = display_extract.data_summary_from_filesystem(config.DATA_DIR)

    # devices 테이블 active 행을 device_id → row dict로 인덱싱 (DESIGN.md §7.3)
    conn = get_connection()
    try:
        device_rows = conn.execute(
            "SELECT device_id, alias, device_type, install_location, deleted_at"
            " FROM devices WHERE deleted_at IS NULL"
        ).fetchall()
    finally:
        conn.close()
    device_meta = {r["device_id"]: dict(r) for r in device_rows}

    # 파일 집계 결과에 디바이스 메타데이터 병합. orphan(삭제된 device의 잔여 CSV)은 None으로 둠.
    items: list[dict] = []
    for s in summary:
        d = device_meta.get(s["device_id"])
        items.append({
            **s,
            "alias":            d["alias"] if d else None,
            "device_type":      d["device_type"] if d else None,
            "install_location": d["install_location"] if d else None,
            "deleted_at":       None if d else "orphan",  # 템플릿이 (deleted) 표기에 사용
        })

    return templates.TemplateResponse(
        request,
        "data.html",
        _ctx(request, items=items, device_types=DEVICE_TYPES),
    )


@router.get("/data/{device_id}/{bundle_key}", response_class=HTMLResponse)
def data_files_page(request: Request, device_id: str, bundle_key: str):
    # CSV 다운로드 링크가 노출되는 페이지이므로 로그인 필수 (DESIGN.md §6.3, §7.1).
    # API와 달리 HTML은 401 대신 /login으로 리다이렉트하는 편이 UX상 자연스럽다.
    if current_user(request) is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT target_date, status, record_count, file_path, file_size_bytes,
                      started_at, finished_at, error_message
                 FROM collection_jobs
                WHERE device_id=? AND bundle_key=?
             ORDER BY target_date DESC""",
            (device_id, bundle_key),
        ).fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(
        request,
        "data_files.html",
        _ctx(request, device_id=device_id, bundle_key=bundle_key,
             files=[dict(r) for r in rows]),
    )


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM collection_jobs ORDER BY id DESC LIMIT 500"
        ).fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "jobs.html", _ctx(request, jobs=[dict(r) for r in rows]),
    )


# ─────────────────────────── 관리자 페이지 ───────────────────────────

@router.get("/admin/token", response_class=HTMLResponse)
def admin_token_page(
    request: Request,
    st_oauth: str | None = Query(None),
    st_pat: str | None = Query(None),
    st_pat_n: int | None = Query(None),
):
    """Aqara 토큰 + SmartThings OAuth 연결 + PAT 관리 화면.

    st_oauth 쿼리: OAuth 콜백 결과 키워드 (success / denied / invalid / state_mismatch / exchange_failed).
    st_pat 쿼리: PAT 저장/삭제 결과 키워드 (saved / cleared / overridden_by_env).
    st_pat_n: 저장 시 검증된 디바이스 수 (saved 결과에 함께 노출).
    """
    user = current_user(request)
    if user is None or not user["is_admin"]:
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    try:
        rec = token_manager.load()
        token_info = {
            "present": True,
            "refreshed_at": rec.get("refreshed_at"),
            "expires_at": rec.get("expires_at"),
            "access_token_masked": (rec["access_token"][:6] + "…") if rec.get("access_token") else "(empty)",
        }
    except token_manager.TokenNotFoundError:
        token_info = {"present": False}
    # SmartThings OAuth 연결 상태 (DESIGN.md §15.2)
    st_token_info = token_manager.smartthings_token_status()
    # OAuth 콜백 결과 메시지 (고정 키워드 → 사용자 안내 문구)
    _st_msgs = {
        "success": ("ok", "SmartThings 연결 성공 — OAuth 토큰이 저장되었습니다."),
        "denied": ("error", "SmartThings 인증이 거부되었습니다 (사용자가 승인하지 않음)."),
        "invalid": ("error", "OAuth 콜백 파라미터가 누락되었습니다. 다시 시도하세요."),
        "state_mismatch": ("error", "OAuth state 불일치 — 보안상 중단되었습니다. 다시 시도하세요."),
        "exchange_failed": ("error", "인증 코드 → 토큰 교환에 실패했습니다. client_id/secret·redirect_uri 설정을 확인하세요."),
    }
    st_oauth_msg = _st_msgs.get(st_oauth or "")
    # PAT 저장/삭제 결과 메시지 (admin UI 의 PAT 입력 폼에서 redirect 로 호출)
    if st_pat == "saved":
        n = st_pat_n if st_pat_n is not None else 0
        st_pat_msg = ("ok", f"PAT 저장 성공 — 디바이스 {n}개 확인됨. 이제 SmartThings 수집이 PAT 로 동작합니다.")
    elif st_pat == "cleared":
        st_pat_msg = ("ok", "PAT 가 삭제되었습니다. OAuth 토큰이 있으면 그쪽으로 복귀합니다.")
    elif st_pat == "overridden_by_env":
        st_pat_msg = ("error", "PAT 가 파일에 저장됐지만 환경변수 SMARTTHINGS_PAT 가 우선됩니다 (파일 PAT 는 무시).")
    else:
        st_pat_msg = None
    return templates.TemplateResponse(
        request, "admin_token.html",
        _ctx(request, token_info=token_info, st_token_info=st_token_info,
             st_oauth_msg=st_oauth_msg, st_pat_msg=st_pat_msg),
    )


@router.get("/admin/smartthings/oauth/start")
def smartthings_oauth_start(request: Request):
    """SmartThings OAuth 연결 시작 — CSRF state 발급 후 authorize URL 로 리다이렉트 (DESIGN.md §15.2)."""
    user = current_user(request)
    if user is None or not user["is_admin"]:
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    from .. import smartthings_client
    state = secrets.token_urlsafe(24)
    request.session["st_oauth_state"] = state
    try:
        url = smartthings_client.oauth_authorize_url(state)
    except smartthings_client.SmartThingsAPIError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/smartthings/oauth/callback")
def smartthings_oauth_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
):
    """SmartThings OAuth 콜백 — authorization code 수신 → 토큰 교환 (DESIGN.md §15.2).

    결과는 /admin/token 으로 고정 키워드(st_oauth)와 함께 리다이렉트해 사용자에게 표시.
    state 는 세션에 저장한 값과 대조 (CSRF 방지).
    """
    user = current_user(request)
    if user is None or not user["is_admin"]:
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    from .. import smartthings_client

    expected = request.session.pop("st_oauth_state", None)

    def _back(keyword: str) -> RedirectResponse:
        return RedirectResponse(f"/admin/token?st_oauth={keyword}",
                                status_code=status.HTTP_303_SEE_OTHER)

    if error:
        return _back("denied")
    if not code or not state:
        return _back("invalid")
    if not expected or state != expected:
        return _back("state_mismatch")
    try:
        smartthings_client.exchange_code_for_tokens(code)
    except smartthings_client.SmartThingsAPIError:
        return _back("exchange_failed")
    return _back("success")


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request):
    user = current_user(request)
    if user is None or not user["is_admin"]:
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    return templates.TemplateResponse(
        request, "admin_users.html", _ctx(request, users=auth.list_users()),
    )


@router.post("/admin/users")
def admin_users_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    is_admin: bool = Form(False),
):
    user = current_user(request)
    if user is None or not user["is_admin"]:
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    try:
        auth.create_user(username, password, is_admin=is_admin)
    except Exception as e:  # noqa: BLE001
        return templates.TemplateResponse(
            request,
            "admin_users.html",
            _ctx(request, users=auth.list_users(), error=str(e)),
            status_code=400,
        )
    return RedirectResponse("/admin/users", status_code=status.HTTP_303_SEE_OTHER)
