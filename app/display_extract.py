"""일자별 CSV → 타임라인 interval/point 추출 (DISPLAY.md §4 SSOT).

순수 함수 모듈. FastAPI/Starlette 의존성 없음 — 단위 테스트가 독립적으로 가능하다.

핵심 규칙(DISPLAY.md §4):
- motion_t1/p1: motion_status==1 이벤트를 GAP_SEC(기본 90s) 임계로 그룹핑 → interval
- door_t1: magnet_status state machine (1=open → 0=close), 일자 경계 복원 필요
- vibration_t1: 단일 wide CSV(`move_knock`) — move_detect 컬럼은 state machine (1=active → 255=deactivated),
  knock_event 컬럼은 point (값이 채워진 행만 tick)
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Optional


def _parse_finite_float(s: str) -> Optional[float]:
    """문자열 → 유한 float. 파싱 실패·NaN·Inf 는 None.

    주의: Python `float('nan')`/`float('inf')` 는 ValueError 를 던지지 않으므로
    측정값 plot 좌표 계산이 깨지지 않도록 isfinite 검사를 명시적으로 한다.
    """
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None

# DISPLAY.md §4.1 기본값. 향후 디바이스별 분리 가능.
MOTION_GROUP_GAP_SEC = 90
MIN_BAR_SEC = 30


# ─────────────────────────── 자료형 ───────────────────────────

@dataclass(frozen=True)
class Interval:
    """일자 D 의 막대 한 개. start/end는 KST 'YYYY-MM-DD HH:MM:SS' 문자열.

    truncated_left/right: 원래 막대가 표시 일자의 0:00 또는 24:00에서 잘렸는지 여부
    (door/vibration의 일자 경계 복원에서 화살표 마커 표시에 사용).
    """
    start: str
    end: str
    truncated_left: bool = False
    truncated_right: bool = False
    event_count: int = 1   # motion 그룹핑 시 그룹에 들어간 이벤트 수


@dataclass(frozen=True)
class PointEvent:
    """단일 시각의 점 이벤트. KST 문자열.

    label: 호버 툴팁용. switch_t1/vibration_aq1처럼 같은 트랙에 여러 종류의 이벤트가
    섞일 때 코드별 한국어 라벨을 미리 채워둔다 (DISPLAY.md §4.4 / §4.5).
    빈 문자열이면 템플릿이 기본 라벨(예: "두드림")을 사용.

    color: 템플릿 SVG `stroke` 에 그대로 들어갈 CSS 색상 표현
    (예: 'var(--vibration-aq1-2-color)'). 빈 문자열이면 트랙 기본 tick 색상 사용.
    같은 트랙에서 이벤트 코드별로 색을 구분하기 위해 extract 함수가 채워준다
    (DISPLAY.md §4.5 진동 센서 코드별 색상 / §4.3 knock 값별 색상).
    """
    at: str
    label: str = ""
    color: str = ""


@dataclass(frozen=True)
class DayTrack:
    """일자 D 한 행에 그릴 데이터.

    date: 'YYYY-MM-DD' (KST)
    intervals: 막대 목록
    points: 점 이벤트 목록 (vibration knock 등)
    has_csv: 해당 일자 CSV가 실제로 존재했는가 (없으면 '수집 없음' 라벨)
    """
    date: str
    intervals: tuple[Interval, ...]
    points: tuple[PointEvent, ...]
    has_csv: bool


# ─────────────────────────── CSV 로더 ───────────────────────────

def _csv_path(data_dir: Path, bundle_key: str, device_id: str, date: str) -> Path:
    """data/{bundle_key}/{device_id}/{YYYYMMDD}_{suffix}.csv (DESIGN.md §4 / §15.7).

    suffix 는 hub 별로 다름 (자동 분기):
      - Aqara('lumi.<hex>')         → last6 (대문자 hex 끝 6자, 예: '752EDB')
      - SmartThings(UUID / 24-hex)  → 대시 제거 후 첫 8자 대문자 (예: '2D21B6A3')
    collector 가 파일을 쓸 때(`devices.device_id_suffix`)와 display 가 파일을 읽을 때
    동일 규칙을 보장하기 위해 `_csv_filename_suffix` 헬퍼에 위임.
    """
    yyyymmdd = date.replace("-", "")
    suffix = _csv_filename_suffix(device_id)
    return data_dir / bundle_key / device_id / f"{yyyymmdd}_{suffix}.csv"


def _csv_filename_suffix(device_id: str) -> str:
    """device_id → 파일명 suffix (DESIGN.md §15.7).

    `devices.device_id_suffix(device_id, hub)` 와 동일 규칙이지만 hub 를 device_id 형식으로
    자동 추정 (display 단계는 devices 행을 거치지 않고 raw device_id 만으로 CSV 경로를 만들기 때문).
    """
    s = device_id.strip()
    if s.lower().startswith("lumi."):
        return s[5:].upper()[-6:]
    return s.replace("-", "").lower()[:8].upper()


def device_id_last6(device_id: str) -> str:
    """'lumi.4cf8cdf3c752edb' → '752EDB'. devices.last6 와 동일한 결과 (Aqara 전용)."""
    s = device_id[5:] if device_id.lower().startswith("lumi.") else device_id
    return s.upper()[-6:]


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """CSV 행을 dict 리스트로 반환. '#'로 시작하는 메타 헤더는 스킵 (DEVICE.md §1.4 옵션 A).

    파일 부재 시 빈 리스트. 파싱 실패는 IOError로 전파 (호출자에서 처리).
    """
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        # '#'로 시작하지 않는 첫 줄이 컬럼 헤더, 그 이후는 데이터
        non_meta_lines = [line for line in f if not line.startswith("#")]
    if not non_meta_lines:
        return []
    reader = csv.DictReader(non_meta_lines)
    return [dict(row) for row in reader]


# ─────────────────────────── motion (T1/P1) ───────────────────────────

def extract_motion_intervals(
    rows: Iterable[dict[str, str]],
    gap_sec: int = MOTION_GROUP_GAP_SEC,
    min_bar_sec: int = MIN_BAR_SEC,
) -> list[Interval]:
    """motion_lux CSV 행에서 motion_status==1 이벤트를 GAP 임계로 그룹핑 (DISPLAY.md §4.1).

    - motion_status가 빈 칸(lux만 기록된 샘플)인 행은 무시 (DEVICE.md §1.4 wide 포맷).
    - 단일 이벤트 그룹은 폭 0이 되므로 min_bar_sec 만큼 보정.
    """
    times: list[datetime] = []
    for r in rows:
        ms = (r.get("motion_status") or "").strip()
        if ms != "1":
            continue
        try:
            t = datetime.strptime(r["time"].strip(), "%Y-%m-%d %H:%M:%S")
        except (KeyError, ValueError):
            continue
        times.append(t)
    times.sort()

    intervals: list[Interval] = []
    if not times:
        return intervals

    group_start = times[0]
    group_last = times[0]
    group_count = 1
    for t in times[1:]:
        if (t - group_last).total_seconds() <= gap_sec:
            group_last = t
            group_count += 1
        else:
            intervals.append(_make_motion_interval(group_start, group_last, group_count, min_bar_sec))
            group_start = t
            group_last = t
            group_count = 1
    intervals.append(_make_motion_interval(group_start, group_last, group_count, min_bar_sec))
    return intervals


def _make_motion_interval(start_dt: datetime, end_dt: datetime, count: int, min_bar_sec: int) -> Interval:
    """단일 이벤트 그룹은 min_bar_sec 보정. 일자 24:00을 넘지 않게 클램프."""
    if (end_dt - start_dt).total_seconds() < min_bar_sec:
        end_dt = start_dt + timedelta(seconds=min_bar_sec)
    # 일자 종료 클램프: 자정 직전 보정 막대가 다음 날로 넘어가지 않게.
    day_end = datetime.combine(start_dt.date(), time(23, 59, 59))
    truncated_right = False
    if end_dt > day_end:
        end_dt = day_end
        truncated_right = True
    return Interval(
        start=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        end=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        event_count=count,
        truncated_right=truncated_right,
    )


# ─────────────────────────── door (magnet_status) ───────────────────────────

def extract_door_intervals(
    rows_today: Iterable[dict[str, str]],
    rows_prev_day: Optional[Iterable[dict[str, str]]] = None,
    target_date: str = "",
) -> list[Interval]:
    """magnet_status 이벤트로 '열린 영역' 추출 (DISPLAY.md §4.2).

    - 1=열림, 0=닫힘 (DEVICE.md §2.2)
    - 직전 일자 CSV의 마지막 상태가 '열림'이고 닫힘이 없으면 target_date 0:00부터 열린 상태로 시작
    - 종료 미해지(열린 채 일자 종료): target_date 23:59:59로 막대 연장 + truncated_right
    """
    target = datetime.strptime(target_date, "%Y-%m-%d").date()
    day_start = datetime.combine(target, time(0, 0, 0))
    day_end = datetime.combine(target, time(23, 59, 59))

    # 1) 초기 상태 결정: 직전 일자 마지막 이벤트가 1(열림)이면 open=True로 시작
    initial_open = False
    if rows_prev_day is not None:
        last_val = _last_value(rows_prev_day, value_key="magnet_status")
        if last_val == "1":
            initial_open = True

    intervals: list[Interval] = []
    open_since: Optional[datetime] = day_start if initial_open else None
    open_truncated_left = initial_open

    for t, v in _sorted_time_value(rows_today, value_key="magnet_status"):
        if v == "1":
            if open_since is None:
                open_since = t
                open_truncated_left = False
        elif v == "0":
            if open_since is not None:
                intervals.append(Interval(
                    start=open_since.strftime("%Y-%m-%d %H:%M:%S"),
                    end=t.strftime("%Y-%m-%d %H:%M:%S"),
                    truncated_left=open_truncated_left,
                ))
                open_since = None
                open_truncated_left = False

    # 2) 일자 종료까지 닫히지 않은 막대 → 24:00으로 연장
    if open_since is not None:
        intervals.append(Interval(
            start=open_since.strftime("%Y-%m-%d %H:%M:%S"),
            end=day_end.strftime("%Y-%m-%d %H:%M:%S"),
            truncated_left=open_truncated_left,
            truncated_right=True,
        ))
    return intervals


# ─────────────────────────── water leak (leak_status) ───────────────────────────

def extract_leak_intervals(
    rows_today: Iterable[dict[str, str]],
    rows_prev_day: Optional[Iterable[dict[str, str]]] = None,
    target_date: str = "",
) -> list[Interval]:
    """leak_status 이벤트로 '누수 지속 영역' 추출 (DISPLAY.md §4.10).

    door_t1 (§4.2) 과 완전히 동일한 이진 상태머신 — 값 토큰만 다르다 (DEVICE.md §12):
    - 1=누수 시작, 0=정상 복귀(종료)
    - 직전 일자 CSV 마지막 값이 1(누수)이고 0(정상)이 없으면 target_date 0:00부터 누수 상태로 시작
    - 종료 미해지(누수 상태로 일자 종료): target_date 23:59:59로 막대 연장 + truncated_right
    """
    target = datetime.strptime(target_date, "%Y-%m-%d").date()
    day_start = datetime.combine(target, time(0, 0, 0))
    day_end = datetime.combine(target, time(23, 59, 59))

    # 1) 초기 상태 결정: 직전 일자 마지막 이벤트가 1(누수)이면 leak=True로 시작
    initial_leak = False
    if rows_prev_day is not None:
        last_val = _last_value(rows_prev_day, value_key="leak_status")
        if last_val == "1":
            initial_leak = True

    intervals: list[Interval] = []
    leak_since: Optional[datetime] = day_start if initial_leak else None
    leak_truncated_left = initial_leak

    for t, v in _sorted_time_value(rows_today, value_key="leak_status"):
        if v == "1":
            if leak_since is None:
                leak_since = t
                leak_truncated_left = False
        elif v == "0":
            if leak_since is not None:
                intervals.append(Interval(
                    start=leak_since.strftime("%Y-%m-%d %H:%M:%S"),
                    end=t.strftime("%Y-%m-%d %H:%M:%S"),
                    truncated_left=leak_truncated_left,
                ))
                leak_since = None
                leak_truncated_left = False

    # 2) 일자 종료까지 정상 복귀가 없는 막대 → 24:00으로 연장
    if leak_since is not None:
        intervals.append(Interval(
            start=leak_since.strftime("%Y-%m-%d %H:%M:%S"),
            end=day_end.strftime("%Y-%m-%d %H:%M:%S"),
            truncated_left=leak_truncated_left,
            truncated_right=True,
        ))
    return intervals


# ─────────────────────────── smart plug (plug_status) ───────────────────────────

def extract_plug_intervals(
    rows_today: Iterable[dict[str, str]],
    rows_prev_day: Optional[Iterable[dict[str, str]]] = None,
    target_date: str = "",
) -> list[Interval]:
    """plug_status 이벤트로 '켜짐 영역' 추출 (DISPLAY.md §4.9).

    door_t1 (§4.2) 과 동일한 이진 상태머신이되 aqara 스마트 플러그는 toggle 값이 추가된다:
    - 1=Open(켜짐) 시작, 0=Close(꺼짐) 종료 (DEVICE.md §11.2)
    - 2=Toggle 는 직전 상태를 반전 (켜짐이면 그 시점에 종료, 꺼짐이면 그 시점부터 시작)
    - 직전 일자 CSV 마지막 값이 1(켜짐)이고 0/2 로 꺼지지 않았으면 target_date 0:00 부터 켜짐으로 시작
    - 종료 미해지(켜진 채 일자 종료): target_date 23:59:59 로 막대 연장 + truncated_right
    - 직전 일자 마지막 값이 2(Toggle) 면 절대 상태 불명 → carry-over 하지 않음 (꺼짐 가정, DEVICE.md §11.4)
    """
    target = datetime.strptime(target_date, "%Y-%m-%d").date()
    day_start = datetime.combine(target, time(0, 0, 0))
    day_end = datetime.combine(target, time(23, 59, 59))

    # 1) 초기 상태 결정: 직전 일자 마지막 이벤트가 1(켜짐)이면 on=True 로 시작.
    #    toggle(2) 로 끝났으면 절대 상태를 알 수 없어 꺼짐으로 가정 (carry-over 안 함).
    initial_on = False
    if rows_prev_day is not None:
        last_val = _last_value(rows_prev_day, value_key="plug_status")
        if last_val == "1":
            initial_on = True

    intervals: list[Interval] = []
    on_since: Optional[datetime] = day_start if initial_on else None
    on_truncated_left = initial_on

    def _close(end_dt: datetime) -> None:
        """현재 켜짐 구간을 end_dt 에서 종료해 interval 로 확정."""
        nonlocal on_since, on_truncated_left
        intervals.append(Interval(
            start=on_since.strftime("%Y-%m-%d %H:%M:%S"),
            end=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            truncated_left=on_truncated_left,
        ))
        on_since = None
        on_truncated_left = False

    for t, v in _sorted_time_value(rows_today, value_key="plug_status"):
        if v == "1":
            if on_since is None:
                on_since = t
                on_truncated_left = False
        elif v == "0":
            if on_since is not None:
                _close(t)
        elif v == "2":
            # 토글: 켜짐↔꺼짐 반전.
            if on_since is not None:
                _close(t)
            else:
                on_since = t
                on_truncated_left = False

    # 2) 일자 종료까지 꺼지지 않은 막대 → 24:00 으로 연장.
    if on_since is not None:
        intervals.append(Interval(
            start=on_since.strftime("%Y-%m-%d %H:%M:%S"),
            end=day_end.strftime("%Y-%m-%d %H:%M:%S"),
            truncated_left=on_truncated_left,
            truncated_right=True,
        ))
    return intervals


def extract_st_switch_intervals(
    rows_today: Iterable[dict[str, str]],
    rows_prev_day: Optional[Iterable[dict[str, str]]] = None,
    target_date: str = "",
) -> list[Interval]:
    """SmartThings switch capability ('on'/'off') 로 켜짐 영역 추출 (DISPLAY.md §4.9 SmartThings 분기).

    Aqara plug_status 1/0 페어와 의미 동일 (on↔1, off↔0). 컬럼명은 'switch'.
    SmartThings 에는 toggle 값이 없어 door 의 contact 와 완전히 같은 페어 패턴이다.
    """
    return _extract_st_paired_intervals(
        rows_today, rows_prev_day, target_date,
        value_key="switch", start_token="on", end_token="off",
    )


# ─────────────────────────── vibration ───────────────────────────

def extract_move_intervals(
    rows_today: Iterable[dict[str, str]],
    rows_prev_day: Optional[Iterable[dict[str, str]]] = None,
    target_date: str = "",
) -> list[Interval]:
    """move_knock CSV의 `move_detect` 컬럼으로 '움직임 영역' 추출 (DISPLAY.md §4.3).

    - 1=Activated 시작, 255=Deactivated 해지 (DEVICE.md §4.2)
    - `move_detect` 컬럼이 빈 칸인 행(knock_event만 있는 샘플)은 무시 (wide outer join 결과)
    - door와 동일한 일자 경계 복원 로직 (직전 일자 마지막이 1이고 255가 없으면 0:00부터 active)
    """
    target = datetime.strptime(target_date, "%Y-%m-%d").date()
    day_start = datetime.combine(target, time(0, 0, 0))
    day_end = datetime.combine(target, time(23, 59, 59))

    initial_active = False
    if rows_prev_day is not None:
        last_val = _last_value(rows_prev_day, value_key="move_detect")
        if last_val == "1":
            initial_active = True

    intervals: list[Interval] = []
    active_since: Optional[datetime] = day_start if initial_active else None
    active_truncated_left = initial_active

    for t, v in _sorted_time_value(rows_today, value_key="move_detect"):
        if v == "1":
            if active_since is None:
                active_since = t
                active_truncated_left = False
        elif v == "255":
            if active_since is not None:
                intervals.append(Interval(
                    start=active_since.strftime("%Y-%m-%d %H:%M:%S"),
                    end=t.strftime("%Y-%m-%d %H:%M:%S"),
                    truncated_left=active_truncated_left,
                ))
                active_since = None
                active_truncated_left = False

    if active_since is not None:
        intervals.append(Interval(
            start=active_since.strftime("%Y-%m-%d %H:%M:%S"),
            end=day_end.strftime("%Y-%m-%d %H:%M:%S"),
            truncated_left=active_truncated_left,
            truncated_right=True,
        ))
    return intervals


def extract_knock_points(rows: Iterable[dict[str, str]]) -> list[PointEvent]:
    """move_knock CSV의 `knock_event` 컬럼이 채워진 행을 point event로 (DISPLAY.md §4.3 tick mark).

    wide outer join 결과에서 knock_event 컬럼이 빈 칸인 행(move_detect만 있는 샘플)은 무시.
    knock_event 값 코드(예: 1=두드림 ON, 255=해지)별로 tick 색상을 구분 (DISPLAY.md §4.3).
    """
    points: list[PointEvent] = []
    for r in rows:
        t = (r.get("time") or "").strip()
        ke = (r.get("knock_event") or "").strip()
        if not t or not ke:
            continue
        # 형식 검증 (잘못된 행은 스킵)
        try:
            datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        label = KNOCK_VALUE_LABELS.get(ke, f"두드림 (코드={ke})")
        # CSS 변수가 정의된 값만 코드별 색상 사용. 그 외는 빈 문자열로 둬서 트랙 기본색 fallback.
        color = f"var(--knock-{ke}-color)" if ke in KNOCK_VALUE_LABELS else ""
        points.append(PointEvent(at=t, label=label, color=color))
    return points


# DISPLAY.md §4.3 — knock_event 값 코드별 한국어 라벨 (UpgoPlus 관측치 기반).
KNOCK_VALUE_LABELS: dict[str, str] = {
    "1":   "두드림 ON",
    "255": "두드림 해지",
}


# ─────────────────────────── 공용 헬퍼 ───────────────────────────

def _sorted_time_value(rows: Iterable[dict[str, str]], value_key: str) -> list[tuple[datetime, str]]:
    """rows에서 (time, value) 페어를 시각 오름차순으로 추출. 잘못된 행은 스킵."""
    out: list[tuple[datetime, str]] = []
    for r in rows:
        t_s = (r.get("time") or "").strip()
        v = (r.get(value_key) or "").strip()
        if not t_s or not v:
            continue
        try:
            dt = datetime.strptime(t_s, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        out.append((dt, v))
    out.sort(key=lambda x: x[0])
    return out


def _last_value(rows: Iterable[dict[str, str]], value_key: str) -> Optional[str]:
    """rows를 시각 오름차순 정렬 후 마지막 value 반환 (없으면 None)."""
    pairs = _sorted_time_value(rows, value_key=value_key)
    return pairs[-1][1] if pairs else None


def _prev_last_float(
    rows_prev_day: Optional[Iterable[dict[str, str]]], value_key: str
) -> Optional[float]:
    """전날 rows 에서 value_key 컬럼의 마지막 유한 float 값 (없으면 None).

    line plot 일자 경계 연장(00:00 시작점)에서 직전 관측값을 가져올 때 사용 (DISPLAY.md §4.6/§4.9).
    """
    if rows_prev_day is None:
        return None
    s = _last_value(rows_prev_day, value_key=value_key)
    return _parse_finite_float(s) if s else None


def _carry_boundary_points(
    series: list[tuple[str, float]],
    target_date: str,
    has_next_day: bool,
    prev_last: Optional[float],
) -> None:
    """line plot 시계열을 일자 양끝(00:00 / 24:00)으로 연장 (in-place, DISPLAY.md §4.6/§4.9).

    - 전날 마지막값(prev_last)이 있으면 오늘 `00:00:00` 점으로 prepend (이미 00:00 샘플이 있으면 생략) —
      직전 관측값이 자정까지 유지되었다고 간주해 선이 좌측 끝에서 시작하게 한다.
    - 다음날 데이터가 있으면(has_next_day) 오늘 마지막값을 `23:59:59`(=24:00) 점으로 append —
      선이 우측 끝까지 이어지게 한다. 다음날 파일이 없으면(당일 등) 연장하지 않는다.
    """
    day_start = f"{target_date} 00:00:00"
    day_end = f"{target_date} 23:59:59"
    if prev_last is not None and (not series or series[0][0] > day_start):
        series.insert(0, (day_start, prev_last))
    if has_next_day and series and series[-1][0] < day_end:
        series.append((day_end, series[-1][1]))


# ─────────────────────────── X축 좌표 변환 ───────────────────────────

def kst_str_to_seconds_of_day(kst_str: str) -> int:
    """'YYYY-MM-DD HH:MM:SS' → 0:00부터의 초 (0~86399).

    SVG X축 매핑에 사용. 24:00 (=86400)은 23:59:59=86399로 클램프해 사용.
    """
    dt = datetime.strptime(kst_str, "%Y-%m-%d %H:%M:%S")
    return dt.hour * 3600 + dt.minute * 60 + dt.second


def date_range_ascending(to_date: str, days: int) -> list[str]:
    """[to-(days-1), to] KST 일자를 오름차순(과거→최신) YYYY-MM-DD 리스트로 반환 (DISPLAY.md §3).

    위쪽 행 = 과거, 아래쪽 행 = 최신. (이전엔 내림차순이었으나 사용자 요청으로 변경.)
    """
    end = datetime.strptime(to_date, "%Y-%m-%d").date()
    return [(end - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]


# ─────────────────────────── 그룹 합산 헬퍼 (DISPLAY.md §4.8) ───────────────────────────

def union_intervals(intervals: list[Interval]) -> list[Interval]:
    """여러 interval 을 시간상 합집합으로 병합하는 순수 유틸.

    NOTE: DISPLAY.md §4.8 그룹 화면이 union(단일 색) → 멤버 색 오버레이로 바뀌면서
    그룹 병합 경로(routes/display.py `_merge_panel_for_type`)는 더 이상 이 함수를 호출하지
    않는다. 향후 "어느 한 멤버라도 active" 합산 뷰가 필요할 때 재사용 가능하도록 유지.


    같은 그룹·같은 device_type 멤버들이 각자 산출한 interval 들을 받아
    겹치거나 인접한 구간을 하나로 합친다. "어느 한 멤버라도 active이면 active" 의미.

    - 시각 문자열 'YYYY-MM-DD HH:MM:SS' 는 lexicographic = chronological 이므로 문자열 비교 가능.
    - event_count 는 병합되는 모든 원본 interval 의 합.
    - truncated_left/right 는 어느 하나라도 set 이면 set (경계 화살표 보존).
    - 빈 입력은 빈 리스트 반환.
    """
    if not intervals:
        return []
    sorted_iv = sorted(intervals, key=lambda iv: (iv.start, iv.end))
    merged: list[Interval] = []
    cur = sorted_iv[0]
    for nxt in sorted_iv[1:]:
        # 시작이 현재 끝과 같거나 그 이전이면 병합 (인접/겹침).
        if nxt.start <= cur.end:
            cur = Interval(
                start=cur.start,
                end=max(cur.end, nxt.end),
                truncated_left=cur.truncated_left or nxt.truncated_left,
                truncated_right=cur.truncated_right or nxt.truncated_right,
                event_count=cur.event_count + nxt.event_count,
            )
        else:
            merged.append(cur)
            cur = nxt
    merged.append(cur)
    return merged


def merge_points(points: list[PointEvent]) -> list[PointEvent]:
    """여러 point 이벤트를 시각 오름차순으로 concat 정렬하는 순수 유틸.

    각 point 의 label 은 그대로 유지 — dedup 하지 않음.
    NOTE: union_intervals 와 마찬가지로 §4.8 멤버 색 오버레이 전환 후 그룹 병합 경로에서는
    미사용. 합산 뷰 재도입 시 재사용 가능하도록 유지.
    """
    return sorted(points, key=lambda p: p.at)


# ───────────────────── SmartThings P2 motion + lux (DESIGN.md §15, DISPLAY.md §4 추가) ─────────────────────

def extract_st_motion_intervals(
    rows_today: Iterable[dict[str, str]],
    rows_prev_day: Optional[Iterable[dict[str, str]]] = None,
    target_date: str = "",
    min_bar_sec: int = MIN_BAR_SEC,
) -> list[Interval]:
    """SmartThings motion 'active'/'inactive' 페어로 움직임 영역 추출 (DISPLAY.md §4.1 SmartThings 분기).

    SmartThings 의 motion capability 는 *상태 변화 시점만* 이벤트로 보낸다.
    즉 사용자가 들어오면 'active', 일정 시간 움직임이 없으면 'inactive' 가 1회씩 기록되며
    그 사이 시간에는 별도 샘플이 없다. 따라서 'active 시작 → inactive 종료' 페어 사이
    구간을 active 상태로 간주하고 그 구간만 막대로 그린다 (Aqara motion_t1 의 gap-grouping 과는 다른 방식).

    규칙:
      - 값 'active' (대소문자 무관) → active 진입 (active_since 설정)
      - 값 'inactive' → active 종료 (interval 확정)
      - motion 컬럼이 빈 칸인 행 (lux 만 보고된 wide outer join 결과) → 무시
      - 짝 없는 inactive (active_since=None 상태) → 안전 스킵
      - 일자 종료까지 inactive 가 안 오면 23:59:59 까지 연장 + truncated_right
      - 직전 일자 마지막 motion 값이 'active' 이면 target_date 00:00 부터 active 로 시작 (truncated_left)

    너무 짧은 active 페어(수 초)는 SVG 폭 1픽셀 미만이 되어 화면에서 사라지므로
    Aqara motion_t1 과 동일한 `min_bar_sec=30s` 폭 보정을 적용 (DISPLAY.md §4.1).
    """
    target = datetime.strptime(target_date, "%Y-%m-%d").date()
    day_start = datetime.combine(target, time(0, 0, 0))
    day_end = datetime.combine(target, time(23, 59, 59))

    initial_active = False
    if rows_prev_day is not None:
        last = _last_value(rows_prev_day, value_key="motion")
        if last and last.lower() == "active":
            initial_active = True

    intervals: list[Interval] = []
    active_since: Optional[datetime] = day_start if initial_active else None
    active_truncated_left = initial_active

    def _emit(start_dt: datetime, end_dt: datetime, trunc_left: bool, trunc_right: bool) -> Interval:
        """짧은 페어의 가시성 보정 + 일자 종료 클램프 (Aqara motion_t1 의 _make_motion_interval 과 동일 정책)."""
        if (end_dt - start_dt).total_seconds() < min_bar_sec:
            end_dt = start_dt + timedelta(seconds=min_bar_sec)
        if end_dt > day_end:
            end_dt = day_end
            trunc_right = True
        return Interval(
            start=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            end=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            truncated_left=trunc_left,
            truncated_right=trunc_right,
        )

    for t, v in _sorted_time_value(rows_today, value_key="motion"):
        v_low = v.lower()
        if v_low == "active":
            if active_since is None:
                active_since = t
                active_truncated_left = False
            # 이미 active 인데 또 active 가 와도 상태 유지 (의미상 중복 이벤트).
        elif v_low == "inactive":
            if active_since is not None:
                intervals.append(_emit(active_since, t, active_truncated_left, False))
                active_since = None
                active_truncated_left = False

    # 일자 종료까지 inactive 가 오지 않은 active 막대 → 23:59:59 로 연장
    if active_since is not None:
        intervals.append(_emit(active_since, day_end, active_truncated_left, True))
    return intervals


def _extract_st_paired_intervals(
    rows_today: Iterable[dict[str, str]],
    rows_prev_day: Optional[Iterable[dict[str, str]]],
    target_date: str,
    value_key: str,
    start_token: str,
    end_token: str,
    min_bar_sec: int = MIN_BAR_SEC,
) -> list[Interval]:
    """SmartThings 측 'start_token'/'end_token' 페어로 활성 영역 추출 (DISPLAY.md §4.2/§4.3).

    SmartThings 의 contact / acceleration / motion capability 는 공통적으로 *상태 변화 시점만*
    이벤트로 보내는 페어 패턴을 사용한다. 토큰만 다를 뿐 로직은 동일하므로
    여기서 일반화한다 (motion 은 별도 함수 `extract_st_motion_intervals` 유지 — 명세 가독성).

    규칙 (모든 SmartThings paired-state 추출 공통):
      - value_key 컬럼이 빈 칸인 행 (다른 컬럼만 보고된 wide outer join 결과) 은 무시.
      - 토큰 비교는 대소문자 무관.
      - 짝 없는 end_token (이미 종료된 상태에서 또 옴) 은 안전 스킵.
      - 일자 경계 복원: 직전 일자 마지막 값이 start_token 이면 target_date 00:00 부터 active 시작.
      - 일자 종료까지 end_token 이 없으면 23:59:59 까지 연장 (`truncated_right`).
      - 짧은 페어가 SVG 1픽셀 미만이 되지 않도록 `min_bar_sec` 폭 보정 (Aqara motion_t1 정책).
    """
    target = datetime.strptime(target_date, "%Y-%m-%d").date()
    day_start = datetime.combine(target, time(0, 0, 0))
    day_end = datetime.combine(target, time(23, 59, 59))
    start_lo = start_token.lower()
    end_lo = end_token.lower()

    initial_active = False
    if rows_prev_day is not None:
        last = _last_value(rows_prev_day, value_key=value_key)
        if last and last.lower() == start_lo:
            initial_active = True

    intervals: list[Interval] = []
    active_since: Optional[datetime] = day_start if initial_active else None
    active_truncated_left = initial_active

    def _emit(s: datetime, e: datetime, t_left: bool, t_right: bool) -> Interval:
        if (e - s).total_seconds() < min_bar_sec:
            e = s + timedelta(seconds=min_bar_sec)
        if e > day_end:
            e = day_end
            t_right = True
        return Interval(
            start=s.strftime("%Y-%m-%d %H:%M:%S"),
            end=e.strftime("%Y-%m-%d %H:%M:%S"),
            truncated_left=t_left,
            truncated_right=t_right,
        )

    for t, v in _sorted_time_value(rows_today, value_key=value_key):
        v_low = v.lower()
        if v_low == start_lo:
            if active_since is None:
                active_since = t
                active_truncated_left = False
        elif v_low == end_lo:
            if active_since is not None:
                intervals.append(_emit(active_since, t, active_truncated_left, False))
                active_since = None
                active_truncated_left = False

    if active_since is not None:
        intervals.append(_emit(active_since, day_end, active_truncated_left, True))
    return intervals


def extract_st_contact_intervals(
    rows_today: Iterable[dict[str, str]],
    rows_prev_day: Optional[Iterable[dict[str, str]]] = None,
    target_date: str = "",
) -> list[Interval]:
    """SmartThings contact capability ('open'/'closed') 로 열린 영역 추출 (DISPLAY.md §4.2 SmartThings 분기).

    Aqara magnet_status 1/0 페어와 의미 동일 (open↔1, closed↔0). 컬럼명은 'contact'.
    """
    return _extract_st_paired_intervals(
        rows_today, rows_prev_day, target_date,
        value_key="contact", start_token="open", end_token="closed",
    )


def extract_st_water_intervals(
    rows_today: Iterable[dict[str, str]],
    rows_prev_day: Optional[Iterable[dict[str, str]]] = None,
    target_date: str = "",
) -> list[Interval]:
    """SmartThings waterSensor.water ('wet'/'dry') 로 누수 영역 추출 (DISPLAY.md §4.10 SmartThings 분기).

    Aqara leak_status 1/0 페어와 의미 동일 (wet↔1, dry↔0). 컬럼명은 'water'.
    door 의 contact 와 완전히 같은 페어 패턴이다 (toggle 없음).
    """
    return _extract_st_paired_intervals(
        rows_today, rows_prev_day, target_date,
        value_key="water", start_token="wet", end_token="dry",
    )


def extract_st_acceleration_intervals(
    rows_today: Iterable[dict[str, str]],
    rows_prev_day: Optional[Iterable[dict[str, str]]] = None,
    target_date: str = "",
) -> list[Interval]:
    """SmartThings accelerationSensor.acceleration ('active'/'inactive') 로 움직임 영역 추출
    (DISPLAY.md §4.3 SmartThings 분기).

    Aqara move_detect 1/255 페어와 의미 동일 (active↔1, inactive↔255). 컬럼명은 'acceleration'.
    SmartThings 측에는 knock_event 표준 capability 가 없어 knock tick 은 표시하지 않는다.
    """
    return _extract_st_paired_intervals(
        rows_today, rows_prev_day, target_date,
        value_key="acceleration", start_token="active", end_token="inactive",
    )


def extract_st_illuminance_points(rows: Iterable[dict[str, str]]) -> list[PointEvent]:
    """(레거시) SmartThings lux 측정값을 point tick 으로 추출.

    Plot 형식(`extract_st_lux_series`)으로 표시 정책이 변경된 후에는 /display 트랙에서
    사용되지 않는다. 호환을 위해 함수만 유지.
    """
    points: list[PointEvent] = []
    for r in rows:
        t = (r.get("time") or "").strip()
        v = (r.get("lux") or "").strip()
        if not t or not v:
            continue
        try:
            datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        points.append(PointEvent(at=t, label=f"lux {v}"))
    return points


def extract_st_lux_series(rows: Iterable[dict[str, str]]) -> dict:
    """motion_lux CSV 의 lux 컬럼 → line plot 시계열 (DISPLAY.md §4.1).

    SmartThings 의 illuminanceMeasurement.illuminance 는 연속 측정값이므로 점/막대가 아닌
    polyline 으로 표시한다. motion active 막대와 같은 트랙에 겹쳐 그려진다.

    반환 dict (템플릿 SVG polyline 좌표 계산용):
      {
        'lux': [(time_str, float), ...],   # 시각 오름차순
        'lux_min': float, 'lux_max': float,  # 자동 Y 범위 (단일 점/동일값이면 ±1 패딩)
      }
    lux 컬럼이 빈 칸이거나 숫자 파싱 실패한 행은 스킵. 시계열이 비어도 키는 항상 존재.
    """
    pts: list[tuple[str, float]] = []
    for r in rows:
        t = (r.get("time") or "").strip()
        v = (r.get("lux") or "").strip()
        if not t or not v:
            continue
        try:
            datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        fv = _parse_finite_float(v)
        if fv is None:
            continue
        pts.append((t, fv))
    pts.sort(key=lambda x: x[0])
    vals = [v for _, v in pts]
    if not vals:
        lo, hi = 0.0, 100.0
    else:
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-9:   # 단일 점 또는 모두 동일 → polyline 이 트랙 중앙에 그려지게 패딩
            lo, hi = lo - 1.0, hi + 1.0
    return {"lux": pts, "lux_min": lo, "lux_max": hi}


# ─────────────────────────── switch_t1 (Wireless Mini Switch T1) ───────────────────────────

# DEVICE.md §5.2 / DISPLAY.md §4.4 — 단발 이벤트 코드 → 한국어 라벨 매핑.
SWITCH_POINT_LABELS = {
    "1":  "1번 클릭",
    "2":  "2번 클릭",
    "3":  "3번 클릭",
    "18": "흔들림",
}


def extract_switch_long_intervals(
    rows_today: Iterable[dict[str, str]],
    rows_prev_day: Optional[Iterable[dict[str, str]]] = None,
    target_date: str = "",
) -> list[Interval]:
    """switch_status 이벤트로 '롱 프레스 영역' 추출 (DISPLAY.md §4.4).

    - 16=long_click_press 시작, 17=long_click_release 해지 (DEVICE.md §5.2)
    - door/vibration과 동일한 일자 경계 복원 패턴
    """
    target = datetime.strptime(target_date, "%Y-%m-%d").date()
    day_start = datetime.combine(target, time(0, 0, 0))
    day_end = datetime.combine(target, time(23, 59, 59))

    initial_pressed = False
    if rows_prev_day is not None:
        # 직전 일자에서 마지막으로 발생한 16/17 이벤트만 본다 (다른 코드는 무시).
        last_long = _last_value_in(rows_prev_day, value_key="switch_status", filter_values=("16", "17"))
        if last_long == "16":
            initial_pressed = True

    intervals: list[Interval] = []
    pressed_since: Optional[datetime] = day_start if initial_pressed else None
    pressed_truncated_left = initial_pressed

    for t, v in _sorted_time_value(rows_today, value_key="switch_status"):
        if v == "16":
            if pressed_since is None:
                pressed_since = t
                pressed_truncated_left = False
        elif v == "17":
            if pressed_since is not None:
                intervals.append(Interval(
                    start=pressed_since.strftime("%Y-%m-%d %H:%M:%S"),
                    end=t.strftime("%Y-%m-%d %H:%M:%S"),
                    truncated_left=pressed_truncated_left,
                ))
                pressed_since = None
                pressed_truncated_left = False
        # 1/2/3/18 등 다른 코드는 별도 함수(extract_switch_point_events)에서 처리

    if pressed_since is not None:
        intervals.append(Interval(
            start=pressed_since.strftime("%Y-%m-%d %H:%M:%S"),
            end=day_end.strftime("%Y-%m-%d %H:%M:%S"),
            truncated_left=pressed_truncated_left,
            truncated_right=True,
        ))
    return intervals


def extract_switch_point_events(rows: Iterable[dict[str, str]]) -> list[PointEvent]:
    """switch_status에서 단발 이벤트(1/2/3/18)를 라벨링된 PointEvent 리스트로 (DISPLAY.md §4.4).

    16/17은 롱 프레스 페어로 별도 처리되므로 여기서는 제외.
    """
    points: list[PointEvent] = []
    for r in rows:
        t = (r.get("time") or "").strip()
        v = (r.get("switch_status") or "").strip()
        if not t or v not in SWITCH_POINT_LABELS:
            continue
        try:
            datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        points.append(PointEvent(at=t, label=SWITCH_POINT_LABELS[v]))
    return points


# ─────────────────────────── vibration_aq1 (Vibration Sensor aq1) ───────────────────────────

# DEVICE.md §6.2 / DISPLAY.md §4.5 — vibration_event 코드별 한국어 라벨.
VIBRATION_AQ1_POINT_LABELS = {
    "0": "두드림(보안모드)",
    "1": "정지 후 트리거",
    "2": "기울임",
    "3": "자유낙하",
    "4": "닫힘 학습 완료",
    "5": "들어 올림",
    "6": "세 번 두드림",
}

# 움직임(Bar)으로 묶는 코드 — 1(시작)·2(기울임/진동)·255(해지) 는 모두 움직임 관련.
# 그 외 코드(0/3/4/5/6 등)는 기타 이벤트 점(tick)으로 표시 (DISPLAY.md §4.5, 사용자 정책).
VIBRATION_AQ1_MOVE_CODES = ("1", "2", "255")


def extract_vibration_aq1_intervals(
    rows: Iterable[dict[str, str]],
    gap_sec: int = MOTION_GROUP_GAP_SEC,
    min_bar_sec: int = MIN_BAR_SEC,
) -> list[Interval]:
    """vibration_event CSV 에서 '움직임 구간' 막대 추출 (DISPLAY.md §4.5).

    값 `1`·`2`·`255` 는 모두 움직임 관련 이벤트로 본다. 이 이벤트들을 시각 오름차순으로
    모아 인접 간격이 `gap_sec` 이내면 같은 막대로 그룹핑한다 (motion_t1 의 gap-grouping 과 동일).
    `0`·`3`·`4`·`5`·`6` 등 다른 코드는 막대에 포함되지 않는다 (점으로 별도 표시).

    단일 이벤트 그룹은 폭 0 이 되므로 `min_bar_sec` 보정. 일자 경계 복원은 하지 않는다.
    """
    times: list[datetime] = []
    for r in rows:
        v = (r.get("vibration_event") or "").strip()
        if v not in VIBRATION_AQ1_MOVE_CODES:
            continue
        try:
            t = datetime.strptime(r["time"].strip(), "%Y-%m-%d %H:%M:%S")
        except (KeyError, ValueError):
            continue
        times.append(t)
    times.sort()

    intervals: list[Interval] = []
    if not times:
        return intervals
    group_start = times[0]
    group_last = times[0]
    group_count = 1
    for t in times[1:]:
        if (t - group_last).total_seconds() <= gap_sec:
            group_last = t
            group_count += 1
        else:
            intervals.append(_make_motion_interval(group_start, group_last, group_count, min_bar_sec))
            group_start = t
            group_last = t
            group_count = 1
    intervals.append(_make_motion_interval(group_start, group_last, group_count, min_bar_sec))
    return intervals


def extract_vibration_aq1_points(rows: Iterable[dict[str, str]]) -> list[PointEvent]:
    """vibration_event CSV 에서 움직임 코드(1·2·255) 외 모든 값을 PointEvent 로 추출 (DISPLAY.md §4.5).

    `0`(두드림)·`3`(자유낙하)·`4`(닫힘 학습)·`5`(들어 올림)·`6`(세 번 두드림) 등 '기타 이벤트' 는
    코드별 색(`--vibration-aq1-N-color`)의 tick. 정의되지 않은 코드는 방어적 라벨 + 트랙 기본색.
    """
    points: list[PointEvent] = []
    for r in rows:
        t = (r.get("time") or "").strip()
        v = (r.get("vibration_event") or "").strip()
        if not t or not v or v in VIBRATION_AQ1_MOVE_CODES:
            continue
        try:
            datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        label = VIBRATION_AQ1_POINT_LABELS.get(v, f"이벤트 (코드={v})")
        color = f"var(--vibration-aq1-{v}-color)" if v in VIBRATION_AQ1_POINT_LABELS else ""
        points.append(PointEvent(at=t, label=label, color=color))
    return points


# ─────────────────────────── temp_humi_t1 (Temperature/Humidity Sensor T1) ───────────────────────────

def extract_temp_humi_points(rows: Iterable[dict[str, str]]) -> list[PointEvent]:
    """(레거시) temp_humi 측정 시각을 tick PointEvent 로 추출.

    Plot 형식(`extract_temp_humi_series`)으로 표시 정책이 변경된 후에는 더 이상
    /display 트랙에서 사용되지 않는다. 호환을 위해 함수만 유지.
    """
    points: list[PointEvent] = []
    for r in rows:
        t = (r.get("time") or "").strip()
        if not t:
            continue
        try:
            datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        temp = (r.get("temperature_value") or "").strip()
        if temp:
            points.append(PointEvent(at=t, label=f"T {temp}°C", color="var(--temp-tick-color)"))
        humi = (r.get("humidity_value") or "").strip()
        if humi:
            points.append(PointEvent(at=t, label=f"H {humi}%RH", color="var(--humi-tick-color)"))
    return points


def extract_temp_humi_series(
    rows: Iterable[dict[str, str]],
    rows_prev_day: Optional[Iterable[dict[str, str]]] = None,
    has_next_day: bool = False,
    target_date: str = "",
) -> dict:
    """temp_humi CSV → 온도/습도 line plot 시계열 데이터 (DISPLAY.md §4.6).

    각 시계열은 (time_str, value_float) 페어 리스트. 빈 컬럼·파싱 실패 행은 스킵.
    두 측정값의 보고 시각은 어긋날 수 있으므로 (DEVICE.md §8.1) 컬럼별로 따로 수집.

    일자 경계 연장 (DISPLAY.md §4.6, target_date 가 주어질 때 — 전력 plot §4.9 와 동일):
      - 전날(rows_prev_day) 데이터가 있으면 그 마지막 값을 오늘 `00:00:00` 점으로 prepend.
      - 다음날 데이터가 있으면(has_next_day) 오늘 마지막 값을 `23:59:59`(=24:00) 점으로 append.

    반환 dict 구조 (템플릿 SVG polyline 좌표 계산용):
      {
        'temperature': [(time_str, float), ...],   # 시각 오름차순
        'humidity':    [(time_str, float), ...],
        'temp_min': float, 'temp_max': float,      # 자동 Y 범위 (단일 점이면 ±1 으로 패딩)
        'humi_min': float, 'humi_max': float,
      }
    시계열이 비어 있어도 *_min/*_max 키는 항상 존재 (템플릿 분기 단순화).
    """
    temps: list[tuple[str, float]] = []
    humis: list[tuple[str, float]] = []
    for r in rows:
        t = (r.get("time") or "").strip()
        if not t:
            continue
        try:
            datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        temp_s = (r.get("temperature_value") or "").strip()
        if temp_s:
            fv = _parse_finite_float(temp_s)
            if fv is not None:
                temps.append((t, fv))
        humi_s = (r.get("humidity_value") or "").strip()
        if humi_s:
            fv = _parse_finite_float(humi_s)
            if fv is not None:
                humis.append((t, fv))
    temps.sort(key=lambda x: x[0])
    humis.sort(key=lambda x: x[0])

    # 일자 경계 연장 — 전날 마지막값 → 00:00, 오늘 마지막값 → 24:00 (DISPLAY.md §4.6).
    if target_date:
        _carry_boundary_points(
            temps, target_date, has_next_day,
            _prev_last_float(rows_prev_day, "temperature_value"),
        )
        _carry_boundary_points(
            humis, target_date, has_next_day,
            _prev_last_float(rows_prev_day, "humidity_value"),
        )

    def _range(vals: list[float], default_lo: float, default_hi: float) -> tuple[float, float]:
        if not vals:
            return (default_lo, default_hi)
        lo, hi = min(vals), max(vals)
        # 시계열 값이 한 점뿐이거나 모두 동일하면 ±1 으로 패딩해 polyline 이 트랙 중앙에 그려지게.
        if hi - lo < 1e-9:
            return (lo - 1.0, hi + 1.0)
        return (lo, hi)

    t_lo, t_hi = _range([v for _, v in temps], 0.0, 30.0)
    h_lo, h_hi = _range([v for _, v in humis], 0.0, 100.0)
    return {
        "temperature": temps,
        "humidity": humis,
        "temp_min": t_lo, "temp_max": t_hi,
        "humi_min": h_lo, "humi_max": h_hi,
    }


def extract_power_series(
    rows: Iterable[dict[str, str]],
    hub: str = "aqara",
    rows_prev_day: Optional[Iterable[dict[str, str]]] = None,
    has_next_day: bool = False,
    target_date: str = "",
) -> dict:
    """plug_status wide CSV 의 전력 컬럼 → 전력/누적에너지 line plot 시계열 (DISPLAY.md §4.9).

    temp_humi(§4.6) 와 동일한 이중축 line plot 구조:
      - load_power(W) : 좌측 축 실선
      - cost_energy(kWh): 우측 축 점선. **단위 통합은 여기서만** 수행 — aqara raw 는
        0.001kWh 정수 단위이므로 ×0.001 로 kWh 환산, smartthings 는 이미 kWh 라 그대로 (DEVICE.md §11).
    두 측정값의 보고 시각은 어긋날 수 있어(온습도와 동일) 컬럼별로 따로 수집한다.

    일자 경계 연장 (DISPLAY.md §4.9, target_date 가 주어질 때):
      - **전날(rows_prev_day) 데이터가 있으면** 그 마지막 값을 오늘 `00:00:00` 점으로 prepend
        해 선이 좌측 끝에서 시작하게 한다 (직전 관측값이 자정까지 유지되었다고 간주).
      - **다음날 데이터가 있으면(has_next_day)** 오늘 마지막 값을 `23:59:59`(=24:00) 점으로
        append 해 선이 우측 끝까지 이어지게 한다. 다음날 파일이 없으면(당일 등) 연장하지 않는다.

    반환 dict (템플릿 SVG polyline 좌표 계산용):
      {'power': [(time, W), ...], 'energy': [(time, kWh), ...],
       'power_min'/'power_max', 'energy_min'/'energy_max'}
    시계열이 비어도 *_min/*_max 키는 항상 존재.
    """
    # aqara cost_energy 만 0.001kWh 정수 단위 → kWh 환산 계수. smartthings 는 kWh 그대로.
    energy_scale = 0.001 if hub == "aqara" else 1.0

    powers: list[tuple[str, float]] = []
    energies: list[tuple[str, float]] = []
    for r in rows:
        t = (r.get("time") or "").strip()
        if not t:
            continue
        try:
            datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        p_s = (r.get("load_power") or "").strip()
        if p_s:
            fv = _parse_finite_float(p_s)
            if fv is not None:
                powers.append((t, fv))
        e_s = (r.get("cost_energy") or "").strip()
        if e_s:
            fv = _parse_finite_float(e_s)
            if fv is not None:
                energies.append((t, fv * energy_scale))
    powers.sort(key=lambda x: x[0])
    energies.sort(key=lambda x: x[0])

    # 일자 경계 연장 — 전날 마지막값을 00:00 으로, 오늘 마지막값을 24:00 으로 셋팅 (DISPLAY.md §4.9).
    if target_date:
        _carry_boundary_points(
            powers, target_date, has_next_day,
            _prev_last_float(rows_prev_day, "load_power"),
        )
        # cost_energy 는 화면 미표시지만 일관성을 위해 동일 처리 (raw → kWh 환산 후 carry).
        el = _prev_last_float(rows_prev_day, "cost_energy")
        _carry_boundary_points(
            energies, target_date, has_next_day,
            el * energy_scale if el is not None else None,
        )

    def _range(vals: list[float], default_lo: float, default_hi: float) -> tuple[float, float]:
        if not vals:
            return (default_lo, default_hi)
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-9:
            return (lo - 1.0, hi + 1.0)
        return (lo, hi)

    # 전력은 0 기준 스케일이 자연스러워 하한을 0 으로 고정(음수 없음), 상한만 자동.
    p_hi = max((v for _, v in powers), default=1.0)
    p_lo = 0.0
    if p_hi <= 0.0:
        p_hi = 1.0
    e_lo, e_hi = _range([v for _, v in energies], 0.0, 1.0)
    return {
        "power": powers,
        "energy": energies,
        "power_min": p_lo, "power_max": p_hi,
        "energy_min": e_lo, "energy_max": e_hi,
    }


# ─────────────────────────── 내부 헬퍼 (확장) ───────────────────────────

def _last_value_in(
    rows: Iterable[dict[str, str]],
    value_key: str,
    filter_values: tuple[str, ...],
) -> Optional[str]:
    """rows를 시각 오름차순 정렬 후, filter_values에 속한 값 중 마지막 값을 반환.

    switch_t1 일자 경계 복원에서 사용 — 16/17 외의 다른 단발 이벤트는 무시하고
    오직 롱 프레스 페어의 마지막 상태만 본다.
    """
    pairs = _sorted_time_value(rows, value_key=value_key)
    pairs = [(t, v) for t, v in pairs if v in filter_values]
    return pairs[-1][1] if pairs else None


# ─────────────────────────── 데이터 폴더 스캔 (DESIGN.md §7.3) ───────────────────────────
# 데이터 현황 화면이 collection_jobs 테이블 대신 파일시스템 walk로 집계할 때 사용.
# 자동 수집뿐 아니라 수동 import한 CSV도 자연스럽게 노출된다.

# 폴더 스캔에서 무시할 1차 디렉토리(원본/임시 등). data/ 직하의 하위 폴더 이름 기준.
_SCAN_IGNORED_DIRS = {"old", "tmp", "trash", "_backup"}


def read_row_count_meta(path: Path) -> int:
    """CSV 메타 헤더에서 '# row_count: N' 라인을 찾아 N 반환 (DEVICE.md §1.4 옵션 A).

    메타 헤더가 없거나 파싱 실패 시 0. 파일 부재 시 0.
    파일 전체를 읽지 않고 '#'로 시작하지 않는 첫 라인을 만나면 즉시 종료한다.
    """
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.startswith("#"):
                    break
                if line.startswith("# row_count:"):
                    try:
                        return int(line.split(":", 1)[1].strip())
                    except (ValueError, IndexError):
                        return 0
    except OSError:
        return 0
    return 0


def iter_data_files(data_dir: Path):
    """data/{bundle_key}/{device_id}/{YYYYMMDD}_{last6}.csv 파일을 모두 yield.

    yield 형식: (bundle_key: str, device_id: str, date: str 'YYYY-MM-DD', path: Path)
    파일명 패턴(YYYYMMDD_*.csv)에 맞지 않는 파일과 _SCAN_IGNORED_DIRS는 스킵.
    """
    if not data_dir.exists():
        return
    for bundle_dir in data_dir.iterdir():
        if not bundle_dir.is_dir() or bundle_dir.name in _SCAN_IGNORED_DIRS:
            continue
        bundle_key = bundle_dir.name
        for device_dir in bundle_dir.iterdir():
            if not device_dir.is_dir():
                continue
            device_id = device_dir.name
            for csv_path in device_dir.glob("*.csv"):
                stem = csv_path.stem  # 예: '20260510_829AED'
                if len(stem) < 8 or not stem[:8].isdigit():
                    continue
                date_str = f"{stem[:4]}-{stem[4:6]}-{stem[6:8]}"
                yield bundle_key, device_id, date_str, csv_path


def data_summary_from_filesystem(data_dir: Path) -> list[dict]:
    """data/ 전체를 walk하여 (device_id, bundle_key) 단위 집계 리스트 반환.

    각 항목 dict 키:
      device_id, bundle_key, file_count, total_bytes,
      first_date, last_date, total_records
    호출자(routes/pages.data_page)가 devices 테이블 정보(별명/종류 등)로 enrich한다.
    """
    summary: dict[tuple[str, str], dict] = {}
    for bundle_key, device_id, date_str, csv_path in iter_data_files(data_dir):
        try:
            size = csv_path.stat().st_size
        except OSError:
            size = 0
        rec_count = read_row_count_meta(csv_path)
        k = (device_id, bundle_key)
        if k not in summary:
            summary[k] = {
                "device_id": device_id,
                "bundle_key": bundle_key,
                "file_count": 0,
                "total_bytes": 0,
                "first_date": date_str,
                "last_date": date_str,
                "total_records": 0,
            }
        s = summary[k]
        s["file_count"] += 1
        s["total_bytes"] += size
        s["total_records"] += rec_count
        if date_str < s["first_date"]:
            s["first_date"] = date_str
        if date_str > s["last_date"]:
            s["last_date"] = date_str
    # 정렬: device_id, bundle_key
    return sorted(summary.values(), key=lambda x: (x["device_id"], x["bundle_key"]))
