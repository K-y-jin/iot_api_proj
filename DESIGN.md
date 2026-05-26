# Aqara 데이터 자동 수집 시스템 설계서

## 1. 개요

Aqara 클라우드 Open API + SmartThings (OAuth 2.0 + CLI history) 두 허브로부터 매일 1회 자동으로 전일자 센서 데이터를 받아와 CSV 로 저장하고, 웹 UI 를 통해 다수의 사용자가 수집 대상을 설정·조회·시각화할 수 있는 시스템. 같은 device_type 이라도 hub 가 다르면 CSV 컬럼·디스플레이 추출 알고리즘이 분기된다 ([§15 SmartThings 통합](#15-smartthings-통합-확장-설계)).

### 1.1 기술 스택
| 구분 | 선택 | 비고 |
|------|------|------|
| 백엔드 | FastAPI + Jinja2 | 비동기 HTTP, 자동 OpenAPI 문서, 기존 `record_data.py` 로직 재사용 |
| 메타 DB | SQLite | 단일 파일, 동시 읽기 충분, 설치 불필요 |
| 시계열 데이터 | CSV 파일 | 요구사항 명시 |
| 스케줄러 | APScheduler | FastAPI 프로세스 내 cron 트리거 |
| 인증 | 단순 ID/PW + 세션 쿠키 | passlib(bcrypt) + Starlette SessionMiddleware |
| 프론트엔드 | Jinja2 + 최소 JS | 별도 SPA 불필요 |

### 1.2 운영 가정
- 실행 환경: 사내 Windows 서버 (24h 상시 가동)
- 시간대: 시스템 시간 = KST (UTC+9)
- 대상 사용자 수: 소규모 (수 명 ~ 수십 명)
- 데이터 보존: 무기한 (수동 삭제 전까지)

---

## 2. 요구사항 정리

### 2.1 기능 요구사항
| ID | 내용 | 권한 | 우선순위 |
|----|------|------|----------|
| F1 | 웹 브라우저 다중 사용자 접속 (로그인 없이도 조회 가능) | Public | 必 |
| F1a | 자동 기록 장치 목록 조회 | Public | 必 |
| F1b | 데이터 수집 현황(파일 수·용량·기간) 조회 | Public | 必 |
| F2 | 장치 추가/삭제 (로그인 필요) | 로그인 | 必 |
| F2a | 장치 추가: 타입(드롭다운) + Device ID(키보드 입력, 자동 대문자) + 설치 장소 + 설치 날짜(YYYY-MM-DD) | 로그인 | 必 |
| F2b | 장치 삭제 시 **삭제자 이름·삭제 날짜** 자동 기록 (soft delete) | 로그인 | 必 |
| F2c | 장치 활성화 토글 (수집 일시중지) | 로그인 | 권장 |
| F3 | 수집된 CSV 파일별 용량(byte) 및 데이터 생성 기간(가장 이른~늦은 일자) 조회 | Public | 必 |
| F4 | 매일 09:00 KST 트리거, 전일 00:00:00 ~ 23:59:59 (KST) 구간 수집 | (자동) | 必 |
| F5 | Access Token 만료 전 자동 refresh, 401 시 강제 갱신 후 재시도 | (자동) | 必 |
| F6 | 수집 실패 시 재시도 및 작업 이력(성공/실패/건수) 기록 | (자동) | 必 |
| F7 | 임의 날짜에 대해 수동 재수집 트리거 | admin | 권장 |
| F8 | CSV 파일 다운로드 (개별 일자) | Public | 권장 |
| F8a | 기간 선택(시작일~종료일) 후 다수 CSV를 zip으로 일괄 다운로드 또는 단일 CSV로 concat 다운로드 | Public | 권장 |
| F9 | Aqara API 호출 실패 시 refresh token으로 토큰 자동 재발급 후 1회 재시도 | (자동) | 必 |
| F10 | 토큰 재발급 또는 재시도까지 실패 시 웹 브라우저 상단에 경고 배너 표시 | Public | 必 |

### 2.2 비기능 요구사항
- 수집 누락 방지: 작업 이력으로 누락분 자동 보충
- 멱등성: 동일 (device, bundle, date) 재실행 시 덮어쓰기
- 단일 프로세스 가정 (다중 워커 동시 수집은 불필요)
- 장치 **삭제 이력 보존**: 물리적 삭제 대신 soft delete (감사 추적 가능)

---

## 3. 수집 대상 사양

수집 대상 기기 종류는 **코드에 고정**한다 (사용자가 신규 모델을 추가하려면 코드 수정 필요). 각 기기의 동작 특성·값 의미·CSV 포맷은 별도 문서 [DEVICE.md](DEVICE.md)에 정리한다.

### 3.1 Bundle 개념
하나의 **bundle** = 하나의 출력 CSV 파일. Bundle은 1개 이상의 resource를 포함하며, 여러 resource를 묶을 때는 **timestamp outer join으로 wide 포맷** CSV를 생성한다.

- Motion Sensor T1/P1: `motion_status` + `lux`를 **한 bundle**로 묶어 단일 CSV(`time, motion_status, lux`)로 저장.
- Vibration Sensor T1: `move_detect` + `knock_event`를 **한 bundle**(`move_knock`)로 묶어 단일 CSV(`time, move_detect, knock_event`)로 저장. ([DEVICE.md §4.3](DEVICE.md#43-csv-저장-형식-통합-wide-포맷))
- Door and Window Sensor T1: `magnet_status` 단일 bundle.

```python
# app/devices.py
DEVICE_TYPES = {
    "motion_t1": {
        "model": "lumi.motion.agl02",
        "display_name": "Motion Sensor T1",
        "display_name_ko": "모션 센서 T1",
        "sampling": "periodic+event",
        "bundles": [
            {
                "key": "motion_lux",        # CSV 파일 식별자
                "resources": [
                    {"id": "3.1.85", "name": "motion_status", "name_ko": "재실 감지 상태"},
                    {"id": "0.3.85", "name": "lux",           "name_ko": "조도"},
                ],
                "csv_columns": ["time", "motion_status", "lux"],
            },
        ],
    },
    "motion_p1": {
        "model": "lumi.motion.ac02",
        "display_name": "Motion Sensor P1",
        "display_name_ko": "모션 센서 P1",
        "sampling": "periodic+event",
        "bundles": [
            {
                "key": "motion_lux",
                "resources": [
                    {"id": "3.1.85", "name": "motion_status", "name_ko": "재실 감지 상태"},
                    {"id": "0.3.85", "name": "lux",           "name_ko": "조도"},
                ],
                "csv_columns": ["time", "motion_status", "lux"],
            },
        ],
    },
    "door_t1": {
        "model": "lumi.magnet.agl02",
        "display_name": "Door and Window Sensor T1",
        "display_name_ko": "열림/닫힘 센서 T1",
        "sampling": "event",
        "bundles": [
            {
                "key": "magnet_status",
                "resources": [
                    {"id": "3.1.85", "name": "magnet_status", "name_ko": "자석 접점 상태"},
                ],
                "csv_columns": ["time", "magnet_status"],
            },
        ],
    },
    "vibration_t1": {
        "model": "lumi.vibration.agl01",
        "display_name": "Vibration Sensor T1",
        "display_name_ko": "진동 센서 T1",
        "sampling": "event",
        "bundles": [
            {
                "key": "move_knock",   # CSV 파일 식별자 (move_detect + knock_event 통합)
                "resources": [
                    {"id": "13.7.85", "name": "move_detect", "name_ko": "움직임 감지"},
                    {"id": "13.3.85", "name": "knock_event", "name_ko": "두드림 이벤트"},
                ],
                "csv_columns": ["time", "move_detect", "knock_event"],
            },
        ],
    },
    "switch_t1": {
        "model": "lumi.remote.b1acn02",
        "display_name": "Wireless Mini Switch T1",
        "display_name_ko": "무선 미니 스위치 T1",
        "sampling": "event",
        "bundles": [
            {
                "key": "switch_status",
                "resources": [
                    {"id": "13.1.85", "name": "switch_status", "name_ko": "스위치 클릭 이벤트"},
                ],
                "csv_columns": ["time", "switch_status"],
            },
        ],
    },
    "vibration_aq1": {
        "model": "lumi.vibration.aq1",
        "display_name": "Vibration Sensor (aq1)",
        "display_name_ko": "진동 센서 (aq1)",
        "sampling": "event",
        "bundles": [
            {
                "key": "vibration_event",
                "resources": [
                    # 모든 이벤트 종류를 단일 resource에 코드 값으로 통합 (DEVICE.md §6).
                    {"id": "13.1.85", "name": "vibration_event", "name_ko": "진동 이벤트(통합)"},
                ],
                "csv_columns": ["time", "vibration_event"],
            },
        ],
    },
    "temp_humi_t1": {
        "model": "lumi.sensor_ht.agl02",
        "display_name": "Temperature and Humidity Sensor T1",
        "display_name_ko": "온습도 센서 T1",
        "sampling": "periodic",
        # 양 hub 가 모두 부동소수 측정값을 보고하므로 컬럼명 통일 (DEVICE.md §8).
        # bundle key 는 hub 공통, hub 별 resources 만 다르다.
        "bundles": [
            {
                "key": "temp_humi",
                "resources": [
                    {"id": "0.1.85", "name": "temperature_value", "name_ko": "온도(°C)"},
                    {"id": "0.2.85", "name": "humidity_value",    "name_ko": "상대습도(%)"},
                ],
                "csv_columns": ["time", "temperature_value", "humidity_value"],
            },
        ],
    },
}
```

> ℹ️ 현재 코드의 `DEVICE_TYPES` 는 위 7종(+ SmartThings 전용 `motion_and_light_p2`)을 모두 포함한다 (`app/devices.py` 참조). 추가/수정 시 본 절과 [DEVICE.md §0 요약표](DEVICE.md#0-요약표)를 동시에 갱신한다.

### 3.2 장치 등록 UX
사용자는 로그인 후 다음을 입력하여 등록한다.

| 필드 | UI | 처리 |
|---|---|---|
| 기기 종류 | **드롭다운** (DEVICE_TYPES 키 + 한국어 명칭) | `device_type` 저장 |
| Device ID | 텍스트 입력 (CSS `text-transform: uppercase` + JS `input` 이벤트로 입력값 자체를 대문자 치환) | 표시: `4CF8CDF3C752EDB` / 저장: `device_id=lumi.4cf8cdf3c752edb`, `device_id_upper=4CF8CDF3C752EDB` |
| 설치 장소 | 텍스트 입력 (예: "거실") | `install_location` |
| 설치 날짜 | `<input type="date">` (브라우저 데이트 피커, YYYY-MM-DD 강제) | `install_date` |
| 별명 | 텍스트 입력 (선택) | `alias` |

- ID 정규화 로직: `record_data.py`의 `normalize_subject_id` 재사용.
- 동일 device_id가 **현재 활성** 상태로 이미 존재하면 등록 거부 (`idx_devices_active_id` 위반 → "이미 등록된 장치입니다").
- 과거 삭제된 동일 ID는 새 row로 재등록 가능 (이전 row의 삭제 이력은 보존).

각 기기당 해당 종류의 **모든 bundle**을 매일 수집한다.

> **참고**: `sampling` 필드는 수집 결과 해석용. `event` 타입은 일별 행 수가 **0건일 수 있으며 이는 정상**(record_count=0이라도 `success`).
>
> **값 기록 차이 메모**:
> - Motion T1/P1의 `motion_status`는 occupied(`1`)만 기록, 해지 시 별도 값 없음.
> - Vibration T1의 `move_detect`는 활성(`1`)과 **해지(`255`)** 양쪽 기록.
> - 후처리 시 occupied 구간 종료 시점은 `motion_status`만으로 판정 불가 → 다음 샘플까지의 timeout 또는 lux 단절로 추정 필요.

---

## 4. 디렉토리 구조

```
aqara_api_proj/
├── app/
│   ├── __init__.py
│   ├── main.py                # FastAPI 앱 진입점, 라우터 등록, 스케줄러 시작 (--dry-run 지원)
│   ├── config.py              # APPID/KEYID/APPKEY, 경로, 시간대, cron 시간 등 환경값
│   ├── db.py                  # SQLite 연결, 스키마 초기화 + 마이그레이션 (CREATE TABLE IF NOT EXISTS + _ensure_*)
│   ├── devices.py             # DEVICE_TYPES 고정 매핑 (Aqara/SmartThings 8종)
│   ├── aqara_client.py        # Aqara API 호출(서명, fetch.resource.history, refreshToken, call_with_auto_refresh)
│   ├── token_manager.py       # access/refresh 토큰 tokens.json 저장·로드·만료 판단
│   ├── alerts.py              # system_alerts 헬퍼 (raise/resolve, 동일 code upsert)
│   ├── collector.py           # 일일 수집 워크플로우 (페이지네이션, wide-join CSV 작성, backfill)
│   ├── display_extract.py     # 일자별 CSV → interval/point 추출 (DISPLAY.md §4 시각화 규칙)
│   ├── scheduler.py           # APScheduler 인스턴스 + 3개 cron job (09:00 수집, 03:00 토큰, N분 헬스체크)
│   ├── auth.py                # 로그인/로그아웃, 세션 의존성, 비밀번호 해싱, admin 시드
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── pages.py           # HTML 페이지 (GET /, /login, /devices, /data, /jobs, /admin/*) + human_bytes Jinja 필터
│   │   ├── display.py         # 디바이스/그룹 활동 타임라인 (DISPLAY.md SSOT)
│   │   └── api.py             # JSON API (devices/groups CRUD, 데이터 다운로드, 수동 트리거, 토큰/알림)
│   ├── templates/
│   │   ├── base.html
│   │   ├── login.html
│   │   ├── dashboard.html
│   │   ├── devices.html       # 활성/삭제 탭 + 그룹 관리 미니 섹션 (DISPLAY.md §4.8)
│   │   ├── data.html
│   │   ├── data_files.html    # /data/{device_id}/{bundle_key} 일자별 파일 목록
│   │   ├── display.html       # /display/{device_id} 단일 디바이스 타임라인 SVG
│   │   ├── display_group.html # /display/group/{group_id} 그룹 타임라인 (멤버=행)
│   │   ├── _device_timeline.html  # display.html / display_group.html 공통 트랙 partial
│   │   ├── jobs.html
│   │   ├── admin_token.html
│   │   └── admin_users.html
│   └── static/
│       └── style.css
├── data/                      # CSV 저장 루트 (gitignore)
│   └── {bundle_key}/{device_id}/{YYYYMMDD}_{last6}.csv
│       # last6 = device_id의 마지막 6자리(대문자 hex), 예: 'lumi.4cf8cdf3c752edb' → '752EDB'
│       # 예: data/motion_lux/lumi.4cf8cdf3c752edb/20260511_752EDB.csv
│       #     data/move_knock/lumi.4cf8cdf3c829aed/20260511_829AED.csv
│       #     data/magnet_status/lumi.xxx/20260511_XXXXXX.csv
│       # bundle_key를 1차 디렉토리로 두면 동종 데이터(동일 컬럼 셋)를 한 폴더에서 일괄 비교/병합하기 쉽다.
│       # device·bundle 정보는 폴더 경로 + CSV 내부의 `#` 메타 헤더에서 식별 (파일명은 짧게 유지)
├── app.db                     # SQLite (gitignore)
├── tokens.json                # access/refresh 토큰 파일 (gitignore, 권한 제한)
├── requirements.txt
├── run.bat                    # Windows 실행 스크립트 (uvicorn app.main:app)
├── DESIGN.md
├── README.md
└── (기존 파일 보존: record_data.py, refresh_access_token.py, *.ipynb, 기존 csv)
```

---

## 5. 데이터 모델 (SQLite)

```sql
-- 사용자 계정
CREATE TABLE users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    is_admin        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL          -- ISO8601 KST
);

-- 디바이스 그룹 (DISPLAY.md §4.8).
-- 디바이스를 묶어 디스플레이에서 함께 시각화한다.
-- 여러 device_type의 디바이스를 혼합 등록할 수 있으며, 디스플레이에서는
-- 멤버 디바이스마다 별도 행으로 표시되어 각자의 시각화 규칙(DISPLAY.md §4)을 따른다.
-- 한 디바이스는 한 그룹에만 소속 (devices.group_id 1:N).
CREATE TABLE device_groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    device_type     TEXT,                  -- (정보용·선택) 그룹의 주 디바이스 종류 라벨. 강제 제약 없음.
    description     TEXT,
    created_by      INTEGER NOT NULL REFERENCES users(id),
    created_by_name TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- 수집 대상 기기 (사용자가 등록, soft delete)
CREATE TABLE devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       TEXT NOT NULL,         -- 정규화된 lumi.xxxx 형식 (소문자)
    device_id_upper TEXT NOT NULL,         -- 화면 표시용 대문자 hex (예: '4CF8CDF3C752EDB')
    device_type     TEXT NOT NULL,         -- DEVICE_TYPES key
    hub             TEXT NOT NULL DEFAULT 'aqara',  -- 'aqara' | 'smartthings' (DESIGN.md §15.3)
    install_location TEXT,                 -- 설치 장소 (예: '거실', '안방')
    install_date    TEXT,                  -- 설치 날짜 (YYYY-MM-DD, KST)
    alias           TEXT,                  -- 사용자 지정 별명 (선택)
    enabled         INTEGER NOT NULL DEFAULT 1,
    group_id        INTEGER REFERENCES device_groups(id) ON DELETE SET NULL,  -- 소속 그룹 (선택)
    created_by      INTEGER NOT NULL REFERENCES users(id),
    created_by_name TEXT NOT NULL,         -- 등록 시점의 username 스냅샷 (사용자 삭제 시에도 보존)
    created_at      TEXT NOT NULL,         -- KST ISO8601 (등록일, 변경 없음)
    updated_by      INTEGER REFERENCES users(id),
    updated_by_name TEXT,                  -- 최종 수정자 username 스냅샷
    updated_at      TEXT,                  -- 최종 수정 시각 KST ISO8601 (NULL=수정 이력 없음)
    deleted_by      INTEGER REFERENCES users(id),
    deleted_by_name TEXT,                  -- 삭제 시점의 username 스냅샷
    deleted_at      TEXT                   -- KST ISO8601 (NULL이면 활성)
);
-- 같은 device_id의 동시 활성 등록은 1건만 (삭제 후 재등록은 허용)
CREATE UNIQUE INDEX idx_devices_active_id
    ON devices(device_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_devices_group ON devices(group_id);

-- 디바이스 변경/삭제 이력 (수정 적용 직후 스냅샷 + 삭제 이벤트 기록).
-- "변경/삭제 이력" UI 는 이 테이블 + devices 의 deleted_at 행을 합쳐 표시한다 (DESIGN.md §7.4).
-- change_type='update'  : 수정 발생 시 devices 의 *적용 후* 컬럼 값을 통째로 스냅샷
--                         (사용자가 이력 표에서 "이 시점에 디바이스가 이렇게 됐다" 를 직관적으로 확인하기 위함)
--                         단, `changed_fields='enabled'` 인 ON/OFF 단독 토글은 기록 제외 (운영 정책).
-- change_type='delete'  : (옵션) 명시적 삭제 이벤트 별도 기록 — 기본 흐름은 devices.deleted_* 컬럼을
--                         그대로 사용하므로 이 값은 향후 확장용으로 예약.
CREATE TABLE device_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    device_pk         INTEGER NOT NULL,    -- devices.id (FK 강제하지 않음 — 디바이스 hard purge 대비)
    change_type       TEXT NOT NULL,       -- 'update' | 'delete'
    changed_by        INTEGER REFERENCES users(id),
    changed_by_name   TEXT NOT NULL,       -- 변경자 username 스냅샷
    changed_at        TEXT NOT NULL,       -- KST ISO8601
    changed_fields    TEXT,                -- 변경된 필드명 쉼표 구분 (예: 'enabled,alias'). NULL=구버전 기록 / delete 행.
    -- 수정 적용 후(또는 삭제 직전) 스냅샷
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
-- 보관 정책: 자동 정리 없음 (무제한). admin 이 `DELETE /api/devices/history/{id}` 로 무의미한
-- 토글 이력 등을 수동 정리할 수 있다 (DESIGN.md §7.2).
CREATE INDEX idx_device_history_pk        ON device_history(device_pk);
CREATE INDEX idx_device_history_changed_at ON device_history(changed_at);

-- 일별 수집 작업 이력 (device × bundle × date 단위, 1 row = 1 CSV)
CREATE TABLE collection_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       TEXT NOT NULL,         -- lumi.xxxx
    bundle_key      TEXT NOT NULL,         -- DEVICE_TYPES[type].bundles[].key (예: 'motion_lux', 'knock_event')
    target_date     TEXT NOT NULL,         -- YYYY-MM-DD (수집 대상 일자, KST)
    status          TEXT NOT NULL,         -- 'pending'|'running'|'success'|'failed'
    record_count    INTEGER,               -- 통합 CSV의 경우 outer join 후 총 행 수
    file_path       TEXT,                  -- 저장된 CSV 상대경로
    file_size_bytes INTEGER,
    started_at      TEXT,
    finished_at     TEXT,
    error_message   TEXT,
    UNIQUE(device_id, bundle_key, target_date)   -- 멱등성 보장 (재실행 시 덮어쓰기)
);
CREATE INDEX idx_jobs_target_date ON collection_jobs(target_date);
CREATE INDEX idx_jobs_status ON collection_jobs(status);

-- 시스템 경고 알림 (웹 배너로 표시)
CREATE TABLE system_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL,         -- 'token_refresh_failed' | 'aqara_persistent_error' 등
    level           TEXT NOT NULL,         -- 'info' | 'warning' | 'error'
    message         TEXT NOT NULL,         -- 사용자에게 표시할 한 줄 메시지
    details         TEXT,                  -- 진단용 상세 (Aqara 응답 본문 등)
    created_at      TEXT NOT NULL,         -- KST ISO8601
    resolved_at     TEXT,                  -- NULL = active (배너 표시), 값 있음 = 해제됨
    resolved_by     INTEGER REFERENCES users(id)  -- 수동 dismiss 시
);
-- 동일 code의 active 알림이 최대 1개만 존재하도록 (raise_alert이 upsert로 동작)
CREATE UNIQUE INDEX idx_alerts_active_code
    ON system_alerts(code) WHERE resolved_at IS NULL;
```

토큰은 별도 `tokens.json` 파일에 저장 (DB에 보안 데이터 두지 않고 OS 파일 권한으로 보호):
```json
{
    "access_token": "...",
    "refresh_token": "...",
    "refreshed_at": "2026-05-12 03:00:00",
    "expires_at":   "2026-05-13 03:00:00"
}
```

---

## 6. 핵심 워크플로우

### 6.1 일일 수집 (Daily Collection)
APScheduler cron 작업 (DESIGN.md §11):
- **09:00 KST** — 전일 1일 수집 (`collect_yesterday`)
- **03:00 KST** — Aqara 토큰 선제 갱신 (`_job_proactive_token_refresh`)
- **03:30 KST** — `collection_jobs` 보관 기간 초과 행 정리 (`prune_old_jobs`). 기본 보관 28일 (`config.JOB_HISTORY_RETENTION_DAYS`). 수집된 CSV 파일은 보존된다 — `/data` 화면이 파일시스템 walk 기준이라 통계는 유지.
- **매시간** — 최근 7일 내 failed/누락 보충 (`backfill_missing`).

```
collect_yesterday():
    target_date = (today_kst() - 1 day).strftime("%Y-%m-%d")
    start = f"{target_date} 00:00:00"
    end   = f"{target_date} 23:59:59"
    for device in db.list_enabled_devices():
        for bundle in DEVICE_TYPES[device.device_type]["bundles"]:
            run_one_bundle(device.device_id, bundle, target_date, start, end)

run_one_bundle(device_id, bundle, target_date, start, end):
    upsert job(device_id, bundle.key, target_date) status='running'
    try:
        # 각 resource를 페이지네이션으로 끝까지 수집
        per_resource = {}                                  # name -> {ts: value}
        for resource in bundle.resources:
            rows = fetch_paginated(device_id, resource.id, start, end)
            per_resource[resource.name] = dict(rows)       # ts → value (중복 제거)

        # 단일 resource bundle: time,value 그대로 저장
        # 멀티 resource bundle: timestamp outer join → wide 포맷
        if len(bundle.resources) == 1:
            name = bundle.resources[0].name
            out_rows = [(ts, v) for ts, v in sorted(per_resource[name].items())]
        else:
            all_ts = sorted({ts for d in per_resource.values() for ts in d})
            out_rows = [
                (ts, *[per_resource[r.name].get(ts, "") for r in bundle.resources])
                for ts in all_ts
            ]
        last6 = device_id_upper(device_id)[-6:]          # 'lumi.4cf8cdf3c752edb' → '752EDB'
        date_compact = target_date.replace("-", "")      # '2026-05-11' → '20260511'
        fname = f"{date_compact}_{last6}.csv"
        path  = f"data/{bundle.key}/{device_id}/{fname}"
        write_csv_with_meta_header(path,
            meta={
                "device_id":    device_id,
                "device_type":  device.device_type,
                "bundle":       bundle.key,
                "resources":    ",".join(r.name for r in bundle.resources),
                "target_date":  target_date,
                "generated_at": now_kst_iso(),
                "row_count":    len(out_rows),
            },
            header=bundle.csv_columns, rows=out_rows)
        update job: status='success', record_count=len(out_rows),
                    file_path=path, file_size_bytes=stat(path).size
    except Exception as e:
        update job: status='failed', error_message=str(e)

fetch_paginated(device_id, resource_id, start, end):
    # Aqara API는 시간 내림차순(최신 먼저)으로 최대 100건을 반환한다 (record_data.py 검증).
    # 따라서 endTime을 batch의 마지막 원소(=가장 오래된) 시각 직전으로 줄여가며
    # 과거 방향으로 페이지네이션한다. 100건/req가 한도이므로 len(batch) < 100이면 종료.
    rows = []
    cursor_end = end
    while True:
        batch = aqara.fetch_history(device_id, resource_id, start, cursor_end)  # [(kst, value), ...] 내림차순
        rows += batch
        if len(batch) < 100: break               # Aqara 100건/req 페이지네이션 종결조건
        cursor_end = prev_second(batch[-1][0])   # 가장 오래된 시각 - 1초로 endTime 이동
    return rows
```

- **Wide 포맷 빈 셀 표현**: outer join 시 한쪽 resource에만 샘플이 있는 timestamp의 결측 컬럼은 빈 문자열로 출력 (`,,` 형태). pandas에서 `read_csv(..., na_values=[''])` 로 NaN 처리 가능.
- **중복 timestamp 처리**: Aqara가 동일 ts에 여러 value를 반환하는 경우는 드물지만, dict로 받아 마지막 값 유지.
- **누락분 자동 보충**: 앱 기동 시 또는 매시간 헬스체크 job이 최근 7일 내 `failed`/누락 (device, bundle, date) 조합을 찾아 재시도 큐에 적재.
- **CSV 인코딩**: UTF-8, LF 개행.

#### CSV 메타 헤더 (`#` 주석 라인, 옵션 A 채택)
파일 최상단에 `#`로 시작하는 메타 라인을 두고, 그 다음 줄부터 실제 컬럼 헤더 + 데이터.

```csv
# device_id: lumi.4cf8cdf3c752edb
# device_type: motion_t1
# bundle: motion_lux
# resources: motion_status,lux
# alias: 거실 모션센서
# install_location: 거실 천장
# install_date: 2026-04-01
# registered_by: 홍길동
# target_date: 2026-05-11
# generated_at: 2026-05-12 00:32:15 KST
# row_count: 14
time,motion_status,lux
2026-05-11 13:10:08,1,5
2026-05-11 13:11:06,1,97
...
```

- `alias`/`install_location`/`install_date`/`registered_by`는 `devices` 테이블의 활성 행에서 가져온 값. 미등록 디바이스(과거 수동 import)·dry-run 환경에서는 값만 빈 문자열, 라인 위치는 유지.
- pandas 분석: `pd.read_csv(path, comment='#')` 한 줄로 메타를 자동 건너뛴다.
- Excel은 첫 N행을 데이터로 인식하므로 사용자에게 "메타 라인은 모두 건너뛰세요" 안내 (또는 데이터 메뉴 → 텍스트 가져오기에서 `#` 시작 행 제외 설정).
- **메타 라인 수는 가변**(향후 항목 추가 가능)하므로 행 수 하드코딩 대신 `comment='#'` 옵션 사용을 권장.
- 0건 CSV의 경우에도 메타 헤더는 동일하게 작성 (`row_count: 0` + 컬럼 헤더만 1줄).
- concat 다운로드 시: 첫 파일의 메타 헤더만 유지, 이후 파일은 메타·컬럼헤더 모두 스킵.

### 6.2 토큰 관리
- **저장**: `tokens.json` (서비스 시작 시 로드, 메모리 캐시; refresh 성공 시 즉시 덮어쓰기).
- **반응형 갱신 (주 메커니즘)**: Aqara API 호출 결과가 **오류**(HTTP non-2xx 또는 응답 `code != 0`)이면 access token 만료로 간주하고 **즉시 refresh token으로 새 토큰 쌍을 발급받아 저장한 뒤 동일 요청을 1회 재시도**한다.
- **2차 실패 처리**: refresh 자체가 실패하거나 새 토큰으로 재시도한 요청도 실패하면, `system_alerts`에 active 알림을 생성하여 **웹 브라우저 상단 배너로 노출**한다 ([§7.6](#76-시스템-경고-알림-배너) 참조). 해당 job은 `failed` 처리.
- **선제 갱신 (보조)**: 매일 03:00 KST cron job이 `expires_at - now < 24h` 이면 `config.auth.refreshToken`을 미리 호출 (수집 시점에 만료 충돌 회피).
- **초기 부트스트랩**: 관리자 화면(`/admin/token`)에서 최초 refresh_token을 1회 입력. 이후 자동 순환.

#### 토큰 갱신 흐름 의사코드
```python
# app/aqara_client.py
def call_with_auto_refresh(intent, data):
    resp = http_post(API_URL, headers=auth_headers(token_manager.access), json={...})
    if not is_ok(resp):
        # 1차 실패 → 토큰 만료 가정, refresh 시도
        try:
            new = aqara.refresh_token(token_manager.refresh)   # intent=config.auth.refreshToken
            token_manager.save(new)                            # tokens.json 즉시 덮어쓰기
        except Exception as e:
            alerts.raise_alert(
                code="token_refresh_failed",
                level="error",
                message="Aqara 토큰 갱신 실패. 관리자가 refresh token을 재등록해야 합니다.",
                details=str(e),
            )
            raise

        # 새 토큰으로 1회 재시도
        resp = http_post(API_URL, headers=auth_headers(token_manager.access), json={...})
        if not is_ok(resp):
            alerts.raise_alert(
                code="aqara_persistent_error",
                level="error",
                message=f"토큰 갱신 후에도 Aqara API 호출이 실패했습니다. "
                        f"(intent={intent}, code={resp.code}, msg={resp.message})",
                details=resp.text[:1000],
            )
            raise APIError(resp)

        # 재시도 성공 → 이전 활성 알림 자동 해제
        alerts.resolve_by_code(["token_refresh_failed", "aqara_persistent_error"])
    return resp
```

- `is_ok(resp)`: HTTP 2xx **그리고** body의 `code == 0`.
- `raise_alert(code, ...)`: 동일 `code`의 active 알림이 이미 있으면 새로 만들지 않고 `details`만 갱신 (중복 배너 방지).
- 정상 호출이 1회라도 성공하면 관련 active 알림은 자동 `resolved`.

### 6.3 사용자 인증
- 비밀번호: `passlib[bcrypt]`로 해시.
- 세션: `SessionMiddleware`(서명 쿠키), 만료 8시간.
- 첫 실행 시 환경변수 `ADMIN_INITIAL_PASSWORD`로 `admin` 계정 자동 생성. admin이 추가 계정 발급(`/admin/users`).
- **공개(비로그인) 접근**: 장치 목록 조회·데이터 수집 현황·CSV 다운로드.
- **로그인 필요**: 장치 추가/삭제/토글·수동 재수집·admin 페이지.
- 비로그인 사용자는 add/delete 버튼이 **렌더링되지 않으며**(클라이언트 노출 차단) 서버 측에서도 변경 라우트에 `require_login` 의존성을 적용한다 (이중 방어).
- **수집 대상 풀은 전사 공유** (사용자별 분리 X). `created_by`/`deleted_by` 및 username 스냅샷은 감사 용도.

#### 권한 매트릭스
| 동작 | 비로그인 | 로그인 | admin |
|---|:-:|:-:|:-:|
| 장치 목록 조회 | ✅ | ✅ | ✅ |
| 데이터 현황 조회 | ✅ | ✅ | ✅ |
| CSV 다운로드 | ✅ | ✅ | ✅ |
| 작업 이력 조회 | ✅ | ✅ | ✅ |
| 장치 추가 | ❌ | ✅ | ✅ |
| 장치 삭제 (soft) | ❌ | ✅ | ✅ |
| 장치 활성/비활성 토글 | ❌ | ✅ | ✅ |
| 단건 수동 재수집 (`/api/jobs/run`) | ❌ | ❌ | ✅ |
| 일괄 수동 수집 (`/api/jobs/bulk_run`, 기간 지정) — 활성 장치 전체 × 기간 | ❌ | ❌ | ✅ |
| 사용자/토큰 관리 | ❌ | ❌ | ✅ |

---

## 7. 화면 및 라우트

### 7.1 페이지 (HTML)
| Method | Path | 권한 | 설명 |
|--------|------|------|------|
| GET | `/` | Public | 대시보드: 전체 기기 수, 어제 수집 성공/실패, 누적 용량 |
| GET | `/devices` | Public 조회 | 활성 장치 + 변경/삭제 이력 + 그룹 관리. 로그인 시 추가/편집/삭제/토글/일괄 ON-OFF 버튼 노출. **설치 장소 드롭다운 필터** + 일괄 ON/OFF + 인라인 편집 폼(적용 버튼). admin 은 변경 이력 단일 삭제 가능. |
| GET | `/data` | Public | 데이터 현황: device×bundle별 파일 수, 총 용량, 최초~최종 수집일자. **설치 장소 드롭다운 필터** + 표시/전체 카운터. |
| GET | `/display/{device_id}?to=&days=` | 로그인 | 디바이스 활동 타임라인 시각화 (1주일×1일/행). hub-agnostic URL — Aqara `lumi.<hex>` 와 SmartThings UUID/24-hex 모두 매치. 시각화 규칙·레이아웃·엣지 케이스는 [DISPLAY.md](DISPLAY.md) 참조 |
| GET | `/display/group/{group_id}?to=&days=` | 로그인 | 그룹 멤버 디바이스를 device_type 별 패널로 합산 시각화 (DISPLAY.md §4.8) |
| GET | `/data/{device_id}/{bundle_key}` | 로그인 | 일자별 CSV 파일 목록(파일별 크기, 행 수) + 다운로드 링크. CSV 다운로드는 로그인 사용자만. |
| GET | `/jobs` | Public | 최근 작업 이력 (상태 필터, 날짜 필터) |
| GET | `/login` | Public | 로그인 폼 |
| POST | `/login` | Public | 인증 처리 |
| POST | `/logout` | 로그인 | 세션 종료 |
| GET | `/admin/token` | admin | 초기 refresh_token 등록, 현재 토큰 상태 표시 |
| GET | `/admin/users` | admin | 사용자 추가/제거 |

### 7.2 JSON API (AJAX 또는 외부 자동화)
| Method | Path | 권한 | 설명 |
|--------|------|------|------|
| GET | `/api/devices?include_deleted=0` | Public | 활성 장치 목록 (선택적으로 삭제 이력 포함) |
| POST | `/api/devices` | 로그인 | 기기 등록 `{device_type, device_id_input, install_location, install_date, alias?}` — 서버가 ID 정규화 + `created_by`/`created_by_name`/`created_at` 자동 기록 |
| DELETE | `/api/devices/{id}` | 로그인 | **Soft delete**: `deleted_at`, `deleted_by`, `deleted_by_name` 자동 설정 (행 보존, 미래 수집 중단) |
| PATCH | `/api/devices/{id}` | 로그인 | 등록자·등록일을 제외한 모든 속성 변경 (`device_id_input`/`device_type`/`hub`/`enabled`/`alias`/`install_location`/`install_date`/`group_id`). 적용 직후 스냅샷이 `device_history` 에 기록되고, `updated_at`/`updated_by_name` 이 갱신된다. `group_id=null` 은 그룹 해제. **예외**: `enabled` 만 단독 변경된 경우 이력에 기록하지 않는다(토글 누적 가치 낮음). |
| GET | `/api/devices/history` | Public | 디바이스 변경/삭제 이력 목록 (수정 적용 후 스냅샷 + 삭제 행 통합) |
| POST | `/api/devices/bulk_enable` | 로그인 | 모든 활성 장치의 `enabled` 를 일괄 ON/OFF. `{enabled: bool}` — `device_history` 에는 기록하지 않는다(토글 누적 가치 낮음). `updated_at`/`updated_by_name` 만 갱신. `{ok, changed, total}` 반환 |
| DELETE | `/api/devices/history/{history_id}` | admin | 단일 `device_history` 행 삭제. admin 이 무의미한 토글 이력 등을 수동 정리할 때 사용. 'delete' 이력은 devices 테이블 의 soft-delete 행에 묶여 있어 이 라우트로 삭제 불가. |
| GET | `/api/groups` | Public | 그룹 목록 + 각 그룹 멤버 수 |
| POST | `/api/groups` | 로그인 | 그룹 생성 `{name, device_type?(정보용), description?}` — `name` 유일 |
| DELETE | `/api/groups/{id}` | 로그인 | 그룹 삭제 (멤버 디바이스의 `group_id`는 ON DELETE SET NULL로 자동 해제) |
| GET | `/api/data/summary` | Public | device×bundle별 집계(총 byte, 시작/종료일, 일수) |
| GET | `/api/data/{device}/{bundle_key}/files` | 로그인 | 일자별 파일 메타 (CSV 다운로드 사전 정보) |
| GET | `/api/data/{device}/{bundle_key}/{date}/download` | 로그인 | 단일 일자 CSV 다운로드 |
| GET | `/api/data/{device}/{bundle_key}/bundle?from=&to=&format=zip\|concat` | 로그인 | 기간 선택 일괄 다운로드. `format=zip`은 일자별 CSV를 묶은 zip, `format=concat`은 헤더 1회 + 일자 오름차순으로 이어붙인 단일 CSV (스트리밍 응답) |
| POST | `/api/jobs/run` | admin | 단건 수동 수집 `{device_id, bundle_key, target_date}` |
| POST | `/api/jobs/bulk_run` | admin | 일괄 수동 수집 `{from, to}` (YYYY-MM-DD) — 활성 장치 × 기간 모든 일자를 백그라운드로 수집. 최대 31일. 202 Accepted 반환 후 `/jobs` 페이지에서 진행 상황 확인. |
| GET | `/api/jobs?from=&to=&status=` | Public | 작업 이력 |
| GET | `/api/alerts` | Public | active 시스템 경고 알림 목록 (배너 폴링용) |
| POST | `/api/alerts/{id}/resolve` | 로그인 | 알림 수동 해제 (`resolved_at`, `resolved_by` 기록) |
| GET | `/api/admin/smartthings_token/status` | admin | SmartThings 인증 상태 — 활성 source(`pat-env`/`pat-file`/`oauth`) + 마스킹된 토큰 + (OAuth) 갱신·만료 시각 |
| POST | `/api/admin/smartthings_pat` | admin | admin 입력 PAT 저장(stopgap). `{pat}` — `/v1/devices` 호출로 즉시 검증 후 `tokens_smartthings_pat.json` 에 저장. 환경변수 `SMARTTHINGS_PAT` 가 있으면 무시됨 |
| DELETE | `/api/admin/smartthings_pat` | admin | 저장된 PAT 파일 삭제 → OAuth 또는 환경변수 PAT 경로로 복귀 |

### 7.3 "데이터 현황" 화면 (F3 핵심)
- 행: device × bundle
- 열: 별명 / 기기 종류 / Bundle 이름(포함 resource 표시) / 파일 수 / 총 용량(KB·MB 환산) / 최초 수집일 / 최종 수집일 / 총 레코드 수
- **용량 표시**: Jinja2 필터 `human_bytes` ([app/routes/pages.py](app/routes/pages.py))를 통해 1024 base · 소수 1자리로 환산 (`512 B` / `1.5 KB` / `1.5 MB` / `1.0 GB`). 셀의 `title` 속성에는 원본 byte 값을 동시 노출 → 마우스 오버 시 정확한 수치 확인 가능. 같은 필터를 대시보드(`/`) 누적 용량, 파일 목록(`/data/{}/{}`) 용량 컬럼, 작업 이력(`/jobs`) 용량 컬럼에 모두 사용.
- **데이터 소스**: `data/{bundle_key}/{device_id}/{YYYYMMDD}_{last6}.csv` 파일시스템 walk로 수집 (CSV 파일이 SSOT, [DISPLAY.md §5](DISPLAY.md#5-데이터-조회-전략)와 일관). 파일명 `YYYYMMDD` 접두로 일자 추출, `# row_count: N` 메타 라인 파싱으로 레코드 수 합산. 파일 크기·일자 범위는 `stat()` + 파일명에서 즉시 산출.
  - 외부 자동 수집뿐 아니라 수동 import한 CSV(예: 과거 데이터 변환분)도 자연스럽게 표시됨.
  - 디바이스 메타데이터(별명/종류/설치 위치)는 `devices` 테이블에서 active 행(`deleted_at IS NULL`)을 dict로 enrich. orphan(완전 삭제된 device의 잔여 CSV)은 "(deleted)"로 표기.
  - 성능: 1주~수개월 데이터 수십~수천 파일은 즉시(< 100ms). 수만 파일 이상이 되면 LRU 캐시 검토.
- Motion T1/P1은 통합 bundle이므로 한 행으로 표시되고 record_count는 outer join 후 행 수임에 유의.

#### 기간 선택 일괄 다운로드 (F8a)
각 행의 "다운로드" 컬럼에 두 입력(시작일·종료일 date picker, 기본값 = 최초·최종 수집일) + 포맷 선택(zip/concat) + 버튼을 둔다.

- **zip**: 일자별 `{YYYYMMDD}_{last6}.csv` 파일들을 `{from_compact}-{to_compact}_{last6}_{bundle_key}.zip`으로 묶어 응답. 누락 일자는 zip에 미포함. (예: `20260501-20260511_752EDB_motion_lux.zip`)
- **concat**: 첫 파일의 `#` 메타 헤더 + 컬럼 헤더를 1회만 출력하고, 일자 오름차순으로 데이터 행만 이어붙인 단일 CSV를 **스트리밍**(`StreamingResponse`)으로 응답 → 메모리에 전체 로드하지 않음. 파일명 예시: `{from_compact}-{to_compact}_{last6}_{bundle_key}.csv`.
- 누락 일자(`collection_jobs`에 row 없거나 `status != 'success'`)는 응답 본문에 영향 없고, 응답 헤더 `X-Missing-Dates`에 콤마로 나열 (사용자가 확인 가능).
- 너무 긴 기간 보호: `to - from > 365일`이면 400 반환 (필요 시 admin은 조정).

### 7.4 "장치 목록" 화면 (F1a/F2)
**활성 장치 탭** (기본):
| 컬럼 | 비고 |
|---|---|
| 기기 종류 (한국어 명칭) | DEVICE_TYPES `display_name_ko` |
| Device ID | `device_id_upper` 표시 (대문자 hex) |
| 별명 | `alias` |
| 설치 장소 | `install_location` |
| 설치 날짜 | `install_date` |
| 그룹 | `group_id` → 그룹명. 로그인 시 드롭다운으로 변경/해제. 그룹 목록은 모든 종류의 그룹을 포함. |
| 등록자 / 등록일 | `created_by_name` / `created_at` (변경 불가). 수정 이력이 있으면 같은 셀에 `updated_at` (수정자) 보조 표기. |
| 수집 상태 | `enabled` 토글 (로그인 시) |
| 작업 | ✏️ 편집 / 🗑️ 삭제 (로그인 시). 편집은 모달/인라인 폼 → **적용** 버튼으로만 확정 (`PATCH /api/devices/{id}`). |

페이지 하단에 **그룹 관리 미니 섹션**(로그인 시)을 두어 그룹 생성·삭제와 멤버 수 조회를 제공한다.

상단에는 **일괄 수동 수집 섹션**(admin 전용)을 두어 기간(from·to YYYY-MM-DD)을 지정하면
활성 장치 전체 × 기간 내 모든 일자를 백그라운드로 수집한다 (`POST /api/jobs/bulk_run`).
- 최대 기간: **31일** (Aqara API 호출량·토큰 만료 위험 완화)
- 동작: 요청 즉시 202 Accepted 응답을 받고 백그라운드 스레드에서 일자별로
  `collector.collect_for_date()` 를 순차 호출. 결과는 기존 `collection_jobs` 테이블에 그대로 기록되어
  `/jobs` 페이지에서 진행/성공/실패를 확인할 수 있다.
- 토큰 미등록 시 즉시 400 (admin이 `/admin/token` 에 refresh_token 등록 필요).

**변경/삭제 이력 탭**:
| 컬럼 | 비고 |
|---|---|
| 구분 | `update` (수정 적용) / `delete` (삭제) |
| 기기 종류 / Device ID / 설치 장소 / 별명 / 설치 날짜 / hub | 스냅샷 시점 값 |
| 등록자 / 등록일 | devices.created_by_name / devices.created_at (행이 hard-purge 되지 않은 한 join 으로 표기) |
| **변경/삭제자** | update → `device_history.changed_by_name`, delete → `devices.deleted_by_name` |
| **변경/삭제 일시** | update → `device_history.changed_at`, delete → `devices.deleted_at` |

> 같은 디바이스가 여러 번 수정된 경우 각 적용 시점마다 한 행이 누적된다 (적용 후 값을 스냅샷).

비로그인 상태에서는 두 탭 모두 조회 가능하지만 토글·편집·삭제 버튼은 렌더링되지 않는다.

### 7.5 장치 추가/삭제 동작 의사코드
```python
# POST /api/devices  (로그인 필요)
def add_device(payload, current_user):
    upper = payload.device_id_input.strip().upper()
    norm  = normalize_subject_id(upper)              # → 'lumi.4cf8cdf3c752edb'
    validate_date(payload.install_date)              # YYYY-MM-DD 파싱
    validate_device_type(payload.device_type)        # DEVICE_TYPES 키 검증
    now = now_kst_iso()
    db.insert(devices,
        device_id=norm,
        device_id_upper=upper.replace('LUMI.', ''),
        device_type=payload.device_type,
        install_location=payload.install_location,
        install_date=payload.install_date,
        alias=payload.alias,
        enabled=1,
        created_by=current_user.id,
        created_by_name=current_user.username,
        created_at=now)
    # 활성 중복은 idx_devices_active_id (UNIQUE WHERE deleted_at IS NULL)가 차단

# PATCH /api/devices/{id}  (로그인 필요)
def update_device(id, payload, current_user):
    """등록자(created_by/name)·등록일(created_at) 제외 모든 속성 편집.

    같은 트랜잭션 안에서 새 값으로 UPDATE 한 뒤, *적용 후* 의 devices 행을 다시 읽어
    device_history 에 스냅샷(change_type='update')으로 남긴다. updated_at/updated_by_name 도 함께 기록.
    device_id/device_type/hub 변경은 정규화·hub 지원 검증 + active unique 충돌 검사 후 진행.
    """
    cur = db.fetch_one("SELECT * FROM devices WHERE id=? AND deleted_at IS NULL", id)
    if cur is None:
        raise NotFound
    now = now_kst_iso()
    db.execute("UPDATE devices SET <변경된 컬럼>=?, "
               "    updated_at=?, updated_by=?, updated_by_name=? "
               " WHERE id=?", ..., now, current_user.id, current_user.username, id)
    new_row = db.fetch_one("SELECT * FROM devices WHERE id=?", id)
    db.insert(device_history,
        device_pk=new_row.id, change_type='update',
        changed_by=current_user.id, changed_by_name=current_user.username, changed_at=now,
        # devices 의 모든 식별/메타 컬럼을 *적용 후* 값 그대로 복사
        device_id=new_row.device_id, device_id_upper=new_row.device_id_upper,
        device_type=new_row.device_type, hub=new_row.hub,
        install_location=new_row.install_location, install_date=new_row.install_date,
        alias=new_row.alias, enabled=new_row.enabled, group_id=new_row.group_id)

# DELETE /api/devices/{id}  (로그인 필요)
def delete_device(id, current_user):
    db.execute("""
        UPDATE devices
           SET deleted_at=?, deleted_by=?, deleted_by_name=?
         WHERE id=? AND deleted_at IS NULL
    """, (now_kst_iso(), current_user.id, current_user.username, id))
    # 이후 일일 수집 루프에서 list_enabled_devices()가 deleted_at IS NULL 조건으로 자동 제외
    # 기존 수집된 CSV·collection_jobs는 보존 (조회·다운로드 가능)
```

- **삭제 후 수집된 CSV 처리**: 삭제는 미래 수집만 중단하며 과거 데이터는 유지된다. 데이터 현황 화면에서는 삭제된 장치도 "삭제된 장치(과거)" 섹션에 표시 가능.
- **물리적 삭제(완전 제거)** 가 필요하면 admin 전용 별도 라우트(`POST /api/devices/{id}/purge`)로 분리. 본 설계 범위에는 포함하지 않는다.

### 7.6 시스템 경고 알림 배너
모든 페이지 상단(`base.html`의 헤더 직하)에 **경고 배너 영역**을 두고, `system_alerts` 테이블의 `resolved_at IS NULL` 행을 `level` 색상으로 렌더링한다.

**배너 UI**:
- `level=error`: 빨간색 배경 (예: 토큰 갱신 실패)
- `level=warning`: 노란색 배경
- `level=info`: 파란색 배경
- 메시지 우측에 ✕ 닫기 버튼 (**로그인 사용자만 표시**, 클릭 시 `POST /api/alerts/{id}/resolve`)
- 비로그인 사용자에게도 메시지는 동일하게 보이지만 닫기 불가

**갱신 방식**:
- 페이지 로드 시 서버측에서 active 알림을 직접 렌더링 (SSR).
- 추가로 클라이언트 JS가 **60초마다** `GET /api/alerts`를 폴링하여 새 알림 출현 시 배너 동적 추가, 자동 해제 시 배너 제거.
- WebSocket은 사용하지 않음 (구현 복잡도 대비 이득 적음).

**자동 해제 규칙**:
- 토큰 갱신 + 재시도가 1회 이상 정상 성공하면 `token_refresh_failed`/`aqara_persistent_error` 코드의 active 알림을 `resolved_at=now` 처리 (resolver=system).
- 사용자 dismiss 시 `resolved_by=current_user.id`.

**예시 메시지**:
- "Aqara 토큰 갱신 실패. 관리자가 `/admin/token`에서 refresh token을 재등록해야 합니다."
- "토큰 갱신 후에도 Aqara API 호출이 실패했습니다. (intent=fetch.resource.history, code=108, msg=...)"

---

## 8. Aqara API 통합

기존 `record_data.py`와 `refresh_access_token.py`의 함수를 모듈로 정리:

```python
# app/aqara_client.py
class AqaraClient:
    def fetch_history(self, device_id, resource_id, start_kst, end_kst) -> list[tuple[str,str]]:
        # 1) 서명 생성 (MD5 of "Accesstoken=...&Appid=...&Keyid=...&Nonce=...&Time=..." + APPKEY, lowercased)
        # 2) POST intent=fetch.resource.history
        # 3) call_with_auto_refresh()로 감싸서 호출
        #    - 1차 실패 시 token_manager.refresh() → 새 토큰 저장 → 1회 재시도
        #    - refresh 실패 또는 재시도도 실패 시 system_alerts에 경고 등록 후 예외 발생
        # 4) 100건 페이지네이션은 호출부(collector)에서 처리
        ...

    def refresh_token(self, refresh_token: str) -> dict:
        # intent=config.auth.refreshToken
        ...
```

`record_data.py`의 `kst_to_utc_millis`, `normalize_subject_id`, `get_list_of_kst_and_value` 로직을 그대로 이전한다.

---

## 9. 시간대 처리 규칙
- 사용자 표시·CSV 파일명·API 입력 시각: **KST (UTC+9)**
- Aqara API 요청 timestamp 본문: **UTC milliseconds**
- DB 저장 시각(`created_at`, `started_at` 등): KST ISO8601 문자열
- `target_date`: KST 자정 기준 일자 (`YYYY-MM-DD`)

`record_data.py`의 `kst_to_utc_millis` 변환을 표준 함수로 사용.

---

## 10. 에러 / 엣지 케이스
| 케이스 | 처리 |
|--------|------|
| Aqara 응답 `code != 0` | job `failed`, `error_message`에 code/message 기록, 알림 X (다음 헬스체크에서 재시도) |
| 빈 결과(0건) | job `success`, `record_count=0`, 빈 헤더만 있는 CSV 생성 (또는 파일 미생성 + 메모) — **빈 CSV 생성** 선택 (구간별 수집 완료 증빙) |
| 100건 정확히 1페이지 | `len(batch)==100`이면 한 번 더 요청해 0/<100건 확인 후 종료 |
| 페이지네이션 중복 timestamp | `(time,value)` 튜플 dedupe |
| Aqara 7일 제한 | 일일 수집은 1일 구간이므로 무관. 수동 재수집도 1일 단위만 허용 |
| 서버 재시작 후 누락 | 기동 직후 + 매시간 헬스체크 job이 최근 7일 누락분 보충 |
| 같은 기기 중복 등록 | `devices.device_id UNIQUE` 위반 시 사용자 친화 메시지 |

---

## 11. 배포 / 실행

```bat
:: run.bat
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

상시 가동은 **NSSM**(Non-Sucking Service Manager)으로 Windows 서비스 등록 권장. (스케줄러가 앱 내장이므로 별도 작업 스케줄러 등록 불필요.)

`requirements.txt`:
```
fastapi>=0.110
uvicorn[standard]>=0.27
jinja2>=3.1
itsdangerous>=2.1
python-multipart>=0.0.9
passlib[bcrypt]>=1.7.4
bcrypt<4.1            # passlib 1.7.4 호환 상한 (bcrypt 4.1+에서 detect_wrap_bug 크래시)
apscheduler>=3.10
requests>=2.31
python-dotenv>=1.0    # .env 자동 로드 (config.py)
```

---

## 12. 단계별 구현 계획

| 단계 | 산출물 | 검증 |
|------|--------|------|
| 1 | `aqara_client.py` + `token_manager.py` + `collector.py` (CLI에서 1일 수집 가능) | `python -m app.collector --date 2026-05-11` 로 CSV 생성 확인 |
| 2 | SQLite 스키마 + `devices.py` + admin 사용자 시드 | 1단계 결과를 DB 작업이력으로 기록 |
| 3 | APScheduler 등록 + 헬스체크 job | 시각 변경 테스트(임시 cron `*/2 * * * *`)로 트리거 확인 |
| 4 | FastAPI 라우트 + 로그인 + 기기 CRUD | 브라우저에서 기기 등록 → 다음 수집 사이클에 반영 확인 |
| 5 | 데이터 현황·작업 이력 화면 + CSV 다운로드 | 다중 일자 수집 후 집계 일치 확인 |
| 6 | 수동 재수집 + admin 토큰 관리 | 강제 토큰 갱신 / 임의 날짜 재수집 동작 확인 |
| 7 | NSSM 서비스 등록 + 운영 문서(README) | 재부팅 후 자동 기동 |

---

## 13. 기존 파일 처리
- `record_data.py`, `refresh_access_token.py`, `*.ipynb`, 기존 CSV는 **삭제하지 않고 보존**. 새 시스템은 `app/` 하위에서만 동작. 기존 CSV의 데이터 이관은 별도 ETL 스크립트로 수동 처리 가능.

---

## 14. 보안 메모
- `tokens.json`, `app.db`는 `.gitignore`. 파일시스템 권한은 운영자 계정만 read/write.
- APPKEY 등 비밀값은 `app/config.py`에 하드코딩 대신 환경변수(`AQARA_APPKEY` 등) 우선 로드, 미설정 시 `.env` 로드 (python-dotenv).
- 비밀번호 bcrypt 해시 (라운드 12).
- HTTPS는 사내 리버스 프록시(Nginx/IIS)에서 종단 처리 가정.

---

## 15. SmartThings 통합 (확장 설계)

기존 Aqara 시스템에 **Samsung SmartThings** 디바이스 데이터 수집을 추가한다. 본 절은
아직 미구현(설계만)이며, 구현 시 §3 ~ §10 의 패턴을 그대로 따라 hub 분기를 더한다.
참고: 기존 `./samsung/` 폴더의 독립 스크립트들이 동작 검증되어 있어 그 로직을 흡수한다.

### 15.1 개요

| 항목 | Aqara (기존) | SmartThings (신규) |
|---|---|---|
| 인증 | APPID/KEYID/APPKEY + access/refresh token (auto-refresh) | **OAuth 2.0 Authorization Code flow** — client_id/secret + access/refresh token. `Authorization: Bearer <access_token>` 헤더 |
| 토큰 만료 | ~7일, refresh token 으로 자동 갱신 | access_token 약 24h, **refresh_token 으로 자동 갱신** (만료 임박 시 호출 직전 + 03:00 cron 선제 갱신) |
| API base | `https://open-kr.aqara.com/v3.0/open/api` | `https://api.smartthings.com/v1` |
| 시계열 조회 | REST `fetch.resource.history` (100건/페이지) | **`smartthings` CLI binary** subprocess (`smartthings devices:history <id> -L <limit> -j --token <access_token>`) — 직접 REST 미공개 |
| Device ID 형식 | `lumi.<hex>` (예: `lumi.4cf8cdf3c829aed`) | UUID (예: `0a59334e-c81f-4081-a501-f09048b9cca9`) |
| 데이터 단위 | resource_id 별 단일 시계열 | (capability, attribute) 페어 — 한 디바이스가 다수 동시 노출 |
| 시간 입출력 | 요청: UTC ms, 응답: UTC ms | 응답: ISO8601 UTC (`2026-05-08T02:51:26.000+00:00`). CLI 옵션은 `-L limit` 외 시각 필터 부재 — 후처리에서 KST 비교 |

### 15.2 토큰 관리 — OAuth 2.0 + PAT stopgap

**정식 인증**: OAuth 2.0 Authorization Code grant + refresh token (SmartThings 가 지속 사용 시 권장).
**stopgap**: OAuth 클라이언트 준비 전 빠른 가동을 위해 Personal Access Token 도 허용.

`smartthings_client.get_access_token()` 의 우선순위 (먼저 매치되는 값 사용):
1. **`config.SMARTTHINGS_PAT`** (환경변수, 운영자 잠금)
2. **`tokens_smartthings_pat.json`** (admin 이 `/admin/token` 페이지에서 직접 입력해 저장)
3. **OAuth access_token** (`tokens_smartthings.json`, 만료 임박 시 refresh_token 으로 자동 갱신)

PAT 경로(1·2)는 refresh 흐름이 없다 — 만료/거부 시 admin 이 새 PAT 로 교체.

**OAuth 선행 작업 (운영자, 코드 외부)**:
- SmartThings Developer Workspace 에서 **OAuth-In 클라이언트** 생성 → `client_id` / `client_secret` 발급.
- 클라이언트에 redirect_uri 등록: `{앱 주소}/admin/smartthings/oauth/callback`.
  - SmartThings 정책상 `https://` 필수, **예외로 `http://localhost`** 만 허용. 사내 IP·도메인은 https 종단 처리 필요 (Nginx/IIS/Caddy 등) 또는 ngrok 같은 임시 https 터널로 일회성 발급.
- `.env` 에 `SMARTTHINGS_CLIENT_ID` / `SMARTTHINGS_CLIENT_SECRET` / `SMARTTHINGS_OAUTH_REDIRECT_URI` / `SMARTTHINGS_OAUTH_SCOPE` 설정.

**OAuth 인증 흐름** (앱 내장 콜백):
1. admin 이 `/admin/token` 의 "SmartThings OAuth 연결" 클릭 → `GET /admin/smartthings/oauth/start`.
   - CSRF 방지용 `state` 를 세션에 저장하고 SmartThings authorize URL 로 303 리다이렉트.
2. admin 이 SmartThings 로그인·승인 → SmartThings 가 `redirect_uri` 로 `?code=&state=` 리다이렉트.
3. `GET /admin/smartthings/oauth/callback` — 세션의 `state` 와 대조 후, `code` 를 token 엔드포인트에 교환
   (`grant_type=authorization_code`, client_id/secret 은 HTTP Basic) → access/refresh 토큰 저장.
4. 결과를 `/admin/token?st_oauth=<keyword>` 로 리다이렉트해 사용자에게 표시.
   > admin 접속 origin 과 `redirect_uri` 의 origin 이 일치해야 세션 쿠키·state 유지. 다르면 콜백이 익명 요청으로 도착해 403 또는 state_mismatch.

**OAuth 토큰 저장·갱신**:
- `tokens_smartthings.json` 에 `{access_token, refresh_token, refreshed_at, expires_at}` (KST) 저장.
- `get_access_token()` 이 REST/CLI 호출 직전 만료(임박, 5분 skew)를 검사하고, 그렇다면 `refresh_token` 으로 자동 갱신 (`grant_type=refresh_token`). refresh 응답의 refresh_token 도 회전되므로 새 값으로 덮어쓴다.
- 03:00 cron(`_job_proactive_token_refresh`)이 만료 6h 이내면 선제 갱신 (Aqara 24h 임계와 별개).
- refresh 실패(만료/거부) 시 `system_alerts` 에 `smartthings_token_invalid` 등록 → admin 이 재연결 필요.
- access_token 은 SmartThings CLI 의 `--token` 인자에도 그대로 bearer 로 전달된다.

**PAT stopgap (admin UI / 환경변수)**:
- `POST /api/admin/smartthings_pat` — admin 입력 PAT 저장 + 즉시 `/v1/devices` 호출 검증. 성공 시 `tokens_smartthings_pat.json` 에 `{pat, saved_at}` 형식으로 저장. 실패 시 롤백.
- `DELETE /api/admin/smartthings_pat` — 저장된 PAT 파일 삭제 → OAuth 또는 환경변수 PAT 경로로 복귀.
- 환경변수(`SMARTTHINGS_PAT`)는 파일보다 우선되며, 그 값이 설정된 동안 admin UI 의 PAT 입력은 *무시*된다 (운영자 잠금).
- `smartthings_token_status()` 의 `source` 필드: `pat-env` / `pat-file` / `oauth` / 없음.

### 15.3 DB 스키마 변경

`devices` 테이블에 `hub` 컬럼 추가 (기존 행은 `'aqara'` 로 기본값).

```sql
-- 마이그레이션: devices 에 hub 컬럼 추가 (기본값 'aqara').
ALTER TABLE devices ADD COLUMN hub TEXT NOT NULL DEFAULT 'aqara';
CREATE INDEX IF NOT EXISTS idx_devices_vendor ON devices(hub);
```

- 기존 행 자동 'aqara' 채워짐 → 호환성 유지.
- 새 컬럼은 `app/db.py` 의 `_ensure_*` 패턴으로 멱등 마이그레이션 (`_ensure_devices_hub`).
- 활성 unique 인덱스 `idx_devices_active_id` 는 device_id 단독이므로
  Aqara/SmartThings 가 같은 hex/uuid를 가질 가능성은 없다고 봐도 안전 (geometrically separated).
  필요 시 `(hub, device_id)` 복합 unique 로 확장.

### 15.4 디바이스 종류 매핑 (`SMARTTHINGS_DEVICE_TYPES`)

SmartThings 디바이스는 `(capability, attribute)` 조합이 매우 다양하므로 §3 의
하드코딩 `DEVICE_TYPES` 와 동일하게 **사용 대상만 화이트리스트로 등록**한다.
한 SmartThings 디바이스가 여러 capability 를 노출해도 우리가 관심 갖는 1개 bundle 만 정의.

```python
# app/smartthings_devices.py (또는 devices.py 확장)
# 각 type 은 (capability, attribute) 페어 1개 또는 N개 (wide bundle) 를 지정.
SMARTTHINGS_DEVICE_TYPES = {
    "st_contact": DeviceType(
        key="st_contact", model="contactSensor",
        display_name="Contact Sensor", display_name_ko="열림/닫힘 센서 (SmartThings)",
        sampling="event",
        bundles=(Bundle(
            key="contact",
            resources=(Resource("contactSensor.contact", "contact", "접점 상태"),),
            csv_columns=("time", "contact"),
        ),),
    ),
    "st_motion": DeviceType(
        key="st_motion", model="motionSensor",
        display_name="Motion Sensor", display_name_ko="모션 센서 (SmartThings)",
        bundles=(Bundle(
            key="motion", resources=(Resource("motionSensor.motion","motion","움직임"),),
            csv_columns=("time", "motion"),
        ),),
    ),
    "st_occupancy": DeviceType(  # 와츠매터-재실센서 류
        key="st_occupancy", model="occupancySensor",
        display_name_ko="재실 센서 (SmartThings)",
        bundles=(Bundle(
            key="occupancy", resources=(Resource("occupancySensor.occupancy","occupancy","재실"),),
            csv_columns=("time", "occupancy"),
        ),),
    ),
    "st_temp_humid": DeviceType(  # 와츠매터-온습도센서 류
        key="st_temp_humid", model="temperatureMeasurement+relativeHumidityMeasurement",
        display_name_ko="온습도 센서 (SmartThings)",
        bundles=(Bundle(
            key="temp_humid",
            resources=(
                Resource("temperatureMeasurement.temperature", "temperature", "온도(°C)"),
                Resource("relativeHumidityMeasurement.humidity", "humidity", "습도(%)"),
            ),
            csv_columns=("time", "temperature", "humidity"),   # wide outer join
        ),),
    ),
    "st_smoke": DeviceType(
        key="st_smoke", model="smokeDetector",
        display_name_ko="연기 센서 (SmartThings)",
        bundles=(Bundle(
            key="smoke", resources=(Resource("smokeDetector.smoke","smoke","연기"),),
            csv_columns=("time", "smoke"),
        ),),
    ),
    "st_switch": DeviceType(
        key="st_switch", model="switch",
        display_name_ko="스위치 (SmartThings)",
        bundles=(Bundle(
            key="switch", resources=(Resource("switch.switch","switch","ON/OFF"),),
            csv_columns=("time", "switch"),
        ),),
    ),
    "st_button": DeviceType(
        key="st_button", model="button",
        display_name_ko="버튼 (SmartThings)",
        bundles=(Bundle(
            key="button", resources=(Resource("button.button","button","눌림 이벤트"),),
            csv_columns=("time", "button"),
        ),),
    ),
    # Aqara Motion and Light Sensor P2 — Matter 디바이스로 SmartThings 터널을 통해 접근 (§15.12).
    # Aqara motion_t1/p1 과 동일 bundle 키(`motion_lux`) + 컬럼명 `lux` 통일.
    # 단 모션 컬럼은 Aqara='motion_status'(1만) / SmartThings='motion'(active/inactive) 으로
    # 값 인코딩이 달라 컬럼명을 의도적으로 분리. 폴더는 device_id 로 격리되므로 schema 혼합 무문제.
    "motion_and_light_p2": DeviceType(
        key="motion_and_light_p2",
        model="Aqara Motion and Light Sensor P2 (Matter)",
        display_name="Aqara Motion and Light Sensor P2",
        display_name_ko="Aqara 모션·조도 센서 P2",
        sampling="periodic+event",
        bundles=(Bundle(
            key="motion_lux",
            resources=(
                Resource("motionSensor.motion", "motion", "움직임 (active/inactive)"),
                Resource("illuminanceMeasurement.illuminance", "lux", "조도(lux)"),
            ),
            csv_columns=("time", "motion", "lux"),
        ),),
    ),
}
```

- `Resource.id` 는 `"<capability>.<attribute>"` 문자열로 두어 응답 매칭에 사용.
- `DEVICE_TYPES` 단일 dict 로 합치는 것보다 hub 별 dict 를 두고 라우팅 단에서
  `hub=='aqara'` → `AQARA_DEVICE_TYPES`, `hub=='smartthings'` → `SMARTTHINGS_DEVICE_TYPES`
  로 dispatch 하는 편이 검증/표시 분리에 깔끔. 두 dict 의 합집합을 노출하는 `DEVICE_TYPES_ALL`
  헬퍼만 추가.
- 추가 capability 가 필요해지면 화이트리스트에 등록하면 됨. 등록되지 않은 capability 의
  이벤트는 수집 단계에서 무시(스킵).

### 15.5 클라이언트 모듈 (`app/smartthings_client.py`)

`aqara_client.py` 와 동등한 인터페이스 + 분기:

```python
# app/smartthings_client.py
class SmartThingsAPIError(RuntimeError):
    """SmartThings API 호출 실패 (HTTP 4xx/5xx, CLI 비정상 종료, JSON 파싱 실패 등)."""

# OAuth: code 교환 / refresh / authorize URL (DESIGN.md §15.2)
def oauth_authorize_url(state: str) -> str: ...
def exchange_code_for_tokens(code: str) -> None: ...
def refresh_smartthings_tokens() -> None: ...
def get_access_token() -> str:
    """유효한 access_token 반환 — 만료 임박 시 refresh_token 으로 자동 갱신."""

def _bearer_headers() -> dict[str, str]:
    """Authorization: Bearer <access_token> + Accept: application/json. get_access_token() 사용."""

# REST: 디바이스/장소 목록
def list_devices() -> list[dict]: ...
def list_locations() -> list[dict]: ...

# CLI 기반 history: subprocess 로 smartthings CLI 호출 후 JSON 파싱
def fetch_device_history(device_id: str, limit: int = 1000) -> list[dict]:
    """대상 디바이스의 최근 N건 history (capability/attribute/value/time 포함).

    CLI: `smartthings devices:history <device_id> -L <limit> -j --token <access_token>`
    응답은 시간 내림차순 (Aqara 와 동일). 직접 페이지네이션 API 가 없으므로
    `limit` 안에서 가능한 만큼 받고, 부족하면 사용자에게 limit 증가 안내
    (samsung/export_all_history_by_date.py 의 `truncated_devices` 로직 흡수).
    """

# 일자/구간 필터 + bundle 매칭
def fetch_history_for_bundle(
    device_id: str, bundle: Bundle, start_kst: str, end_kst: str, limit: int = 1000,
) -> dict[str, list[tuple[str, str]]]:
    """단일 디바이스 × bundle 의 각 resource(capability.attribute) 별 (kst_time, value) 리스트.

    1) fetch_device_history(device_id, limit) 호출.
    2) item.capability == bundle.resources[i].capability AND item.attribute == ... 만 필터.
    3) item.time(UTC ISO) → KST 'YYYY-MM-DD HH:MM:SS' 변환.
    4) start_kst <= t <= end_kst 인 행만 유지.
    반환: {resource_name: [(kst, value), ...]} — collector 가 wide outer join 에 그대로 사용.
    """
```

- 토큰 401/403 시 `system_alerts.raise_alert('smartthings_token_invalid', level='error', ...)`.
- CLI 미설치 시 (`shutil.which('smartthings') is None`) 명확한 에러 + 알림.
- Rate limit (HTTP 429) 시 `Retry-After` 기반 단순 백오프 (samsung/smartthings_export.py 패턴 그대로).

### 15.6 수집 워크플로우 분기 (`app/collector.py` 확장)

`run_one_bundle` 직전에 hub 분기를 추가한다.

```python
def run_one_bundle(device_id, device_type, bundle, target_date, page_fetcher=None, device_meta=None):
    # ... 기존 job_id upsert ...
    try:
        hub = (device_meta or {}).get("hub", "aqara")
        if hub == "aqara":
            per_resource = _fetch_aqara_bundle(device_id, bundle, target_date, page_fetcher)
        elif hub == "smartthings":
            per_resource = _fetch_smartthings_bundle(device_id, bundle, target_date)
        else:
            raise ValueError(f"unknown hub: {hub}")
        # ... wide outer join → write_bundle_csv ...
```

- `_fetch_aqara_bundle` 은 현행 `fetch_history_paginated` 호출 그대로.
- `_fetch_smartthings_bundle` 은 `smartthings_client.fetch_history_for_bundle` 호출.
- 일일 cron (09:00) `collect_yesterday` 는 hub 무관하게 모든 활성 장치 순회 → 자연스럽게 hub 별로 분기.

#### 15.6.1 일일 cron 시점의 SmartThings 흐름 (09:00 KST)

기존 `_job_daily_collect` → `collect_yesterday()` → `collect_for_date(yesterday_kst())` 사이클 안에서
SmartThings 디바이스는 다음 알고리즘으로 수집된다:

1. `cursor_before_ms = (target_date + 1d 00:00:00 KST).epoch_ms` — 다음 날 자정 직전까지의 이벤트.
2. CLI 호출 `smartthings devices:history <id> -L <page_size> -U -j -B <cursor_before_ms> --token <access_token>` → 시간 내림차순 batch.
3. Python 에서 `start_kst ≤ time < end_kst` 필터링 + batch 내 가장 오래된 시각 추적.
4. 종료 조건:
   - batch 비어 있음, 또는
   - oldest_in_batch < target_date 00:00, 또는
   - len(batch) < page_size, 또는
   - max_pages (안전 가드, 기본 50) 도달.
5. 다음 페이지: `cursor_before_ms = oldest_in_batch.epoch_ms` (CLI 의 `-B` 가 exclusive 가정).
6. 수집 완료 후 capability/attribute 별로 분리 → resource_name 매핑 → wide outer join → CSV.

별도 SmartThings 전용 cron 불필요. `backfill_missing` (매시간) 도 hub 무관하게 `run_one_bundle` 호출하므로 자동 적용.

**Rate limit 처리** (`smartthings_client._RateLimiter`):
- 분당 호출 수 `SMARTTHINGS_CLI_MAX_RPM` (기본 200) 이하로 sliding window 제한.
- 429 / "rate limit" / "too many requests" 감지 시 60/120/180s 점진 백오프 후 재시도 (최대 3회).
- 401/403 (토큰 거부) → 우선 `refresh_token` 으로 자동 갱신 시도. 갱신도 실패하면 `smartthings_token_invalid` alert + 중단 (admin 재연결 필요).
  - **분류 정책** (false-positive 방지): REST 경로는 `resp.status_code` 를 직접 확인. CLI 경로는 `_looks_like_auth_error()` 가 `unauthorized` / `forbidden` / `invalid token` / `token expired` **단어**, 또는 `HTTP 401`·`status: 403` 처럼 HTTP 상태 코드 *prefix* 와 결합된 `401/403` 만 토큰 거부로 분류한다. 단순한 숫자 substring (예: device id 안의 `12401`) 으로는 토큰 alert 가 발생하지 않는다.
  - 그 외 모든 SmartThings 오류 (네트워크 timeout, device 권한 부족, 기타 비-2xx) 는 일반 수집 실패로 처리되어 `collection_jobs.status='failed'` 로만 기록 — token alert 는 띄우지 않는다.
- CLI 바이너리 부재 → `smartthings_cli_missing` alert + 해당 디바이스 수집만 실패 (Aqara 디바이스는 정상 진행).

### 15.7 CSV 저장 경로 규칙

기존 규칙 그대로 사용 (DESIGN.md §4):

```
data/{bundle_key}/{device_id}/{YYYYMMDD}_{suffix}.csv
```

- `bundle_key` 는 SmartThings 의 새 bundle 이름(`contact`, `motion`, `temp_humid` 등)을 그대로 사용. 단 `motion_and_light_p2` 는 Aqara motion_t1/p1 과 동일한 `motion_lux` 키를 공유.
- `device_id` 디렉토리명: Aqara 는 `lumi.<hex>`, SmartThings 는 **원본 ID 그대로** 사용.
  표준 UUID 형식(예: `0a59334e-c81f-4081-a501-f09048b9cca9`) 외에 Matter 디바이스의 경우
  대시 없는 24자리 hex(예: `3e7b675d14dfa559dae13000`) 도 그대로 폴더명으로 받아들인다.
- 파일명 `suffix` 규칙:
  - Aqara: 기존 `last6` (대문자 hex 끝 6자리, 예: `829AED`).
  - SmartThings (UUID·24-hex 모두): **device_id 의 첫 8자리 대문자** (예: `0A59334E`, `3E7B675D`)
    — 가독성 + last6 만으로는 식별 어려움.
  - 헬퍼 `device_id_suffix(device_id, hub)` 로 분기. `devices.py` 의 `last6` 는 Aqara 전용으로 유지.

### 15.8 CSV 메타 헤더

기존 11-라인 메타 헤더(DESIGN.md §6.1)는 그대로 사용하되 한 줄 추가:

```
# hub: smartthings   ← 신규
# device_id: <DEVICE ID>
# device_type: st_contact
# bundle: contact
# resources: contact
# alias: 3차 화장실 도어센서
# install_location: 3차 화장실
# install_date: 2025-12-01
# registered_by: <NAME>
# target_date: 2026-05-08
# generated_at: 2026-05-12 16:00:00 KST
# row_count: 7
time,contact
2026-05-08 11:51:26,open
...
```

`hub` 라인을 가장 위에 추가. 기존 Aqara 파일은 마이그레이션 시점에 갱신하지 않고
**다음 수집부터 새 헤더 적용** (옛 파일은 hub 라인 부재 → 파서는 missing 시 `'aqara'` 기본값).

### 15.9 디스플레이 시각화 규칙 (DISPLAY.md 후속)

본 절은 [DISPLAY.md §4](DISPLAY.md#4-디바이스-타입별-시각화-규칙) 의 device_type 추가 항목으로
이관해 DEVICE/DISPLAY 책임 분리를 유지한다. 매핑 초안:

| SmartThings device_type | 시각화 | 비고 |
|---|---|---|
| `st_contact` | open bar (door_t1 과 동일 로직) | 값: `open`/`closed` — `1`/`0` 매핑 후 door_t1 추출기 재사용 |
| `st_motion` | motion bar (motion_t1 과 동일 그룹핑) | 값: `active`/`inactive` |
| `st_occupancy` | bar (occupied → 시작, vacant → 종료) | 값: `occupied`/`vacant` |
| `st_temp_humid` | **line/area chart** (신규) — 시계열 연속값 | 기존 interval/point 가 아닌 새 시각화 (DISPLAY.md §10 후속) |
| `st_smoke` | 점 (tick) | 값: `detected`/`clear` |
| `st_switch` | bar (on 구간) | 값: `on`/`off` |
| `st_button` | 점 (tick) | 값: `pushed`/`held`/`double` |
| `motion_and_light_p2` | **motion bar + lux tick** — door 상태머신 + 조도 시계열 점 | 값: motion `active`/`inactive`, lux 정수. `inactive` 이벤트로 bar 종료 가능 (motion_t1 의 gap-grouping 보다 정보 풍부) |

### 15.10 운영 의존성

- **smartthings CLI binary** 설치 필요 (`samsung/export_all_history_by_date.py` 와 동일 가정).
  - Windows: `winget install SmartThings.SmartThingsCLI` 또는 GitHub release 다운로드.
  - `requirements.txt` 에는 추가 항목 없음 (Python 의존성 아님).
- requirements.txt 추가는 불필요. 이미 `requests`, `python-dotenv` 가 있음.

### 15.11 단계별 구현 체크리스트 (구현 시점에 참조)

- [ ] §15.3 마이그레이션: `_ensure_devices_hub` (멱등 ALTER TABLE)
- [ ] `app/smartthings_devices.py` (또는 `devices.py` 확장) — `SMARTTHINGS_DEVICE_TYPES` 화이트리스트 (P2 = `motion_and_light_p2` 포함)
- [ ] `app/smartthings_client.py` — REST `/devices`·`/locations` + CLI history wrapper + KST 변환 + bundle 매칭
- [ ] `app/token_manager.py` 확장 — SmartThings OAuth 토큰(access/refresh/expires) 저장/로드 + 만료 판정
- [ ] `app/collector.py` — `run_one_bundle` hub 분기, `device_id_suffix(hub)` 도입
- [ ] `app/routes/api.py` / `pages.py` — `POST /api/devices` 에 `hub` 필드 (기본 'aqara'), OAuth `GET /admin/smartthings/oauth/start`·`callback`
- [ ] 디바이스 추가 UI (`/devices` 페이지) — hub 드롭다운 + SmartThings 일 때 device_id 형식 검증(UUID 또는 24-hex)
- [ ] DISPLAY.md §4 에 SmartThings device_type 시각화 규칙 (§15.9 표) 이관·확장 — 특히 `motion_and_light_p2` 의 motion bar + lux line 신규 시각화
- [ ] dry-run (CLAUDE.md §3.1): `app/smartthings_client.py` 단위 — 모킹 CLI 응답으로 capability 필터·KST 변환·bundle 매칭 검증; CLI 미설치 환경 명확 에러; 401 응답 → `smartthings_token_invalid` 알림 발생
- [ ] 통합 dry-run: §15.12 의 P2 디바이스 1개 + 모킹된 motion/illuminance history → `data/motion_lux/<device_uuid>/<YYYYMMDD>_<UUID8>.csv` 생성 확인 (Aqara motion_t1/p1 과 동일 폴더, device_id 로 격리)

### 15.12 현재 타겟 디바이스 — Aqara Motion and Light Sensor P2

본 SmartThings 통합의 **최초 구현 타겟**은 다음 단일 디바이스다 (사용자 지정).

| 항목 | 값 |
|---|---|
| 디바이스 명 | **Aqara Motion and Light Sensor P2** |
| 종류 (DEVICE_TYPES 키) | `motion_and_light_p2` (§15.4) |
| device_id (SmartThings) | `3e7b675d14dfa559dae13000` |
| 접근 경로 | Matter 디바이스 → SmartThings 터널 (직접 Aqara Open API 미사용) |
| Bundle | `motion_lux` — wide CSV (`time, motion, lux`). Aqara motion_t1/p1 과 동일 키·동일 lux 컬럼명 공유 |
| Capability / Attribute | `motionSensor.motion`, `illuminanceMeasurement.illuminance` (→ CSV 컬럼명 `lux` 로 매핑) |

**device_id 형식 주의**:
- 표준 SmartThings UUID(예: `0a59334e-c81f-4081-a501-f09048b9cca9`, 8-4-4-4-12 의 32 hex + 4 dash)와
  달리 본 디바이스 ID 는 **대시 없는 24자리 hex**. Matter 디바이스의 새 ID 표기로 보임.
- 구현 시 ID 검증 정규식은 두 형식을 모두 수용:
  - `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$` (표준 UUID)
  - `^[0-9a-f]{24}$` (Matter 24-hex)
- 파일명 suffix 는 `device_id[:8].upper()` 로 동일 규칙 적용 → `3E7B675D`.

**`./samsung/devicie ID.md` 등 기존 SmartThings 디바이스 무시**:
- 사용자가 명시한 대로 `samsung/devicie ID.md` 및 `samsung/current_devices.md` 의 디바이스 목록은
  **이번 통합 범위에 포함하지 않는다**. SmartThings 인프라(클라이언트·DB 컬럼·UI 분기)는 일반 목적으로
  추가하되, 실제 등록할 SmartThings 디바이스는 본 §15.12 의 P2 한 개로 시작한다.
- 향후 동일 SmartThings 토큰 하 다른 디바이스를 추가할 필요가 생기면 `SMARTTHINGS_DEVICE_TYPES` 에 항목만 추가.

**예상 데이터 형태 (CLI `smartthings devices:history` 응답 가정)**:
```json
[
  {"time":"2026-05-12T08:00:00.000+00:00", "capability":"motionSensor",
   "attribute":"motion", "value":"active", "deviceId":"3e7b675d14dfa559dae13000"},
  {"time":"2026-05-12T08:00:00.500+00:00", "capability":"illuminanceMeasurement",
   "attribute":"illuminance", "value":127, "unit":"lux", "deviceId":"3e7b675d14dfa559dae13000"},
  {"time":"2026-05-12T08:01:30.000+00:00", "capability":"motionSensor",
   "attribute":"motion", "value":"inactive", "deviceId":"3e7b675d14dfa559dae13000"}
]
```

**저장 CSV 예시** (`data/motion_lux/<device_uuid>/20260512_<UUID8>.csv`):
```csv
# hub: smartthings
# device_id: <DEVICE ID>
# device_type: motion_and_light_p2
# bundle: motion_lux
# resources: motion,lux
# alias: Aqara P2 거실
# install_location: 거실
# install_date: 2026-05-01
# registered_by: <NAME>
# target_date: 2026-05-12
# generated_at: 2026-05-12 17:00:00 KST
# row_count: 3
time,motion,lux
2026-05-12 17:00:00,active,
2026-05-12 17:00:00,,127
2026-05-12 17:01:30,inactive,
```

> Aqara motion_t1/p1 의 motion_lux CSV (`time, motion_status, lux`) 와 같은 `data/motion_lux/` 폴더에 들어가지만, `device_id` 서브폴더로 격리되어 schema 충돌 없음. lux 컬럼명은 통일됨.

**구현 우선순위**:
1. 본 P2 디바이스 1개에 대한 end-to-end (DB 등록 → 수집 → CSV → display) 가 동작하면
   SmartThings 통합의 핵심 경로가 검증된다.
2. 그 후 `SMARTTHINGS_DEVICE_TYPES` 에 추가 capability 매핑을 늘려가며 다른 디바이스 종류 확장.
