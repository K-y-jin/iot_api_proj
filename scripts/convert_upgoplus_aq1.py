"""data/old/0_output_UpgoPlus_Summary.xlsx 의 비-motion·비-tap 이벤트 → vibration_aq1 변환.

UpgoPlus xlsx 한 파일에 두 종류 디바이스의 이벤트가 섞여 있다:
  - motion / tap : vibration_t1 (`move_knock` bundle, scripts/convert_upgoplus.py 가 처리)
  - 그 외       : vibration_aq1 (`vibration_event` bundle, 본 스크립트가 처리)

매핑된 vibration_aq1 코드 (DEVICE.md §6.2):
  xlsx event_name                          xlsx value  → vibration_event 코드
  ─────────────────────────────────────    ──────────    ──────────────────────
  Triggered_after_stillness                  1            1 (정지 후 트리거)
  Vibration(Triggered_after_stillness)       1            1 (정지 후 트리거)
  Tilting                                    2            2 (기울임)
  Tilting_event                              2            2 (기울임)
  Free_Fall                                  3            3 (자유 낙하)

→ xlsx value 가 이미 DEVICE.md 의 vibration_aq1 코드와 일치하므로 추가 변환 불필요.
   event_name 라벨의 미세한 구분(예: Tilting vs Tilting_event)은 단일 코드로 통합되어 사라진다.

Position → device_id 매핑 (사용자 정정 — DB alias 우측/중앙/좌측에 맞춤):
  R → lumi.158d0006d63f77  (suffix D63F77, 우측)
  M → lumi.158d0006a0c061  (suffix A0C061, 중앙)
  L → lumi.158d0006794264  (suffix 794264, 좌측)

출력: data/vibration_event/{device_id}/{YYYYMMDD}_{suffix}.csv
메타 헤더는 app/collector.py 와 동일 (DESIGN.md §6.1) — devices 테이블의 부가 정보 포함.
원본 순서 보존(안정 정렬) — 같은 timestamp 중복 행도 1:1 기록.
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
DEVICE_TYPE = "vibration_aq1"
BUNDLE_KEY = "vibration_event"

POSITION_TO_DEVICE: dict[str, str] = {
    "R": "lumi.158d0006d63f77",
    "M": "lumi.158d0006a0c061",
    "L": "lumi.158d0006794264",
}

# 본 변환에서 제외할 event_name 셋 (vibration_t1 쪽으로 분리됨).
EXCLUDED_EVENTS = {"motion", "tap"}

# 안전 검증: 본 변환이 기대하는 (event_name → value) 매핑.
# DEVICE.md §6.2 에 정의된 vibration_aq1 코드 0~6 중 본 xlsx 에 실제 등장하는 1/2/3 만 사용.
EXPECTED_NAME_TO_CODE: dict[str, int] = {
    "Triggered_after_stillness": 1,
    "Vibration(Triggered_after_stillness)": 1,
    "Tilting": 2,
    "Tilting_event": 2,
    "Free_Fall": 3,
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

    # 1) motion/tap 외 행만 필터
    df = df[~df["event_name"].isin(EXCLUDED_EVENTS)].copy()

    # 2) Position 셋 검증
    if not set(df["Position"].unique()).issubset(POSITION_TO_DEVICE.keys()):
        raise RuntimeError(f"unexpected positions: {sorted(df['Position'].unique())}")

    # 3) event_name → value 매핑 일관성 검증 (혹시 다른 코드가 섞이면 즉시 실패)
    unexpected = set(df["event_name"].unique()) - set(EXPECTED_NAME_TO_CODE.keys())
    if unexpected:
        raise RuntimeError(f"unexpected event_name in aq1 subset: {unexpected}")
    for name, expected in EXPECTED_NAME_TO_CODE.items():
        sub = df[df["event_name"] == name]
        actual = set(sub["value"].unique())
        if not actual.issubset({expected}):
            raise RuntimeError(
                f"event_name={name!r} 가 value={expected} 이외의 코드를 포함: {sorted(actual)}"
            )

    # 4) vibration_aq1 정의 코드(0~6) 범위 안에 있는지 최종 확인
    aq1_codes = {0, 1, 2, 3, 4, 5, 6}
    if not set(df["value"].unique()).issubset(aq1_codes):
        raise RuntimeError(f"value codes outside vibration_aq1 range 0~6: {sorted(df['value'].unique())}")

    # 5) 안정 정렬 (원본 행 순서 보존)
    df = df.sort_values("time", kind="stable").reset_index(drop=True)
    df["date"] = df["time"].dt.date

    bundle = next(b for b in DEVICE_TYPES[DEVICE_TYPE].bundles if b.key == BUNDLE_KEY)
    resources_str = ",".join(r.name for r in bundle.resources)
    device_meta_map = fetch_device_meta_map()

    # 6) Position×date 분할 후 CSV 기록 (단일 컬럼: time, vibration_event)
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
                [t.strftime("%Y-%m-%d %H:%M:%S"), str(int(v))]
                for t, v in zip(g["time"], g["value"], strict=True)
            ]
            fname = f"{d.strftime('%Y%m%d')}_{suffix}.csv"
            path = out_dir / fname
            with path.open("w", encoding="utf-8", newline="") as f:
                for line in meta_lines(device_id, meta, target_date, len(rows), resources_str):
                    f.write(line + "\n")
                w = csv.writer(f, lineterminator="\n")
                w.writerow(bundle.csv_columns)  # ('time', 'vibration_event')
                w.writerows(rows)
            pos_rows += len(rows)
            pos_files += 1
        per_pos[pos] = (pos_rows, pos_files)
        total_written += pos_rows
        total_files += pos_files

    if total_written != len(df):
        raise RuntimeError(f"row count mismatch: source={len(df)} written={total_written}")
    print(f"source rows (non-motion/tap): {len(df)}  written: {total_written}  files: {total_files}")
    for pos, (r, fc) in per_pos.items():
        print(f"  Position={pos} -> {POSITION_TO_DEVICE[pos]}  rows={r}  files={fc}")


if __name__ == "__main__":
    main()
