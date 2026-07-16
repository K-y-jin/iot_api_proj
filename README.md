# Aqara 데이터 자동 수집 시스템

Aqara Open API + SmartThings (OAuth 2.0) 두 허브에서 매일 1회 전일자 센서 데이터를 받아 CSV 로 저장하고,
웹 UI 로 다수 사용자가 수집 대상을 설정·조회·시각화하는 시스템.

설계 문서: [DESIGN.md](DESIGN.md) · 수집 대상 명세: [DEVICE.md](DEVICE.md) · 디스플레이 명세: [DISPLAY.md](DISPLAY.md) · 개발 가이드: [CLAUDE.md](CLAUDE.md)

---

## 지원 디바이스 (8종, [DEVICE.md §0](DEVICE.md#0-요약표))

| Key | 모델 | 한국어 명칭 | 지원 hub | 비고 |
|---|---|---|---|---|
| `motion_t1` | `lumi.motion.agl02` | 모션 센서 T1 | aqara · smartthings | hub 별 컬럼·추출 규칙 분리 |
| `motion_p1` | `lumi.motion.ac02` | 모션 센서 P1 | aqara · smartthings | 광각·민감도 3단계 |
| `motion_and_light_p2` | (Matter) | 모션 센서 P2 | **smartthings only** | Matter, Aqara 측 직접 노출 없음 |
| `door_t1` | `lumi.magnet.agl02` | 열림/닫힘 센서 T1 | aqara · smartthings | `contactSensor.contact` 대응 |
| `vibration_t1` | `lumi.vibration.agl01` | 진동 센서 T1 | aqara · smartthings | smartthings 측은 acceleration 만 (knock 없음) |
| `switch_t1` | `lumi.remote.b1acn02` | 무선 미니 스위치 T1 | aqara · smartthings | `button.button` 대응 |
| `vibration_aq1` | `lumi.vibration.aq1` | 진동 센서 (aq1) | aqara only | 단일 resource 통합 코드 |
| `temp_humi_t1` | `lumi.sensor_ht.agl02` | 온습도 센서 T1 | aqara · smartthings | 부동소수 측정값, 디스플레이는 line plot |
| `water_leak_t1` | `lumi.flood.agl02` | 누수 센서 T1 | aqara · smartthings | 이진 상태(1=누수/0=정상, `waterSensor.water` wet/dry), door_t1 과 동일 패턴 |

같은 device_type 이라도 hub 가 다르면 CSV 컬럼·디스플레이 추출 알고리즘이 분기된다 ([DEVICE.md §0 요약표](DEVICE.md#0-요약표), [DISPLAY.md §4.7 매핑표](DISPLAY.md#47-디바이스-타입--bundle-매핑)).

---

## 빠른 시작

```powershell
# 1) 의존성 설치
pip install -r requirements.txt

# 2) 환경값 설정
copy .env.example .env
# .env 편집: AQARA_APPID/KEYID/APPKEY, ADMIN_INITIAL_PASSWORD, SESSION_SECRET 입력
# SmartThings 디바이스도 수집할 경우:
#   - smartthings CLI 가 PATH 또는 SMARTTHINGS_CLI_PATH 에 있어야 함
#   - SMARTTHINGS_CLIENT_ID / SMARTTHINGS_CLIENT_SECRET / SMARTTHINGS_OAUTH_REDIRECT_URI 입력 (OAuth)

# 3) 모듈 dry-run 점검 (CLAUDE.md §3.1)
python -m app.devices            # DEVICE_TYPES 일관성 (8종)
python -m app.db                 # DB 스키마 + 인덱스 + 마이그레이션
python -m app.collector --dry-run --date 2026-05-11   # 모킹 데이터로 CSV 작성
python -m app.main --dry-run     # 스케줄러 job 목록

# 4) 서버 기동
run.bat
# 또는: python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

브라우저 → `http://localhost:8000`.

**최초 1회 설정 순서**:
1. `admin` 계정(비밀번호=`.env` 의 `ADMIN_INITIAL_PASSWORD`)으로 로그인.
2. `/admin/token` 에서:
   - **Aqara** refresh_token (Aqara Console 발급) 등록.
   - **SmartThings 연결** — "SmartThings 연결" 버튼 → OAuth 승인 → 자동 복귀. (SmartThings 디바이스를 수집하는 경우에만. 아래 [운영자 선행 작업](#운영자-선행-작업-smartthings-oauth) 을 먼저 완료해야 함.)
3. `/devices` 에서 장치 추가 (hub 드롭다운에서 aqara/smartthings 선택). 같은 페이지 하단에서 **디바이스 그룹**도 생성 가능 — 혼합 종류 허용 ([DISPLAY.md §4.8](DISPLAY.md#48-디바이스-그룹-화면-displaygroupgroup_id)).

---

## 운영자 선행 작업 (SmartThings OAuth)

SmartThings 디바이스를 수집하려면 **앱 설치 전에** SmartThings 측 OAuth 클라이언트를 준비해야 한다 (Aqara 만 쓰면 이 절은 건너뛴다). 인증 방식은 OAuth 2.0 Authorization Code flow + refresh token ([DESIGN.md §15.2](DESIGN.md#152-토큰-관리--oauth-20-authorization-code-flow)).

**1) SmartThings CLI 설치**
- history 수집은 `smartthings` CLI 바이너리를 통해 이뤄진다. 서버에 설치하고 PATH 에 두거나 `.env` 의 `SMARTTHINGS_CLI_PATH` 에 절대 경로를 지정.

**2) OAuth-In 클라이언트 생성**
- [SmartThings Developer Workspace](https://developer.smartthings.com/) 또는 CLI(`smartthings apps:create`)에서 **OAuth-In** 타입 앱(클라이언트)을 생성.
- 생성 시 다음을 설정한다:
  - **Redirect URI**: `{앱 접속주소}/admin/smartthings/oauth/callback`
    - 예: `http://localhost:8000/admin/smartthings/oauth/callback`
    - admin 브라우저가 도달 가능한 주소여야 하며, `.env` 값과 **문자 단위로 정확히 일치**해야 한다.
  - **Scopes**: 디바이스·장소 조회 + history 에 필요한 권한. 기본 권장: `r:devices:*`, `r:locations:*`, `x:devices:*`.
- 생성 후 발급되는 **Client ID** 와 **Client Secret** 을 받아둔다 (Secret 은 생성 직후 1회만 표시될 수 있으므로 즉시 보관).

**3) `.env` 설정**
```ini
SMARTTHINGS_CLIENT_ID=<발급받은 client id>
SMARTTHINGS_CLIENT_SECRET=<발급받은 client secret>
SMARTTHINGS_OAUTH_REDIRECT_URI=http://localhost:8000/admin/smartthings/oauth/callback
SMARTTHINGS_OAUTH_SCOPE=r:devices:* r:locations:* x:devices:*
# CLI 가 PATH 에 없으면 절대 경로 지정
# SMARTTHINGS_CLI_PATH=C:\Program Files\SmartThings\smartthings.exe
```
- `SMARTTHINGS_OAUTH_SCOPE` 는 2)에서 클라이언트에 부여한 scope 와 일치해야 한다.
- `SMARTTHINGS_OAUTH_AUTHORIZE_URL` / `SMARTTHINGS_OAUTH_TOKEN_URL` 은 보통 기본값 그대로 둔다.

**4) 서버 재시작 후 연결**
- `.env` 변경은 재시작 시 반영된다. 재시작 후 `/admin/token` → "SmartThings 연결" → SmartThings 로그인·승인 → 자동 복귀하면 access/refresh 토큰이 `tokens_smartthings.json` 에 저장된다.
- 이후 access_token 은 만료 시 refresh_token 으로 자동 갱신되므로 재연결은 불필요하다. 단, refresh 까지 실패하면(`smartthings_token_invalid` 경고 배너) 같은 화면에서 다시 "SmartThings 연결" 한다.

> 주의: 기존에 PAT 방식으로 만든 `tokens_smartthings.json` 은 새 OAuth 형식과 호환되지 않는다. 전환 후 첫 연결 시 자동으로 OAuth 레코드로 덮어쓰여진다.

### 급할 때 임시 PAT 모드 (stopgap)

OAuth 클라이언트 생성·redirect_uri 등록을 천천히 진행하는 동안 **즉시 SmartThings 수집을 가동**해야 한다면, Personal Access Token 으로 우회 운영할 수 있다. 두 가지 입력 방법 — 우선순위는 **환경변수 > 파일(admin 입력) > OAuth** 순.

**PAT 발급** (공통):
[account.smartthings.com/tokens](https://account.smartthings.com/tokens) → 새 PAT 발급 (디바이스·history 권한 체크).

**방법 A — admin 페이지에서 입력 (재시작 불필요, 권장)**:
1. `/admin/token` 접속 → "임시 PAT 입력 (stopgap)" 섹션의 입력란에 PAT 붙여넣기 → 저장.
2. 서버가 `/v1/devices` 호출로 즉시 검증 → 성공하면 `tokens_smartthings_pat.json` 에 저장하고 즉시 활성.
3. OAuth 로 복귀할 때는 같은 페이지의 "PAT 삭제" 버튼.

**방법 B — `.env` 환경변수 (운영자 잠금)**:
1. `.env` 에 `SMARTTHINGS_PAT=<발급받은 PAT>` 추가.
2. 서버 재시작.
3. 환경변수가 설정된 동안은 admin 페이지의 PAT 입력보다 우선됨.

**주의·한계**:
- refresh 흐름 없음 — PAT 가 만료/거부되면 새 PAT 로 교체해야 한다.
- `client_id/secret`, `redirect_uri` 가 비어 있어도 동작하므로 OAuth 클라이언트 생성을 미룰 수 있다.
- OAuth 가 준비되면 PAT 를 제거(`PAT 삭제` 버튼 또는 `.env` 라인 비우기) → 정식 OAuth 경로(자동 refresh)로 자연스럽게 복귀.

---

## 일일 흐름

| 시각 (KST) | 작업 id | 동작 |
|---|---|---|
| 09:00 | `daily_collect` | 활성 장치 × bundle 전일자 1일 수집 |
| 03:00 | `token_refresh` | Aqara refresh token 만료 24h 이내면 선제 갱신 |
| 03:30 | `prune_old_jobs` | `collection_jobs` 4주(`JOB_HISTORY_RETENTION_DAYS`) 경과 행 자동 정리. **CSV 파일은 보존** |
| 매시간 | `healthcheck_backfill` | 최근 7일 내 `failed`/누락 (device, bundle, date) 재시도 |

기본 시각은 `app/config.py` 의 `COLLECT_CRON_HOUR/MINUTE`, `TOKEN_PROACTIVE_REFRESH_HOUR`, `HEALTHCHECK_INTERVAL_MINUTES`, `JOB_HISTORY_RETENTION_DAYS` 로 조정 가능.

Aqara API 오류 시: refresh token 으로 자동 재발급 + 1회 재시도. 재시도까지 실패하면 상단 경고 배너에 표시 ([DESIGN.md §7.6](DESIGN.md#76-시스템-경고-알림-배너)).

SmartThings access_token 은 만료(임박) 시 refresh_token 으로 자동 갱신된다 (호출 직전 + 03:00 cron 선제). refresh 까지 실패할 때만 `smartthings_token_invalid` 알림이 뜨며 admin 이 재연결해야 한다. 401/403 외 오류(네트워크 timeout, 권한 부족, 비-2xx)는 일반 수집 실패로 처리되어 token alert 가 뜨지 않는다 ([DESIGN.md §15.6](DESIGN.md#156-수집-워크플로우-분기-appcollectorpy-확장)).

---

## 파일 / 디렉토리

```
data/{bundle_key}/{device_id}/{YYYYMMDD}_{suffix}.csv
```

`suffix` 는 hub 별로 다름 (DESIGN.md §15.7 / [app/devices.py:device_id_suffix](app/devices.py)):
- **Aqara**: 끝 6자 대문자 hex (예: `lumi.4cf8cdf3c752edb` → `752EDB`)
- **SmartThings**: 대시 제거 후 첫 8자 대문자 (UUID/24-hex 공통, 예: `2d21b6a3-...` → `2D21B6A3`)

예:
```
data/motion_lux/lumi.4cf8cdf3c752edb/20260511_752EDB.csv
data/motion_lux/2d21b6a3-d646-47a5-a704-3a2cc2c90697/20260513_2D21B6A3.csv
```

CSV 최상단에 `#` 주석으로 device·bundle·hub·생성시각 등 메타 헤더. pandas 는 `pd.read_csv(path, comment="#")` 로 자동 무시.

---

## 주요 화면

| 경로 | 권한 | 설명 |
|---|---|---|
| `/` | Public | 대시보드 (활성 장치 수, 어제 성공/실패, 누적 용량) |
| `/devices` | Public 조회 / 로그인 변경 | 활성 장치 + 변경/삭제 이력 + 그룹 관리. **설치 장소 드롭다운 필터**, **편집(모든 속성, 적용 버튼)**, **일괄 ON/OFF** |
| `/data` | Public | device × bundle 집계. **설치 장소 드롭다운 필터** |
| `/data/{device_id}/{bundle_key}` | 로그인 | 일자별 CSV 파일 목록 + 단일/기간 다운로드 (zip / concat) |
| `/display/{device_id}` | 로그인 | 활동 타임라인 1주일치 SVG. Bar / Point / Plot 3가지 표현 ([DISPLAY.md](DISPLAY.md)) |
| `/display/group/{group_id}` | 로그인 | 그룹 멤버를 device_type 별 패널로 합산 표시 |
| `/jobs` | Public | 작업 이력 (보관 4주 — 03:30 cron 자동 정리) |
| `/admin/token` | admin | Aqara refresh token 등록 + SmartThings OAuth 연결·상태 + PAT 입력(stopgap) |
| `/admin/users` | admin | 사용자 추가/제거 |

**시각화 어휘** ([DISPLAY.md §4](DISPLAY.md#4-디바이스-타입별-시각화-규칙)):
- **Bar** — 막대 (지속 구간). 예: door open, motion active, vibration move
- **Point** — 점 tick (순간 이벤트). 예: knock, switch click, vibration aq1 코드
- **Plot** — 연속 측정값 line plot. 예: 온습도 (이중축 — 좌=온도 실선, 우=습도 점선)

용량 표시는 모두 `human_bytes` Jinja 필터로 1024 base · KB/MB/GB 환산.

---

## 디바이스 편집 / 이력 정책

- **편집 가능 필드** (등록자·등록일 제외 모두): device_id_input, device_type, hub, enabled, alias, install_location, install_date, group_id. 편집 행에서 **적용** 버튼을 눌러야 확정. 식별 필드 변경은 partial unique index 충돌 검사.
- **수정일 표시**: 활성 표의 등록일 셀에 `✏️ <updated_at> (수정자)` 보조 표기.
- **이력 스냅샷 정책**: 적용 *후* 값을 `device_history` 에 저장 — 사용자가 "이 시점에 디바이스가 이렇게 됐다" 를 직관적으로 확인 ([DESIGN.md §7.5](DESIGN.md#75-장치-추가삭제-동작-의사코드)).
- **이력 보관**: 자동 정리 없음(무제한). admin 은 변경/삭제 이력 표의 🗑 버튼으로 무의미한 행을 수동 정리 가능 (`DELETE /api/devices/history/{id}`).

---

## 권한 매트릭스

| 동작 | 비로그인 | 로그인 | admin |
|---|:-:|:-:|:-:|
| 장치 목록·데이터 현황·CSV 메타 조회 | ✅ | ✅ | ✅ |
| 장치 추가/삭제/편집/토글, 일괄 ON·OFF, 그룹 변경 | ❌ | ✅ | ✅ |
| 디스플레이(`/display/*`) 조회 | ❌ | ✅ | ✅ |
| CSV 다운로드 | ❌ | ✅ | ✅ |
| 수동 단건/일괄 수집 (`/api/jobs/run`, `/api/jobs/bulk_run`) | ❌ | ❌ | ✅ |
| 변경 이력 단일 삭제 (`DELETE /api/devices/history/{id}`) | ❌ | ❌ | ✅ |
| 사용자·토큰(Aqara/SmartThings OAuth·PAT) 관리 | ❌ | ❌ | ✅ |

---

## 운영 (NSSM Windows 서비스)

상시 가동은 [NSSM](https://nssm.cc/) 으로 등록 권장 ([DESIGN.md §11](DESIGN.md#11-배포--실행)):

```powershell
nssm install AqaraCollector "C:\Python311\python.exe" "-m uvicorn app.main:app --host 0.0.0.0 --port 8000"
nssm set AqaraCollector AppDirectory "C:\path\to\aqara_api_proj"
nssm start AqaraCollector
```

스케줄러가 앱 내장이므로 별도 Windows 작업 스케줄러 등록은 불필요.

SmartThings 디바이스 수집이 있는 경우 `smartthings` CLI 바이너리가 서비스 계정의 PATH 에 있거나, `.env` 의 `SMARTTHINGS_CLI_PATH` 에 절대 경로로 지정돼야 한다 ([DESIGN.md §15.10](DESIGN.md#1510-운영-의존성)).

---

## 기존 파일

`record_data.py`, `refresh_access_token.py`, `*.ipynb`, 기존 CSV 는 새 시스템과 별개로 **보존**된다 ([DESIGN.md §13](DESIGN.md#13-기존-파일-처리)). 새 시스템은 `app/` 하위에서만 동작한다.
