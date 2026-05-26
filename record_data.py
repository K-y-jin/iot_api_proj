"""Aqara Open API 단발 수동 다운로드 스크립트 — Motion Sensor P1 기준.

`app/` 의 정식 수집 파이프라인과 별개로, 단일 디바이스의 특정 구간을 빠르게 받아
CSV 로 떨어뜨리고 싶을 때 쓰는 테스트/디버그용 스크립트. DEVICE.md / DESIGN.md 의
정식 포맷과 동일하게 출력하도록 갱신했다.

대상 디바이스: **Motion Sensor P1** (`lumi.motion.ac02`)
  - resource `motion_status` (`3.1.85`) — 재실 감지. 값 `1` 만 기록 (DEVICE.md §1.3 / §3.1).
  - resource `lux`           (`0.3.85`) — 조도. occupied 기간에만 샘플 생성.
저장 포맷: motion_lux bundle wide 포맷 (DEVICE.md §1.4)
  컬럼: `time, motion_status, lux` — 두 resource 의 timestamp 를 outer join.
  한쪽만 있는 시각의 결측 컬럼은 빈 문자열 (`,,`) 로 둔다.

비밀값(APPID/KEYID/APPKEY/ACCESS_TOKEN)은 환경변수에서만 로드한다.
미설정 시 명확히 실패 (CLAUDE.md §2.5 / §6 — 하드코딩 금지).
운영에서는 [app/](app) 의 정식 수집 사용.
"""

from __future__ import annotations

import csv
import hashlib
import os
import random
import time
from datetime import datetime, timedelta, timezone

import requests

# ─────────────────────────── 인증 / 엔드포인트 ───────────────────────────

API_URL = "https://open-kr.aqara.com/v3.0/open/api"
# 비밀값은 반드시 환경변수(.env)로 주입한다. 하드코딩 fallback 금지 (CLAUDE.md §2.5 / §6).
# 미설정 시 명확히 실패시켜 실수로 운영 토큰이 코드에 박히는 일을 차단.
def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"환경변수 {name} 가 설정되어 있지 않습니다. .env 또는 셸 환경에 설정 후 재실행하세요."
        )
    return val

APPID = _require_env("AQARA_APPID")
KEYID = _require_env("AQARA_KEYID")
APPKEY = _require_env("AQARA_APPKEY")
# Access Token (약 7일 갱신). https://developer.aqara.com/ → Console → Manage Project →
# Detail → Authorization management → Authorization details
ACCESS_TOKEN = _require_env("AQARA_ACCESS_TOKEN")

# Motion Sensor P1 의 두 resource ID (DEVICE.md §3.1 요약표).
RESOURCE_MOTION = "3.1.85"   # motion_status
RESOURCE_LUX    = "0.3.85"   # lux


# ─────────────────────────── 시간/ID 유틸 ───────────────────────────

def kst_to_utc_millis(kst_str: str) -> str:
    """'YYYY-MM-DD HH:MM:SS' (KST) → UTC millisecond timestamp 문자열.

    Aqara API 요청 본문의 startTime/endTime 은 UTC ms 문자열을 요구한다 (DESIGN.md §9).
    """
    kst = timezone(timedelta(hours=9))
    dt = datetime.strptime(kst_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=kst)
    return str(int(dt.astimezone(timezone.utc).timestamp() * 1000))


def normalize_subject_id(subject_id: str) -> str:
    """'4CF8CDF3C829AED' → 'lumi.4cf8cdf3c829aed'. 이미 lumi. prefix 있으면 소문자만 정리."""
    s = subject_id.strip()
    if s.lower().startswith("lumi."):
        return s.lower()
    return "lumi." + s.lower()


# ─────────────────────────── Aqara API 호출 ───────────────────────────

def get_history_of_device_attr(subjectId: str, resourceId: str, start_kst: str, end_kst: str):
    """단일 resource 의 시계열 1페이지 조회 (Aqara intent=fetch.resource.history).

    응답은 최대 100건. 페이지네이션은 호출부에서 endTime 을 batch 마지막 시각 + 1초로
    재설정해 반복 호출한다 (Aqara 는 최신 → 과거 내림차순으로 반환, DESIGN.md §6.1).
    """
    start_ms = kst_to_utc_millis(start_kst)
    end_ms = kst_to_utc_millis(end_kst)
    print(start_ms, end_ms)
    print(normalize_subject_id(subjectId), "resource=", resourceId)

    nonce = str(random.randint(100000, 999999))
    ts = str(int(time.time() * 1000))
    pre_str = f"Accesstoken={ACCESS_TOKEN}&Appid={APPID}&Keyid={KEYID}&Nonce={nonce}&Time={ts}{APPKEY}"
    sign = hashlib.md5(pre_str.lower().encode("utf-8")).hexdigest()

    header = {
        "Appid": APPID,
        "Keyid": KEYID,
        "Accesstoken": ACCESS_TOKEN,
        "Nonce": nonce,
        "Time": ts,
        "Sign": sign,
        "Content-Type": "application/json",
    }
    payload = {
        "intent": "fetch.resource.history",
        "data": {
            "subjectId": normalize_subject_id(subjectId),
            "resourceIds": [resourceId],
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }
    return requests.post(API_URL, headers=header, json=payload)


def get_list_of_kst_and_value(resp) -> list[tuple[str, str]]:
    """API 응답 → [(KST 'YYYY-MM-DD HH:MM:SS', value), ...] 리스트. 오류 시 빈 리스트."""
    result = resp.json()
    if result.get("code") != 0:
        print("API Error:", result)
        return []
    out: list[tuple[str, str]] = []
    for item in result.get("result", {}).get("data", []):
        ts = int(item["timeStamp"])
        v = item["value"]
        utc_dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        kst_dt = utc_dt.astimezone(timezone(timedelta(hours=9)))
        out.append((kst_dt.strftime("%Y-%m-%d %H:%M:%S"), v))
    return out


# ─────────────────────────── 페이지네이션 + 두 resource 통합 수집 ───────────────────────────

def fetch_paginated(subject_id: str, resource_id: str, start_kst: str, end_kst: str) -> list[tuple[str, str]]:
    """단일 resource 의 전체 구간을 페이지네이션으로 끝까지 수집 (DESIGN.md §6.1 fetch_paginated).

    Aqara 는 시간 내림차순(최신 → 과거) 최대 100건 반환. len(batch) < 100 이면 종료.
    그 외엔 endTime 을 batch 의 가장 오래된 시각으로 줄여 재호출.
    """
    rows: list[tuple[str, str]] = []
    cursor_end = end_kst
    while True:
        resp = get_history_of_device_attr(subject_id, resource_id, start_kst, cursor_end)
        batch = get_list_of_kst_and_value(resp)
        print(f"  -> {len(batch)} rows")
        rows += batch
        if len(batch) < 100:
            break
        # 가장 오래된 시각의 1초 전으로 endTime 이동 (Aqara 내림차순 반환 가정).
        oldest = batch[-1][0]
        cursor_end = oldest  # 동일 분 중복 제거를 위해 그대로 사용 (record_data.py 원본 로직 유지)
    return rows


def outer_join_wide(motion_rows: list[tuple[str, str]], lux_rows: list[tuple[str, str]]) -> list[list[str]]:
    """motion_status + lux 를 timestamp outer join 하여 wide 포맷 행 생성 (DEVICE.md §1.4).

    한쪽만 샘플이 있는 시각의 결측 컬럼은 빈 문자열.
    동일 timestamp 에 같은 resource 의 중복 값이 있으면 마지막 값 유지 (DESIGN.md §6.1 dict 규칙).
    """
    by_ts: dict[str, dict[str, str]] = {}
    for ts, v in motion_rows:
        by_ts.setdefault(ts, {})["motion_status"] = str(v)
    for ts, v in lux_rows:
        by_ts.setdefault(ts, {})["lux"] = str(v)
    return [[ts, by_ts[ts].get("motion_status", ""), by_ts[ts].get("lux", "")]
            for ts in sorted(by_ts)]


# ─────────────────────────── 실행 설정 (CLI 인자 대신 상단 변수 편집) ───────────────────────────

# 수집 기간 (KST). 1회 호출당 최대 100건 반환되므로 짧은 구간 권장 — 페이지네이션으로 자동 확장.
START_KST = "2026-05-13 13:00:00"
END_KST   = "2026-05-13 14:00:00"

# 대상 디바이스 ID — 사용자가 보유한 Motion Sensor P1 의 hex 또는 'lumi.xxxx' (대/소문자·prefix 무관).
SUBJECT_ID = "4CF8CDF3C8A8432"

# 출력 파일 — 프로젝트 표준 wide CSV (DEVICE.md §1.4)
OUTPUT_FILE_NAME = "motion_P1.csv"


def main() -> None:
    # 두 resource 각각 페이지네이션으로 수집 후 wide join.
    print("[motion_status]")
    motion_rows = fetch_paginated(SUBJECT_ID, RESOURCE_MOTION, START_KST, END_KST)
    print("[lux]")
    lux_rows = fetch_paginated(SUBJECT_ID, RESOURCE_LUX, START_KST, END_KST)

    rows = outer_join_wide(motion_rows, lux_rows)
    with open(OUTPUT_FILE_NAME, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time", "motion_status", "lux"])  # DEVICE.md §1.4 컬럼 헤더
        w.writerows(rows)

    print(f"motion_status={len(motion_rows)}  lux={len(lux_rows)}  joined_rows={len(rows)}  -> {OUTPUT_FILE_NAME}")


if __name__ == "__main__":
    main()
