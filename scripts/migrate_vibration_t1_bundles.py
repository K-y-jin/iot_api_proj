"""기존 vibration_t1 CSV(`data/move_detect/`, `data/knock_event/`)를
새 통합 bundle `data/move_knock/`(wide: time,move_detect,knock_event)로 마이그레이션.

대상 케이스:
1) `data/move_detect/{device_id}/*.csv`만 있는 경우 (예: lumi.54ef4410007a5dab의 API 수집분)
   → 같은 일자 knock_event 파일이 없다면 knock_event 컬럼 빈 칸으로 wide 변환.
2) 같은 디바이스의 (move_detect, knock_event)가 같은 일자에 모두 있는 경우
   → 두 파일을 timestamp outer join.

이미 변환 스크립트(scripts/convert_*.py)가 직접 `data/move_knock/`에 쓰는 디바이스는
중복 변환이 되므로 본 스크립트는 그 device_id 셋을 제외한다.
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.devices import DEVICE_TYPES, last6  # noqa: E402

DEVICE_TYPE = "vibration_t1"
NEW_BUNDLE = "move_knock"
OLD_MOVE = "move_detect"
OLD_KNOCK = "knock_event"

# scripts/convert_*.py가 직접 새 bundle에 기록하는 디바이스(중복 마이그레이션 회피).
# Etac/UpgoPlus/WS9700의 7개 디바이스는 변환 스크립트가 처리하므로 본 마이그레이션 대상에서 제외.
EXCLUDED_DEVICE_IDS = {
    "lumi.4cf8cdf3c82a198",   # Etac turner pro
    "lumi.54ef4410007a5d2e",  # UpgoPlus R
    "lumi.54ef4410007a542b",  # UpgoPlus M
    "lumi.54ef4410007a5881",  # UpgoPlus L
    "lumi.4cf8cdf3c829efd",   # WS9700 R
    "lumi.4cf8cdf3c829aed",   # WS9700 M
    "lumi.4cf8cdf3c829b1b",   # WS9700 L
}


def now_kst_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fetch_meta(device_id: str) -> dict:
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


def read_old_csv(path: Path) -> list[tuple[str, str]]:
    """구 포맷(time,value) CSV에서 데이터 행만 (time, value) 리스트로 반환.
    `#` 메타 헤더와 컬럼 헤더는 자동 스킵.
    """
    if not path.exists():
        return []
    out: list[tuple[str, str]] = []
    with path.open("r", encoding="utf-8") as f:
        non_meta = [ln for ln in f if not ln.startswith("#")]
    if not non_meta:
        return out
    reader = csv.reader(non_meta)
    header = next(reader, None)
    if not header or header[0].strip() != "time":
        return out
    for r in reader:
        if len(r) >= 2 and r[0].strip():
            out.append((r[0].strip(), r[1].strip()))
    return out


def meta_lines_for(device_id: str, meta: dict, target_date: str, row_count: int, resources_str: str) -> list[str]:
    return [
        f"# device_id: {device_id}",
        f"# device_type: {DEVICE_TYPE}",
        f"# bundle: {NEW_BUNDLE}",
        f"# resources: {resources_str}",
        f"# alias: {meta.get('alias') or ''}",
        f"# install_location: {meta.get('install_location') or ''}",
        f"# install_date: {meta.get('install_date') or ''}",
        f"# registered_by: {meta.get('created_by_name') or ''}",
        f"# target_date: {target_date}",
        f"# generated_at: {now_kst_iso()} KST",
        f"# row_count: {row_count}",
    ]


def gather_target_devices() -> set[str]:
    """data/move_detect/ 또는 data/knock_event/ 하위에서 device_id 폴더를 수집."""
    devs: set[str] = set()
    for old in (OLD_MOVE, OLD_KNOCK):
        base = ROOT / "data" / old
        if not base.exists():
            continue
        for sub in base.iterdir():
            if sub.is_dir() and sub.name.startswith("lumi."):
                devs.add(sub.name)
    return devs


def gather_dates(device_id: str) -> set[str]:
    """해당 디바이스의 두 구 폴더에 존재하는 일자 셋(YYYY-MM-DD)을 합집합 반환."""
    dates: set[str] = set()
    for old in (OLD_MOVE, OLD_KNOCK):
        d = ROOT / "data" / old / device_id
        if not d.exists():
            continue
        for p in d.glob("*.csv"):
            stem = p.stem
            if len(stem) < 8 or not stem[:8].isdigit():
                continue
            dates.add(f"{stem[:4]}-{stem[4:6]}-{stem[6:8]}")
    return dates


def migrate_device(device_id: str, bundle_resources_str: str, bundle_columns: tuple[str, ...]) -> tuple[int, int]:
    meta = fetch_meta(device_id)
    suffix = last6(device_id)
    dates = gather_dates(device_id)
    out_dir = ROOT / "data" / NEW_BUNDLE / device_id
    out_dir.mkdir(parents=True, exist_ok=True)

    total_rows = 0
    files_written = 0
    for date_str in sorted(dates):
        compact = date_str.replace("-", "")
        fname = f"{compact}_{suffix}.csv"
        move_path = ROOT / "data" / OLD_MOVE / device_id / fname
        knock_path = ROOT / "data" / OLD_KNOCK / device_id / fname

        # ts → (move_detect_value, knock_event_value)
        joined: dict[str, list[str]] = {}
        # 같은 timestamp에서 한쪽이 여러 값을 가지면 마지막 값 유지 (Aqara 100건 pagedup과 동일 규칙).
        for ts, v in read_old_csv(move_path):
            joined.setdefault(ts, ["", ""])[0] = v
        for ts, v in read_old_csv(knock_path):
            joined.setdefault(ts, ["", ""])[1] = v

        rows = [[ts, md, ke] for ts, (md, ke) in sorted(joined.items())]
        out_path = out_dir / fname
        with out_path.open("w", encoding="utf-8", newline="") as f:
            for line in meta_lines_for(device_id, meta, date_str, len(rows), bundle_resources_str):
                f.write(line + "\n")
            w = csv.writer(f, lineterminator="\n")
            w.writerow(bundle_columns)
            w.writerows(rows)
        total_rows += len(rows)
        files_written += 1
    return files_written, total_rows


def main() -> None:
    bundle = next(b for b in DEVICE_TYPES[DEVICE_TYPE].bundles if b.key == NEW_BUNDLE)
    resources_str = ",".join(r.name for r in bundle.resources)

    devs = gather_target_devices() - EXCLUDED_DEVICE_IDS
    if not devs:
        print("no devices to migrate (excluded all or no old bundle folders).")
        return

    print(f"target devices: {len(devs)}")
    for did in sorted(devs):
        files, rows = migrate_device(did, resources_str, bundle.csv_columns)
        print(f"  {did}: files={files}  rows={rows}")


if __name__ == "__main__":
    main()
