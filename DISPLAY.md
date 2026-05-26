# 데이터 디스플레이 화면 명세 (DISPLAY.md)

데이터 현황(`/data`)에서 선택한 디바이스의 일일 활동을 한 페이지에서 한눈에 비교하는
**타임라인 시각화 화면의 단일 진실 공급원(SSOT)**. 구현은 이 문서를 따른다.

- 시계열 데이터의 의미: [DEVICE.md](DEVICE.md)
- 라우트·인증·DB 컨텍스트: [DESIGN.md](DESIGN.md)
- 코딩 규칙·dry-run 절차: [CLAUDE.md](CLAUDE.md)

---

## 1. 목적과 위치

- **목적**: 모션·진동 센서의 "감지 영역", 열림/닫힘 센서의 "열린 영역"을 일자별 가로 막대 그래프로 1주일치 한 페이지에 시각화한다. 사용자가 한눈에 일별 활동 패턴을 비교할 수 있어야 한다.
- **위치**: 새 화면 `/display/{device_id}` ([DESIGN.md §7.1](DESIGN.md#71-페이지-html) 페이지 라우트에 추가).
- **진입 경로**: 데이터 현황(`/data`)에서 행의 device_id 링크를 클릭하면 해당 디바이스의 디스플레이로 이동.
  - 기존 일자별 파일 목록 페이지(`/data/{device_id}/{bundle_key}`)는 같은 행에 보조 "파일" 링크로 분리.
- **권한**: 로그인 필요. 비로그인 시 HTML은 `/login`으로 303 리다이렉트, JSON API는 401.
  - 사유: 시각화는 원시 CSV의 파생물이고 동일한 데이터 보호 수준이 필요하다 ([DESIGN.md §6.3](DESIGN.md#63-사용자-인증)).

---

## 2. URL과 쿼리 파라미터

| Method | Path | 권한 | 설명 |
|--------|------|------|------|
| GET | `/display/{device_id}` | 로그인 | 디바이스의 활동 타임라인. 기본 = 오늘(KST) 기준 최근 7일 |

### 쿼리

| 이름 | 형식 | 기본값 | 제약 |
|---|---|---|---|
| `to` | `YYYY-MM-DD` (KST) | 오늘 | 미래 일자 불가 |
| `days` | 정수 | `7` | 1~7 |

표시 기간 = `[to - (days-1), to]` KST 일자. 위반 시 400.

---

## 3. 레이아웃

```
┌──────────────────────────────────────────────────────────────────────────┐
│ [기기 종류]  [별명·설치 장소]  Device: [device_id]                        │
│ ◀ 데이터 현황 / 파일 목록                                                  │
│ to: [2026-05-12]  days: [7▾]  [적용]                                       │
├──────────────────────────────────────────────────────────────────────────┤
│              0    3    6    9    12    15    18    21    24              │
│ 05-12 (월)  ▕    ▕    ▕    ▕    ▕     ▕     ▕     ▕     ▕  ← X-axis    │
│            [── ── ─────────  ──   ─── ────── ──]   ← bar = 감지 영역    │
│ 05-11 (일)  ▕    ▕    ▕    ▕    ▕     ▕     ▕     ▕     ▕              │
│            [─────── ── ─────────────────────────]                        │
│ ... (총 7행, 일자 오름차순으로 위→아래: 위=과거, 아래=최신)              │
│              0    3    6    9    12    15    18    21    24             │
└──────────────────────────────────────────────────────────────────────────┘
```

- **각 행 = 1일** (사용자 요구). 위쪽 = 최근 일자.
- **X축**: KST `00:00` ~ `24:00` (1440분). 좌→우.
- **눈금**: 3시간 단위 grid line (0/3/6/9/12/15/18/21/24). 첫·마지막 행에 시각 레이블 표기, 중간 행은 grid line만.
- **행 높이**: 트랙 약 24px + 일자 레이블 행. 7행 + 헤더가 일반 노트북 화면(약 800px) 한 페이지에 무리 없이 들어가야 함.
- **빈 일자**: 행은 그대로 표시하되 막대 없이 회색 트랙만. §9 케이스 표 참조.

---

## 4. 디바이스 타입별 시각화 규칙

DEVICE.md의 4개 디바이스 타입별로 막대(interval)와 점(point event) 추출 규칙을 정의한다.

### 4.1 Motion Sensor T1 / P1 (`motion_t1`, `motion_p1`)

- **데이터 소스**: `data/motion_lux/{device_id}/YYYYMMDD_*.csv` ([DEVICE.md §1.4](DEVICE.md#14-csv-저장-형식-통합-wide-포맷) wide 포맷)
- **사용 컬럼**: hub 에 따라 다름
  - `aqara`       → `time`, `motion_status` (lux는 이번 버전 미사용 — §10 향후 확장 참조)
  - `smartthings` → `time`, `motion` (값=`active`/`inactive`), `lux` (측정값)

#### Aqara hub — Gap threshold 그룹핑

`motion_status`는 `1`만 기록되고 종료 이벤트가 없으므로([DEVICE.md §1.3](DEVICE.md#13-값-의미)), 인접 이벤트 간 시간 간격을 임계값으로 그룹핑하여 영역을 만든다.

1. `motion_status == 1`인 행만 추출.
2. 시각 오름차순 정렬.
3. 인접 두 이벤트의 시각 차가 **`MOTION_GROUP_GAP_SEC`** 이내면 같은 영역, 초과면 영역 분리.
   - 기본값 `MOTION_GROUP_GAP_SEC = 90`초 (모션 센서 일반적 hold timeout 근거).
   - 향후 디바이스별 설정 가능 ([DEVICE.md §3.1](DEVICE.md#31-동작-특성): P1은 민감도 3단계 → hold timeout 다를 가능성).
4. 각 그룹의 첫 시각 = `start`, 마지막 시각 = `end`.
5. 그룹의 이벤트가 1개뿐이면 폭이 0이 되므로 **`MIN_BAR_SEC = 30`초**로 보정 (`end = start + 30s`).

일자 경계는 일별 CSV 단위로 처리하므로 자연스럽게 일자 내에서만 그룹핑된다.

#### SmartThings hub — `active`/`inactive` 상태머신

SmartThings 의 motion capability 는 **상태 변화 시점만** 이벤트로 보낸다.
즉 움직임 진입 시 `motion=active`, 일정 시간 움직임이 없으면 `motion=inactive` 가 1회씩 기록되며 그 사이에는 별도 샘플이 없다 (lux 만 보고되는 행은 motion 컬럼이 비어 있음 — wide outer join 결과).

따라서 `active` 시작 → 다음 `inactive` 종료 사이가 **active 구간**이며 그 구간만 막대로 그린다.

1. CSV 행을 시각 오름차순으로 처리. `motion` 컬럼이 빈 칸인 행은 무시.
2. 상태머신: `active` → active 시작, `inactive` → active 종료. 짝 없는 `inactive` (이미 종료된 상태에서 또 옴) 는 안전 스킵.
3. **일자 경계 복원** (door/move_detect 와 동일 패턴): 직전 일자 마지막 motion 값이 `active` 이고 `inactive` 가 없었다면 표시 일자 `00:00` 부터 active 로 시작 (`←` 화살표). 일자 종료까지 `inactive` 가 없으면 `24:00` 까지 연장 (`→` 화살표).
4. Aqara 와 동일한 **`MIN_BAR_SEC = 30`초** 폭 보정 적용 (짧은 페어가 SVG 1픽셀 미만이 되어 사라지는 것 방지).
5. **lux 컬럼은 line plot** — motion 막대와 같은 트랙에 노란색 polyline (`--lux-tick-color`) 으로 오버레이한다 (`extract_st_lux_series`). 점/tick 이 아닌 연속 선그래프. Y 범위는 자동(lux min~max), 우측 트랙 모서리에 범위 라벨(`<값>lx`) 표시. 막대보다 나중에 그려 선이 막대 위로 보인다.

> Aqara/SmartThings 모두 동일한 막대 색(`--motion-color`)을 쓰지만 추출 알고리즘은 hub 별로 분기된다 ([app/display_extract.py](app/display_extract.py) `extract_motion_intervals` vs `extract_st_motion_intervals`).

#### 표시
- 막대 색상 `--motion-color` (양 hub 공통). lux polyline 은 `--lux-tick-color`.
- 호버 툴팁: 막대 — Aqara `HH:MM:SS – HH:MM:SS (이벤트 M개)` / SmartThings `HH:MM:SS – HH:MM:SS`. lux polyline — `조도 lux (<min>~<max>, 샘플 N개)`.
- 범례 표기: `Plot — 조도 (lux, 우측 축 실선)`.

#### 일자 상태별 처리 (motion 공통, door_t1 §4.2 와 동일 정책)
- **이벤트 없음** (CSV 존재, 이벤트 0건): SmartThings 의 경우 직전 일자 마지막 motion 값이 `active` 이면 24시간 active 막대(`←`/`→` 마커). Aqara 는 `motion_status=1` 만 기록되므로 carry-over 개념 자체가 없어 그날은 빈 트랙.
- **수집 없음** (CSV 자체 부재): **막대·lux polyline 모두 그리지 않는다** — 데이터 없는 날 carry-over 만으로 24h 막대를 그리면 오해 소지. day-meta 에는 "수집 없음" 안내만.

> `routes/display.py` 의 motion_t1/p1·motion_and_light_p2 분기에서 `path.exists()=False` 일 때 `intervals`/`lux_series` 를 빈 값으로 강제해 구현.

### 4.2 Door and Window Sensor T1 (`door_t1`)

- **데이터 소스**: `data/magnet_status/{device_id}/YYYYMMDD_*.csv`
- **사용 컬럼**: hub 에 따라 다름
  - `aqara`       → `time`, `magnet_status` (값=`1`=열림, `0`=닫힘 — [DEVICE.md §2.2](DEVICE.md#22-값-의미))
  - `smartthings` → `time`, `contact` (값=`open` / `closed`)

#### Aqara hub — 상태머신

1. 시각 오름차순으로 이벤트 처리.
2. 상태머신: `1`이면 open 시작, `0`이면 open 종료. 이미 open 상태에서 `1`이 또 오면 무시 (상태 유지).
3. **일자 경계 복원** ([DEVICE.md §2.4](DEVICE.md#24-후처리-주의)):
   - **시작 경계**: 표시 일자 `D`의 직전 일자(`D-1`) CSV에서 마지막 이벤트가 `1`(열림)이고 그 이후 `0`이 없었다면, 일자 `D`는 `00:00`부터 열린 상태로 간주.
   - **종료 경계**: 일자 `D`가 끝날 때까지 닫히지 않았다면 막대를 `24:00`까지 연장하고, 시각적으로 `→` 화살표를 막대 우측에 표시 (다음 날 계속됨을 의미).
4. 짝이 맞지 않는 이벤트(예: `0`만 있고 `1` 없음)는 위 경계 복원 로직으로 자연스럽게 처리됨.

#### SmartThings hub — `open`/`closed` 상태머신

Aqara 와 동일한 상태머신을 다른 값 토큰으로 적용. SmartThings 의 contact capability 는 상태 변화 시점만 이벤트로 보내므로 (`open` 시작, `closed` 종료) Aqara 의 `1`/`0` 과 의미가 정확히 대응한다.

- `open` → 열림 시작, `closed` → 열림 종료. 일자 경계 복원 규칙은 위와 동일 (직전 일자 마지막 값이 `open` 이면 `00:00` 부터 시작, 미해지 시 `24:00` 까지 연장).
- 짝 없는 `closed` (이미 닫힌 상태에서 또 옴) 는 안전 스킵.

#### 표시
- 막대 색상 `--door-open-color` (양 hub 공통).
- 호버 툴팁: `HH:MM:SS – HH:MM:SS`.
- 경계 복원으로 산출된 막대는 시작/끝에 `←` / `→` 마커 추가 ("이전/다음 날에서 이어짐").

#### 일자 상태별 처리 (door_t1 한정)
- **이벤트 없음** (CSV 는 존재, 그날 새 이벤트 0건): 직전 일자 마지막 상태가 `1`/`open` 이면 **24시간 내내 열린 상태 막대** (`←`/`→` 마커 포함). 사용자가 "그날 변화 없이 계속 열려 있었음" 을 한눈에 확인.
- **수집 없음** (CSV 자체 부재): **막대를 그리지 않는다** — 데이터가 없는 날에 carry-over 만으로 24h 막대를 그리면 오해 소지. day-meta 에는 "수집 없음" 안내만 표시.

> 이 분기는 `routes/display.py` 의 door_t1 분기에서 `path.exists()=False` 일 때 intervals 를 빈 tuple 로 강제해 구현. 다른 센서(vibration_t1 등)는 현재 동일 처리하지 않음.

### 4.3 Vibration Sensor T1 (`vibration_t1`)

진동 센서는 hub 에 따라 컬럼 구성이 다르다 ([DEVICE.md §4](DEVICE.md#4-vibration-sensor-t1-진동-센서-t1)):
- `aqara`       → `move_detect` + `knock_event` 를 **wide CSV** 로 ([§4.3](DEVICE.md#43-csv-저장-형식-통합-wide-포맷)). 컬럼: `time, move_detect, knock_event`.
- `smartthings` → `acceleration` 만. 컬럼: `time, acceleration` (knock 에 대응하는 SmartThings 표준 capability 없음 — 후속).

Bundle 키는 양 hub 공통 `move_knock`.

#### Aqara hub — 움직임 영역(interval) `move_detect` 컬럼

- **데이터 소스**: `data/move_knock/{device_id}/YYYYMMDD_*.csv`
- **사용 컬럼**: `time`, `move_detect`
- **상태머신** ([DEVICE.md §4.2](DEVICE.md#42-resource-의미)): `1`=Activated 시작, `255`=Deactivated 해지.
  - `move_detect` 컬럼이 빈 칸인 행(knock_event만 있는 샘플)은 무시.
  - `1` 만나면 active 시작, `255` 만나면 active 종료.
- **일자 경계 복원**: door 센서와 동일 로직 (직전 일자 마지막 상태 확인 + 미해지 시 `24:00`까지 연장).

#### SmartThings hub — 가속도 영역(interval) `acceleration` 컬럼

- **사용 컬럼**: `time`, `acceleration`
- **상태머신**: `active` → active 시작, `inactive` → active 종료. (motion/door SmartThings 와 동일 active/inactive 페어 패턴.)
- **일자 경계 복원**: 위와 동일.
- knock tick 은 표시하지 않는다 (SmartThings 측 데이터 없음).
- 짧은 페어가 SVG 에서 1픽셀 미만이 되지 않도록 motion 과 동일한 `MIN_BAR_SEC = 30`초 폭 보정 적용.

#### 두드림 이벤트(point) — `knock_event` 컬럼

- **데이터 소스**: 같은 CSV (`data/move_knock/{device_id}/YYYYMMDD_*.csv`).
- **사용 컬럼**: `time`, `knock_event`
- `knock_event` 컬럼이 **비어있지 않은** 행을 모두 X축 시각 위치에 **세로 tick mark** (`|`, 높이 ~6px)로 오버레이.
- `move_detect` 막대와 **같은 row**에 표시 (트랙 상단 1/3 영역).

#### 표시
- 막대 색상 `--vibration-color` (move_detect).
- Tick 색상은 `knock_event` 값 코드별로 구분 (관측된 UpgoPlus 데이터 기준):
  | 값 | 의미 | CSS 변수 |
  |---|---|---|
  | `1`   | 두드림 ON   | `--knock-1-color`   |
  | `255` | 두드림 해지 | `--knock-255-color` |
  | 기타  | 방어용      | 트랙 기본 `--knock-color` |
- 호버 툴팁(막대): `HH:MM:SS – HH:MM:SS (움직임 N초)`.
- 호버 툴팁(tick): `<라벨> HH:MM:SS` (예: "두드림 ON 14:23:11").

### 4.4 Wireless Mini Switch T1 (`switch_t1`)

- **데이터 소스**: `data/switch_status/{device_id}/YYYYMMDD_*.csv`
- **컬럼**: `time, switch_status` ([DEVICE.md §5.2](DEVICE.md#52-값-의미))

#### 롱 프레스 영역(interval) — 코드 `16` → `17`
- 상태머신: `16` (long_click_press 시작) 만나면 active, `17` (long_click_release 해지) 만나면 종료.
- **일자 경계 복원**: door 센서와 동일 로직 (직전 일자 마지막이 `16`이고 `17`이 없으면 `00:00`부터 active로 시작, 미해지 시 `24:00`까지 연장).

#### 단발 이벤트(point) — 코드 `1`, `2`, `3`, `18`
- 모든 단발 코드를 동일 색 tick mark로 오버레이. 호버 툴팁에 코드별 이벤트명 표시:

| 코드 | tooltip 라벨 |
|---|---|
| `1` | "1번 클릭" |
| `2` | "2번 클릭" |
| `3` | "3번 클릭" |
| `18` | "흔들림" |

#### 표시
- 막대 색상 `--switch-long-color`.
- Tick 색상 `--switch-event-color`.
- 호버 툴팁(막대): `HH:MM:SS – HH:MM:SS (롱 프레스 N초)`.
- 호버 툴팁(tick): `<라벨> HH:MM:SS`.

### 4.5 Vibration Sensor (aq1) (`vibration_aq1`)

- **데이터 소스**: `data/vibration_event/{device_id}/YYYYMMDD_*.csv`
- **컬럼**: `time, vibration_event` ([DEVICE.md §6.2](DEVICE.md#62-값-의미))

실제 관측 데이터에서 `1`·`2`·`255` 가 대부분이며 셋 다 움직임 관련 신호(시작·진동·해지)다. 따라서 **`1`·`2`·`255` 를 움직임 막대(interval)로**, 그 외 코드(`0`·`3`·`4`·`5`·`6`)는 기타 이벤트 점(tick)으로 표시한다.

#### 움직임 영역(interval) — `1`·`2`·`255`

`extract_vibration_aq1_intervals` 가 산출 (motion_t1 과 동일한 gap-grouping):

1. 값이 `1`·`2`·`255` 중 하나인 이벤트만 추출, 시각 오름차순 정렬.
2. 인접 두 이벤트의 시각 차가 **`MOTION_GROUP_GAP_SEC`**(기본 90초) 이내면 같은 막대, 초과면 막대 분리.
3. 각 그룹의 첫 시각 = `start`, 마지막 시각 = `end`.
4. 그룹의 이벤트가 1개뿐이면 폭 0 이 되므로 **`MIN_BAR_SEC = 30`초** 보정.
5. 일자 경계 복원은 하지 않는다 (일자별 CSV 단위 독립 처리).

#### 기타 이벤트(point) — `0`·`3`·`4`·`5`·`6`

`1`·`2`·`255` 외 모든 코드를 코드별 색 tick 으로 표시. 호버 툴팁에 코드별 라벨:

| 코드 | tooltip 라벨 | CSS 변수 |
|---|---|---|
| `0` | "두드림(보안모드)" | `--vibration-aq1-0-color` |
| `3` | "자유낙하" | `--vibration-aq1-3-color` |
| `4` | "닫힘 학습 완료" | `--vibration-aq1-4-color` |
| `5` | "들어 올림" | `--vibration-aq1-5-color` |
| `6` | "세 번 두드림" | `--vibration-aq1-6-color` |
| 기타 | "이벤트 (코드=N)" (방어용) | 트랙 기본 `--vibration-aq1-color` |

#### 표시
- 막대 색상 `--vibration-color` (vibration_t1 move bar 와 동일 녹색).
- tick 은 위 표의 코드별 색.
- 호버 툴팁: 막대 `HH:MM:SS – HH:MM:SS`, tick `<라벨> HH:MM:SS`.

### 4.6 Temperature and Humidity Sensor T1 (`temp_humi_t1`)

- **데이터 소스**: `data/temp_humi/{device_id}/YYYYMMDD_*.csv` ([DEVICE.md §8.3](DEVICE.md#83-csv-저장-형식-통합-wide-포맷) wide 포맷)
- **사용 컬럼**: `time`, `temperature_value`, `humidity_value` (양 hub 공통 — DEVICE.md §8)

#### 표시 규칙 — 이중축 Line Plot

부동소수 측정값을 **두 polyline 으로 표시** (점/막대 형식이 아닌 line chart).
한 트랙 안에 온도와 습도를 동시에 그리되 Y 스케일이 서로 다르므로 **이중축**(좌측 = 온도, 우측 = 습도) 으로 분리한다.

- 시계열 추출 (`extract_temp_humi_series`):
  - `temperature_value` 가 채워진 행만 (time, float) 페어로 수집 → 온도 시계열
  - `humidity_value` 가 채워진 행만 (time, float) 페어로 수집 → 습도 시계열
  - 두 시계열의 보고 시각은 어긋날 수 있다 (DEVICE.md §8.1) — 독립 polyline 으로 처리하면 자연스럽게 해결됨
  - 잘못된 시각 포맷 / 숫자 파싱 실패 행은 스킵
- Y 범위는 **자동** — 각 시계열의 min/max 로 트랙 높이 100% 에 정규화. 단일 점이면 ±1 패딩.
- 시계열이 비어 있으면 해당 polyline·축 라벨 모두 생략.
- 막대(interval) 산출 없음. 일자 경계 복원 불필요 (주기 측정 — 상태머신이 아님).

#### 표시
- **온도 polyline**: 실선, 색 `--temp-tick-color` (빨강). 좌측 트랙 모서리에 Y 범위(°C) 작은 텍스트로 표시.
- **습도 polyline**: 점선(`stroke-dasharray="4,2"`), 색 `--humi-tick-color` (파랑). 우측 트랙 모서리에 Y 범위(%RH) 표시.
- **관측 지점 마커**: 모든 plot polyline 은 실제 측정값이 있는 각 지점에 반지름 1.8px 원(`<circle>`)을 찍어 보간된 선 구간과 실제 관측점을 구분한다. 호버 시 `HH:MM:SS · <값>`. 마커 색:
  | plot | 선 색 | 마커 색 |
  |---|---|---|
  | 온도 (temp_humi) | 빨강 `--temp-tick-color` 실선 | 주황 `#f97316` |
  | 습도 (temp_humi) | 파랑 `--humi-tick-color` 점선 | 하늘색 `#38bdf8` |
  | 조도 (lux, §4.1) | 노랑 `--lux-tick-color` 실선 | 주황 `#f97316` |
- 호버 툴팁(polyline 전체): `온도 (좌측 축, 21.3~26.8°C, 샘플 N개)` / `습도 (우측 축, 38~62%RH, 샘플 N개)`.
- 일별 카운트 라벨(우측): `T N · H M` — 온도/습도 측정 샘플 수.

> 범례 표기에서는 `Plot — 온도 (°C, 좌측 축 실선)` / `Plot — 습도 (%RH, 우측 축 점선)` 로 노출.

### 4.7 디바이스 타입 → bundle 매핑

| device_type | 사용 bundle(s) | 표시 요소 | 직전 일자 CSV 필요? |
|---|---|---|---|
| `motion_t1` (aqara) | `motion_lux` | motion bar | 아니오 (일자별 독립) |
| `motion_t1` (smartthings) | `motion_lux` | motion bar (active→inactive) + lux line plot | **예** (active→inactive 경계 복원) |
| `motion_p1` (aqara) | `motion_lux` | motion bar | 아니오 |
| `motion_p1` (smartthings) | `motion_lux` | motion bar (active→inactive) + lux line plot | **예** (active→inactive 경계 복원) |
| `motion_and_light_p2` (smartthings) | `motion_lux` | motion bar (active→inactive) + lux line plot | **예** (active→inactive 경계 복원) |
| `motion_and_light_wm` (smartthings) | `motion_lux` | P2 와 동일 (motion bar + lux plot) | **예** (active→inactive 경계 복원) |
| `door_t1` (aqara) | `magnet_status` | open bar | **예** (경계 복원) |
| `door_t1` (smartthings) | `magnet_status` (컬럼 `contact`: open/closed) | open bar | **예** (경계 복원) |
| `vibration_t1` (aqara) | `move_knock` (wide: move_detect + knock_event) | move bar + knock tick | **예** (move_detect 경계 복원) |
| `vibration_t1` (smartthings) | `move_knock` (컬럼 `acceleration`: active/inactive) | move bar (knock 없음) | **예** (경계 복원) |
| `switch_t1` | `switch_status` | long press bar + click/shake tick | **예** (16↔17 경계 복원) |
| `vibration_aq1` | `vibration_event` | 움직임 bar (`1`·`2`·`255` gap-grouping) + 기타 코드 tick | 아니오 |
| `temp_humi_t1` | `temp_humi` (wide: temperature_value + humidity_value) | line plot (이중축: 좌=온도 실선, 우=습도 점선, interval 없음) | 아니오 |
| `temp_humi_wm` (smartthings) | `temp_humi` | T1 과 동일 (line plot 이중축) | 아니오 |

### 4.8 디바이스 그룹 화면 (`/display/group/{group_id}`)

여러 디바이스를 묶어 한 페이지에서 함께 보는 화면. 그룹은 **혼합 device_type**을 허용한다.

**패널 구성 규칙**:
- **같은 device_type 멤버 N개 → 한 패널로 합산.** 멤버 각자의 이벤트(interval·point)를 시간상 **합집합**으로 병합하여 단일 트랙으로 시각화한다.
- **서로 다른 device_type → 별도 패널.** 각 종류는 §4.1~§4.5 의 시각화 규칙을 따른다.

**합집합 병합 규칙** (같은 device_type 패널 내부):
- **interval (motion / door / move / switch_long)**: 멤버별로 §4.x 규칙에 따라 interval을 산출한 뒤, 시간상 겹치거나 인접한 구간을 하나로 합친다. `event_count` 는 합산. `truncated_left/right` 는 어느 한 멤버라도 잘렸으면 set.
  - 멤버별 산출 후 union 이유: 상태머신(door/move/switch_long)을 raw 이벤트 concat 으로 돌리면 한 멤버의 close 가 다른 멤버의 open 을 닫는 등 의미가 깨진다. "어느 한 멤버라도 active이면 active" 의미는 멤버별 interval 추출 → 시간상 union 이 가장 자연스럽다.
- **point (knock / switch_event / vibration_aq1)**: 멤버 전체 point 를 concat 후 시각 오름차순 정렬. (각 point 의 label 은 그대로 유지.)
- **`has_csv`**: 어느 한 멤버라도 해당 일자 CSV가 있으면 true.
- **`job_status`**: 어느 한 멤버라도 `failed` 면 `failed`, 그렇지 않고 하나라도 `success` 면 `success`, 그 외 None.

**레이아웃**:
```
┌────────────────────────────────────────────────────────────────┐
│ 그룹: <name>  (멤버 N개)  설명: <description>                  │
├────────────────────────────────────────────────────────────────┤
│ [기기 종류 A]  (멤버: alias1 · alias2 · ...)                   │
│      (1주일 × 1일/행 트랙 — 멤버 이벤트 합산)                   │
│                                                                 │
│ [기기 종류 B]  (멤버: alias3 · ...)                            │
│      (1주일 × 1일/행 트랙 — 멤버 이벤트 합산)                   │
└────────────────────────────────────────────────────────────────┘
```

- 패널 정렬: `device_type` 키 오름차순. 같은 종류 그룹 내 멤버 라벨 정렬: `alias` → `device_id`.
- 패널의 트랙 마크업은 `/display/{device_id}` 단일 디바이스 화면과 **동일한 `_device_timeline.html` partial** 을 재사용 (intervals/points 만 합집합으로 치환).
- 빈 그룹(멤버 0개)도 정상 응답: "이 그룹에는 멤버 디바이스가 없습니다." 안내 표시.
- 표시 기간 쿼리(`to`, `days`)는 §2와 동일.
- **그룹의 생성·삭제·멤버 할당 UI** 는 [DESIGN.md §7.4](DESIGN.md#74-장치-목록-화면-f1af2) "장치 목록" 화면 하단의 그룹 관리 미니 섹션에서 수행. API 는 [DESIGN.md §7.2](DESIGN.md#72-json-api-ajax-또는-외부-자동화) `POST /api/groups`, `DELETE /api/groups/{id}`, `PATCH /api/devices/{id}` (group_id 필드).

---

## 5. 데이터 조회 전략

서버는 표시 기간 내 일자마다 해당 디바이스/bundle의 CSV를 직접 읽어 처리:

```
for each day D in [to-(days-1), to]:
    for each bundle b in device_type → bundles:
        path = data/{b}/{device_id}/{YYYYMMDD}_{last6}.csv
        if path missing: 빈 결과
        else:
            rows = parse_csv(path, skip='#')           # DEVICE.md 메타 헤더 무시
            intervals, points = extract(rows, b, device_type)
            (door/vibration의 경계 복원이 필요하면 D-1 CSV도 읽음)
```

- **CSV가 SSOT**: `collection_jobs.status='success'` 여부와 무관하게 **파일 존재**를 기준으로 판단.
- 파일 부재(미수집 일자)는 예외가 아니라 빈 결과로 처리.
- 모든 처리는 **서버측에서 수행 후 SVG 마크업을 페이지에 직접 삽입**한다 (외부 차트 라이브러리 불필요, [CLAUDE.md §2.3](CLAUDE.md#23-외부-의존) 의존성 최소화).
- AJAX/JSON API는 이번 버전에서 노출하지 않음 (필요 시 §10 후속 작업).

### 성능 가이드
- 1주일 × 디바이스 1개 × resource 1~2개 = 7~14 CSV. 디바이스당 일별 수천 줄 이내가 일반적이므로 동기 처리로 충분.
- 동일 (device, range) 요청이 잦다면 후속 단계에서 메모리 LRU 캐시 검토.

---

## 6. 색상·레이블·접근성

CSS 변수로 정의해 `app/static/style.css` 한 곳에서 변경 가능하게 한다:

```css
:root {
  --motion-color:        #3b82f6;  /* 파랑   — motion bar */
  --door-open-color:     #f59e0b;  /* 주황   — door open bar */
  --vibration-color:     #10b981;  /* 녹색   — vibration_t1 move bar */
  --knock-color:         #ef4444;  /* 빨강   — vibration_t1 knock tick */
  --switch-long-color:   #8b5cf6;  /* 보라   — switch_t1 long press bar */
  --switch-event-color:  #06b6d4;  /* 청록   — switch_t1 click/shake tick */
  --vibration-aq1-color: #ec4899;  /* 분홍   — vibration_aq1 event tick */
  --grid-color:          #e5e7eb;
  --bg-track:            #f9fafb;
  --today-bg:            #fef3c7;  /* 오늘 행 강조 */
}
```

- 마우스 오버 툴팁은 SVG 표준 `<title>` 요소 사용 (JS 불필요).
- 일자 레이블: `MM-DD (요일)`. **오늘 행은 bold + `--today-bg` 배경**으로 강조.
- X축 시각 레이블: 0/3/6/9/12/15/18/21/24. 첫 행 상단·마지막 행 하단 양쪽에 표기.
- 색맹 사용자를 위해 막대 패턴(빗금/점선) 차별화는 §10 후속.

---

## 7. 시간대

모든 표시 시각은 **KST** ([CLAUDE.md §2.4](CLAUDE.md#24-시간대), [DESIGN.md §6.1](DESIGN.md#61-일일-수집-워크플로우)).

- "오늘" = KST 자정 기준 현재 일자.
- `to` 쿼리의 미래 검증도 KST 기준.
- CSV의 시각은 이미 KST 문자열이므로 추가 변환 없이 그대로 X축에 매핑.

---

## 8. 데이터 현황(`/data`) 변경

[`app/templates/data.html`](app/templates/data.html)의 device_id 셀 동작을 변경한다.

| 위치 | 기존 | 변경 후 |
|---|---|---|
| device_id 셀의 링크 | `/data/{device_id}/{bundle_key}` | **`/display/{device_id}`** |
| 일자별 파일 목록 진입 | (위 링크) | 같은 행에 작은 "파일" 보조 링크로 분리 |

- `vibration_t1`처럼 한 디바이스가 여러 bundle을 가지면 `/data` 화면에는 여전히 여러 행으로 표시되지만, **디스플레이는 device 단위**이므로 device_id 링크에는 bundle을 포함하지 않는다.
- 디스플레이 페이지가 `device_type`을 보고 어떤 bundle(s)을 읽을지 결정 (§4.4 매핑표).

---

## 9. 빈 일자 / 부분 수집 / 오류 케이스

| 케이스 | 표시 |
|---|---|
| 그날 CSV 파일 없음 | 행 표시, 막대 없음, 우측에 회색 "수집 없음" 라벨 |
| CSV 있으나 0건 | 행 표시, 막대 없음, "이벤트 없음" 라벨 |
| `collection_jobs.status='failed'` | 행 표시, 우측에 "수집 실패" 빨간 라벨 + `/jobs`로 이동 링크 |
| 직전 일자 미수집 (door/vibration 경계 복원 불가) | 일자 시작 상태를 "닫힘/비활성"으로 가정, 행 우측에 "경계 추정" 회색 작은 표시 |
| device_id 미등록·삭제됨 | 404 |

---

## 10. 향후 확장 (이번 버전 범위 밖)

- **Lux 시각화** ([DEVICE.md §1.1](DEVICE.md#11-동작-특성) occupied 한정 표시): 히트맵 또는 라인 차트
- **1주일 초과 기간**: 월간 캘린더 뷰 (행 = 일자, 셀에 활동 요약 색 채움)
- **다중 디바이스 동시 비교**: 같은 페이지에 여러 디바이스 row 묶음
- **JSON API 노출** (`GET /api/display/{device_id}?to=&days=`): 외부 자동화·대시보드 연동
- **색맹 패턴 옵션**: 빗금/점선 차별화
- **디바이스별 설정**: `MOTION_GROUP_GAP_SEC` per device_type 또는 per device ([DEVICE.md §3.1](DEVICE.md#31-동작-특성) P1 민감도 반영)

---

## 11. 구현 체크리스트 (변경 PR 작성 시)

본 SSOT 본문(§1~§10)은 구현 완료 상태를 반영한다. 시각화 규칙·라우트·CSS·extract 함수를 새로 추가/수정할 때 다음을 확인한다.

- [ ] [DESIGN.md §7.1](DESIGN.md#71-페이지-html) 라우트 표와 일관 (단일/그룹 디스플레이 두 라우트 유지)
- [ ] 새 device_type을 추가했다면 §4 시각화 규칙 + §4.7 매핑표에 행 추가, `app/display_extract.py` 추출 함수 + `app/routes/display.py` `_build_day_rows` 분기 + `app/templates/_device_timeline.html` 렌더링 분기를 모두 갱신
- [ ] CSS 변수는 `app/static/style.css` 한 곳에서 (§6 참조)
- [ ] **dry-run** ([CLAUDE.md §3](CLAUDE.md#3-dry-run-점검-프로토콜)): 변경 영향 device_type에 대해 모킹 CSV로 interval/point 산출 단위 테스트
  - motion: 0건 / 1건(MIN_BAR_SEC 보정) / 짧은 간격 그룹 / GAP 초과 분리
  - door: 정상 open-close 쌍 / 일자 시작 시 이미 열림 / 일자 종료 시 미닫힘
  - vibration_t1 (`move_knock`): move_detect 1↔255 정상 / 미해지 / 같은 CSV의 knock_event 컬럼 tick 오버레이
  - switch_t1: 16↔17 long press 정상/일자 경계 / 1·2·3·18 tick 오버레이
  - vibration_aq1: 0~6 tick (interval 없음)
- [ ] 권한 dry-run: 비로그인 → `/login` 303, 로그인 → 200 (단일 디바이스/그룹 양쪽)
- [ ] 그룹 화면 dry-run: 빈 그룹 / 단일 device_type / 혼합 device_type 멤버 (§4.8)
