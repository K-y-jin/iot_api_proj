# 수집 대상 기기 명세

> **허브 접근 방식 개요 (DESIGN.md §15)**
> 같은 물리 디바이스라도 **두 종류의 API** 로 접근 가능 — 디바이스 등록 시 사용자가 선택.
>
> | Hub | 인증 | 시계열 호출 | resource 표기 | 시각 형식 |
> |---|---|---|---|---|
> | **aqara** | APPID/KEYID/APPKEY + access/refresh token | REST `fetch.resource.history` | dotted id (예: `3.1.85`) | UTC ms 입력 / UTC ms 응답 |
> | **smartthings** | OAuth 2.0 access/refresh token (Bearer, 자동 갱신) | CLI `smartthings devices:history` | `<capability>.<attribute>` (예: `motionSensor.motion`) | epoch ms 입력 / UTC ISO8601 응답 |
>
> 한 device_type 이 두 hub 를 모두 지원하면 `bundles_by_vendor` 에 양쪽 정의를 둔다
> ([app/devices.py](app/devices.py), DESIGN.md §3.1, §15.4). CSV `# hub:` 메타 라인으로 구분되며 같은
> `bundle_key` 폴더에 device_id 서브폴더로 격리되어 schema 충돌 없음.

## 0. 요약표

| # | Device Name | 한국어 명칭 | Aqara Model | 지원 hub | Aqara resource (id) | SmartThings capability (attribute) | 의미 |
|---|---|---|---|---|---|---|---|
| 1 | Motion Sensor T1 | 모션 센서 T1 | `lumi.motion.agl02` | aqara · smartthings | `motion_status` (`3.1.85`) | `motionSensor.motion` | 재실 감지 |
| 1 | Motion Sensor T1 | 모션 센서 T1 | `lumi.motion.agl02` | aqara · smartthings | `lux` (`0.3.85`) | `illuminanceMeasurement.illuminance` | 조도 |
| 2 | Door and Window Sensor T1 | 열림/닫힘 센서 T1 | `lumi.magnet.agl02` | aqara · smartthings | `magnet_status` (`3.1.85`) | `contactSensor.contact` | 자석 접점 상태 |
| 3 | Motion Sensor P1 | 모션 센서 P1 | `lumi.motion.ac02` | aqara · smartthings | `motion_status` (`3.1.85`) | `motionSensor.motion` | 재실 감지 |
| 3 | Motion Sensor P1 | 모션 센서 P1 | `lumi.motion.ac02` | aqara · smartthings | `lux` (`0.3.85`) | `illuminanceMeasurement.illuminance` | 조도 |
| 4 | Vibration Sensor T1 | 진동 센서 T1 | `lumi.vibration.agl01` | aqara · smartthings | `knock_event` (`13.3.85`) | (SmartThings 표준 capability 없음 — 후속) | 두드림 이벤트 |
| 4 | Vibration Sensor T1 | 진동 센서 T1 | `lumi.vibration.agl01` | aqara · smartthings | `move_detect` (`13.7.85`) | `accelerationSensor.acceleration` | 움직임 감지 |
| 5 | Wireless Mini Switch T1 | 무선 미니 스위치 T1 | `lumi.remote.b1acn02` | aqara · smartthings | `switch_status` (`13.1.85`) | `button.button` | 스위치 클릭 |
| 6 | Vibration Sensor (aq1) | 진동 센서 (aq1) | `lumi.vibration.aq1` | aqara only | `vibration_event` (`13.1.85`) | — | 진동 이벤트(통합) |
| 7 | Motion and Light Sensor P2 | Aqara 모션·조도 센서 P2 | (Matter, Aqara 측 모델명 무관) | **smartthings only** | — | `motionSensor.motion` + `illuminanceMeasurement.illuminance` | 모션 + 조도 |
| 8 | Temperature and Humidity Sensor T1 | 온습도 센서 T1 | `lumi.sensor_ht.agl02` | aqara · smartthings | `temperature_value` (`0.1.85`) | `temperatureMeasurement.temperature` | 온도 (°C, 주기 측정) |
| 8 | Temperature and Humidity Sensor T1 | 온습도 센서 T1 | `lumi.sensor_ht.agl02` | aqara · smartthings | `humidity_value` (`0.2.85`) | `relativeHumidityMeasurement.humidity` | 상대습도 (%, 주기 측정) |
| 9 | Motion and Light Sensor (Watts Matter) | 모션 센서 (와츠매터) | (Matter) | **smartthings only** | — | `motionSensor.motion` + `illuminanceMeasurement.illuminance` | 모션 + 조도 |
| 10 | Temperature and Humidity Sensor (Watts Matter) | 온습도 센서 (와츠매터) | (Matter) | **smartthings only** | — | `temperatureMeasurement.temperature` + `relativeHumidityMeasurement.humidity` | 온도 + 상대습도 (주기 측정) |

> SmartThings 측 값 인코딩은 Aqara 와 다름: motion `active`/`inactive` (Aqara=`1`만), contact `open`/`closed` (Aqara=`1`/`0`), button `pushed`/`held` 등 (Aqara=숫자 코드). 추출 로직은 hub 별로 분기 ([app/display_extract.py](app/display_extract.py)). 온습도(`temp_humi_t1`)는 양 hub 모두 부동소수 측정값을 그대로 보고하므로 컬럼명·해석 모두 통일.

---

## 1. Motion Sensor T1 (모션 센서 T1)

- **Model**: `lumi.motion.agl02`
- **지원 hub**: `aqara` · `smartthings` (디바이스 등록 시 선택)

### Hub 별 resource·CSV 컬럼 매핑

| Hub | resource id / capability.attribute | CSV 컬럼명 | 값 인코딩 |
|---|---|---|---|
| **aqara** | `motion_status` (`3.1.85`) | `motion_status` | `1` 만 기록 (Occupied) |
| **aqara** | `lux` (`0.3.85`) | `lux` | 정수 (occupied 기간만) |
| **smartthings** | `motionSensor.motion` | `motion` | `active` / `inactive` (시작·종료 페어) |
| **smartthings** | `illuminanceMeasurement.illuminance` | `lux` | 정수 (capability 정의대로 주기 보고) |

> 같은 bundle 키 `motion_lux` 를 공유하지만 모션 컬럼명이 다름 (`motion_status` vs `motion`).
> 폴더는 device_id 로 격리되므로 schema 혼합 없음. lux 컬럼명은 통일.

### 1.1 동작 특성
- `motion_status` 값이 **1**일 때 **Occupied(재실)** 상태를 나타낸다.
- `lux`는 **occupied 기간 동안에만 기록**된다. 즉, 사람이 없는(Unoccupied) 시간대에는 조도 샘플이 생성되지 않는다.

### 1.2 샘플링 시각 불일치
- 두 resource의 sampling 시각이 **완전히 일치하지는 않는다.**
- 대부분의 타임스탬프는 동일하나, `lux` 샘플 수가 더 많은 경우가 있다 (`motion_status` 이벤트 사이에도 추가 lux 샘플 발생 가능).

### 1.3 값 의미
| Resource | Value | 의미 |
|---|---|---|
| `motion_status` | `1` | Occupied (재실) |
| `lux` | 정수 | 조도(lux, occupied 기간 동안만) |

> ⚠️ `motion_status`는 **`1`(Occupied)만 기록**된다. Unoccupied 상태로 전환되더라도 `255` 등 별도 값이 기록되지 **않음**. (진동 센서의 `move_detect`와 대비 — [§4.2](#42-resource-의미) 참조.)

### 1.4 CSV 저장 형식 (통합 wide 포맷)
Motion Sensor T1은 `motion_status`와 `lux`를 **하나의 CSV** 로 합쳐 저장한다. 컬럼: `time, motion_status, lux`.

- 두 resource의 timestamp를 **outer join**한다 (합집합).
- 어느 한쪽에만 샘플이 있는 시각에는 해당 컬럼을 **빈 값**으로 남긴다.
- 예시 (`motion_T1_lux.csv` + `motion_T1_motion.csv`, 2026-05-11 13:00~14:00 1시간 구간 통합):

  ```csv
  time,motion_status,lux
  2026-05-11 13:10:08,1,5
  2026-05-11 13:11:06,1,97
  2026-05-11 13:14:35,1,97
  2026-05-11 13:16:05,1,93
  2026-05-11 13:17:29,1,93
  2026-05-11 13:22:14,1,93
  2026-05-11 13:40:11,1,94
  2026-05-11 13:41:13,1,93
  2026-05-11 13:42:16,1,93
  2026-05-11 13:43:14,1,92
  2026-05-11 13:44:57,1,91
  2026-05-11 13:51:46,,91
  2026-05-11 13:53:46,1,93
  2026-05-11 13:56:17,1,92
  ```

  → 13:51:46 행은 `motion_status` 컬럼이 비어있고 `lux=91`. (lux만 단독으로 기록된 샘플)
- 행은 `time` 오름차순으로 정렬.

---

## 2. Door and Window Sensor T1 (열림/닫힘 센서 T1)

- **Model**: `lumi.magnet.agl02`
- **지원 hub**: `aqara` · `smartthings` (디바이스 등록 시 선택)

### Hub 별 resource·CSV 컬럼 매핑

| Hub | resource id / capability.attribute | CSV 컬럼명 | 값 인코딩 |
|---|---|---|---|
| **aqara** | `magnet_status` (`3.1.85`) | `magnet_status` | `1`=열림 / `0`=닫힘 |
| **smartthings** | `contactSensor.contact` | `contact` | `open` / `closed` |

> bundle key 는 양 hub 공통(`magnet_status`)이지만 컬럼명·값 인코딩이 다르다. 통합 해석(open↔1, closed↔0)은 [app/display_extract.py](app/display_extract.py) hub 별 분기에서 처리.

### 2.1 동작 특성
- **이벤트 기반** 기록: 상태가 바뀔 때마다 **1회씩** 기록된다 (주기 샘플링 아님).
- 따라서 일별 데이터 행 수는 해당 일자에 발생한 개폐 횟수와 동일.

### 2.2 값 의미
| Value | 의미 |
|---|---|
| `1` | 열림 (Open) |
| `0` | 닫힘 (Close) |

### 2.3 데이터 사례 (`door_T1_magnet.csv`)
| 시각 (KST) | magnet_status | 해석 |
|---|---|---|
| 2026-05-08 13:14:55 | 1 | 열림 |
| 2026-05-08 16:43:04 | 0 | 닫힘 |

→ 13:14:55에 열린 후 16:43:04까지 약 3시간 28분 동안 열린 상태였다는 의미.

### 2.4 후처리 주의
- 두 이벤트(열림 → 닫힘) 사이의 **지속 시간(duration)** 은 별도 계산이 필요하다.
- 일자 경계를 넘어 열린 상태가 지속될 수 있으므로 일 단위 CSV만으로는 개폐 쌍이 완결되지 않을 수 있다 (전후 일자 CSV를 함께 확인).

---

## 3. Motion Sensor P1 (고감도 모션 센서 P1)

- **Model**: `lumi.motion.ac02`
- **지원 hub**: `aqara` · `smartthings` (디바이스 등록 시 선택)

### Hub 별 resource·CSV 컬럼 매핑
T1 과 **완전 동일** ([§1](#1-motion-sensor-t1-모션-센서-t1)의 매핑표 참조). bundle key `motion_lux`.

### 3.1 동작 특성
- Resource 구성·값 의미·CSV 저장 형식은 [1. Motion Sensor T1](#1-motion-sensor-t1-모션-센서-t1)과 동일하다.
  - aqara: `motion_status` 는 `1` 만 기록 / `lux` 는 occupied 기간만 / 시각 불일치 가능. **`time, motion_status, lux` wide 포맷**.
  - smartthings: `motion` 은 `active`/`inactive` 페어 / `lux` 는 capability 정의대로 보고. **`time, motion, lux` wide 포맷**.
- T1 대비 **감지 범위가 더 넓다**.
- **3단계 민감도 설정**이 가능하다 (Aqara 앱에서 설정; API로 수집되는 시계열에는 민감도 값 자체는 포함되지 않으며, 감지 빈도/이벤트 발생 임계점에 간접 영향).

### 3.2 T1과의 차이 요약
| 항목 | Motion Sensor T1 | Motion Sensor P1 |
|---|---|---|
| Model | `lumi.motion.agl02` | `lumi.motion.ac02` |
| 감지 범위 | 표준 | 넓음 |
| 민감도 설정 | 없음 | 3단계 |
| Resource 구성 | `motion_status`, `lux` (동일) | `motion_status`, `lux` (동일) |
| Resource ID | `3.1.85`, `0.3.85` (동일) | `3.1.85`, `0.3.85` (동일) |

---

## 4. Vibration Sensor T1 (진동 센서 T1)

- **Model**: `lumi.vibration.agl01`
- **지원 hub**: `aqara` · `smartthings` (디바이스 등록 시 선택)

### Hub 별 resource·CSV 컬럼 매핑

| Hub | resource id / capability.attribute | CSV 컬럼명 | 값 인코딩 |
|---|---|---|---|
| **aqara** | `knock_event` (`13.3.85`) | `knock_event` | 숫자 코드 (`1`=두드림 ON, `255`=두드림 해지 등) |
| **aqara** | `move_detect` (`13.7.85`) | `move_detect` | `1`=움직임 Activated, `255`=Deactivated |
| **smartthings** | `accelerationSensor.acceleration` | `acceleration` | `active` / `inactive` (시작·종료 페어) |

> SmartThings 노출 시 `acceleration` 만 `move_detect` 와 의미 대응 (active↔1, inactive↔255). 두드림(`knock_event`) 에 해당하는 SmartThings 표준 capability 는 없으므로 해당 컬럼은 SmartThings 측에서는 수집되지 않는다 (후속에 vendor-specific custom capability 검토 가능).
>
> bundle key 는 양 hub 공통(`move_knock`)이지만 CSV 컬럼 셋이 다르다:
> - aqara: `time, move_detect, knock_event` (wide outer join — DEVICE.md §4.3)
> - smartthings: `time, acceleration` (knock 없음)

### 4.1 동작 특성
- **이벤트 기반** 기록: 두드림/움직임 이벤트 발생 시점에만 데이터가 생성된다 (주기 샘플링 아님).
- 두 resource는 서로 독립적인 이벤트이므로 시각이 일치할 필요가 없다.

### 4.2 Resource 의미
| Resource | 설명 |
|---|---|
| `knock_event` | 센서에 가해진 두드림(노크)을 감지하여 이벤트 기록 |
| `move_detect` | 센서 본체의 움직임(위치 변동·기울임)을 감지하여 이벤트 기록 |

#### `move_detect` 값 의미
| Value | 의미 |
|---|---|
| `1` | 움직임 감지 (Activated) |
| `255` | **상태 해지** (Deactivated, 움직임이 멈춰 상태가 해제됨) |

> ℹ️ Motion 센서(T1/P1)의 `motion_status`는 `1`만 기록되고 해지 시 별도 값이 없는 반면, 진동 센서의 `move_detect`는 시작/해지 양쪽이 모두 기록된다는 점이 다르다.

### 4.3 CSV 저장 형식 (통합 wide 포맷)
Vibration Sensor T1은 `move_detect`와 `knock_event`를 **하나의 CSV** 로 합쳐 저장한다. Bundle 키는 `move_knock`, 컬럼은 `time, move_detect, knock_event`.

- 두 resource의 timestamp를 **outer join**한다 (합집합).
- 어느 한쪽에만 샘플이 있는 시각에는 해당 컬럼을 **빈 값**으로 남긴다 ([§1.4](#14-csv-저장-형식-통합-wide-포맷)의 motion_lux와 동일 패턴).
- 값 코드는 resource별로 그대로 유지: `move_detect`는 `1`(감지)/`255`(해지), `knock_event`는 원본 코드값.
- 행은 `time` 오름차순으로 정렬.

```csv
time,move_detect,knock_event
2026-02-10 05:08:00,1,
2026-02-10 05:08:00,255,
2026-02-10 09:31:00,,1
2026-02-10 09:31:00,,255
```

→ 05:08:00의 두 행은 움직임 감지·해지, 09:31:00의 두 행은 두드림 이벤트(값 자체는 source에 따라 다름).

**저장 경로**: `data/move_knock/{device_id}/{YYYYMMDD}_{last6}.csv`

---

## 5. Wireless Mini Switch T1 (무선 미니 스위치 T1)

- **Model**: `lumi.remote.b1acn02`
- **지원 hub**: `aqara` · `smartthings` (디바이스 등록 시 선택)

### Hub 별 resource·CSV 컬럼 매핑

| Hub | resource id / capability.attribute | CSV 컬럼명 | 값 인코딩 |
|---|---|---|---|
| **aqara** | `switch_status` (`13.1.85`) | `switch_status` | 숫자 코드 (§5.2 표) |
| **smartthings** | `button.button` | `button` | `pushed` / `held` / `double` 등 문자열 |

> bundle key 는 양 hub 공통(`switch_status`)이지만 컬럼명·값 인코딩이 다르다.
> Aqara 숫자 코드(1·2·3·16·17·18) ↔ SmartThings 문자열 매핑 통합 해석은 [app/display_extract.py](app/display_extract.py) 의 hub 별 분기에서 처리 (raw 값은 CSV 에 그대로 적재).

### 5.1 동작 특성
- **이벤트 기반** 기록: 사용자가 스위치를 조작할 때마다 1회씩 기록된다 (주기 샘플링 아님).
- 따라서 일별 데이터 행 수는 해당 일자에 발생한 조작 횟수와 동일.

### 5.2 값 의미
| Value | 의미 (영문) | 한국어 설명 |
|---|---|---|
| `1`  | `click`               | 한 번 클릭 |
| `2`  | `double_click`        | 두 번 클릭 |
| `3`  | `three_click`         | 세 번 클릭 |
| `16` | `long_click_press`    | 롱 프레스 시작 (누르기 시작) |
| `17` | `long_click_release`  | 롱 프레스 해제 (누름 종료) |
| `18` | `shake`               | 흔들림 감지 |

> ℹ️ 롱 프레스의 **지속 시간**은 `16`(시작)과 그 다음 `17`(해제) 두 이벤트의 시각 차이로 계산한다. 진동 센서의 `move_detect`(`1`↔`255`)와 유사한 시작/해제 페어 패턴.

### 5.3 CSV 저장 형식
단일 resource이므로 하나의 CSV에 단순 저장. 컬럼: `time, switch_status`.

```csv
time,switch_status
2026-05-12 09:14:22,1
2026-05-12 09:14:25,2
2026-05-12 10:02:11,16
2026-05-12 10:02:13,17
2026-05-12 14:30:00,18
```

→ 09:14:22 클릭, 09:14:25 더블 클릭, 10:02:11~13 약 2초간 롱 프레스, 14:30:00 흔들림.

### 5.4 후처리 주의
- 짝이 맞지 않는 롱 프레스(예: `16`만 있고 `17`이 없거나 일자 경계를 횡단)는 별도 처리 필요. `door_t1`/`vibration_t1`의 일자 경계 처리(§2.4 / §4.1)와 동일한 패턴 적용 가능.
- `1`/`2`/`3`/`18`은 단발 이벤트이므로 짝이 없다.

---

## 6. Vibration Sensor (aq1) (진동 센서, 구형 SKU)

- **Model**: `lumi.vibration.aq1`
- **지원 hub**: `aqara` only (구형 SKU 로 SmartThings 노출 없음)
- **Resources** (aqara):
  - `vibration_event` (`13.1.85`) — 진동 이벤트 (모든 종류를 단일 resource에 코드 값으로 통합)

> ⚠️ §4의 "Vibration Sensor T1"(`lumi.vibration.agl01`)과는 **다른 SKU** 다. T1은 `knock_event`/`move_detect` 두 resource로 분리되지만, 본 모델은 단일 resource에 모든 이벤트를 다른 값 코드로 기록한다. 등록 시 모델명을 정확히 구분.

### 6.1 동작 특성
- **이벤트 기반** 기록: 이벤트 발생 시점에만 1회 기록 (주기 샘플링 아님).
- 모든 이벤트 종류가 하나의 시계열에 섞여 들어오므로, 후처리에서 값 코드로 분류해야 한다.

### 6.2 값 의미
| Value | 의미 (영문) | 한국어 설명 |
|---|---|---|
| `0` | `Knock` (in security mode)                | 보안 모드에서 두드림 감지 |
| `1` | Triggered after being stationary (3 modes) | 일정 시간 정지 후 트리거 (3가지 모드 모두) — 움직임 **시작** 신호 |
| `2` | `Tilt event`                              | 기울임/진동 감지 — 움직임이 지속되는 동안 연속 발생 |
| `3` | `Free Fall`                               | 자유 낙하 감지 |
| `4` | Auto-completed close-state learning (security mode) | 보안 모드에서 닫힘 상태 학습 자동 완료 |
| `5` | `Take Away`                               | 가져감(들어 올림) 감지 |
| `6` | `Three knocks`                            | 세 번 두드림 감지 |
| `255` | Deactivated                              | 움직임 **해지** 신호 |

- 세 가지 모드에 대한 언급이 있으나 현재 모드 확인이나 모드 변경에 대한 설명은 제공되지 않음.
  - Security Mode: 문/창 개폐 상태 모니터링
  - Knock Mode: 무선 연결로 다른 스마트 기기 제어
  - Bed Mode: 침대 활동을 모니터링해 수면 품질 판단 보조

> ℹ️ 실제 관측 데이터(UpgoPlus 진동센서)에서는 `1`·`2`·`255` 가 대부분을 차지한다. 이 세 값은 모두 움직임 관련 신호(시작·진동·해지)이므로, 시각화에서 **`1`·`2`·`255` 를 움직임 막대(interval)** 로 — 인접 이벤트를 gap 임계로 그룹핑 — 표시하고, 그 외 코드(`0`·`3`·`4`·`5`·`6`)는 **기타 이벤트 점(tick)** 으로 표시한다 ([DISPLAY.md §4.5](DISPLAY.md#45-vibration-sensor-aq1-vibration_aq1)).

### 6.3 CSV 저장 형식
단일 resource이므로 하나의 CSV에 단순 저장. 컬럼: `time, vibration_event`.

```csv
time,vibration_event
2026-05-12 03:14:00,3
2026-05-12 09:00:00,2
2026-05-12 09:00:30,5
2026-05-12 12:00:00,6
```

→ 새벽 자유 낙하, 오전 기울임 후 30초 뒤 가져감, 정오에 세 번 두드림.

### 6.4 후처리 주의
- 값 코드별로 별개의 이벤트 종류를 의미하므로, 분석 시 코드별 분리 집계가 필수.
- 보안 모드(security mode) 활성 여부에 따라 일부 코드(`0`, `4`)의 발생 가능성이 달라진다 (모드 정보 자체는 시계열에 포함되지 않음).

---

## 7. Motion and Light Sensor P2 (Aqara 모션·조도 센서 P2)

- **device_type 키**: `motion_and_light_p2`
- **Model**: Matter 디바이스 (Aqara 측 모델명은 무관 — SmartThings 가 capability 단으로 노출)
- **지원 hub**: `smartthings` only — Matter 프로토콜 디바이스로 SmartThings 허브 페어링 후
  토큰을 통해 접근. Aqara Open API 로는 노출되지 않음.

### 7.1 동작 특성
- T1/P1 보다 신형이며 Matter/Thread 프로토콜로 동작.
- SmartThings 측에 `motionSensor` + `illuminanceMeasurement` (+ `battery`) capability 노출.
- motion 은 명시적 `inactive` 종료 이벤트를 보냄 → T1/P1 의 `motion_status=1` only 와 달리 상태머신
  기반 영역 추출 가능 (DISPLAY.md §4 SmartThings 분기).

### 7.2 Resource·CSV 컬럼 매핑

| Hub | capability.attribute | CSV 컬럼명 | 값 인코딩 |
|---|---|---|---|
| **smartthings** | `motionSensor.motion` | `motion` | `active` / `inactive` |
| **smartthings** | `illuminanceMeasurement.illuminance` | `lux` | 정수 (lux) |

bundle 키 = `motion_lux` (Aqara motion_t1/p1 과 공유). 폴더는 device_id 로 격리되므로 schema 혼합 없음.

### 7.3 CSV 저장 형식 (통합 wide 포맷)
컬럼: `time, motion, lux`. outer join, 빈 셀은 `,,`.

```csv
time,motion,lux
2026-05-12 17:00:00,active,
2026-05-12 17:00:00,,127
2026-05-12 17:01:30,inactive,
```

### 7.4 등록 시 주의
- device_id 형식: 표준 SmartThings UUID(`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`) 또는 Matter 의
  24-hex 형식 (예: `3e7b675d14dfa559dae13000`) — 둘 다 허용. 정확한 ID 는
  `smartthings devices --token <access_token>` 출력에서 확인 (DESIGN.md §15.12).

---

## 8. Temperature and Humidity Sensor T1 (온습도 센서 T1)

- **device_type 키**: `temp_humi_t1`
- **Model**: `lumi.sensor_ht.agl02`
- **지원 hub**: `aqara` · `smartthings` (디바이스 등록 시 선택)

### Hub 별 resource·CSV 컬럼 매핑

| Hub | resource id / capability.attribute | CSV 컬럼명 | 값 인코딩 |
|---|---|---|---|
| **aqara** | `temperature_value` (`0.1.85`) | `temperature_value` | 부동소수 °C (예: `23.4`) |
| **aqara** | `humidity_value` (`0.2.85`) | `humidity_value` | 부동소수 %RH (예: `42.0`) |
| **smartthings** | `temperatureMeasurement.temperature` | `temperature_value` | 부동소수 °C (`23.4`) |
| **smartthings** | `relativeHumidityMeasurement.humidity` | `humidity_value` | 부동소수 %RH (`42.0`) |

> 양 hub 모두 부동소수 측정값이며 의미·단위가 동일하므로 **컬럼명 통일** (`temperature_value`, `humidity_value`). bundle 키 `temp_humi`, 폴더는 device_id 로 격리. lux 컬럼명 통일과 동일한 패턴 ([§1](#1-motion-sensor-t1-모션-센서-t1)).

### 8.1 동작 특성
- **주기 샘플링**: 일정 주기(또는 측정값 변화 임계) 마다 측정값을 보고한다. Motion T1/P1 의 lux 처럼 occupied 조건이 없어 24시간 내내 샘플이 들어온다 (단, 보고 간격은 디바이스 펌웨어·환경에 따라 수 분~수십 분).
- **온도/습도는 독립 resource**: 두 값의 보고 시각이 완전히 일치하지는 않을 수 있다 — motion_lux 의 motion_status/lux 불일치와 같은 패턴 ([§1.2](#12-샘플링-시각-불일치)).
- 이벤트 기반 센서가 아니므로 일별 행 수가 0건이면 수집 실패를 의심해야 한다 (정상 동작 시 0 가능성 매우 낮음).

### 8.2 값 의미
| Resource | 단위 | 비고 |
|---|---|---|
| `temperature_value` | °C | 실내 공기 온도. 음수 가능 (영하 환경) |
| `humidity_value`    | %RH | 상대습도. 0~100 범위 |

### 8.3 CSV 저장 형식 (통합 wide 포맷)
컬럼: `time, temperature_value, humidity_value`. 두 resource 의 timestamp 를 outer join, 빈 셀은 `,,` ([§1.4](#14-csv-저장-형식-통합-wide-포맷) 와 동일 패턴).

```csv
time,temperature_value,humidity_value
2026-05-12 09:00:00,23.4,42.0
2026-05-12 09:05:00,23.5,
2026-05-12 09:05:30,,41.8
2026-05-12 09:10:00,23.6,41.9
```

→ 09:05:00 행은 humidity 보고 없음, 09:05:30 행은 temperature 보고 없음. 두 resource 의 sampling 시각이 어긋날 수 있음을 보여주는 예시.

**저장 경로**: `data/temp_humi/{device_id}/{YYYYMMDD}_{suffix}.csv` (suffix 는 hub 별 분기 — Aqara 끝 6자리 / SmartThings 첫 8자리, [부록 A](#부록-a-api-호출-파라미터-매핑) 참조).

### 8.4 후처리 주의
- 부동소수 값이므로 정확 비교(`==`)는 피하고 임계 범위 비교 사용.
- 단위·범위 sanity check: 일반 실내 환경 기준 `-20 ≤ temperature ≤ 50`, `0 ≤ humidity ≤ 100`. 이 범위를 벗어나면 센서 오류·전송 손실 의심.
- SmartThings 가 `null` 을 보내는 일시적 케이스는 빈 셀로 저장된다 (CSV 빈 문자열). 분석 시 NaN 처리.

---

## 9. Motion and Light Sensor (Watts Matter) (모션 센서 — 와츠매터)

- **device_type 키**: `motion_and_light_wm`
- **지원 hub**: `smartthings` only (Matter 디바이스)
- Motion and Light Sensor P2 (§7) 와 **완전 동일한 SmartThings 매핑**을 사용한다. bundle key `motion_lux`, 컬럼 `time, motion, lux`.
  - `motion`: `active` / `inactive` 페어
  - `lux`: 부동소수 정기 보고
- 폴더는 device_id 로 격리되므로 P2 와 schema 충돌 없음.
- 디스플레이는 §7 / P2 와 동일 (motion bar + lux line plot, `extract_st_motion_intervals` + `extract_st_lux_series`).

> ℹ️ "와츠매터" = Watts × Matter — Matter 프로토콜로 SmartThings 허브에 노출되는 외부 제조사 디바이스. Aqara 측 모델 매핑은 없다.

---

## 10. Temperature and Humidity Sensor (Watts Matter) (온습도 센서 — 와츠매터)

- **device_type 키**: `temp_humi_wm`
- **지원 hub**: `smartthings` only (Matter 디바이스)
- Temperature and Humidity Sensor T1 (§8) 의 SmartThings 분기와 **완전 동일한 매핑**. bundle key `temp_humi`, 컬럼 `time, temperature_value, humidity_value`.
- 폴더는 device_id 로 격리되므로 T1 과 schema 충돌 없음.
- 디스플레이는 §8 / T1 과 동일 line plot (`extract_temp_humi_series`, 이중축 — 좌=온도, 우=습도).

---

## 부록 A. API 호출 파라미터 매핑

Aqara Open API `fetch.resource.history` 호출 시 사용:

```json
{
  "intent": "fetch.resource.history",
  "data": {
    "subjectId": "lumi.<device_id_소문자>",
    "resourceIds": ["<resource_id>"],
    "startTime": "<UTC milliseconds>",
    "endTime":   "<UTC milliseconds>"
  }
}
```

- `subjectId`는 항상 `lumi.` prefix + 소문자 hex device ID. (`record_data.py`의 `normalize_subject_id` 참조.)
- `resourceIds`는 위 요약표의 Resource ID 값 사용.
- 1회 응답 최대 100건 → 페이지네이션은 마지막 timestamp + 1초로 `startTime` 재설정하여 반복 호출.

### 부록 A-2. SmartThings 호출 (CLI 기반)

hub=`smartthings` 디바이스의 history 는 REST 가 아닌 **`smartthings` CLI subprocess** 로 호출 (DESIGN.md §15.5):

```bash
smartthings devices:history <deviceId> \
    -L <page_size>          # 한 페이지 최대 N건 (기본 1000)
    -U                      # UTC ISO8601 출력
    -j                      # JSON
    -B <before_epoch_ms>    # before: 이 시각 이전 이벤트만
    --token <access_token>
```

- `<deviceId>` 는 SmartThings UUID(8-4-4-4-12) 또는 Matter 24-hex 둘 다 허용.
- `-A` (after) 는 일부 디바이스에서 CLI 버그 발생 → **사용 안 함**, 시작점 필터는 Python 측 후처리.
- 응답은 시간 내림차순. batch 의 가장 오래된 시각이 target_date 시작 이전이면 종료, 그 외엔 `-B` 를 갱신해 다음 페이지.
- Rate limit: 429 / `rate limit` / `too many requests` 감지 시 60/120/180s 점진 백오프.
- 응답 형태:
  ```json
  [
    {"time":"2026-05-12T08:00:00.000+00:00", "capability":"motionSensor",
     "attribute":"motion", "value":"active", "deviceId":"<id>"},
    ...
  ]
  ```

## 부록 B. 운영 메모

- **이벤트 기반 센서**(Door and Window Sensor T1, Vibration Sensor T1, Wireless Mini Switch T1, Vibration Sensor aq1)는 일별 행 수가 0건일 수 있다. 이는 정상이며 수집 실패가 아님.
- **주기성+이벤트 혼합 센서**(Motion T1/P1)는 occupied 시간 비중에 따라 일별 행 수 편차가 크다.
- **주기 측정 센서**(Temperature and Humidity Sensor T1)는 24시간 내내 보고되므로 일별 행 수가 0건이면 수집 실패·디바이스 무응답을 의심해야 한다.
- 모든 시각은 CSV 저장 시점에 **KST**로 변환되어 기록된다.
- 일자 경계를 횡단하는 "시작↔해지" 페어가 있는 센서(Door T1 `1↔0`, Vibration T1 `1↔255` move_detect, Switch T1 `16↔17` long press)는 디스플레이 단계에서 직전 일자 CSV로 경계를 복원한다 ([DISPLAY.md §4.7](DISPLAY.md#47-디바이스-타입--bundle-매핑) 매핑표).
