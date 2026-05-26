"""SQLite 연결·스키마 초기화 (DESIGN.md §5).

테이블:
- users           : 로그인 계정
- devices         : 수집 대상 기기 (soft delete, partial unique on active, group_id FK)
- device_groups   : 동일 device_type 디바이스를 묶는 그룹 (DISPLAY.md §4.8 그룹 뷰)
- collection_jobs : 일별 수집 작업 이력 (device × bundle × date)
- system_alerts   : 웹 배너로 표시할 경고 알림 (DESIGN.md §7.6)
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config


# ─────────────────────────── 시간 유틸 ───────────────────────────

def now_kst_iso() -> str:
    """현재 시각을 KST ISO8601 문자열로 반환 (DB 저장 시각의 단일 진입점)."""
    from datetime import datetime, timezone, timedelta
    kst = timezone(timedelta(hours=9))
    return datetime.now(tz=kst).strftime("%Y-%m-%d %H:%M:%S")


def today_kst() -> str:
    """오늘 날짜(KST) YYYY-MM-DD."""
    from datetime import datetime, timezone, timedelta
    kst = timezone(timedelta(hours=9))
    return datetime.now(tz=kst).strftime("%Y-%m-%d")


def yesterday_kst() -> str:
    """어제 날짜(KST) YYYY-MM-DD. 일일 수집 대상 일자 산출에 사용."""
    from datetime import datetime, timezone, timedelta
    kst = timezone(timedelta(hours=9))
    return (datetime.now(tz=kst).date() - timedelta(days=1)).strftime("%Y-%m-%d")


# ─────────────────────────── 연결 / 커넥션 ───────────────────────────

def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """SQLite 연결 생성. row_factory로 dict-like 접근 가능.

    체크 동일 스레드 비활성화 → FastAPI 비동기 + APScheduler 백그라운드 양쪽에서 공용.
    write 작업은 짧고 빈번하지 않으므로 별도 락 불필요.
    """
    path = Path(db_path) if db_path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def cursor(conn: sqlite3.Connection) -> Iterator[sqlite3.Cursor]:
    """간단 커서 컨텍스트. autocommit 모드이므로 명시적 트랜잭션은 호출자가 BEGIN/COMMIT."""
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


# ─────────────────────────── 스키마 ───────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    is_admin        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

-- 디바이스 그룹 (DISPLAY.md §4.8).
-- 그룹은 혼합 device_type 디바이스를 포함할 수 있다. 디스플레이에서는 멤버 디바이스마다
-- 별도 행으로 표시되어 각자의 device_type 시각화 규칙(§4)을 따른다.
-- 한 디바이스는 한 그룹에만 소속 (devices.group_id 1:N).
CREATE TABLE IF NOT EXISTS device_groups (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL UNIQUE,
    device_type      TEXT,                    -- (정보용·선택) 그룹의 주 디바이스 종류 라벨. 강제 제약 없음.
    description      TEXT,
    created_by       INTEGER NOT NULL REFERENCES users(id),
    created_by_name  TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id        TEXT NOT NULL,
    device_id_upper  TEXT NOT NULL,
    device_type      TEXT NOT NULL,
    install_location TEXT,
    install_date     TEXT,
    alias            TEXT,
    enabled          INTEGER NOT NULL DEFAULT 1,
    group_id         INTEGER REFERENCES device_groups(id) ON DELETE SET NULL,
    created_by       INTEGER NOT NULL REFERENCES users(id),
    created_by_name  TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_by       INTEGER REFERENCES users(id),
    updated_by_name  TEXT,
    updated_at       TEXT,
    deleted_by       INTEGER REFERENCES users(id),
    deleted_by_name  TEXT,
    deleted_at       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_active_id
    ON devices(device_id) WHERE deleted_at IS NULL;
-- idx_devices_group 은 _ensure_devices_group_id() 에서 ALTER TABLE 후 생성한다.
-- 기존 DB에 group_id 컬럼이 없는 상태에서 executescript 가 인덱스를 만들면 실패하므로
-- SCHEMA_SQL 안에 두지 않는다.

-- 디바이스 변경/삭제 이력 (DESIGN.md §5, §7.4).
-- 수정 적용 직전 devices 행 스냅샷을 누적 저장. "삭제 이력" 화면은 이 테이블의
-- change_type='update' 행과 devices.deleted_at 행을 합쳐 렌더한다.
CREATE TABLE IF NOT EXISTS device_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    device_pk         INTEGER NOT NULL,
    change_type       TEXT NOT NULL,        -- 'update' | 'delete'
    changed_by        INTEGER REFERENCES users(id),
    changed_by_name   TEXT NOT NULL,
    changed_at        TEXT NOT NULL,
    changed_fields    TEXT,                 -- 쉼표 구분 필드명 목록 (예: 'enabled,alias'). NULL=구버전 기록.
    device_id         TEXT NOT NULL,
    device_id_upper   TEXT NOT NULL,
    device_type       TEXT NOT NULL,
    hub               TEXT NOT NULL,
    install_location  TEXT,
    install_date      TEXT,
    alias             TEXT,
    enabled           INTEGER NOT NULL,
    group_id          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_device_history_pk         ON device_history(device_pk);
CREATE INDEX IF NOT EXISTS idx_device_history_changed_at ON device_history(changed_at);

CREATE TABLE IF NOT EXISTS collection_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       TEXT NOT NULL,
    bundle_key      TEXT NOT NULL,
    target_date     TEXT NOT NULL,
    status          TEXT NOT NULL,
    record_count    INTEGER,
    file_path       TEXT,
    file_size_bytes INTEGER,
    started_at      TEXT,
    finished_at     TEXT,
    error_message   TEXT,
    UNIQUE(device_id, bundle_key, target_date)
);
CREATE INDEX IF NOT EXISTS idx_jobs_target_date ON collection_jobs(target_date);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON collection_jobs(status);

CREATE TABLE IF NOT EXISTS system_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL,
    level           TEXT NOT NULL,
    message         TEXT NOT NULL,
    details         TEXT,
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    resolved_by     INTEGER REFERENCES users(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_active_code
    ON system_alerts(code) WHERE resolved_at IS NULL;
"""


def _ensure_devices_hub(conn: sqlite3.Connection) -> None:
    """devices.hub 컬럼 보장 (DESIGN.md §15.3 SmartThings 통합 마이그레이션).

    멱등 처리:
    - 'vendor' 만 있으면 (이전 명칭) → RENAME COLUMN 으로 'hub' 로 변경.
    - 'hub' 도 'vendor' 도 없으면 (신규 DB) → ADD COLUMN.
    값 도메인: 'aqara' (Aqara Open API) | 'smartthings' (SmartThings PAT + CLI).
    기본값 'aqara' 로 기존 행은 자동 채워짐.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(devices)")}
    if "vendor" in cols and "hub" not in cols:
        # 이전 명칭 'vendor' → 'hub' 로 rename (SQLite 3.25+)
        conn.execute("ALTER TABLE devices RENAME COLUMN vendor TO hub")
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(devices)")}
    if "hub" not in cols:
        conn.execute("ALTER TABLE devices ADD COLUMN hub TEXT NOT NULL DEFAULT 'aqara'")
    # 이전 인덱스 정리 + 새 인덱스 생성
    try:
        conn.execute("DROP INDEX IF EXISTS idx_devices_vendor")
    except sqlite3.OperationalError:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_hub ON devices(hub)")


def _ensure_devices_group_id(conn: sqlite3.Connection) -> None:
    """devices.group_id 컬럼이 없으면 ALTER TABLE 로 추가 (기존 DB 마이그레이션).

    SQLite 는 ADD COLUMN 만 지원. group_id 는 NULL 허용 + device_groups(id) FK + ON DELETE SET NULL.
    REFERENCES 는 ALTER TABLE ADD COLUMN 시점에 선언 가능 (기존 행은 NULL 로 채워짐).
    인덱스도 멱등 생성.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(devices)")}
    if "group_id" not in cols:
        conn.execute(
            "ALTER TABLE devices ADD COLUMN group_id INTEGER "
            "REFERENCES device_groups(id) ON DELETE SET NULL"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_group ON devices(group_id)")


def _ensure_device_history_changed_fields(conn: sqlite3.Connection) -> None:
    """device_history.changed_fields 컬럼 보장 (DESIGN.md §5).

    PATCH 가 변경된 필드명 목록을 쉼표 구분으로 기록한다 (예: 'enabled,alias').
    구버전 기록은 NULL — UI 는 NULL 을 '필드 불명' 으로 표시.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(device_history)")}
    if "changed_fields" not in cols:
        conn.execute("ALTER TABLE device_history ADD COLUMN changed_fields TEXT")


def _purge_enabled_only_history(conn: sqlite3.Connection) -> None:
    """ON/OFF 토글만 기록한 과거 이력 행을 삭제 (DESIGN.md §7.5 정책 변경).

    `changed_fields = 'enabled'` 인 update 이력은 누적 가치가 낮다는 운영 정책에 따라
    더 이상 INSERT 하지 않으며, 기존 행도 일회성으로 제거한다. 멱등 — 이미 정리됐다면 0행 삭제.

    삭제 대상이 *정확히* 'enabled' 한 필드만 변경된 행이라, 다른 필드와 함께 변경된
    이력 (예: 'enabled,alias') 은 영향을 받지 않는다.
    """
    conn.execute(
        "DELETE FROM device_history WHERE change_type='update' AND changed_fields='enabled'"
    )


def _ensure_devices_updated_columns(conn: sqlite3.Connection) -> None:
    """devices.updated_by / updated_by_name / updated_at 컬럼 보장 (DESIGN.md §5).

    "편집 적용" 시점의 최종 수정자·수정 시각 기록용. 기존 DB 멱등 마이그레이션.
    NULL = 등록 후 한 번도 수정되지 않음 (devices.html 에서 등록일만 표시).
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(devices)")}
    if "updated_by" not in cols:
        conn.execute("ALTER TABLE devices ADD COLUMN updated_by INTEGER REFERENCES users(id)")
    if "updated_by_name" not in cols:
        conn.execute("ALTER TABLE devices ADD COLUMN updated_by_name TEXT")
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE devices ADD COLUMN updated_at TEXT")


def _ensure_device_groups_type_nullable(conn: sqlite3.Connection) -> None:
    """device_groups.device_type 의 NOT NULL 제약을 해제 (기존 DB 마이그레이션).

    초기 설계는 동일 device_type만 한 그룹에 묶도록 강제했으나, 혼합 종류 그룹을
    허용하도록 정책이 변경됨 (DISPLAY.md §4.8). SQLite 는 ALTER COLUMN 미지원이므로
    NOT NULL 인 경우에만 테이블을 재생성하여 데이터를 옮긴다. 이미 nullable 이면 no-op.
    """
    cols = list(conn.execute("PRAGMA table_info(device_groups)"))
    target = next((c for c in cols if c["name"] == "device_type"), None)
    # 테이블이 아직 없거나(SCHEMA_SQL 직후엔 이미 nullable로 생성) device_type이 nullable이면 종료.
    if target is None or target["notnull"] == 0:
        return
    # NOT NULL → nullable 로 변환 (테이블 재생성).
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE device_groups_new (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT NOT NULL UNIQUE,
            device_type      TEXT,
            description      TEXT,
            created_by       INTEGER NOT NULL REFERENCES users(id),
            created_by_name  TEXT NOT NULL,
            created_at       TEXT NOT NULL
        );
        INSERT INTO device_groups_new(id, name, device_type, description,
                                      created_by, created_by_name, created_at)
            SELECT id, name, device_type, description,
                   created_by, created_by_name, created_at
              FROM device_groups;
        DROP TABLE device_groups;
        ALTER TABLE device_groups_new RENAME TO device_groups;
        COMMIT;
        """
    )


def init_db(db_path: Path | str | None = None) -> None:
    """앱 기동 시 1회 호출. 테이블이 없으면 생성하고, 신규 컬럼·제약은 마이그레이션 (멱등)."""
    conn = get_connection(db_path)
    with cursor(conn) as cur:
        cur.executescript(SCHEMA_SQL)
    _ensure_devices_group_id(conn)
    _ensure_device_groups_type_nullable(conn)
    _ensure_devices_hub(conn)
    _ensure_devices_updated_columns(conn)
    _ensure_device_history_changed_fields(conn)
    _purge_enabled_only_history(conn)
    conn.close()


if __name__ == "__main__":
    # dry-run: 임시 DB로 스키마 생성 후 테이블/인덱스 목록 출력 (CLAUDE.md §3.1)
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        tmp = Path(tf.name)
    init_db(tmp)
    conn = get_connection(tmp)
    print("=== tables ===")
    for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        print(" ", r["name"])
    print("=== indexes ===")
    for r in conn.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        print(f"  {r['name']:30} on {r['tbl_name']}")
    conn.close()
    tmp.unlink(missing_ok=True)
