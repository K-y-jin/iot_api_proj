"""data/old/0_output_UpgoPlus_Summary.xlsx → 프로젝트 표준 CSV 변환.

원본 컬럼: time, time txt, Position(R/M/L), value, event_name

event_name → vibration_t1 wide bundle `move_knock` 컬럼 매핑 (DEVICE.md §4.3):
  'motion' → move_detect 컬럼 (값 1=감지 / 255=해지)
  'tap'    → knock_event 컬럼 (두드림 이벤트, 값은 원본 그대로)
그 외 event_name(Tilting_event, Free_Fall 등)은 vibration_t1 bundle에 속하지 않으므로 제외.

Position → device_id 매핑 (사용자 지정, 세 디바이스 모두 vibration_t1):
  R → lumi.54ef4410007a5d2e  (suffix 7A5D2E)
  M → lumi.54ef4410007a542b  (suffix 7A542B)
  L → lumi.54ef4410007a5881  (suffix 7A5881)

출력: data/move_knock/{device_id}/{YYYYMMDD}_{suffix}.csv (device×일자 분할, 단일 wide CSV)
메타 헤더는 app/collector.py 의 _meta_lines 와 동일 (DESIGN.md §6.1) — devices 테이블의
alias/install_location/install_date/created_by_name 포함.

원본 순서 보존(안정 정렬) → 같은 timestamp 중복 행도 1:1로 기록.
한 행에는 motion 또는 tap 중 하나의 컬럼만 채워지며 나머지는 빈 칸 (outer join 빈 셀 규칙).
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

SRC = ROOT / "data" / "old" / "0_output_UpgoPlus_Summary.xlsx"
DEVICE_TYPE = "vibration_t1"
BUNDLE_KEY = "move_knock"

# event_name → wide CSV의 컬럼명
EVENT_TO_COLUMN: dict[str, str] = {
    "motion": "move_detect",
    "tap": "knock_event",
}

POSITION_TO_DEVICE: dict[str, str] = {
    "R": "lumi.54ef4410007a5d2e",
    "M": "lumi.54ef4410007a542b",
    "L": "lumi.54ef4410007a5881",
}


def now_kst_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fetch_device_meta_map() -> dict[str, dict]:
    """대상 device_id 셋에 대해 devices 테이블 메타를 일괄 조회."""
    db = ROOT / "app.db"
    if not db.exists():
        return {}
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        out: dict[str, dict] = {}
        for did in POSITION_TO_DEVICE.values():
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
    """CSV 메타 헤더 (DESIGN.md §6.1)."""
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
    required = {"time", "Position", "value", "event_name"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"missing columns: {missing}")

    # 1) motion/tap 행만 필터 (vibration_t1 bundle 범위)
    df = df[df["event_name"].isin(EVENT_TO_COLUMN.keys())].copy()
    if not set(df["Position"].unique()).issubset(POSITION_TO_DEVICE.keys()):
        raise RuntimeError(f"unexpected positions: {sorted(df['Position'].unique())}")
    motion_vals = set(df[df["event_name"] == "motion"]["value"].unique())
    if not motion_vals.issubset({1, 255}):
        raise RuntimeError(f"unexpected motion value codes: {sorted(motion_vals)}")

    # 2) 안정 정렬 (원본 행 순서 보존)
    df = df.sort_values("time", kind="stable").reset_index(drop=True)

    # 3) 행마다 (move_detect, knock_event) wide 셀로 변환.
    #    한 row는 motion 또는 tap 중 하나의 컬럼만 채워지고 나머지는 빈 칸.
    df["date"] = df["time"].dt.date
    df["move_detect"] = df.apply(
        lambda r: str(int(r["value"])) if r["event_name"] == "motion" else "", axis=1
    )
    df["knock_event"] = df.apply(
        lambda r: str(int(r["value"])) if r["event_name"] == "tap" else "", axis=1
    )

    bundle = next(b for b in DEVICE_TYPES[DEVICE_TYPE].bundles if b.key == BUNDLE_KEY)
    resources_str = ",".join(r.name for r in bundle.resources)
    device_meta_map = fetch_device_meta_map()

    # 4) Position×date 분할 후 CSV 기록
    total_written = 0
    total_files = 0
    per_pos: dict[str, tuple[int, int]] = {}

    for pos, device_id in POSITION_TO_DEVICE.items():
        sub = df[df["Position"] == pos]
        suffix = last6(device_id)
        out_dir = ROOT / "data" / BUNDLE_KEY / device_id
        out_dir.mkdir(parents=True, exist_ok=True)
        meta = device_meta_map.get(device_id, {})

        pos_rows = 0
        pos_files = 0
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
                w.writerow(bundle.csv_columns)  # ('time', 'move_detect', 'knock_event')
                w.writerows(rows)
            pos_rows += len(rows)
            pos_files += 1
        per_pos[pos] = (pos_rows, pos_files)
        total_written += pos_rows
        total_files += pos_files

    if total_written != len(df):
        raise RuntimeError(f"row count mismatch: source={len(df)} written={total_written}")
    print(f"source rows (motion+tap): {len(df)}  written: {total_written}  files: {total_files}")
    for pos, (r, fc) in per_pos.items():
        print(f"  Position={pos} -> {POSITION_TO_DEVICE[pos]}  rows={r}  files={fc}")


if __name__ == "__main__":
    main()
