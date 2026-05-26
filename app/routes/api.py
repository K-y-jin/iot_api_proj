"""JSON API 라우트 (DESIGN.md §7.2)."""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import alerts, collector, config, scheduler, token_manager
from ..auth import CurrentUser, current_user, require_admin, require_login
from ..db import get_connection, now_kst_iso
from ..devices import DEVICE_TYPES, device_id_upper, last6, normalize_device_id


router = APIRouter(prefix="/api")


# ─────────────────────────── Pydantic 모델 ───────────────────────────

class DeviceCreate(BaseModel):
    device_type: str = Field(..., description="DEVICE_TYPES 키")
    device_id_input: str = Field(..., description="사용자 입력 device ID. Aqara: hex (lumi. 유무 무관). SmartThings: UUID 또는 24-hex")
    hub: Optional[str] = Field(None, description="'aqara' | 'smartthings' — 미지정 시 device_type 키 prefix로 자동 추론 (st_* → smartthings)")
    install_location: Optional[str] = None
    install_date: Optional[str] = Field(None, description="YYYY-MM-DD")
    alias: Optional[str] = None


class DevicePatch(BaseModel):
    """디바이스 편집 페이로드 (DESIGN.md §7.2, §7.5).

    등록자(created_by/name)·등록일(created_at)을 제외한 모든 속성을 편집 가능.
    "필드 미제공" vs "null 명시"의 구분은 pydantic v2 model_fields_set 으로 판정한다
    (예: group_id=null 은 그룹 해제 의도이므로 None 미제공과 달라야 함).
    """
    enabled: Optional[bool] = None
    alias: Optional[str] = None
    install_location: Optional[str] = None
    install_date: Optional[str] = None
    group_id: Optional[int] = None
    # 식별/접근 속성 — 변경 시 device_id 정규화 + hub 지원 검증 + active unique 충돌 검사.
    # 기존 수집 CSV·collection_jobs 는 이전 device_id 키로 보존되며 자동 이전되지 않는다.
    device_id_input: Optional[str] = None
    device_type: Optional[str] = None
    hub: Optional[str] = None


class GroupCreate(BaseModel):
    """그룹 생성 페이로드. device_type은 정보용(라벨)이며 멤버 추가 시 강제 제약 없음."""
    name: str = Field(..., min_length=1)
    device_type: Optional[str] = None
    description: Optional[str] = None


class JobRunRequest(BaseModel):
    device_id: str
    bundle_key: str
    target_date: str


class BulkJobRunRequest(BaseModel):
    """일괄 수동 수집 요청 (DESIGN.md §7.4).

    from_/to_ 는 YYYY-MM-DD KST. 클라이언트에서는 `from`, `to` 키를 보내고
    pydantic alias 로 매핑한다 (예약어 회피).
    """
    from_: str = Field(..., alias="from")
    to: str

    model_config = {"populate_by_name": True}


class TokenSeed(BaseModel):
    refresh_token: str


class SmartThingsPatSeed(BaseModel):
    """admin 페이지에서 직접 입력하는 SmartThings PAT (stopgap, OAuth 우회용)."""
    pat: str


# ─────────────────────────── 헬퍼 ───────────────────────────

def _validate_yyyymmdd(s: str) -> str:
    """'YYYY-MM-DD' 파싱 검증. 잘못된 형식은 400."""
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"날짜 형식이 YYYY-MM-DD가 아닙니다: {s}") from e
    return s


# ─────────────────────────── 장치 CRUD ───────────────────────────

@router.get("/devices")
def list_devices(include_deleted: bool = Query(False)) -> dict:
    """활성 장치 목록. include_deleted=True 시 삭제 이력도 포함 (DESIGN.md §7.2)."""
    conn = get_connection()
    try:
        if include_deleted:
            rows = conn.execute(
                "SELECT * FROM devices ORDER BY deleted_at IS NULL DESC, id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM devices WHERE deleted_at IS NULL ORDER BY id"
            ).fetchall()
        return {"devices": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.post("/devices", status_code=201)
def create_device(payload: DeviceCreate, user: CurrentUser = Depends(require_login)) -> dict:
    """장치 등록 (DESIGN.md §7.5, §15.7).

    hub 결정: 명시 입력 우선, 미지정 시 device_type 키 prefix(st_*)로 자동 추론.
    """
    if payload.device_type not in DEVICE_TYPES:
        raise HTTPException(status_code=400, detail=f"알 수 없는 device_type: {payload.device_type}")
    if payload.install_date:
        _validate_yyyymmdd(payload.install_date)

    # hub: 명시 입력 우선. 미지정 시 device_type 이 1개 hub 만 지원하면 그것으로 자동 결정.
    # 다중 hub 지원 type 인데 미지정이면 400.
    from ..devices import supported_hubs
    svs = supported_hubs(payload.device_type)
    if not svs:
        raise HTTPException(status_code=500, detail=f"device_type {payload.device_type} 에 정의된 hub 가 없습니다.")
    hub = (payload.hub or "").strip().lower()
    if not hub:
        if len(svs) == 1:
            hub = svs[0]
        else:
            raise HTTPException(
                status_code=400,
                detail=f"device_type {payload.device_type} 은(는) 여러 hub({','.join(svs)})를 지원합니다. hub 를 명시하세요.",
            )
    if hub not in svs:
        raise HTTPException(
            status_code=400,
            detail=f"device_type {payload.device_type} 은(는) hub {hub} 를 지원하지 않습니다. 가능: {','.join(svs)}",
        )

    norm = normalize_device_id(payload.device_id_input, hub=hub)
    upper = norm.upper().replace("LUMI.", "") if hub == "aqara" else norm.upper()

    conn = get_connection()
    try:
        try:
            cur = conn.execute(
                """INSERT INTO devices(device_id, device_id_upper, device_type, hub,
                                       install_location, install_date, alias,
                                       enabled, created_by, created_by_name, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (norm, upper, payload.device_type, hub, payload.install_location,
                 payload.install_date, payload.alias,
                 user["id"], user["username"], now_kst_iso()),
            )
        except Exception as e:
            # partial unique index 위반 → 이미 활성 등록 (DESIGN.md §3.2)
            if "idx_devices_active_id" in str(e) or "UNIQUE" in str(e).upper():
                raise HTTPException(status_code=409, detail="이미 등록된 장치입니다.") from e
            raise
        return {"id": int(cur.lastrowid), "device_id": norm, "hub": hub}
    finally:
        conn.close()


@router.patch("/devices/{device_pk}")
def patch_device(device_pk: int, payload: DevicePatch, user: CurrentUser = Depends(require_login)) -> dict:
    """디바이스 편집 적용 (DESIGN.md §7.5).

    동작:
      1) 대상 활성 행을 조회 (없으면 404).
      2) 새 값을 같은 트랜잭션에서 UPDATE 하면서 updated_at/updated_by(_name) 갱신.
      3) UPDATE 직후의 *변경 후 값* 을 device_history(change_type='update') 에 스냅샷.

    편집 가능 필드 (DESIGN.md §7.2):
      enabled / alias / install_location / install_date / group_id /
      device_id_input / device_type / hub.
      등록자(created_by/name) · 등록일(created_at) 은 변경하지 않는다.

    group_id 는 "필드 미제공" vs "null=해제" 구분이 필요해 model_fields_set 으로 판정.

    이력 스냅샷 정책: 사용자가 변경 적용 시점에 *어떤 값이 됐는지* 를 직관적으로 확인할 수
    있도록 적용 후 값을 저장한다 (기존 적용 직전 값 정책에서 변경 — DESIGN.md §7.5).
    """
    sent = payload.model_fields_set
    new_type = payload.device_type
    new_hub = (payload.hub or "").strip().lower() or None
    new_id_input = payload.device_id_input

    if new_type is not None and new_type not in DEVICE_TYPES:
        raise HTTPException(status_code=400, detail=f"알 수 없는 device_type: {new_type}")

    # 텍스트 필드 비교 시 빈 문자열과 NULL 을 동치로 취급 (DB 에 "" 와 None 이 혼재해도 false-positive 방지).
    def _txt_eq(a, b) -> bool:
        return (a or None) == (b or None)

    conn = get_connection()
    try:
        cur_row = conn.execute(
            "SELECT * FROM devices WHERE id=? AND deleted_at IS NULL", (device_pk,)
        ).fetchone()
        if cur_row is None:
            raise HTTPException(status_code=404, detail="대상 장치를 찾을 수 없습니다.")

        # 각 필드를 *현재 행* 과 비교해 실제로 다른 경우에만 fields 에 추가.
        # 폼이 미변경 필드도 함께 전송하지만 서버에서 거른다 → changed_fields 에는 진짜 변경만.
        fields: list[str] = []
        values: list = []

        if payload.enabled is not None:
            new_v = 1 if payload.enabled else 0
            if new_v != cur_row["enabled"]:
                fields.append("enabled=?"); values.append(new_v)
        if payload.alias is not None and not _txt_eq(payload.alias, cur_row["alias"]):
            fields.append("alias=?"); values.append(payload.alias)
        if payload.install_location is not None and not _txt_eq(payload.install_location, cur_row["install_location"]):
            fields.append("install_location=?"); values.append(payload.install_location)
        if payload.install_date is not None:
            _validate_yyyymmdd(payload.install_date)
            if not _txt_eq(payload.install_date, cur_row["install_date"]):
                fields.append("install_date=?"); values.append(payload.install_date)
        if "group_id" in sent and payload.group_id != cur_row["group_id"]:
            # null=해제, int=해당 그룹으로 이동 (혼합 device_type 허용 — DISPLAY.md §4.8).
            if payload.group_id is not None:
                exists = conn.execute(
                    "SELECT 1 FROM device_groups WHERE id=?", (payload.group_id,)
                ).fetchone()
                if not exists:
                    raise HTTPException(status_code=400, detail=f"존재하지 않는 그룹: {payload.group_id}")
            fields.append("group_id=?"); values.append(payload.group_id)

        # 식별 필드 변경 (device_type / hub / device_id_input) — 셋이 서로 의존적이라 묶어 처리.
        # device_type 또는 hub 만 단독 변경되더라도 normalize_device_id 의 hub 분기가 달라질 수
        # 있으므로 현재 행 값과 함께 최종 값을 결정한 뒤 *cur_row 와 다를 때만* 변경으로 친다.
        eff_type = new_type if new_type is not None else cur_row["device_type"]
        eff_hub = new_hub if new_hub is not None else (cur_row["hub"] or "aqara")
        from ..devices import supported_hubs
        svs = supported_hubs(eff_type)
        if eff_hub not in svs:
            raise HTTPException(
                status_code=400,
                detail=f"device_type {eff_type} 은(는) hub {eff_hub} 를 지원하지 않습니다. 가능: {','.join(svs)}",
            )
        if eff_type != cur_row["device_type"]:
            fields.append("device_type=?"); values.append(eff_type)
        if eff_hub != (cur_row["hub"] or "aqara"):
            fields.append("hub=?"); values.append(eff_hub)
        # device_id 정규화 — id_input 또는 hub 가 변경됐을 *가능성* 이 있으면 재계산.
        # 결과가 cur_row 와 같으면 변경 없음으로 간주.
        if new_id_input is not None or new_hub is not None:
            raw = new_id_input if new_id_input is not None else cur_row["device_id"]
            norm = normalize_device_id(raw, hub=eff_hub)
            upper = norm.upper().replace("LUMI.", "") if eff_hub == "aqara" else norm.upper()
            if norm != cur_row["device_id"]:
                # 다른 활성 행과 동일 device_id 면 partial unique index 위반 — 미리 차단해 의미 있는 에러.
                dup = conn.execute(
                    "SELECT id FROM devices WHERE device_id=? AND deleted_at IS NULL AND id<>?",
                    (norm, device_pk),
                ).fetchone()
                if dup:
                    raise HTTPException(status_code=409, detail="동일 device_id 의 활성 장치가 이미 존재합니다.")
                fields.append("device_id=?"); values.append(norm)
                fields.append("device_id_upper=?"); values.append(upper)

        if not fields:
            raise HTTPException(status_code=400, detail="변경할 필드가 없습니다.")

        now = now_kst_iso()
        # 변경된 필드 이름 목록 — DB 의 changed_fields 컬럼은 유지되지만 UI 에는 노출하지 않는다.
        # ('device_id_upper' 는 device_id 변경의 부산물이라 제외.)
        changed_field_names = [f.split("=", 1)[0] for f in fields if not f.startswith("device_id_upper=")]
        changed_fields_csv = ",".join(changed_field_names)

        # 1) 먼저 새 값으로 UPDATE — partial unique 등 제약 검사도 여기서 발생.
        fields.append("updated_at=?"); values.append(now)
        fields.append("updated_by=?"); values.append(user["id"])
        fields.append("updated_by_name=?"); values.append(user["username"])
        values.append(device_pk)
        try:
            cur = conn.execute(
                f"UPDATE devices SET {', '.join(fields)} WHERE id=? AND deleted_at IS NULL",
                values,
            )
        except Exception as e:
            if "idx_devices_active_id" in str(e) or "UNIQUE" in str(e).upper():
                raise HTTPException(status_code=409, detail="동일 device_id 의 활성 장치가 이미 존재합니다.") from e
            raise
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="대상 장치를 찾을 수 없습니다.")

        # 2) UPDATE 직후의 *변경 후* 행을 다시 읽어 device_history 에 스냅샷.
        # 단, ON/OFF 토글만 변경된 경우(`changed_fields` 가 정확히 'enabled')는 이력 누적 가치가
        # 낮다고 판단해 INSERT 를 건너뛴다 (사용자 정책 — DESIGN.md §7.5).
        # `enabled` 와 다른 필드가 함께 변경된 경우는 정상적으로 기록한다.
        history_field_set = {n for n in changed_field_names if n not in ("updated_at", "updated_by", "updated_by_name")}
        skip_history = history_field_set == {"enabled"}
        if not skip_history:
            new_row = conn.execute(
                "SELECT * FROM devices WHERE id=?", (device_pk,)
            ).fetchone()
            conn.execute(
                """INSERT INTO device_history(
                        device_pk, change_type, changed_by, changed_by_name, changed_at, changed_fields,
                        device_id, device_id_upper, device_type, hub,
                        install_location, install_date, alias, enabled, group_id)
                   VALUES (?, 'update', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    device_pk, user["id"], user["username"], now, changed_fields_csv,
                    new_row["device_id"], new_row["device_id_upper"],
                    new_row["device_type"], new_row["hub"] or "aqara",
                    new_row["install_location"], new_row["install_date"],
                    new_row["alias"], new_row["enabled"], new_row["group_id"],
                ),
            )
        return {"ok": True, "updated_at": now}
    finally:
        conn.close()


class DeviceBulkEnable(BaseModel):
    """활성 장치 전체의 enabled 일괄 변경 페이로드 (DESIGN.md §7.2)."""
    enabled: bool


@router.post("/devices/bulk_enable")
def bulk_enable_devices(
    payload: DeviceBulkEnable, user: CurrentUser = Depends(require_login)
) -> dict:
    """모든 활성 장치의 `enabled` 를 일괄로 ON 또는 OFF (DESIGN.md §7.4).

    동작:
      - 대상: `deleted_at IS NULL` 인 모든 행. 이미 원하는 상태(enabled == payload.enabled)인 행은 건너뜀.
      - 변경 행마다 updated_at/updated_by(_name) 갱신.
      - **device_history 에는 기록하지 않는다** — ON/OFF 토글은 누적 가치가 낮아 이력에서 제외하는
        정책 (PATCH 의 enabled-단독 변경과 동일 — DESIGN.md §7.5).

    반환: {ok, changed: 변경된 디바이스 수, total: 활성 디바이스 수}.
    """
    now = now_kst_iso()
    new_enabled = 1 if payload.enabled else 0
    conn = get_connection()
    try:
        # 변경 대상 카운트 산출 (응답에 포함). 같은 상태인 행은 UPDATE 영향 0 이라 일치.
        changed = conn.execute(
            "SELECT COUNT(*) AS c FROM devices WHERE deleted_at IS NULL AND enabled <> ?",
            (new_enabled,),
        ).fetchone()["c"]
        total_active = conn.execute(
            "SELECT COUNT(*) AS c FROM devices WHERE deleted_at IS NULL"
        ).fetchone()["c"]

        # 일괄 UPDATE — partial unique 등 제약 영향 없음 (enabled 만 변경). 이력 INSERT 없음.
        conn.execute(
            """UPDATE devices
                  SET enabled=?, updated_at=?, updated_by=?, updated_by_name=?
                WHERE deleted_at IS NULL AND enabled <> ?""",
            (new_enabled, now, user["id"], user["username"], new_enabled),
        )
        return {"ok": True, "changed": int(changed), "total": int(total_active)}
    finally:
        conn.close()


@router.delete("/devices/{device_pk}")
def delete_device(device_pk: int, user: CurrentUser = Depends(require_login)) -> dict:
    """Soft delete: deleted_at / deleted_by / deleted_by_name 자동 기록 (DESIGN.md §7.5)."""
    conn = get_connection()
    try:
        cur = conn.execute(
            """UPDATE devices
                  SET deleted_at=?, deleted_by=?, deleted_by_name=?
                WHERE id=? AND deleted_at IS NULL""",
            (now_kst_iso(), user["id"], user["username"], device_pk),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="대상 장치를 찾을 수 없습니다.")
        return {"ok": True}
    finally:
        conn.close()


@router.get("/devices/history")
def list_device_history() -> dict:
    """디바이스 변경/삭제 이력 목록 (DESIGN.md §7.2, §7.4).

    두 출처를 시간 역순으로 합쳐 반환:
      - device_history.change_type='update'  : 수정 적용 직전 스냅샷
      - devices.deleted_at IS NOT NULL       : 삭제된 행 (스냅샷 = 삭제 직전 = 마지막 값)
    각 항목은 동일한 키 셋(change_type, changed_at, changed_by_name, 스냅샷 필드)을 노출해
    템플릿이 분기 없이 렌더할 수 있도록 한다.
    """
    conn = get_connection()
    try:
        updates = conn.execute(
            """SELECT h.id             AS history_id,
                      'update'          AS change_type,
                      h.device_pk       AS device_pk,
                      h.changed_at      AS changed_at,
                      h.changed_by_name AS changed_by_name,
                      h.changed_fields  AS changed_fields,
                      h.device_id, h.device_id_upper, h.device_type, h.hub,
                      h.install_location, h.install_date, h.alias, h.enabled, h.group_id,
                      d.created_by_name, d.created_at
                 FROM device_history h
            LEFT JOIN devices d ON d.id = h.device_pk
                WHERE h.change_type = 'update'"""
        ).fetchall()
        deletes = conn.execute(
            """SELECT NULL              AS history_id,
                      'delete'          AS change_type,
                      d.id              AS device_pk,
                      d.deleted_at      AS changed_at,
                      d.deleted_by_name AS changed_by_name,
                      NULL              AS changed_fields,
                      d.device_id, d.device_id_upper, d.device_type, d.hub,
                      d.install_location, d.install_date, d.alias, d.enabled, d.group_id,
                      d.created_by_name, d.created_at
                 FROM devices d
                WHERE d.deleted_at IS NOT NULL"""
        ).fetchall()
        items = [dict(r) for r in (*updates, *deletes)]
        # changed_at 가 None 인 경우는 없지만 안전하게 빈 문자열로 폴백.
        items.sort(key=lambda x: x.get("changed_at") or "", reverse=True)
        return {"history": items}
    finally:
        conn.close()


@router.delete("/devices/history/{history_id}")
def delete_device_history(
    history_id: int, user: CurrentUser = Depends(require_admin)
) -> dict:
    """단일 device_history 행 삭제 (admin 전용).

    무의미한 토글 이력 등을 정리할 때 사용. 'delete' 이력(soft-delete 행)은 devices 테이블에
    묶여 있으므로 이 라우트로는 삭제하지 않는다 — 그 경우 devices.purge 별도 흐름이 필요.
    """
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM device_history WHERE id=?", (history_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="대상 이력을 찾을 수 없습니다.")
        return {"ok": True}
    finally:
        conn.close()


# ─────────────────────────── 디바이스 그룹 ───────────────────────────

@router.get("/groups")
def list_groups() -> dict:
    """그룹 목록 + 각 그룹의 활성 멤버 수 (DISPLAY.md §4.8). 공개 조회."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT g.id, g.name, g.device_type, g.description,
                      g.created_by_name, g.created_at,
                      (SELECT COUNT(*) FROM devices d
                        WHERE d.group_id=g.id AND d.deleted_at IS NULL) AS member_count
                 FROM device_groups g
                ORDER BY g.id"""
        ).fetchall()
        return {"groups": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.post("/groups", status_code=201)
def create_group(payload: GroupCreate, user: CurrentUser = Depends(require_login)) -> dict:
    """그룹 생성. device_type은 정보용 라벨이며 멤버 강제 일치 제약 없음."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="그룹명이 비어있습니다.")
    # device_type은 선택. 입력했다면 DEVICE_TYPES 키여야 (오타 방지) 정보 일관성 유지.
    if payload.device_type and payload.device_type not in DEVICE_TYPES:
        raise HTTPException(status_code=400, detail=f"알 수 없는 device_type: {payload.device_type}")
    conn = get_connection()
    try:
        try:
            cur = conn.execute(
                """INSERT INTO device_groups(name, device_type, description,
                                              created_by, created_by_name, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (name, payload.device_type, payload.description,
                 user["id"], user["username"], now_kst_iso()),
            )
        except Exception as e:
            if "UNIQUE" in str(e).upper():
                raise HTTPException(status_code=409, detail=f"이미 존재하는 그룹명입니다: {name}") from e
            raise
        return {"id": int(cur.lastrowid), "name": name}
    finally:
        conn.close()


@router.delete("/groups/{group_id}")
def delete_group(group_id: int, user: CurrentUser = Depends(require_login)) -> dict:
    """그룹 삭제. 멤버 디바이스의 group_id 는 FK ON DELETE SET NULL 로 자동 해제."""
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM device_groups WHERE id=?", (group_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="대상 그룹을 찾을 수 없습니다.")
        return {"ok": True}
    finally:
        conn.close()


# ─────────────────────────── 데이터 현황 / 다운로드 ───────────────────────────

@router.get("/data/summary")
def data_summary() -> dict:
    """device × bundle 집계 (DESIGN.md §7.3)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT device_id, bundle_key,
                      COUNT(*) AS file_count,
                      COALESCE(SUM(file_size_bytes), 0) AS total_bytes,
                      MIN(target_date) AS first_date,
                      MAX(target_date) AS last_date,
                      COALESCE(SUM(record_count), 0) AS total_records
                 FROM collection_jobs
                WHERE status='success'
                GROUP BY device_id, bundle_key
                ORDER BY device_id, bundle_key"""
        ).fetchall()
        # 사용자 친화 메타(별명/기기 종류) 결합
        dev_meta = {}
        for r in conn.execute(
            "SELECT device_id, device_type, alias, install_location FROM devices"
        ):
            dev_meta[r["device_id"]] = dict(r)
        return {
            "items": [
                {**dict(row), **dev_meta.get(row["device_id"], {})}
                for row in rows
            ]
        }
    finally:
        conn.close()


@router.get("/data/{device_id}/{bundle_key}/files")
def list_files(device_id: str, bundle_key: str, _user: CurrentUser = Depends(require_login)) -> dict:
    """일자별 파일 메타 (DESIGN.md §7.2). CSV 다운로드 사전 정보이므로 로그인 필수."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT target_date, status, record_count, file_path, file_size_bytes,
                      started_at, finished_at, error_message
                 FROM collection_jobs
                WHERE device_id=? AND bundle_key=?
                ORDER BY target_date DESC""",
            (device_id, bundle_key),
        ).fetchall()
        return {"files": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.get("/data/{device_id}/{bundle_key}/{date}/download")
def download_single(device_id: str, bundle_key: str, date: str, _user: CurrentUser = Depends(require_login)):
    """단일 일자 CSV 다운로드 (DESIGN.md §6.3, 로그인 필수)."""
    _validate_yyyymmdd(date)
    date_compact = date.replace("-", "")
    fname = f"{date_compact}_{last6(device_id)}.csv"
    path = config.DATA_DIR / bundle_key / device_id / fname
    if not path.exists():
        raise HTTPException(status_code=404, detail="CSV 파일이 없습니다.")
    return FileResponse(path, media_type="text/csv", filename=fname)


def _iter_concat_csv(paths: list[Path]):
    """concat CSV 스트리밍: 첫 파일의 # 메타 + 컬럼 헤더 1회, 나머지는 데이터 행만 (DESIGN.md §7.3)."""
    first = True
    for p in paths:
        with p.open("r", encoding="utf-8") as f:
            # 첫 파일: 메타 + 헤더 + 데이터 모두 출력
            # 나머지: 메타(#)와 헤더(첫 비주석 줄)를 스킵하고 데이터 행만 출력
            if first:
                yield f.read()
                first = False
                continue
            skipped_header = False
            for line in f:
                if line.startswith("#"):
                    continue
                if not skipped_header:
                    skipped_header = True  # 컬럼 헤더 1줄 스킵
                    continue
                yield line


@router.get("/data/{device_id}/{bundle_key}/bundle")
def download_bundle(
    device_id: str,
    bundle_key: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    format: str = Query("zip", pattern="^(zip|concat)$"),
    _user: CurrentUser = Depends(require_login),
):
    """기간 선택 일괄 다운로드 (zip 또는 concat, DESIGN.md §7.3). 로그인 필수."""
    _validate_yyyymmdd(from_)
    _validate_yyyymmdd(to)
    d_from = datetime.strptime(from_, "%Y-%m-%d").date()
    d_to = datetime.strptime(to, "%Y-%m-%d").date()
    if d_to < d_from:
        raise HTTPException(status_code=400, detail="to 가 from 보다 이전입니다.")
    if (d_to - d_from).days > 365:
        raise HTTPException(status_code=400, detail="기간이 365일을 초과합니다.")

    # 존재 파일 수집 + 누락 일자 산출
    base = config.DATA_DIR / bundle_key / device_id
    paths: list[Path] = []
    missing: list[str] = []
    cur = d_from
    while cur <= d_to:
        ds = cur.strftime("%Y-%m-%d")
        fname = f"{cur.strftime('%Y%m%d')}_{last6(device_id)}.csv"
        p = base / fname
        if p.exists():
            paths.append(p)
        else:
            missing.append(ds)
        cur += timedelta(days=1)

    suffix6 = last6(device_id)
    name_root = f"{d_from.strftime('%Y%m%d')}-{d_to.strftime('%Y%m%d')}_{suffix6}_{bundle_key}"

    if format == "zip":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for p in paths:
                z.write(p, arcname=p.name)
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{name_root}.zip"',
                "X-Missing-Dates": ",".join(missing),
            },
        )
    # concat: 메모리에 전체 로드하지 않도록 generator 스트리밍
    return StreamingResponse(
        _iter_concat_csv(paths),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{name_root}.csv"',
            "X-Missing-Dates": ",".join(missing),
        },
    )


# ─────────────────────────── 작업 이력 ───────────────────────────

@router.get("/jobs")
def list_jobs(
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=2000),
) -> dict:
    sql = "SELECT * FROM collection_jobs WHERE 1=1"
    params: list = []
    if from_:
        _validate_yyyymmdd(from_); sql += " AND target_date >= ?"; params.append(from_)
    if to:
        _validate_yyyymmdd(to); sql += " AND target_date <= ?"; params.append(to)
    if status:
        sql += " AND status=?"; params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"; params.append(limit)
    conn = get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
        return {"jobs": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.post("/jobs/bulk_run", status_code=202)
def run_jobs_bulk(req: BulkJobRunRequest, user: CurrentUser = Depends(require_admin)) -> dict:
    """일괄 수동 수집 (DESIGN.md §7.4). **admin 전용**.

    `[from, to]` 기간의 매일 × 활성 장치 전체를 백그라운드로 수집한다.
    최대 31일 (Aqara API 호출량·토큰 만료 위험 완화).
    응답 즉시 202 Accepted 반환 — 진행 상황은 `/jobs` 페이지에서 확인.
    """
    _validate_yyyymmdd(req.from_)
    _validate_yyyymmdd(req.to)
    d_from = datetime.strptime(req.from_, "%Y-%m-%d").date()
    d_to = datetime.strptime(req.to, "%Y-%m-%d").date()
    if d_to < d_from:
        raise HTTPException(status_code=400, detail="to 가 from 보다 이전입니다.")
    span_days = (d_to - d_from).days + 1
    if span_days > 31:
        raise HTTPException(status_code=400, detail=f"기간이 31일을 초과합니다 (요청 {span_days}일).")

    # 토큰 미등록 시 모든 job 이 실패하므로 즉시 거부 (사용자 친화 메시지).
    try:
        token_manager.load()
    except token_manager.TokenNotFoundError:
        raise HTTPException(
            status_code=400,
            detail="Aqara refresh token 이 등록되지 않았습니다. 관리자가 /admin/token 에서 등록해야 합니다.",
        )

    # 활성 장치 수 (예상 작업량 표시용)
    devices = collector.list_active_devices()
    if not devices:
        raise HTTPException(status_code=400, detail="활성 장치가 없습니다.")

    # 작업 수 = 기간(일) × 활성 장치의 (device_type × hub 별 bundle 수) 총합.
    # DeviceType.bundles_by_hub 가 hub → tuple[Bundle, ...] 구조이므로 각 디바이스의 실제 hub
    # 에 해당하는 bundle 수만 합산 (DESIGN.md §15 hub 다중 지원 이후 데이터 모델 반영).
    from ..devices import bundles_for as _bundles_for
    bundles_per_device = [
        len(_bundles_for(d["device_type"], d.get("hub") or "aqara"))
        for d in devices if d["device_type"] in DEVICE_TYPES
    ]
    estimated_jobs = span_days * sum(bundles_per_device)

    # APScheduler 의 워커 스레드로 즉시 1회 실행 위임 (응답 사이클 차단 회피).
    job_id = scheduler.run_in_background(
        collector.collect_date_range, req.from_, req.to,
        id_prefix="bulk_collect",
        name=f"bulk collect {req.from_}..{req.to}",
    )
    return {
        "started": True,
        "job_id": job_id,
        "from": req.from_,
        "to": req.to,
        "date_count": span_days,
        "device_count": len(devices),
        "estimated_jobs": estimated_jobs,
        "message": "수집이 백그라운드로 시작되었습니다. /jobs 페이지에서 진행 상황을 확인하세요.",
    }


@router.post("/jobs/run")
def run_job_manually(req: JobRunRequest, user: CurrentUser = Depends(require_admin)) -> dict:
    """단건 수동 수집 트리거 (admin 전용)."""
    _validate_yyyymmdd(req.target_date)
    conn = get_connection()
    try:
        dev = conn.execute(
            """SELECT device_type, hub, alias, install_location, install_date, created_by_name
                 FROM devices WHERE device_id=? AND deleted_at IS NULL""",
            (req.device_id,),
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail="활성 장치를 찾을 수 없습니다.")
        dev = dict(dev)
    finally:
        conn.close()
    if dev["device_type"] not in DEVICE_TYPES:
        raise HTTPException(status_code=400, detail="알 수 없는 device_type")
    from ..devices import bundles_for
    hub = dev.get("hub") or "aqara"
    bundle = next((b for b in bundles_for(dev["device_type"], hub) if b.key == req.bundle_key), None)
    if bundle is None:
        raise HTTPException(status_code=400, detail=f"bundle_key 미존재: {req.bundle_key} (hub={hub})")
    result = collector.run_one_bundle(
        req.device_id, dev["device_type"], bundle, req.target_date,
        device_meta=dev,
    )
    return result


# ─────────────────────────── 알림 / 토큰 / 사용자 ───────────────────────────

@router.get("/alerts")
def get_active_alerts() -> dict:
    return {"alerts": alerts.list_active()}


@router.post("/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: int, user: CurrentUser = Depends(require_login)) -> dict:
    ok = alerts.resolve_by_id(alert_id, user["id"])
    if not ok:
        raise HTTPException(status_code=404, detail="해당 알림이 없거나 이미 해제되었습니다.")
    return {"ok": True}


@router.get("/admin/token/status")
def token_status(user: CurrentUser = Depends(require_admin)) -> dict:
    try:
        rec = token_manager.load()
        return {
            "present": True,
            "refreshed_at": rec.get("refreshed_at"),
            "expires_at": rec.get("expires_at"),
            "access_token_masked": (rec["access_token"][:6] + "…") if rec.get("access_token") else None,
        }
    except token_manager.TokenNotFoundError:
        return {"present": False}


@router.post("/admin/token/seed")
def token_seed(req: TokenSeed, user: CurrentUser = Depends(require_admin)) -> dict:
    """관리자가 발급받은 refresh_token으로 초기/재등록.

    저장만 하면 다음 API 호출 시 자동 갱신 흐름으로 access_token까지 발급되지만,
    UX상 즉시 한 번 호출해 검증한다.
    """
    token_manager.save({
        "access_token": "",
        "refresh_token": req.refresh_token,
        "refreshed_at": "",
        "expires_at": "",
    })
    # 즉시 refresh 시도 (성공 시 새 토큰 쌍이 저장됨, 실패 시 명확한 에러)
    from .. import aqara_client
    try:
        aqara_client._refresh_token()  # noqa: SLF001
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"입력하신 refresh token으로 갱신 실패: {e}") from e
    return {"ok": True}


# ─────────────────────── SmartThings OAuth 토큰 (DESIGN.md §15.2) ───────────────────────
# OAuth 연결 시작/콜백은 HTML 페이지 라우트(routes/pages.py)에 둔다 (브라우저 리다이렉트 흐름).
# 여기서는 상태 조회 JSON API 만 제공.

@router.get("/admin/smartthings_token/status")
def smartthings_token_status(user: CurrentUser = Depends(require_admin)) -> dict:
    """SmartThings 인증 상태 — 활성 source (pat-env / pat-file / oauth / 없음) + 마스킹."""
    return token_manager.smartthings_token_status()


@router.post("/admin/smartthings_pat")
def smartthings_pat_seed(req: SmartThingsPatSeed, user: CurrentUser = Depends(require_admin)) -> dict:
    """admin 이 입력한 PAT 저장 + 즉시 /v1/devices 호출로 검증 (stopgap).

    저장 후 환경변수 PAT 가 없는 한 즉시 활성화 (재시작 불필요). 검증 실패 시 저장 안 함.
    """
    pat = (req.pat or "").strip()
    if not pat:
        raise HTTPException(status_code=400, detail="PAT 가 비어 있습니다.")
    # 검증 — 임시로 파일에 저장 후 list_devices 호출. 실패 시 롤백.
    # 단, 환경변수 SMARTTHINGS_PAT 가 우선되므로 검증도 *그 값* 으로 시도된다는 점을 호출자에게 알림용으로
    # 응답 source 에 'pat-env' 표기.
    prev = token_manager.load_smartthings_pat_file()
    token_manager.save_smartthings_pat_file(pat)
    from .. import smartthings_client
    try:
        devices = smartthings_client.list_devices()
    except smartthings_client.SmartThingsAPIError as e:
        # 롤백
        if prev:
            token_manager.save_smartthings_pat_file(prev)
        else:
            token_manager.clear_smartthings_pat_file()
        raise HTTPException(status_code=400, detail=f"PAT 검증 실패: {e}") from e
    source = token_manager.smartthings_token_status().get("source")
    return {"ok": True, "device_count": len(devices), "source": source}


@router.delete("/admin/smartthings_pat")
def smartthings_pat_clear(user: CurrentUser = Depends(require_admin)) -> dict:
    """저장된 PAT 파일 삭제 — OAuth 또는 환경변수 PAT 경로로 복귀."""
    removed = token_manager.clear_smartthings_pat_file()
    return {"ok": True, "removed": removed}
