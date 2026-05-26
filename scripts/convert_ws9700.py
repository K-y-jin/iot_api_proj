"""data/old/0_output_WS9700_Summary.xlsx → 프로젝트 표준 CSV 변환.

원본 컬럼: time, event_type, value, Hub
(UpgoPlus xlsx와 컬럼명이 다름: event_name→event_type, Position→Hub)

event_type → vibration_t1 wide bundle `move_knock` 컬럼 매핑 (DEVICE.md §4.3):
  'motion' → move_detect 컬럼 (값 1=감지 / 255=해지)
  'tap'    → knock_event 컬럼 (두드림 이벤트)
event_type가 NaN(2675행)이거나 그 외인 행은 제외 (사용자 요청).

Hub → device_id 매핑 (사용자 지정, 세 디바이스 모두 vibration_t1):
  R → lumi.4cf8cdf3c829efd  (suffix 829EFD)
  M → lumi.4cf8cdf3c829aed  (suffix 829AED)
  L → lumi.4cf8cdf3c829b1b  (suffix 829B1B)

출력: data/move_knock/{device_id}/{YYYYMMDD}_{suffix}.csv (device×일자 분할, 단일 wide CSV)
메타 헤더는 app/collector.py 의 _meta_lines 와 동일 (DESIGN.md §6.1) — devices 테이블의
alias/install_location/install_date/created_by_name 포함.

원본 순서 보존(안정 정렬) → 같은 timestamp 중복 행도 1:1로 기록.
(이 xlsx는 약 67% 행이 분 단위 정밀도. Etac turner pro와 유사한 요약 단계 초 절삭이
있으며 본 변환에서는 더 복원 불가. raw API 재호출이 필요하면 별도 작업.)
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.devices import DEVICE_TYPES, last6  # noqa: E402

SRC = ROOT / "data" / "old" / "0_output_WS9700_Summary.xlsx"
DEVICE_TYPE = "vibration_t1"
BUNDLE_KEY = "move_knock"

EVENT_TO_COLUMN: dict[str, str] = {
    "motion": "move_detect",
    "tap": "knock_event",
}

HUB_TO_DEVICE: dict[str, str] = {
    "R": "lumi.4cf8cdf3c829efd",
    "M": "lumi.4cf8cdf3c829aed",
    "L": "lumi.4cf8cdf3c829b1b",
}


def now_kst_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fetch_device_meta_map() -> dict[str, dict]:
    db = ROOT / "app.db"
    if not db.exists():
        return {}
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        out: dict[str, dict] = {}
        for did in HUB_TO_DEVICE.values():
            row = con.execute(
                """SELECT alias, install_location, install_date, created_by_name
                     FROM devices
                    WHERE device_id=? AND deleted_at IS NULL
                    ORDER BY id DESC LIMIT 1""",
                (did,),
            ).fetchone()
            out[did] = dict(row) if row else {}
        return out
    finally:
        con.close()


def meta_lines(
    device_id: str, device_meta: dict, target_date: str, row_count: int, resources_str: str
) -> list[str]:
    return [
        f"# device_id: {device_id}",
        f"# device_type: {DEVICE_TYPE}",
        f"# bundle: {BUNDLE_KEY}",
        f"# resources: {resources_str}",
        f"# alias: {device_meta.get('alias') or ''}",
        f"# install_location: {device_meta.get('install_location') or ''}",
        f"# install_date: {device_meta.get('install_date') or ''}",
        f"# registered_by: {device_meta.get('created_by_name') or ''}",
        f"# target_date: {target_date}",
        f"# generated_at: {now_kst_iso()} KST",
        f"# row_count: {row_count}",
    ]


def main() -> None:
    df = pd.read_excel(SRC, sheet_name=0)
    required = {"time", "event_type", "value", "Hub"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"missing columns: {missing}")

    # 1) motion/tap 행만 필터 (event_type NaN과 기타는 모두 제외)
    df = df[df["event_type"].isin(EVENT_TO_COLUMN.keys())].copy()
    if not set(df["Hub"].unique()).issubset(HUB_TO_DEVICE.keys()):
        raise RuntimeError(f"unexpected Hub: {sorted(df['Hub'].unique())}")
    motion_vals = set(df[df["event_type"] == "motion"]["value"].unique())
    if not motion_vals.issubset({1, 255}):
        raise RuntimeError(f"unexpected motion value codes: {sorted(motion_vals)}")

    # 2) 안정 정렬
    df = df.sort_values("time", kind="stable").reset_index(drop=True)

    # 3) wide 셀 변환
    df["date"] = df["time"].dt.date
    df["move_detect"] = df.apply(
        lambda r: str(int(r["value"])) if r["event_type"] == "motion" else "", axis=1
    )
    df["knock_event"] = df.apply(
        lambda r: str(int(r["value"])) if r["event_type"] == "tap" else "", axis=1
    )

    bundle = next(b for b in DEVICE_TYPES[DEVICE_TYPE].bundles if b.key == BUNDLE_KEY)
    resources_str = ",".join(r.name for r in bundle.resources)
    device_meta_map = fetch_device_meta_map()

    # 4) Hub×date 분할 후 CSV 기록
    total_written = 0
    total_files = 0
    per_hub: dict[str, tuple[int, int]] = {}

    for hub, device_id in HUB_TO_DEVICE.items():
        sub = df[df["Hub"] == hub]
        suffix = last6(device_id)
        out_dir = ROOT / "data" / BUNDLE_KEY / device_id
        out_dir.mkdir(parents=True, exist_ok=True)
        meta = device_meta_map.get(device_id, {})

        hub_rows = 0
        hub_files = 0
        for d, g in sub.groupby("date", sort=True):
            target_date = d.isoformat()
            rows = [
                [t.strftime("%Y-%m-%d %H:%M:%S"), md, ke]
                for t, md, ke in zip(g["time"], g["move_detect"], g["knock_event"], strict=True)
            ]
            fname = f"{d.strftime('%Y%m%d')}_{suffix}.csv"
            path = out_dir / fname
            with path.open("w", encoding="utf-8", newline="") as f:
                for line in meta_lines(device_id, meta, target_date, len(rows), resources_str):
                    f.write(line + "\n")
                w = csv.writer(f, lineterminator="\n")
                w.writerow(bundle.csv_columns)
                w.writerows(rows)
            hub_rows += len(rows)
            hub_files += 1
        per_hub[hub] = (hub_rows, hub_files)
        total_written += hub_rows
        total_files += hub_files

    if total_written != len(df):
        raise RuntimeError(f"row count mismatch: source={len(df)} written={total_written}")
    print(f"source rows (motion+tap): {len(df)}  written: {total_written}  files: {total_files}")
    for hub, (r, fc) in per_hub.items():
        print(f"  Hub={hub} -> {HUB_TO_DEVICE[hub]}  rows={r}  files={fc}")


if __name__ == "__main__":
    main()
