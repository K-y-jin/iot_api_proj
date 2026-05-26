"""수집 대상 기기 종류 매핑 (DESIGN.md §3, DEVICE.md SSOT).

DEVICE_TYPES = 기기 종류 키 → 모델/표시명/hub별 bundle 목록.
하나의 bundle = 하나의 출력 CSV 파일.
- 단일 resource bundle: time,value 그대로 저장
- 멀티 resource bundle: timestamp outer join → wide 포맷 CSV

hub 다중 지원 (DESIGN.md §15):
- 한 device_type 이 여러 hub 로 접근될 수 있음 (예: Aqara Motion T1 은
  Aqara cloud + SmartThings Zigbee 양쪽 접근 가능).
- `bundles_by_hub` dict 로 hub → bundle 목록을 분리.
- 같은 bundle key 라도 hub 별로 resource id 와 csv_columns 가 다를 수 있음.
- 등록 시 사용자가 hub 를 명시 선택 (devices.hub 컬럼).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Resource:
    """단일 resource 메타데이터.

    id: hub 별 다른 형식
        - aqara       : '3.1.85' 등 dotted resource id
        - smartthings : '<capability>.<attribute>' (예: 'motionSensor.motion')
    name: CSV 컬럼명 (hub 간 다를 수 있음 — 예: aqara 'motion_status' vs smartthings 'motion')
    """
    id: str
    name: str
    name_ko: str


@dataclass(frozen=True)
class Bundle:
    """하나의 출력 CSV에 묶이는 resource 그룹 (DESIGN.md §3.1).

    같은 bundle key 라도 hub 별로 resources 와 csv_columns 가 다를 수 있음
    (예: motion_lux 는 aqara=motion_status,lux / smartthings=motion,lux).
    """
    key: str
    resources: tuple[Resource, ...]
    csv_columns: tuple[str, ...]


@dataclass(frozen=True)
class DeviceType:
    """기기 종류 메타데이터 (DESIGN.md §3.1, §15).

    bundles_by_hub: hub 별 Bundle 목록.
      'aqara'       — Aqara Open Cloud API 로 접근
      'smartthings' — SmartThings PAT + CLI history 로 접근
    한 hub 만 지원하는 종류는 dict 에 그 키만 둠.
    """
    key: str
    model: str                 # 대표 모델명 (Aqara 우선)
    display_name: str
    display_name_ko: str
    sampling: str              # 'periodic+event' | 'event'
    bundles_by_hub: dict[str, tuple[Bundle, ...]] = field(default_factory=dict)


# ─────────────────────────── 헬퍼 ───────────────────────────

def bundles_for(device_type_key: str, hub: str) -> tuple[Bundle, ...]:
    """device_type × hub 의 bundle 목록. 미지원 조합은 빈 tuple."""
    dt = DEVICE_TYPES.get(device_type_key)
    if dt is None:
        return ()
    return dt.bundles_by_hub.get(hub, ())


def supported_hubs(device_type_key: str) -> tuple[str, ...]:
    """device_type 이 지원하는 hub 목록 (등록 시 hub 드롭다운 필터)."""
    dt = DEVICE_TYPES.get(device_type_key)
    if dt is None:
        return ()
    return tuple(dt.bundles_by_hub.keys())


# ───────────────── 디바이스 종류 정의 (DEVICE.md 요약표 + §15) ─────────────────

# 자주 쓰는 SmartThings motion 센서 bundle — Aqara T1/P1/P2 가 SmartThings 로 노출될 때 동일.
_ST_MOTION_LUX_BUNDLES: tuple[Bundle, ...] = (
    Bundle(
        key="motion_lux",
        resources=(
            Resource("motionSensor.motion", "motion", "움직임 (active/inactive)"),
            Resource("illuminanceMeasurement.illuminance", "lux", "조도(lux)"),
        ),
        csv_columns=("time", "motion", "lux"),
    ),
)

# SmartThings 측 무선 미니 스위치 bundle (DEVICE.md §5).
# bundle key 는 aqara 측("switch_status")과 동일하게 유지해 device_type 내 통일.
# CSV 컬럼명은 SmartThings 표준명("button") 사용 — 값은 'pushed'/'held'/'double' 등 문자열로
# 그대로 저장하고, Aqara 숫자 코드(1·2·3·16·17·18)와의 통합은 display_extract.py 단계에서 분기.
_ST_BUTTON_BUNDLES: tuple[Bundle, ...] = (
    Bundle(
        key="switch_status",
        resources=(
            Resource("button.button", "button", "버튼 눌림 이벤트"),
        ),
        csv_columns=("time", "button"),
    ),
)

# SmartThings 측 도어/창 센서 bundle (DEVICE.md §2).
# bundle key 는 aqara 측("magnet_status")과 동일. CSV 컬럼은 SmartThings 표준명("contact"),
# 값은 'open'/'closed' 문자열 그대로 저장 (Aqara 1/0 과의 의미 매핑은 display 단계 분기).
_ST_CONTACT_BUNDLES: tuple[Bundle, ...] = (
    Bundle(
        key="magnet_status",
        resources=(
            Resource("contactSensor.contact", "contact", "접점 상태 (열림/닫힘)"),
        ),
        csv_columns=("time", "contact"),
    ),
)

# SmartThings 측 진동 센서 bundle (DEVICE.md §4).
# bundle key 는 aqara 측("move_knock")과 동일. SmartThings 측에는 두드림(knock) 에 대응하는 표준
# capability 가 없으므로 acceleration 단일 컬럼만 노출 (active/inactive 페어 → display 에서 move bar).
_ST_ACCELERATION_BUNDLES: tuple[Bundle, ...] = (
    Bundle(
        key="move_knock",
        resources=(
            Resource("accelerationSensor.acceleration", "acceleration", "가속도 감지 (active/inactive)"),
        ),
        csv_columns=("time", "acceleration"),
    ),
)


DEVICE_TYPES: dict[str, DeviceType] = {
    # ─── Motion Sensor T1 — Aqara 또는 SmartThings(Zigbee) ───
    "motion_t1": DeviceType(
        key="motion_t1",
        model="lumi.motion.agl02",
        display_name="Motion Sensor T1",
        display_name_ko="모션 센서 T1",
        sampling="periodic+event",
        bundles_by_hub={
            "aqara": (
                Bundle(
                    key="motion_lux",
                    resources=(
                        Resource("3.1.85", "motion_status", "재실 감지 상태"),
                        Resource("0.3.85", "lux", "조도"),
                    ),
                    csv_columns=("time", "motion_status", "lux"),
                ),
            ),
            "smartthings": _ST_MOTION_LUX_BUNDLES,
        },
    ),
    # ─── Motion Sensor P1 — Aqara 또는 SmartThings(Zigbee) ───
    "motion_p1": DeviceType(
        key="motion_p1",
        model="lumi.motion.ac02",
        display_name="Motion Sensor P1",
        display_name_ko="모션 센서 P1",
        sampling="periodic+event",
        bundles_by_hub={
            "aqara": (
                Bundle(
                    key="motion_lux",
                    resources=(
                        Resource("3.1.85", "motion_status", "재실 감지 상태"),
                        Resource("0.3.85", "lux", "조도"),
                    ),
                    csv_columns=("time", "motion_status", "lux"),
                ),
            ),
            "smartthings": _ST_MOTION_LUX_BUNDLES,
        },
    ),
    # ─── Door T1 — Aqara 또는 SmartThings(contactSensor.contact) ───
    # 의미 대응: aqara magnet_status 1 ↔ smartthings contact 'open',  0 ↔ 'closed' (DEVICE.md §2).
    "door_t1": DeviceType(
        key="door_t1",
        model="lumi.magnet.agl02",
        display_name="Door and Window Sensor T1",
        display_name_ko="열림/닫힘 센서 T1",
        sampling="event",
        bundles_by_hub={
            "aqara": (
                Bundle(
                    key="magnet_status",
                    resources=(Resource("3.1.85", "magnet_status", "자석 접점 상태"),),
                    csv_columns=("time", "magnet_status"),
                ),
            ),
            "smartthings": _ST_CONTACT_BUNDLES,
        },
    ),
    # ─── Vibration T1 — Aqara 또는 SmartThings(accelerationSensor.acceleration) ───
    # SmartThings 측에는 knock_event 에 대응하는 표준 capability 가 없어 acceleration 한 컬럼만 수집.
    # 의미 대응: aqara move_detect 1 ↔ smartthings acceleration 'active', 255 ↔ 'inactive' (DEVICE.md §4).
    "vibration_t1": DeviceType(
        key="vibration_t1",
        model="lumi.vibration.agl01",
        display_name="Vibration Sensor T1",
        display_name_ko="진동 센서 T1",
        sampling="event",
        bundles_by_hub={
            "aqara": (
                Bundle(
                    key="move_knock",
                    resources=(
                        Resource("13.7.85", "move_detect", "움직임 감지"),
                        Resource("13.3.85", "knock_event", "두드림 이벤트"),
                    ),
                    csv_columns=("time", "move_detect", "knock_event"),
                ),
            ),
            "smartthings": _ST_ACCELERATION_BUNDLES,
        },
    ),
    # ─── Switch T1 — Aqara 또는 SmartThings(button.button) ───
    # SmartThings 측 button 값 'pushed'/'held'/'double' 등은 Aqara 숫자 코드
    # (1·2·3·16·17·18)와 의미가 1:1로 매핑되지만, raw 값 그대로 CSV 에 적재하고
    # 통합 해석은 display_extract.py 에서 hub 별 분기로 처리 (DEVICE.md §5).
    "switch_t1": DeviceType(
        key="switch_t1",
        model="lumi.remote.b1acn02",
        display_name="Wireless Mini Switch T1",
        display_name_ko="무선 미니 스위치 T1",
        sampling="event",
        bundles_by_hub={
            "aqara": (
                Bundle(
                    key="switch_status",
                    resources=(Resource("13.1.85", "switch_status", "스위치 클릭 이벤트"),),
                    csv_columns=("time", "switch_status"),
                ),
            ),
            "smartthings": _ST_BUTTON_BUNDLES,
        },
    ),
    # ─── Vibration aq1 — Aqara only (구형 SKU). ───
    "vibration_aq1": DeviceType(
        key="vibration_aq1",
        model="lumi.vibration.aq1",
        display_name="Vibration Sensor aq1",
        display_name_ko="진동 센서 aq1",
        sampling="event",
        bundles_by_hub={
            "aqara": (
                Bundle(
                    key="vibration_event",
                    resources=(Resource("13.1.85", "vibration_event", "진동 이벤트"),),
                    csv_columns=("time", "vibration_event"),
                ),
            ),
        },
    ),
    # ─── Motion and Light Sensor P2 — SmartThings(Matter) only ───
    # 키 prefix 'st_' 제거 (hub 가 별도 컬럼으로 분리됐으므로 device_type 키에 hub 인코딩 불필요).
    # display_name 은 다른 device_type 과 통일된 명칭 양식 ("모션 센서 <SKU>") 사용.
    "motion_and_light_p2": DeviceType(
        key="motion_and_light_p2",
        model="Aqara Motion and Light Sensor P2 (Matter)",  # 내부 메타용 — UI 노출 안 함
        display_name="Motion Sensor P2",
        display_name_ko="모션 센서 P2",
        sampling="periodic+event",
        bundles_by_hub={"smartthings": _ST_MOTION_LUX_BUNDLES},
    ),
    # ─── Motion and Light Sensor (Watts Matter) — SmartThings(Matter) only ───
    # P2 와 동일한 motion_lux bundle (active/inactive + lux). 폴더는 device_id 로 격리되므로
    # P2 와 schema 충돌 없음. 디스플레이는 P2 와 동일 분기 (st_motion).
    "motion_and_light_wm": DeviceType(
        key="motion_and_light_wm",
        model="Watts Matter Motion and Light Sensor (Matter)",
        display_name="Motion Sensor (Watts Matter)",
        display_name_ko="모션 센서 (와츠매터)",
        sampling="periodic+event",
        bundles_by_hub={"smartthings": _ST_MOTION_LUX_BUNDLES},
    ),
    # ─── Temperature and Humidity Sensor T1 — Aqara 또는 SmartThings ───
    # 양 hub 모두 부동소수 측정값을 보고하므로 컬럼명 통일 (DEVICE.md §8). lux 컬럼 통일
    # 패턴과 동일하게, bundle key 와 CSV 컬럼 셋(`temp_humi` / `time,temperature_value,humidity_value`)을
    # 두 hub 가 공유한다. 폴더는 device_id 로 격리되므로 schema 혼합 없음.
    "temp_humi_t1": DeviceType(
        key="temp_humi_t1",
        model="lumi.sensor_ht.agl02",
        display_name="Temperature and Humidity Sensor T1",
        display_name_ko="온습도 센서 T1",
        sampling="periodic",
        bundles_by_hub={
            "aqara": (
                Bundle(
                    key="temp_humi",
                    resources=(
                        Resource("0.1.85", "temperature_value", "온도(°C)"),
                        Resource("0.2.85", "humidity_value", "상대습도(%)"),
                    ),
                    csv_columns=("time", "temperature_value", "humidity_value"),
                ),
            ),
            "smartthings": (
                Bundle(
                    key="temp_humi",
                    resources=(
                        Resource("temperatureMeasurement.temperature", "temperature_value", "온도(°C)"),
                        Resource("relativeHumidityMeasurement.humidity", "humidity_value", "상대습도(%)"),
                    ),
                    csv_columns=("time", "temperature_value", "humidity_value"),
                ),
            ),
        },
    ),
    # ─── Temperature and Humidity Sensor (Watts Matter) — SmartThings(Matter) only ───
    # temp_humi_t1 의 smartthings 분기와 동일 bundle/컬럼. 폴더는 device_id 로 격리.
    # 디스플레이는 temp_humi_t1 과 동일 분기 (temp_humi_plot).
    "temp_humi_wm": DeviceType(
        key="temp_humi_wm",
        model="Watts Matter Temperature and Humidity Sensor (Matter)",
        display_name="Temperature and Humidity (Watts Matter)",
        display_name_ko="온습도 센서 (와츠매터)",
        sampling="periodic",
        bundles_by_hub={
            "smartthings": (
                Bundle(
                    key="temp_humi",
                    resources=(
                        Resource("temperatureMeasurement.temperature", "temperature_value", "온도(°C)"),
                        Resource("relativeHumidityMeasurement.humidity", "humidity_value", "상대습도(%)"),
                    ),
                    csv_columns=("time", "temperature_value", "humidity_value"),
                ),
            ),
        },
    ),
}


# ─────────────────────────── 정규화 / 식별자 헬퍼 ───────────────────────────

def normalize_device_id(raw: str, hub: str = "aqara") -> str:
    """사용자 입력 device_id를 표준형으로 정규화 (DESIGN.md §3.2 / §15.7).

    hub='aqara'      : 'lumi.<hex 소문자>'  — lumi. prefix 보장
    hub='smartthings': 원본 그대로(소문자). 표준 UUID 또는 24-hex 둘 다 허용.
    """
    s = raw.strip()
    if hub == "smartthings":
        return s.lower()
    if s.lower().startswith("lumi."):
        return s.lower()
    return "lumi." + s.lower()


def device_id_upper(normalized: str) -> str:
    """'lumi.4cf8cdf3c752edb' → '4CF8CDF3C752EDB' (화면/파일명 표시용)."""
    if normalized.lower().startswith("lumi."):
        return normalized[5:].upper()
    return normalized.upper()


def last6(normalized: str) -> str:
    """Aqara 파일명 끝 6자리 식별자 (DESIGN.md §4). SmartThings 는 device_id_suffix 참조."""
    return device_id_upper(normalized)[-6:]


def device_id_suffix(device_id: str, hub: str = "aqara") -> str:
    """파일명 suffix 산출 (DESIGN.md §15.7).

    - aqara       : 기존 last6 (대문자 hex 끝 6자리, 예: '829AED')
    - smartthings : device_id 의 첫 8자리 대문자 (대시 제거, 예: '0A59334E' / '3E7B675D')
    """
    if hub == "smartthings":
        s = device_id.replace("-", "").lower()
        return s[:8].upper()
    return last6(device_id)


def list_resource_ids(device_type_key: str, hub: str = "aqara") -> list[str]:
    """주어진 (device_type, hub) 의 모든 resource id 평탄화."""
    return [r.id for b in bundles_for(device_type_key, hub) for r in b.resources]


if __name__ == "__main__":
    # dry-run: DEVICE_TYPES consistency check (CLAUDE.md §3.1)
    # NOTE: console output is English to avoid Windows cp949 encoding issues.
    for key, dt in DEVICE_TYPES.items():
        hubs = ",".join(dt.bundles_by_hub.keys())
        print(f"[{key}] {dt.display_name} ({dt.model}) sampling={dt.sampling} hubs=[{hubs}]")
        for hub, bundles in dt.bundles_by_hub.items():
            for b in bundles:
                res_names = ",".join(r.name for r in b.resources)
                print(f"   {hub:11} bundle={b.key:15} cols={b.csv_columns}  resources=({res_names})")
