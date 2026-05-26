"""디바이스 활동 타임라인 디스플레이 라우트 (DISPLAY.md SSOT).

GET /display/{device_id}?to=&days= : 디바이스의 1주일 활동을 SVG 타임라인으로 표시.

권한: 로그인 필수 (DISPLAY.md §1). 비로그인 시 /login 으로 303 리다이렉트.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import alerts, config, display_extract
from ..auth import current_user
from ..db import get_connection
from ..devices import DEVICE_TYPES


router = APIRouter()
templates = Jinja2Templates(directory=str(config.PROJECT_ROOT / "app" / "templates"))
# 'YYYY-MM-DD HH:MM:SS' KST → 0:00 부터의 초. 템플릿 SVG X 좌표 계산에 사용 (DISPLAY.md §3).
templates.env.globals["display_seconds"] = display_extract.kst_str_to_seconds_of_day

# DISPLAY.md §3 1주일 = 7행 한 페이지 한도
MAX_DAYS = 7
DEFAULT_DAYS = 7


def _today_kst() -> date:
    """KST 자정 기준 오늘 일자. db.now_kst_iso 와 일관성 유지."""
    from ..db import now_kst_iso
    return datetime.strptime(now_kst_iso()[:10], "%Y-%m-%d").date()


def _fetch_device(raw_id: str) -> dict:
    """URL 의 raw device_id 로 활성 디바이스 조회 (hub-agnostic).

    Aqara(`lumi.<hex>`) 와 SmartThings(UUID / 24-hex) 가 한 URL 라우트(`/display/{device_id}`)를
    공유하므로 hub 를 알 수 없는 상태에서 두 정규화 후보를 차례로 조회한다:
      1) raw 그대로(소문자) — SmartThings device_id 그대로 저장된 경우
      2) `lumi.` 접두 추가(소문자) — Aqara device_id (URL 에 `lumi.` 가 생략된 경우)
    URL 인코딩된 대시·하이픈은 FastAPI path 디코딩이 처리하므로 별도 변환 불필요.
    """
    s = raw_id.strip().lower()
    candidates = [s]
    if not s.startswith("lumi."):
        candidates.append("lumi." + s)
    conn = get_connection()
    try:
        for cand in candidates:
            row = conn.execute(
                "SELECT * FROM devices WHERE device_id=? AND deleted_at IS NULL",
                (cand,),
            ).fetchone()
            if row is not None:
                return dict(row)
    finally:
        conn.close()
    raise HTTPException(status_code=404, detail="등록되지 않았거나 삭제된 디바이스입니다.")


def _job_status_map(device_id_norm: str, dates: list[str]) -> dict[tuple[str, str], str]:
    """(date, bundle_key) → status 매핑. '수집 실패' 라벨 표시에 사용 (DISPLAY.md §9)."""
    if not dates:
        return {}
    placeholders = ",".join(["?"] * len(dates))
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT target_date, bundle_key, status FROM collection_jobs
                 WHERE device_id=? AND target_date IN ({placeholders})""",
            (device_id_norm, *dates),
        ).fetchall()
    finally:
        conn.close()
    return {(r["target_date"], r["bundle_key"]): r["status"] for r in rows}


def _build_day_rows(device: dict, dates_desc: list[str]) -> list[dict]:
    """각 일자별로 디바이스 타입에 맞는 bundle CSV를 읽어 day_row 데이터 구성.

    day_row 구조:
      {
        'date': 'YYYY-MM-DD',
        'weekday_ko': '월'|...,
        'is_today': bool,
        'tracks': [
            {
              'kind': 'motion'|'door'|'vibration'|'switch'|'vibration_aq1'|'temp_humi'|'unsupported',
              'intervals': tuple[Interval, ...],
              'points': tuple[PointEvent, ...],   # 라벨 채워져 있을 수 있음
              'has_csv': bool,                    # 메인 bundle CSV 존재 여부
              'job_status': str|None,             # 메인 bundle 수집 상태
              'aux_has_csv': bool,                # 과거 보조 bundle 필드 (현재 미사용, True 고정)
              'aux_job_status': str|None,
            }
        ]
      }

    한 디바이스 = 한 트랙. vibration_t1은 과거 두 bundle(knock_event/move_detect) 분리 구조였으나
    단일 wide bundle `move_knock`으로 통합 (DEVICE.md §4.3) — 보조 필드는 호환용 placeholder.
    """
    device_type_key = device["device_type"]
    dt = DEVICE_TYPES.get(device_type_key)
    if dt is None:
        raise HTTPException(status_code=500, detail=f"알 수 없는 device_type: {device_type_key}")

    today = _today_kst().strftime("%Y-%m-%d")
    weekday_ko = ["월", "화", "수", "목", "금", "토", "일"]
    job_status = _job_status_map(device["device_id"], dates_desc)

    rows: list[dict] = []
    for d in dates_desc:
        wd = datetime.strptime(d, "%Y-%m-%d").weekday()
        day = {
            "date": d,
            "weekday_ko": weekday_ko[wd],
            "is_today": (d == today),
            "tracks": [],
            "knock_points": (),
        }
        prev_d = (datetime.strptime(d, "%Y-%m-%d").date() - timedelta(days=1)).strftime("%Y-%m-%d")

        # 공통 헬퍼: 메인 bundle CSV 읽기
        def _read(bundle_key: str, date: str):
            p = display_extract._csv_path(config.DATA_DIR, bundle_key, device["device_id"], date)
            return p, display_extract.read_csv_rows(p)

        if device_type_key in ("motion_t1", "motion_p1"):
            # DISPLAY.md §4.1 / §4 (SmartThings 확장).
            # 같은 device_type 이라도 hub 에 따라 추출기가 다름:
            #   aqara       → motion_status(1만) gap-grouping
            #   smartthings → motion(active/inactive) 상태머신 + lux 측정점
            # "수집 없음" 일자(CSV 부재)는 carry-over 막대도 그리지 않는다 (사용자 정책 — door_t1 과 동일).
            dev_hub = device.get("hub") or "aqara"
            path, csv_rows = _read("motion_lux", d)
            csv_exists = path.exists()
            if dev_hub == "smartthings":
                _, prev_rows = _read("motion_lux", prev_d)
                intervals = display_extract.extract_st_motion_intervals(
                    csv_rows, prev_rows or None, target_date=d) if csv_exists else []
                lux_series = display_extract.extract_st_lux_series(csv_rows) if csv_exists \
                    else {"lux": [], "lux_min": 0.0, "lux_max": 100.0}
                day["tracks"].append({
                    "kind": "st_motion",
                    "intervals": tuple(intervals),
                    "points": (),
                    "lux_series": lux_series,
                    "has_csv": csv_exists,
                    "job_status": job_status.get((d, "motion_lux")),
                    "aux_has_csv": True,
                    "aux_job_status": None,
                })
            else:
                intervals = display_extract.extract_motion_intervals(csv_rows) if csv_exists else []
                day["tracks"].append({
                    "kind": "motion",
                    "intervals": tuple(intervals),
                    "points": (),
                    "has_csv": csv_exists,
                    "job_status": job_status.get((d, "motion_lux")),
                    "aux_has_csv": True,
                    "aux_job_status": None,
                })

        elif device_type_key == "door_t1":
            # DISPLAY.md §4.2 — 직전 일자 CSV로 경계 복원. hub 별 토큰만 다른 동일 상태머신:
            #   aqara       → magnet_status 1/0
            #   smartthings → contact 'open'/'closed'
            dev_hub = device.get("hub") or "aqara"
            path, csv_rows = _read("magnet_status", d)
            _, prev_rows = _read("magnet_status", prev_d)
            if dev_hub == "smartthings":
                intervals = display_extract.extract_st_contact_intervals(
                    csv_rows, prev_rows or None, target_date=d)
            else:
                intervals = display_extract.extract_door_intervals(
                    csv_rows, prev_rows or None, target_date=d)
            # "수집 없음" 일자(파일 자체 부재)는 전날 상태 carry-over 막대를 표시하지 않는다 — 사용자 정책.
            # 단, CSV 가 존재하지만 그날 이벤트가 0건인 "이벤트 없음" 일자는 직전 일자 상태로 복원된 막대를 그대로 표시.
            csv_exists = path.exists()
            if not csv_exists:
                intervals = []
            day["tracks"].append({
                "kind": "door",
                "intervals": tuple(intervals),
                "points": (),
                "has_csv": csv_exists,
                "job_status": job_status.get((d, "magnet_status")),
                "aux_has_csv": True,
                "aux_job_status": None,
            })

        elif device_type_key == "vibration_t1":
            # DISPLAY.md §4.3 — bundle key 는 hub 공통(move_knock) 이지만 컬럼 셋이 다름:
            #   aqara       → wide(move_detect + knock_event): move bar + knock tick
            #   smartthings → time, acceleration (active/inactive): move bar 만 (knock 없음)
            dev_hub = device.get("hub") or "aqara"
            path, csv_rows = _read("move_knock", d)
            _, prev_rows = _read("move_knock", prev_d)
            if dev_hub == "smartthings":
                day["tracks"].append({
                    "kind": "vibration",
                    "intervals": tuple(display_extract.extract_st_acceleration_intervals(
                        csv_rows, prev_rows or None, target_date=d)),
                    "points": (),
                    "has_csv": path.exists(),
                    "job_status": job_status.get((d, "move_knock")),
                    "aux_has_csv": True,
                    "aux_job_status": None,
                })
            else:
                day["tracks"].append({
                    "kind": "vibration",
                    "intervals": tuple(display_extract.extract_move_intervals(
                        csv_rows, prev_rows or None, target_date=d)),
                    "points": tuple(display_extract.extract_knock_points(csv_rows)),
                    "has_csv": path.exists(),
                    "job_status": job_status.get((d, "move_knock")),
                    "aux_has_csv": True,
                    "aux_job_status": None,
                })

        elif device_type_key == "switch_t1":
            # DISPLAY.md §4.4 — long press bar + click/shake tick (단일 bundle)
            path, csv_rows = _read("switch_status", d)
            _, prev_rows = _read("switch_status", prev_d)
            day["tracks"].append({
                "kind": "switch",
                "intervals": tuple(display_extract.extract_switch_long_intervals(csv_rows, prev_rows or None, target_date=d)),
                "points": tuple(display_extract.extract_switch_point_events(csv_rows)),
                "has_csv": path.exists(),
                "job_status": job_status.get((d, "switch_status")),
                "aux_has_csv": True,
                "aux_job_status": None,
            })

        elif device_type_key in ("motion_and_light_p2", "motion_and_light_wm"):
            # DESIGN.md §15.4 / DISPLAY.md §4 (SmartThings) — motion bar + lux line plot.
            # P2(Aqara Matter) 와 Watts Matter 모션 센서 모두 동일한 motion_lux bundle 사용.
            # motion 컬럼은 active/inactive 페어 상태머신 (door 와 유사), lux 컬럼은 연속 측정값 polyline.
            # Aqara motion_t1/p1 과 동일 bundle 키(motion_lux) 사용 — 폴더는 device_id 로 분리.
            # "수집 없음" 일자는 carry-over 막대를 그리지 않는다 (사용자 정책).
            path, csv_rows = _read("motion_lux", d)
            _, prev_rows = _read("motion_lux", prev_d)
            csv_exists = path.exists()
            intervals = display_extract.extract_st_motion_intervals(
                csv_rows, prev_rows or None, target_date=d) if csv_exists else []
            lux_series = display_extract.extract_st_lux_series(csv_rows) if csv_exists \
                else {"lux": [], "lux_min": 0.0, "lux_max": 100.0}
            day["tracks"].append({
                "kind": "st_motion",
                "intervals": tuple(intervals),
                "points": (),
                "lux_series": lux_series,
                "has_csv": csv_exists,
                "job_status": job_status.get((d, "motion_lux")),
                "aux_has_csv": True,
                "aux_job_status": None,
            })

        elif device_type_key == "vibration_aq1":
            # DISPLAY.md §4.5 — 연속 '2' 그룹(1 시작 / 255 끝)을 움직임 지속 구간 막대로,
            # 코드 '3'·'4' 는 서로 다른 색 점으로. 그 외 코드는 미표시.
            path, csv_rows = _read("vibration_event", d)
            day["tracks"].append({
                "kind": "vibration_aq1",
                "intervals": tuple(display_extract.extract_vibration_aq1_intervals(csv_rows)),
                "points": tuple(display_extract.extract_vibration_aq1_points(csv_rows)),
                "has_csv": path.exists(),
                "job_status": job_status.get((d, "vibration_event")),
                "aux_has_csv": True,
                "aux_job_status": None,
            })

        elif device_type_key in ("temp_humi_t1", "temp_humi_wm"):
            # DISPLAY.md §4.6 — 양 hub / Watts Matter 모두 동일 컬럼(temperature_value/humidity_value).
            # 점/막대가 아닌 line plot 으로 표시: 트랙 안에 온도(red)·습도(blue) 두 polyline
            # 을 이중축으로 그린다 (좌측=온도, 우측=습도, 자동 Y 범위).
            path, csv_rows = _read("temp_humi", d)
            day["tracks"].append({
                "kind": "temp_humi_plot",
                "intervals": (),
                "points": (),
                "series": display_extract.extract_temp_humi_series(csv_rows),
                "has_csv": path.exists(),
                "job_status": job_status.get((d, "temp_humi")),
                "aux_has_csv": True,
                "aux_job_status": None,
            })

        else:
            # 신규 device_type이 DEVICE.md에 추가되었으나 DISPLAY.md §4에 시각화 규칙이
            # 아직 정의되지 않은 경우의 안전 placeholder.
            day["tracks"].append({
                "kind": "unsupported",
                "intervals": (),
                "points": (),
                "has_csv": False,
                "job_status": None,
                "aux_has_csv": True,
                "aux_job_status": None,
            })

        rows.append(day)
    return rows


def _fetch_group(group_id: int) -> dict:
    """device_groups 행 조회. 없으면 404."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM device_groups WHERE id=?", (group_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="존재하지 않는 그룹입니다.")
    return dict(row)


def _fetch_group_members(group_id: int) -> list[dict]:
    """그룹의 활성 멤버 디바이스 목록. DISPLAY.md §4.8 정렬: device_type → alias → device_id."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM devices
                WHERE group_id=? AND deleted_at IS NULL
                ORDER BY device_type, alias, device_id""",
            (group_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _merge_panel_for_type(
    devs: list[dict], dates_desc: list[str]
) -> list[dict]:
    """같은 device_type 멤버들의 day_rows 를 하루 단위로 합집합 병합 (DISPLAY.md §4.8).

    상태머신 의미 보존을 위해 raw 이벤트가 아닌 멤버별 interval/point 산출 결과를 union 한다.
    반환: list[dict] (per date) — 기존 _device_timeline.html partial 이 기대하는 day_rows 형태.
    """
    if not devs:
        return []
    # 멤버별 day_rows 계산 (재사용: _build_day_rows). 결과는 list[list[dict]] (devs × dates).
    per_device_rows: list[list[dict]] = [_build_day_rows(dev, dates_desc) for dev in devs]

    merged_rows: list[dict] = []
    weekday_ko_table = ["월", "화", "수", "목", "금", "토", "일"]
    today_str = _today_kst().strftime("%Y-%m-%d")

    # plot 시계열 자동 Y 범위 — 단일 디바이스 추출기와 동일 정책 (빈/단일 점 패딩).
    def _auto_range(vals: list[float], default_lo: float, default_hi: float) -> tuple[float, float]:
        if not vals:
            return default_lo, default_hi
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-9:
            return lo - 1.0, hi + 1.0
        return lo, hi

    for i, date_str in enumerate(dates_desc):
        intervals_buf: list[display_extract.Interval] = []
        points_buf: list[display_extract.PointEvent] = []
        # plot 시계열 합산 버퍼 — 멤버별 점을 모두 모아 일자별로 시각순 정렬한다 (단일 디바이스
        # plot 과 동일한 자동 Y 범위). st_motion=lux, temp_humi_plot=temperature/humidity.
        lux_pts_buf: list[tuple[str, float]] = []
        temp_pts_buf: list[tuple[str, float]] = []
        humi_pts_buf: list[tuple[str, float]] = []
        any_csv = False
        kinds: set[str] = set()
        statuses: list[str] = []
        for k in range(len(devs)):
            day = per_device_rows[k][i]
            if not day["tracks"]:
                continue
            tr = day["tracks"][0]
            kinds.add(tr["kind"])
            intervals_buf.extend(tr["intervals"])
            points_buf.extend(tr["points"])
            any_csv = any_csv or tr["has_csv"]
            if tr["job_status"]:
                statuses.append(tr["job_status"])
            # 멤버가 st_motion 이면 lux_series 의 점을 합산.
            mlx = tr.get("lux_series") if isinstance(tr, dict) else None
            if mlx and mlx.get("lux"):
                lux_pts_buf.extend(mlx["lux"])
            # 멤버가 temp_humi_plot 이면 series 의 temperature/humidity 점을 합산.
            mts = tr.get("series") if isinstance(tr, dict) else None
            if mts:
                if mts.get("temperature"):
                    temp_pts_buf.extend(mts["temperature"])
                if mts.get("humidity"):
                    humi_pts_buf.extend(mts["humidity"])
        merged_intervals = tuple(display_extract.union_intervals(intervals_buf))
        merged_points = tuple(display_extract.merge_points(points_buf))
        # kind는 같은 device_type 패널이므로 단일 값 (다르면 'unsupported' 폴백).
        kind = next(iter(kinds)) if len(kinds) == 1 else "unsupported"
        # job_status 우선순위: failed > success > None.
        if "failed" in statuses:
            job_status = "failed"
        elif "success" in statuses:
            job_status = "success"
        else:
            job_status = None

        track_out: dict = {
            "kind": kind,
            "intervals": merged_intervals,
            "points": merged_points,
            "has_csv": any_csv,
            "job_status": job_status,
            "aux_has_csv": True,
            "aux_job_status": None,
        }
        # 합산 plot 시계열을 트랙 dict 에 채워 partial 의 polyline 블록이 그려지게 한다.
        # st_motion → lux_series, temp_humi_plot → series. 정렬 + 자동 Y 범위는 단일 디바이스와 동일.
        if kind == "st_motion":
            lux_pts_buf.sort(key=lambda x: x[0])
            lo, hi = _auto_range([v for _, v in lux_pts_buf], 0.0, 100.0)
            track_out["lux_series"] = {"lux": lux_pts_buf, "lux_min": lo, "lux_max": hi}
        elif kind == "temp_humi_plot":
            temp_pts_buf.sort(key=lambda x: x[0])
            humi_pts_buf.sort(key=lambda x: x[0])
            t_lo, t_hi = _auto_range([v for _, v in temp_pts_buf], 0.0, 30.0)
            h_lo, h_hi = _auto_range([v for _, v in humi_pts_buf], 0.0, 100.0)
            track_out["series"] = {
                "temperature": temp_pts_buf,
                "humidity": humi_pts_buf,
                "temp_min": t_lo, "temp_max": t_hi,
                "humi_min": h_lo, "humi_max": h_hi,
            }

        wd = datetime.strptime(date_str, "%Y-%m-%d").weekday()
        merged_rows.append({
            "date": date_str,
            "weekday_ko": weekday_ko_table[wd],
            "is_today": (date_str == today_str),
            "tracks": [track_out],
        })
    return merged_rows


def _build_group_panels(members: list[dict], dates_desc: list[str]) -> list[dict]:
    """그룹 멤버를 device_type 별로 묶어 각 종류당 1개 패널 생성 (DISPLAY.md §4.8).

    panels[i] = {
      'type_key': str,
      'device_type': DeviceType | None,   # 알 수 없는 종류는 None
      'devices': list[dict],              # 같은 종류 멤버 (alias→device_id 순)
      'day_rows': list[dict],             # _device_timeline partial 이 기대하는 구조 (합산 결과)
    }
    """
    # device_type 키 → 멤버 리스트
    by_type: dict[str, list[dict]] = {}
    for dev in members:
        by_type.setdefault(dev["device_type"], []).append(dev)

    panels: list[dict] = []
    for type_key in sorted(by_type.keys()):
        devs = sorted(by_type[type_key], key=lambda d: (d.get("alias") or "", d["device_id"]))
        dt = DEVICE_TYPES.get(type_key)
        if dt is None:
            # 알 수 없는 종류는 stub 패널만 노출 (시각화 없음).
            panels.append({
                "type_key": type_key, "device_type": None,
                "devices": devs, "day_rows": [],
            })
            continue
        day_rows = _merge_panel_for_type(devs, dates_desc)
        panels.append({
            "type_key": type_key,
            "device_type": dt,
            "devices": devs,
            "day_rows": day_rows,
        })
    return panels


@router.get("/display/group/{group_id}", response_class=HTMLResponse)
def display_group_page(
    request: Request,
    group_id: int,
    to: str | None = Query(None, description="표시 기간 마지막 일자 YYYY-MM-DD KST. 기본=오늘."),
    days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS, description="표시 일수 1~7. 기본=7."),
):
    """그룹 디스플레이 — 같은 device_type 멤버는 1개 패널로 합산, 다른 종류는 별도 패널 (DISPLAY.md §4.8)."""
    if current_user(request) is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    group = _fetch_group(group_id)
    members = _fetch_group_members(group_id)

    today = _today_kst()
    if to is None:
        to_date = today
    else:
        try:
            to_date = datetime.strptime(to, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="to 는 YYYY-MM-DD 형식이어야 합니다.")
        if to_date > today:
            raise HTTPException(status_code=400, detail="to 는 오늘(KST)보다 미래일 수 없습니다.")

    # DISPLAY.md §3: 오름차순(과거→최신) — 위쪽 행 = 과거.
    dates_asc = display_extract.date_range_ascending(to_date.strftime("%Y-%m-%d"), days)
    panels = _build_group_panels(members, dates_asc)

    ctx = {
        "request": request,
        "user": current_user(request),
        "active_alerts": alerts.list_active(),
        "group": group,
        "members": members,   # 헤더의 멤버 수 표시용
        "panels": panels,
        "to": to_date.strftime("%Y-%m-%d"),
        "today": today.strftime("%Y-%m-%d"),
        "days": days,
        "max_days": MAX_DAYS,
        "svg_width": 1000,
        "svg_height": 28,
        "seconds_per_day": 86400,
    }
    return templates.TemplateResponse(request, "display_group.html", ctx)


@router.get("/display/{device_id}", response_class=HTMLResponse)
def display_page(
    request: Request,
    device_id: str,
    to: str | None = Query(None, description="표시 기간 마지막 일자 YYYY-MM-DD KST. 기본=오늘."),
    days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS, description="표시 일수 1~7. 기본=7."),
):
    """디바이스 활동 타임라인 (DISPLAY.md §2)."""
    # 비로그인은 /login 리다이렉트 (HTML UX 일관성, /data/{d}/{b} 와 동일 패턴)
    if current_user(request) is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    # _fetch_device 는 hub-agnostic 으로 두 정규화 후보 모두 조회 (Aqara/SmartThings 공용 URL).
    # 이후 device["device_id"] 가 DB 에 저장된 정규화 값 — collection_jobs / CSV 경로 조회는 그 값을 그대로 사용.
    device = _fetch_device(device_id)

    today = _today_kst()
    if to is None:
        to_date = today
    else:
        try:
            to_date = datetime.strptime(to, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="to 는 YYYY-MM-DD 형식이어야 합니다.")
        if to_date > today:
            raise HTTPException(status_code=400, detail="to 는 오늘(KST)보다 미래일 수 없습니다.")

    # DISPLAY.md §3: 오름차순(과거→최신) — 위쪽 행 = 과거.
    dates_asc = display_extract.date_range_ascending(to_date.strftime("%Y-%m-%d"), days)
    day_rows = _build_day_rows(device, dates_asc)
    dt = DEVICE_TYPES[device["device_type"]]

    # 첫 번째 bundle_key — "파일 보기" 보조 링크 대상 (DISPLAY.md §1, §8)
    from ..devices import bundles_for as _bundles_for
    hub = device.get("hub") or "aqara"
    primary_bundles = _bundles_for(device["device_type"], hub)
    primary_bundle_key = primary_bundles[0].key if primary_bundles else None

    ctx = {
        "request": request,
        "user": current_user(request),
        "active_alerts": alerts.list_active(),
        "device": device,
        "device_type": dt,
        "day_rows": day_rows,
        "to": to_date.strftime("%Y-%m-%d"),
        # 폼의 <input type="date" max=...>에 사용. 항상 오늘(KST)이어야 사용자가
        # 과거→미래 양방향으로 탐색할 수 있다 (이전 'max=to' 버그 수정).
        "today": today.strftime("%Y-%m-%d"),
        "days": days,
        "max_days": MAX_DAYS,
        "primary_bundle_key": primary_bundle_key,
        # SVG 좌표 상수 (템플릿에서 사용)
        "svg_width": 1000,
        "svg_height": 28,
        "seconds_per_day": 86400,
    }
    return templates.TemplateResponse(request, "display.html", ctx)
