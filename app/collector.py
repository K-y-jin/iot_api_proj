"""일일 수집 워크플로우 (DESIGN.md §6.1).

- collect_yesterday(): 전일 KST 00:00:00 ~ 23:59:59 구간을 모든 활성 기기 × 모든 bundle에 대해 수집.
- run_one_bundle(): 단일 bundle 수집 → wide CSV 작성 → collection_jobs 갱신.
- backfill_missing(): 최근 N일 내 실패/누락 (device, bundle, date) 자동 재시도 (DESIGN.md §10).

dry-run 지원: --dry-run 플래그 사용 시 모킹된 페이지 데이터로 CSV/DB까지 전 경로 점검.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from . import aqara_client, config, smartthings_client
from .db import get_connection, now_kst_iso, yesterday_kst
from .devices import DEVICE_TYPES, Bundle, bundles_for, device_id_suffix, supported_hubs


# 페이지 fetcher 시그니처: (device_id, resource_id, start_kst, end_kst) -> list[(time, value)]
PageFetcher = Callable[[str, str, str, str], list[tuple[str, str]]]


# ─────────────────────── CSV 작성 (메타 헤더 + wide outer join) ───────────────────────

def _meta_lines(
    device_id: str,
    device_type: str,
    bundle: Bundle,
    target_date: str,
    row_count: int,
    device_meta: dict | None = None,
) -> list[str]:
    """CSV 최상단 `#` 메타 라인 (DESIGN.md §6.1, §15.8 옵션 A).

    device_meta는 devices 테이블에서 가져온 부가 정보 dict
    (hub / alias / install_location / install_date / created_by_name).
    dry-run 등 DB 미연결 환경에선 None/빈 dict 허용 — 라인은 유지하되 값만 빈 문자열.
    라인 순서를 고정하면 후처리 파서가 위치 기반으로 안전하게 동작한다.
    hub 라인은 §15.8 에 따라 가장 위에 위치 (옛 Aqara 파일과의 호환을 위해 파서는 부재 시 'aqara' 기본).
    """
    resources = ",".join(r.name for r in bundle.resources)
    m = device_meta or {}
    # hub 는 device_meta 가 권위적. 누락 시 supported_hubs 의 첫 항목 (단일 hub 종류 안전 기본값).
    hub = m.get("hub")
    if not hub:
        svs = supported_hubs(device_type)
        hub = svs[0] if svs else "aqara"
    return [
        f"# hub: {hub}",
        f"# device_id: {device_id}",
        f"# device_type: {device_type}",
        f"# bundle: {bundle.key}",
        f"# resources: {resources}",
        f"# alias: {m.get('alias') or ''}",
        f"# install_location: {m.get('install_location') or ''}",
        f"# install_date: {m.get('install_date') or ''}",
        f"# registered_by: {m.get('created_by_name') or ''}",
        f"# target_date: {target_date}",
        f"# generated_at: {now_kst_iso()} KST",
        f"# row_count: {row_count}",
    ]


def _join_to_wide_rows(per_resource: dict[str, dict[str, str]], bundle: Bundle) -> list[list[str]]:
    """multi-resource bundle을 timestamp outer join 하여 wide 포맷 행 생성.

    한쪽 resource에만 샘플이 있는 시각의 결측 컬럼은 빈 문자열(`,,`).
    행은 time 오름차순 정렬 (DEVICE.md §1.4 규칙).
    """
    all_ts = sorted({ts for d in per_resource.values() for ts in d})
    rows: list[list[str]] = []
    for ts in all_ts:
        row = [ts]
        for r in bundle.resources:
            row.append(per_resource.get(r.name, {}).get(ts, ""))
        rows.append(row)
    return rows


def write_bundle_csv(
    path: Path,
    device_id: str,
    device_type: str,
    bundle: Bundle,
    target_date: str,
    rows: list[list[str]],
    device_meta: dict | None = None,
) -> int:
    """메타 헤더 + 컬럼 헤더 + 데이터 행을 UTF-8/LF로 기록. 반환값은 데이터 행 수.

    device_meta는 devices 테이블 행 정보(alias/install_location/install_date/created_by_name)를
    담는 dict로 메타 헤더에 함께 기록한다 (DESIGN.md §6.1).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        for line in _meta_lines(device_id, device_type, bundle, target_date, len(rows), device_meta):
            f.write(line + "\n")
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(bundle.csv_columns)
        writer.writerows(rows)
    return len(rows)


def csv_path_for(device_id: str, bundle: Bundle, target_date: str, hub: str = "aqara") -> Path:
    """data/{bundle_key}/{device_id}/{YYYYMMDD}_{suffix}.csv (DESIGN.md §4, §15.7).

    suffix 는 hub 별 분기:
      - aqara: 끝 6자리 (예: '829AED')
      - smartthings: 첫 8자리 (예: '3E7B675D')
    bundle_key를 1차 디렉토리로 두는 이유: 같은 bundle은 컬럼 셋이 동일하므로
    여러 디바이스 데이터를 한 폴더에서 일괄 비교/병합하기 쉽다.
    """
    date_compact = target_date.replace("-", "")
    fname = f"{date_compact}_{device_id_suffix(device_id, hub)}.csv"
    return config.DATA_DIR / bundle.key / device_id / fname


# ─────────────────────────── 단일 bundle 수집 ───────────────────────────

def _upsert_job_start(conn: sqlite3.Connection, device_id: str, bundle_key: str, target_date: str) -> int:
    """수집 시작 시점에 job 행을 'running'으로 upsert. 반환 id."""
    now = now_kst_iso()
    cur = conn.execute(
        """INSERT INTO collection_jobs(device_id, bundle_key, target_date, status, started_at)
                VALUES (?, ?, ?, 'running', ?)
           ON CONFLICT(device_id, bundle_key, target_date) DO UPDATE
                SET status='running', started_at=excluded.started_at,
                    finished_at=NULL, error_message=NULL""",
        (device_id, bundle_key, target_date, now),
    )
    _ = cur  # rowid는 별도 SELECT (UPSERT는 last_insert_rowid가 비신뢰)
    row = conn.execute(
        "SELECT id FROM collection_jobs WHERE device_id=? AND bundle_key=? AND target_date=?",
        (device_id, bundle_key, target_date),
    ).fetchone()
    return int(row["id"])


def _finish_job(
    conn: sqlite3.Connection,
    job_id: int,
    status: str,
    *,
    record_count: int | None = None,
    file_path: str | None = None,
    file_size_bytes: int | None = None,
    error_message: str | None = None,
) -> None:
    """job 완료 처리 (success/failed)."""
    conn.execute(
        """UPDATE collection_jobs
              SET status=?, record_count=?, file_path=?, file_size_bytes=?,
                  finished_at=?, error_message=?
            WHERE id=?""",
        (status, record_count, file_path, file_size_bytes, now_kst_iso(), error_message, job_id),
    )


def _fetch_aqara_bundle(
    device_id: str, bundle: Bundle, start: str, end: str,
    page_fetcher: PageFetcher | None = None,
) -> dict[str, dict[str, str]]:
    """Aqara: resource 마다 페이지네이션 수집 (DESIGN.md §6.1)."""
    per_resource: dict[str, dict[str, str]] = {}
    for r in bundle.resources:
        pairs = aqara_client.fetch_history_paginated(
            device_id, r.id, start, end, page_caller=page_fetcher
        )
        per_resource[r.name] = {ts: v for ts, v in pairs}
    return per_resource


def _fetch_smartthings_bundle(
    device_id: str, bundle: Bundle, target_date: str,
) -> dict[str, dict[str, str]]:
    """SmartThings: 1 CLI 호출(페이지네이션 포함)로 모든 resource 수집 후 capability 별 분리 (DESIGN.md §15.6.1)."""
    return smartthings_client.fetch_history_for_bundle(device_id, bundle, target_date)


def run_one_bundle(
    device_id: str,
    device_type: str,
    bundle: Bundle,
    target_date: str,
    page_fetcher: PageFetcher | None = None,
    device_meta: dict | None = None,
) -> dict:
    """단일 (device, bundle, date) 수집 (DESIGN.md §6.1, §15.6).

    page_fetcher: dry-run/테스트에서 aqara_client.fetch_history_page를 대체할 콜러블 (aqara 만 사용).
    device_meta: devices 테이블 메타(hub/alias/install_location/install_date/created_by_name) — CSV 헤더·경로 분기.
    반환: {'status', 'record_count', 'file_path', 'file_size_bytes', 'error'}.
    """
    start = f"{target_date} 00:00:00"
    end = f"{target_date} 23:59:59"

    conn = get_connection()
    job_id = _upsert_job_start(conn, device_id, bundle.key, target_date)

    try:
        # hub 는 device_meta 가 권위. 누락 시 supported_hubs 첫 항목 (단일 hub 종류 fallback).
        hub = (device_meta or {}).get("hub")
        if not hub:
            svs = supported_hubs(device_type)
            hub = svs[0] if svs else "aqara"
        if hub == "aqara":
            per_resource = _fetch_aqara_bundle(device_id, bundle, start, end, page_fetcher)
        elif hub == "smartthings":
            per_resource = _fetch_smartthings_bundle(device_id, bundle, target_date)
        else:
            raise ValueError(f"unknown hub: {hub}")

        # 단일 resource는 그대로, 멀티는 wide outer join
        if len(bundle.resources) == 1:
            only = bundle.resources[0].name
            rows = [[ts, v] for ts, v in sorted(per_resource[only].items())]
        else:
            rows = _join_to_wide_rows(per_resource, bundle)

        path = csv_path_for(device_id, bundle, target_date, hub=hub)
        count = write_bundle_csv(
            path, device_id, device_type, bundle, target_date, rows, device_meta=device_meta
        )
        size = path.stat().st_size

        _finish_job(
            conn, job_id, "success",
            record_count=count, file_path=str(path.relative_to(config.PROJECT_ROOT)),
            file_size_bytes=size,
        )
        return {"status": "success", "record_count": count, "file_path": str(path), "file_size_bytes": size, "error": None}

    except Exception as e:
        _finish_job(conn, job_id, "failed", error_message=str(e)[:1000])
        return {"status": "failed", "record_count": None, "file_path": None, "file_size_bytes": None, "error": str(e)}

    finally:
        conn.close()


# ─────────────────────────── 일일 수집 / 보충 ───────────────────────────

def _list_devices_where(flag_column: str) -> list[dict]:
    """수집 대상 기기 조회 공통 헬퍼 (deleted_at IS NULL AND <flag_column>=1).

    flag_column 은 내부 상수('enabled' | 'manual_enabled')로만 전달되며 사용자 입력이 아니다
    (SQL injection 무관). CSV 메타 헤더(DESIGN.md §6.1)에 기록할 부가 정보도 함께 반환한다.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT device_id, device_type, hub, alias,
                       install_location, install_date, created_by_name
                  FROM devices
                 WHERE deleted_at IS NULL AND {flag_column}=1
                 ORDER BY id"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_active_devices() -> list[dict]:
    """자동 수집 대상 기기 (deleted_at IS NULL AND enabled=1).

    매일 09:00 cron(collect_yesterday) 과 매시간 backfill 이 참조한다 (DESIGN.md §5).

    hub='smartthings' 는 자동 수집을 지원하지 않으므로(§5) enabled 값과 무관하게 제외한다.
    등록/편집/일괄 토글 단에서 이미 enabled=1 을 막지만, 과거 데이터·직접 DB 수정에 대비한 backstop.
    """
    return [d for d in _list_devices_where("enabled") if (d.get("hub") or "aqara") != "smartthings"]


def list_manual_devices() -> list[dict]:
    """수동 수집 대상 기기 (deleted_at IS NULL AND manual_enabled=1).

    일괄 수동 수집(/api/jobs/bulk_run) 만 참조한다. enabled(자동) 와 독립 (DESIGN.md §5).
    """
    return _list_devices_where("manual_enabled")


def collect_for_date(
    target_date: str,
    page_fetcher: PageFetcher | None = None,
    devices: list[dict] | None = None,
) -> list[dict]:
    """주어진 일자에 대해 대상 기기 × 모든 bundle 수집.

    devices=None 이면 자동 수집 대상(list_active_devices). 일괄 수동 수집은 수동 대상
    (list_manual_devices) 을 명시적으로 전달한다 (DESIGN.md §7.4).
    """
    config.ensure_dirs()
    results = []
    for dev in (devices if devices is not None else list_active_devices()):
        dt = DEVICE_TYPES.get(dev["device_type"])
        if dt is None:
            results.append({"device": dev, "skipped": True, "reason": "unknown device_type"})
            continue
        hub = dev.get("hub") or "aqara"
        type_bundles = bundles_for(dev["device_type"], hub)
        if not type_bundles:
            results.append({"device": dev, "skipped": True,
                            "reason": f"hub {hub} not supported for {dev['device_type']}"})
            continue
        for bundle in type_bundles:
            r = run_one_bundle(
                dev["device_id"], dev["device_type"], bundle, target_date,
                page_fetcher, device_meta=dev,
            )
            r.update({"device_id": dev["device_id"], "bundle": bundle.key, "target_date": target_date})
            results.append(r)
    return results


def collect_yesterday(page_fetcher: PageFetcher | None = None) -> list[dict]:
    """매일 09:00 KST cron에서 호출 — 시스템 시간 기준 어제 1일치 수집."""
    return collect_for_date(yesterday_kst(), page_fetcher=page_fetcher)


def collect_date_range(
    from_date: str, to_date: str, page_fetcher: PageFetcher | None = None
) -> list[dict]:
    """[from_date, to_date] 기간의 매일에 대해 수동 수집 대상 전체 수집 (DESIGN.md §7.4 일괄 수동 수집).

    대상은 **manual_enabled=1** 장치 (list_manual_devices) — 자동 수집(enabled) 과 독립.
    기간 전체에서 동일한 대상 목록을 쓰도록 진입 시 1회 조회한다 (도중 토글돼도 일관 유지).

    백그라운드 스레드에서 호출되는 워커. 각 (device, bundle, date) 의 결과는 collection_jobs 에
    그대로 기록되므로 별도 진행 상태 저장은 불필요 — `/jobs` 페이지에서 확인.

    실패한 일자가 있어도 다음 일자로 계속 진행 (best-effort). 토큰 만료/네트워크 에러 등으로 인한
    개별 실패는 collection_jobs.status='failed' + alerts 로 표면화된다.
    """
    from datetime import datetime as _dt, timedelta as _td
    start = _dt.strptime(from_date, "%Y-%m-%d").date()
    end = _dt.strptime(to_date, "%Y-%m-%d").date()
    if end < start:
        raise ValueError(f"to({to_date}) is before from({from_date})")
    config.ensure_dirs()
    manual_devices = list_manual_devices()
    all_results: list[dict] = []
    cur = start
    while cur <= end:
        d_str = cur.strftime("%Y-%m-%d")
        results = collect_for_date(d_str, page_fetcher=page_fetcher, devices=manual_devices)
        all_results.extend(results)
        cur += _td(days=1)
    return all_results


def backfill_missing(page_fetcher: PageFetcher | None = None) -> list[dict]:
    """최근 7일 내 failed/누락 (device, bundle, date) 조합을 찾아 재시도 (DESIGN.md §10)."""
    from datetime import date, timedelta as td
    today = date.today()
    # device_id 별 메타 dict를 보관해 backfill 호출 시 CSV 헤더에 동일하게 기입.
    device_meta_by_id: dict[str, dict] = {}
    targets: list[tuple[str, str, str, str]] = []  # device_id, device_type, bundle.key, target_date
    devices = list_active_devices()
    if not devices:
        return []
    conn = get_connection()
    try:
        for dev in devices:
            device_meta_by_id[dev["device_id"]] = dev
            dt = DEVICE_TYPES.get(dev["device_type"])
            if dt is None:
                continue
            hub = dev.get("hub") or "aqara"
            for b in bundles_for(dev["device_type"], hub):
                for n in range(1, config.HEALTHCHECK_LOOKBACK_DAYS + 1):
                    d = (today - td(days=n)).strftime("%Y-%m-%d")
                    row = conn.execute(
                        """SELECT status FROM collection_jobs
                            WHERE device_id=? AND bundle_key=? AND target_date=?""",
                        (dev["device_id"], b.key, d),
                    ).fetchone()
                    if row is None or row["status"] != "success":
                        targets.append((dev["device_id"], dev["device_type"], b.key, d))
    finally:
        conn.close()

    results = []
    for device_id, device_type, bundle_key, target_date in targets:
        meta = device_meta_by_id.get(device_id, {})
        hub = meta.get("hub") or "aqara"
        bundle = next((b for b in bundles_for(device_type, hub) if b.key == bundle_key), None)
        if bundle is None:
            continue
        r = run_one_bundle(
            device_id, device_type, bundle, target_date, page_fetcher,
            device_meta=device_meta_by_id.get(device_id),
        )
        r.update({"device_id": device_id, "bundle": bundle_key, "target_date": target_date, "backfill": True})
        results.append(r)
    return results


# ─────────────────────────── 보관 정책 ───────────────────────────

def prune_old_jobs(retention_days: int | None = None) -> int:
    """`collection_jobs` 에서 보관 기간을 초과한 행 삭제 (DESIGN.md §6.1 보관 정책).

    매일 03:30 KST cron 에서 호출. `target_date < (오늘 - retention_days)` 인 행을 삭제한다.
    기본 보관 일수는 `config.JOB_HISTORY_RETENTION_DAYS` (4주).
    수집된 CSV 파일은 별개 자산이라 이 함수가 건드리지 않는다 — `/data` 화면은 파일시스템 walk 로
    집계하므로 CSV 만 존재하면 통계는 유지된다.

    반환: 삭제된 행 수.
    """
    from datetime import date as _date, timedelta as _td
    days = retention_days if retention_days is not None else config.JOB_HISTORY_RETENTION_DAYS
    cutoff = (_date.today() - _td(days=days)).strftime("%Y-%m-%d")
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM collection_jobs WHERE target_date < ?", (cutoff,))
        return int(cur.rowcount or 0)
    finally:
        conn.close()


# ─────────────────────────── CLI / dry-run ───────────────────────────

def _make_dry_run_fetcher() -> PageFetcher:
    """모킹 페이지 데이터: motion_status는 13건, lux는 14건 (DEVICE.md §1.4 예시 재현).

    그 외 resource는 0건 응답.
    """
    motion_times = [
        "13:10:08", "13:11:06", "13:14:35", "13:16:05", "13:17:29", "13:22:14",
        "13:40:11", "13:41:13", "13:42:16", "13:43:14", "13:44:57", "13:53:46", "13:56:17",
    ]
    lux_pairs = [
        ("13:10:08", "5"), ("13:11:06", "97"), ("13:14:35", "97"), ("13:16:05", "93"),
        ("13:17:29", "93"), ("13:22:14", "93"), ("13:40:11", "94"), ("13:41:13", "93"),
        ("13:42:16", "93"), ("13:43:14", "92"), ("13:44:57", "91"), ("13:51:46", "91"),
        ("13:53:46", "93"), ("13:56:17", "92"),
    ]

    # smart_plug_eu 단일 wide bundle(plug_status) dry-run 샘플 (DEVICE.md §11):
    #   4.1.85 plug_status(on/off 이벤트), 0.12.85 load_power(W), 0.13.85 cost_energy(0.001kWh raw, 단조 증가).
    # 세 resource 의 보고 시각이 어긋나는 상황(전력만 있는 시각·on/off 만 있는 시각)을 재현해 wide join 을 점검.
    plug_pairs = [("08:00:00", "1"), ("08:02:30", "0")]
    power_pairs = [("08:00:03", "45.2"), ("08:01:03", "44.8"), ("08:02:03", "0.0")]
    energy_pairs = [("08:00:03", "12030"), ("08:01:03", "12031"), ("08:02:03", "12031")]

    def fetcher(device_id: str, resource_id: str, start_kst: str, end_kst: str) -> list[tuple[str, str]]:
        date_prefix = start_kst.split(" ")[0]
        if resource_id == "3.1.85":
            return [(f"{date_prefix} {t}", "1") for t in motion_times]
        if resource_id == "0.3.85":
            return [(f"{date_prefix} {t}", v) for t, v in lux_pairs]
        if resource_id == "4.1.85":
            return [(f"{date_prefix} {t}", v) for t, v in plug_pairs]
        if resource_id == "0.12.85":
            return [(f"{date_prefix} {t}", v) for t, v in power_pairs]
        if resource_id == "0.13.85":
            return [(f"{date_prefix} {t}", v) for t, v in energy_pairs]
        return []

    return fetcher


def main() -> None:
    parser = argparse.ArgumentParser(description="Aqara 일일 수집 워크플로우 (DESIGN.md §6.1)")
    parser.add_argument("--date", help="수집 대상 일자 YYYY-MM-DD (기본: 어제 KST)")
    parser.add_argument("--dry-run", action="store_true",
                        help="모킹된 데이터로 전 경로 점검 (Aqara API 호출 안 함)")
    args = parser.parse_args()

    # dry-run이면 활성 기기가 없어도 동작하도록 임시 demo device를 1개 주입 (DB 미초기화 환경 대비)
    target_date = args.date or yesterday_kst()
    fetcher = _make_dry_run_fetcher() if args.dry_run else None

    if args.dry_run:
        # DB 무관하게 wide join + CSV 작성 경로만 점검
        config.ensure_dirs()
        device_id = "lumi.4cf8cdf3c752edb"
        device_type = "motion_t1"
        # Aqara motion_t1 의 첫 bundle (motion_lux)
        bundle = bundles_for(device_type, "aqara")[0]
        # DB가 없을 수도 있으므로 임시 메모리 DB로 초기화
        from .db import init_db
        init_db()
        result = run_one_bundle(
            device_id, device_type, bundle, target_date,
            page_fetcher=fetcher, device_meta={"hub": "aqara"},
        )
        print(f"[dry-run] target_date={target_date} result={result}")
        return

    results = collect_for_date(target_date)
    ok = sum(1 for r in results if r.get("status") == "success")
    fail = sum(1 for r in results if r.get("status") == "failed")
    print(f"target_date={target_date} jobs={len(results)} success={ok} failed={fail}")
    for r in results:
        print(" ", r)


if __name__ == "__main__":
    main()
