"""data/old/etac turner pro raw/Output_Etac turner pro_*.csv → 프로젝트 표준 CSV 변환.

대상 디바이스: lumi.4cf8cdf3c82a198 (vibration_t1, Etac turner pro 진동 센서)
대상 bundle: move_knock (DEVICE.md §4.3, wide 포맷: time,move_detect,knock_event)

원본:
- `data/old/etac turner pro raw/` 폴더의 6개 raw CSV (`Output_Etac turner pro_<날짜범위>.csv`).
- 컬럼: `time, event_type, value` (첫 파일에만 `Position`이 추가로 있고 항상 'R' → 단일 디바이스이므로 무시).
- event_type은 전부 'motion' (move_detect bundle 대상). knock_event 컬럼은 항상 빈 칸.

시각 정밀도:
- 5개 파일은 초 정밀도 (`HH:MM:SS`) 보존.
- 1개 파일(`260309~260318`)은 분 정밀도 (`H:MM`, 단일자리 시간). 더 복원 불가하므로 그대로 둔다.
- 같은 분 내 다중 이벤트는 안정 정렬로 원본 순서 유지하여 중복 행을 모두 보존.

출력: data/move_knock/lumi.4cf8cdf3c82a198/YYYYMMDD_82A198.csv (일자별 분할).
메타 헤더는 app/collector.py 의 _meta_lines 와 동일 (DESIGN.md §6.1).
"""

from __future__ import annotations

import csv
import glob
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.devices import DEVICE_TYPES, last6  # noqa: E402

DEVICE_ID = "lumi.4cf8cdf3c82a198"
DEVICE_TYPE = "vibration_t1"
BUNDLE_KEY = "move_knock"
SRC_DIR = ROOT / "data" / "old" / "etac turner pro raw"


def now_kst_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fetch_device_meta(device_id: str) -> dict:
    db = ROOT / "app.db"
    if not db.exists():
        return {}
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            """SELECT alias, install_location, install_date, created_by_name
                 FROM devices
                WHERE device_id=? AND deleted_at IS NULL
                ORDER BY id DESC LIMIT 1""",
            (device_id,),
        ).fetchone()
    finally:
        con.close()
    return dict(row) if row else {}


def meta_lines(device_meta: dict, target_date: str, row_count: int, resources_str: str) -> list[str]:
    return [
        f"# device_id: {DEVICE_ID}",
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


def parse_time_flexible(s: str) -> datetime | None:
    """raw CSV의 time 컬럼은 'YYYY-MM-DD HH:MM:SS' 또는 'YYYY-MM-DD H:MM'(초 없음) 혼재.

    두 포맷을 모두 시도. 둘 다 실패하면 None.
    """
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def main() -> None:
    files = sorted(glob.glob(str(SRC_DIR / "Output_Etac turner pro_*.csv")))
    if not files:
        raise RuntimeError(f"no raw csv found in {SRC_DIR}")

    # 1) 6개 CSV 통합 + 컬럼 정합성 검증
    frames: list[pd.DataFrame] = []
    for fp in files:
        df = pd.read_csv(fp, dtype={"time": str})
        if "Position" in df.columns:
            df = df.drop(columns=["Position"])  # 첫 파일에만 있고 항상 'R' (단일 디바이스 → 무시)
        if list(df.columns) != ["time", "event_type", "value"]:
            raise RuntimeError(f"unexpected columns in {fp}: {list(df.columns)}")
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)

    if not set(df["event_type"].unique()).issubset({"motion"}):
        raise RuntimeError(f"unexpected event_type: {df['event_type'].unique()}")
    if not set(df["value"].unique()).issubset({1, 255}):
        raise RuntimeError(f"unexpected value codes: {df['value'].unique()}")

    # 2) 혼합 포맷 시각 파싱
    df["dt"] = df["time"].apply(parse_time_flexible)
    bad = df["dt"].isna().sum()
    if bad:
        raise RuntimeError(f"{bad} rows failed time parse")

    # 3) 안정 정렬 + 일자별 그룹핑
    df = df.sort_values("dt", kind="stable").reset_index(drop=True)
    df["date"] = df["dt"].dt.date

    bundle = next(b for b in DEVICE_TYPES[DEVICE_TYPE].bundles if b.key == BUNDLE_KEY)
    resources_str = ",".join(r.name for r in bundle.resources)
    device_meta = fetch_device_meta(DEVICE_ID)

    # 4) 출력 (wide: time, move_detect, knock_event — knock_event는 항상 빈 칸)
    out_dir = ROOT / "data" / BUNDLE_KEY / DEVICE_ID
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = last6(DEVICE_ID)
    written: list[tuple[str, int, Path]] = []
    for d, g in df.groupby("date", sort=True):
        target_date = d.isoformat()
        rows = [
            [t.strftime("%Y-%m-%d %H:%M:%S"), str(int(v)), ""]
            for t, v in zip(g["dt"], g["value"], strict=True)
        ]
        fname = f"{d.strftime('%Y%m%d')}_{suffix}.csv"
        path = out_dir / fname
        with path.open("w", encoding="utf-8", newline="") as f:
            for line in meta_lines(device_meta, target_date, len(rows), resources_str):
                f.write(line + "\n")
            w = csv.writer(f, lineterminator="\n")
            w.writerow(bundle.csv_columns)
            w.writerows(rows)
        written.append((target_date, len(rows), path))

    total = sum(c for _, c, _ in written)
    print(f"source rows: {len(df)}  written: {total}  files: {len(written)}")
    for date_str, count, path in written:
        print(f"  {date_str}  rows={count:4d}  {path.relative_to(ROOT)}")
    if total != len(df):
        raise RuntimeError(f"row count mismatch: source={len(df)} written={total}")


if __name__ == "__main__":
    main()
