# CLAUDE.md

이 저장소에서 Claude Code가 작업할 때 따라야 할 규칙. 모든 구현·수정 작업의 출발점은 [DESIGN.md](DESIGN.md), [DEVICE.md](DEVICE.md), [DISPLAY.md](DISPLAY.md)다.

---

## 1. 권위 있는 문서

| 문서 | 역할 |
|---|---|
| [DESIGN.md](DESIGN.md) | 시스템 아키텍처·DB 스키마·라우트·워크플로우의 **단일 진실 공급원(SSOT)**. 구현은 이 문서를 따른다. |
| [DEVICE.md](DEVICE.md) | 수집 대상 기기별 동작 특성·값 의미·CSV 포맷 규칙. `DEVICE_TYPES` 매핑과 후처리 주의사항의 SSOT. |
| [DISPLAY.md](DISPLAY.md) | 디바이스 활동 타임라인 디스플레이 화면(`/display/{device_id}`)의 SSOT. 디바이스 타입별 시각화 규칙·레이아웃·색상·엣지 케이스. |

코드와 문서가 충돌하면 **문서를 먼저 갱신**한 뒤 코드를 맞춘다. 문서 갱신 없이 동작 변경 금지.

---

## 2. 코딩 규칙

### 2.1 주석 (한국어, 기능 단위)
- **주요 기능 단위마다 한국어 주석** 필수. 단위 예시: 모듈 docstring, 클래스, 외부 호출 래퍼, 워크플로우 함수(`collect_yesterday`, `run_one_bundle`, `call_with_auto_refresh`, `add_device`, `delete_device`, `raise_alert` 등), 비자명한 비즈니스 규칙 블록.
- 한 줄짜리 사소한 식·자명한 식별자에는 주석 달지 않는다 (코드가 자명한 경우 노이즈).
- 주석은 **WHY**(왜 이렇게 했는지·DESIGN.md 어느 절을 따랐는지·엣지 케이스 이유)에 집중. WHAT은 함수명/변수명으로 표현.
- 예시:
  ```python
  def call_with_auto_refresh(intent: str, data: dict) -> dict:
      """
      Aqara API 호출 + 자동 토큰 갱신 래퍼 (DESIGN.md §6.2).

      1차 실패 시 access token 만료로 간주하고 refresh token으로
      새 토큰 쌍을 발급받아 저장한 뒤 1회 재시도한다.
      refresh 실패 또는 재시도도 실패하면 system_alerts에 경고를 등록한다.
      """
      ...
  ```

### 2.2 모듈 구조
- DESIGN.md §4의 디렉토리 구조를 그대로 따른다. 새 모듈을 임의로 추가하지 말 것.
- 각 모듈 최상단에 한 줄 docstring으로 역할 명시 (예: `"""Aqara Open API 클라이언트 (DESIGN.md §8)."""`).

### 2.3 외부 의존
- 패키지 추가 시 `requirements.txt`에 명시. DESIGN.md §11 목록을 우선으로 사용.
- HTTP 요청은 `requests` 동기 호출 사용 (기존 `record_data.py`와 일관).

### 2.4 시간대
- 사용자 표시·CSV·DB의 모든 시각은 **KST (UTC+9)** 문자열.
- Aqara API 요청 본문 timestamp만 **UTC milliseconds**. 변환은 `record_data.py`의 `kst_to_utc_millis`를 모듈로 옮겨 단일 함수로만 사용.

### 2.5 비밀값
- `APPID`/`KEYID`/`APPKEY`는 환경변수 우선, 없으면 `.env`. 코드에 하드코딩 금지(기존 `record_data.py` 값은 마이그레이션 후 제거).
- `tokens.json`, `app.db`는 `.gitignore`. 절대 커밋하지 않는다.

---

## 3. Dry-run 점검 프로토콜

코드를 변경한 뒤에는 **실제 Aqara API 호출 없이** 동작을 점검하는 dry-run을 우선 수행한다. 외부 호출은 모킹하거나 `--dry-run` 플래그로 건너뛴다.

### 3.1 모듈별 dry-run 항목
| 모듈 | dry-run 점검 |
|---|---|
| `app/devices.py` | `python -m app.devices` 실행 시 `DEVICE_TYPES` 전체를 print → 8종(motion_t1/door_t1/motion_p1/vibration_t1/switch_t1/vibration_aq1/motion_and_light_p2/temp_humi_t1) 키, 각 bundle의 `csv_columns` 일치 확인 ([DEVICE.md 요약표](DEVICE.md#0-요약표)와 1:1) |
| `app/db.py` | 빈 임시 경로로 `init_db()` → 모든 테이블·인덱스가 `sqlite_master`에 생성되는지 점검. partial unique index 2개(`idx_devices_active_id`, `idx_alerts_active_code`) 존재 확인 |
| `app/aqara_client.py` | 호출 함수에 `dry_run=True`/모킹된 HTTP layer로 서명 생성·payload 구조만 검증. 실제 네트워크 호출 없이 stub 응답으로 `call_with_auto_refresh`의 1차 실패 → refresh → 재시도 경로가 트리거되는지 단위 테스트 |
| `app/token_manager.py` | 임시 경로의 `tokens.json` 읽기/쓰기/덮어쓰기. 파일 부재 시 `load()`가 명확한 예외를 던지는지 확인 |
| `app/collector.py` | 모킹된 `aqara_client`로 빈 응답(0건) / <100건(1페이지) / 정확히 100건(2페이지) / 200건 이상(다중 페이지) 4 케이스를 통과시켜 CSV 행 수와 `#` 메타 헤더(`row_count`)가 일치하는지 |
| `app/collector.py` (wide join) | motion_lux bundle에 motion_status 13건 + lux 14건(1건은 motion 누락) 입력 시 결과 14행 + 빈 셀 1개 확인 ([DEVICE.md §1.4](DEVICE.md#14-csv-저장-형식-통합-wide-포맷) 예시 재현) |
| `app/scheduler.py` | 등록된 cron job 목록 print → 매일 09:00 일일 수집 + 03:00 토큰 선제 갱신 + 매시간 헬스체크 3개 등록 확인 |
| `app/auth.py` | bcrypt 해시 round-trip(`verify(hash(pw), pw) == True`), 세션 쿠키 시그니처 검증 |
| `app/routes/*` | FastAPI TestClient로 GET 라우트가 비로그인 200 / 변경 라우트가 비로그인 401 응답하는지 권한 매트릭스([DESIGN.md §6.3](DESIGN.md#63-사용자-인증))대로 검증 |

### 3.2 통합 dry-run
- `python -m app.collector --dry-run --date YYYY-MM-DD`: 모킹 데이터로 1일 수집 전체 흐름을 돌려 CSV 생성·DB 기록까지 확인 (Aqara 미호출).
- `python -m app.main --dry-run`: 앱 부팅 후 스케줄러 job 목록 print 후 즉시 종료.

### 3.3 실제 호출 점검 (수동)
- dry-run 통과 후, admin이 `/admin/token`에서 refresh_token 입력 → `POST /api/jobs/run`으로 1일 수동 트리거 → DB·CSV 결과 검증.
- 자동 cron은 마지막에 활성화한다.

---

## 4. 변경 작업 체크리스트
새 기능·수정 PR을 만들 때 항상 다음을 확인한다.

- [ ] DESIGN.md / DEVICE.md와 일치하는가? 불일치면 문서 먼저 갱신.
- [ ] 주요 기능 단위에 한국어 docstring/주석이 있는가?
- [ ] 시간대 변환은 단일 함수만 사용했는가?
- [ ] 비밀값을 코드/로그에 노출하지 않는가? (access_token, refresh_token, password 마스킹)
- [ ] dry-run 모듈 점검을 통과했는가?
- [ ] `requirements.txt`에 새 의존성이 반영되었는가?
- [ ] `.gitignore`에 새 비밀/생성 파일이 추가되었는가?

---

## 5. 환경

- OS: Windows 11. 셸은 PowerShell (이 저장소 도구는 PowerShell 문법으로 명령 실행).
- Python: 3.10+ 권장 (FastAPI · APScheduler · passlib 호환).
- 실행: `run.bat` 또는 `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- 기존 파일 (`record_data.py`, `refresh_access_token.py`, `*.ipynb`, 기존 CSV)은 **삭제하지 않는다** ([DESIGN.md §13](DESIGN.md#13-기존-파일-처리)).

---

## 6. 금지사항

- **금지**: DESIGN.md/DEVICE.md를 우회한 임시 하드코딩, hex device_id를 코드 내부 상수로 박는 행위, 비밀값 git 커밋, `--no-verify`로 hook 우회, `git push --force` (사용자 명시 지시 없이).
- **금지**: dry-run 점검 없이 자동 cron 활성화 (실제 Aqara API 호출 횟수 낭비 + 토큰 무효화 위험).
- **금지**: `MEMORY.md` 또는 운영 메모를 위해 새로운 마크다운 파일 생성 (사용자가 요청한 경우 제외).
