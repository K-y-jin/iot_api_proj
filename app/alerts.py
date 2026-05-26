"""시스템 경고 알림 (DESIGN.md §7.6).

- raise_alert: 동일 code의 active 알림이 있으면 메시지/상세만 갱신 (중복 배너 방지)
- resolve_by_code: 정상 호출 1회 성공 시 호출되어 관련 active 알림 자동 해제
- list_active: 배너 SSR/폴링 응답용
"""

from __future__ import annotations

from typing import Iterable

from .db import get_connection, now_kst_iso


def raise_alert(code: str, level: str, message: str, details: str | None = None) -> None:
    """active 경고 알림 upsert.

    동일 code의 미해제 알림이 이미 있으면 message/details만 갱신해 배너 중복 방지.
    """
    conn = get_connection()
    now = now_kst_iso()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id FROM system_alerts WHERE code=? AND resolved_at IS NULL",
            (code,),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE system_alerts SET message=?, details=?, created_at=? WHERE id=?",
                (message, details, now, row["id"]),
            )
        else:
            cur.execute(
                """INSERT INTO system_alerts (code, level, message, details, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (code, level, message, details, now),
            )
    finally:
        cur.close()
        conn.close()


def resolve_by_code(codes: Iterable[str], resolved_by_user_id: int | None = None) -> None:
    """주어진 code 목록의 active 알림을 모두 resolved 처리 (system 또는 사용자 dismiss)."""
    codes = list(codes)
    if not codes:
        return
    conn = get_connection()
    now = now_kst_iso()
    placeholders = ",".join("?" * len(codes))
    try:
        conn.execute(
            f"""UPDATE system_alerts
                   SET resolved_at=?, resolved_by=?
                 WHERE resolved_at IS NULL AND code IN ({placeholders})""",
            (now, resolved_by_user_id, *codes),
        )
    finally:
        conn.close()


def resolve_by_id(alert_id: int, resolved_by_user_id: int) -> bool:
    """특정 알림을 수동 dismiss. 이미 해제되었거나 없으면 False."""
    conn = get_connection()
    try:
        cur = conn.execute(
            """UPDATE system_alerts
                  SET resolved_at=?, resolved_by=?
                WHERE id=? AND resolved_at IS NULL""",
            (now_kst_iso(), resolved_by_user_id, alert_id),
        )
        return cur.rowcount > 0
    finally:
        conn.close()


def list_active() -> list[dict]:
    """배너 렌더링용 active 알림 목록 (최신순)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT id, code, level, message, details, created_at
                 FROM system_alerts
                WHERE resolved_at IS NULL
                ORDER BY created_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
