"""Aqara Open API 클라이언트 (DESIGN.md §6.2 / §8).

핵심:
- 서명 생성(MD5) 후 POST 호출
- 모든 API 호출은 call_with_auto_refresh()로 감싸 1차 실패 → 토큰 갱신 → 1회 재시도
- refresh 또는 재시도까지 실패하면 system_alerts에 경고를 등록
"""

from __future__ import annotations

import hashlib
import random
import string
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

from . import alerts, config, token_manager


class AqaraAPIError(RuntimeError):
    """Aqara API 호출이 자동 재시도 후에도 실패했을 때 사용자/스케줄러에게 던지는 예외."""

    def __init__(self, message: str, payload: dict | None = None):
        super().__init__(message)
        self.payload = payload or {}


# ─────────────────────────── 서명 / 헤더 ───────────────────────────

def _nonce() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=16))


def _ts_ms() -> str:
    return str(int(time.time() * 1000))


def _build_sign(access_token: str | None, nonce: str, ts_ms: str) -> str:
    """서명 문자열 생성.

    access_token이 있으면 Accesstoken을 포함, 없으면(예: refreshToken intent) 제외.
    record_data.py의 서명 로직과 동일 규칙: 전체 소문자 변환 후 MD5.
    """
    if access_token:
        pre = (
            f"Accesstoken={access_token}&Appid={config.AQARA_APPID}"
            f"&Keyid={config.AQARA_KEYID}&Nonce={nonce}&Time={ts_ms}{config.AQARA_APPKEY}"
        )
    else:
        pre = (
            f"Appid={config.AQARA_APPID}&Keyid={config.AQARA_KEYID}"
            f"&Nonce={nonce}&Time={ts_ms}{config.AQARA_APPKEY}"
        )
    return hashlib.md5(pre.lower().encode("utf-8")).hexdigest()


def _build_headers(access_token: str | None) -> dict[str, str]:
    nonce = _nonce()
    ts = _ts_ms()
    headers = {
        "Content-Type": "application/json",
        "Appid": config.AQARA_APPID,
        "Keyid": config.AQARA_KEYID,
        "Nonce": nonce,
        "Time": ts,
        "Sign": _build_sign(access_token, nonce, ts),
    }
    if access_token:
        headers["Accesstoken"] = access_token
    return headers


# ─────────────────────────── 저수준 POST ───────────────────────────

def _post(payload: dict, access_token: str | None) -> dict:
    """단일 POST 호출. HTTP 또는 body code != 0 둘 다 비정상으로 간주."""
    resp = requests.post(
        config.AQARA_API_URL,
        headers=_build_headers(access_token),
        json=payload,
        timeout=30,
    )
    if resp.status_code != 200:
        raise AqaraAPIError(
            f"HTTP {resp.status_code}: {resp.text[:300]}",
            payload={"http_status": resp.status_code, "body": resp.text[:1000]},
        )
    body = resp.json()
    if body.get("code") != 0:
        raise AqaraAPIError(
            f"code={body.get('code')} message={body.get('message')}",
            payload={"body": body},
        )
    return body


# ─────────────────────────── refresh ───────────────────────────

def _refresh_token() -> dict:
    """refreshToken으로 새 access/refresh 쌍 발급 후 tokens.json 덮어쓰기.

    실패 시 AqaraAPIError 또는 TokenNotFoundError를 그대로 전파.
    """
    cur_refresh = token_manager.get_refresh_token()
    body = _post(
        {"intent": "config.auth.refreshToken", "data": {"refreshToken": cur_refresh}},
        access_token=None,
    )
    new_tokens = token_manager.build_record(body["result"])
    token_manager.save(new_tokens)
    return new_tokens


# ─────────────────────────── 자동 재시도 래퍼 ───────────────────────────

def call_with_auto_refresh(payload: dict) -> dict:
    """API 호출 + 1차 실패 시 토큰 갱신 + 1회 재시도 (DESIGN.md §6.2).

    정상 호출 1회라도 성공하면 관련 active 알림(token_refresh_failed, aqara_persistent_error)을
    자동 resolved 처리한다.
    """
    # 1차 시도
    try:
        access = token_manager.get_access_token()
    except token_manager.TokenNotFoundError as e:
        alerts.raise_alert(
            code="token_refresh_failed",
            level="error",
            message="Aqara 토큰이 등록되지 않았습니다. /admin/token에서 refresh token을 등록하세요.",
            details=str(e),
        )
        raise AqaraAPIError(str(e)) from e

    try:
        result = _post(payload, access_token=access)
        alerts.resolve_by_code(["token_refresh_failed", "aqara_persistent_error"])
        return result
    except AqaraAPIError as first_err:
        # 1차 실패 → 토큰 만료로 간주, refresh 시도 (DESIGN.md §6.2)
        try:
            _refresh_token()
        except Exception as refresh_err:  # noqa: BLE001 — 어떤 실패든 사용자 알림이 필요
            alerts.raise_alert(
                code="token_refresh_failed",
                level="error",
                message="Aqara 토큰 갱신 실패. 관리자가 refresh token을 재등록해야 합니다.",
                details=f"refresh_error={refresh_err}; first_call_error={first_err}",
            )
            raise AqaraAPIError(f"token refresh failed: {refresh_err}") from refresh_err

        # 새 토큰으로 1회 재시도
        try:
            new_access = token_manager.get_access_token()
            result = _post(payload, access_token=new_access)
        except AqaraAPIError as retry_err:
            alerts.raise_alert(
                code="aqara_persistent_error",
                level="error",
                message=(
                    "토큰 갱신 후에도 Aqara API 호출이 실패했습니다. "
                    f"(intent={payload.get('intent')}, error={retry_err})"
                ),
                details=str(retry_err.payload)[:1000],
            )
            raise

        alerts.resolve_by_code(["token_refresh_failed", "aqara_persistent_error"])
        return result


# ─────────────────────────── 시계열 조회 ───────────────────────────

def _kst_to_utc_millis(kst_str: str) -> str:
    """'YYYY-MM-DD HH:MM:SS' (KST) → UTC milliseconds 문자열.

    record_data.py의 kst_to_utc_millis 이식 (CLAUDE.md §2.4 시간대 단일 함수 규칙).
    """
    kst = timezone(timedelta(hours=9))
    dt = datetime.strptime(kst_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=kst)
    return str(int(dt.astimezone(timezone.utc).timestamp() * 1000))


def _utc_ms_to_kst_str(ts_ms: int | str) -> str:
    """API 응답 timestamp(UTC ms) → KST 'YYYY-MM-DD HH:MM:SS' 문자열."""
    ts = int(ts_ms)
    utc_dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    return utc_dt.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S")


def fetch_history_page(device_id: str, resource_id: str, start_kst: str, end_kst: str) -> list[tuple[str, str]]:
    """1회 호출분(최대 100건)을 (kst_time, value) 리스트로 반환.

    페이지네이션은 호출자(collector)가 마지막 timestamp + 1초로 cursor 갱신해 반복.
    """
    payload = {
        "intent": "fetch.resource.history",
        "data": {
            "subjectId": device_id,
            "resourceIds": [resource_id],
            "startTime": _kst_to_utc_millis(start_kst),
            "endTime": _kst_to_utc_millis(end_kst),
        },
    }
    body = call_with_auto_refresh(payload)
    data_list = body.get("result", {}).get("data", [])
    out: list[tuple[str, str]] = []
    for item in data_list:
        out.append((_utc_ms_to_kst_str(item["timeStamp"]), str(item["value"])))
    return out


def fetch_history_paginated(
    device_id: str,
    resource_id: str,
    start_kst: str,
    end_kst: str,
    page_caller: Callable[..., list[tuple[str, str]]] | None = None,
    page_limit: int = 100,
) -> list[tuple[str, str]]:
    """주어진 KST 구간의 모든 (time, value) 페어를 페이지네이션으로 수집 (DESIGN.md §6.2).

    Aqara API는 시간 내림차순(최신 먼저)으로 최대 100건씩 반환한다 (record_data.py에서 검증된 동작).
    따라서 endTime을 batch의 마지막 원소(= 가장 오래된 시각) 직전으로 줄여가며
    과거 방향으로 페이지네이션하고, len(batch) < page_limit이면 더 이상 데이터가 없다고 보고 종료한다.

    page_caller: 테스트/dry-run에서 fetch_history_page를 스텁으로 대체할 때 사용.
    """
    caller = page_caller or fetch_history_page
    rows: list[tuple[str, str]] = []
    cursor_end = end_kst
    while True:
        batch = caller(device_id, resource_id, start_kst, cursor_end)
        rows += batch
        if len(batch) < page_limit:
            break
        # batch는 내림차순 → batch[-1]가 가장 오래된 시각. 그 시각 - 1초를 새 endTime으로
        # 사용하면 경계 레코드 중복 없이 더 과거 페이지를 가져올 수 있다.
        oldest_ts = batch[-1][0]
        next_end = datetime.strptime(oldest_ts, "%Y-%m-%d %H:%M:%S") - timedelta(seconds=1)
        cursor_end = next_end.strftime("%Y-%m-%d %H:%M:%S")
        # 안전 가드: 새 endTime이 startTime보다 이전이면 종료
        if cursor_end < start_kst:
            break
    return rows
