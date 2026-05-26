"""APScheduler cron job 등록 (DESIGN.md §6.1, §6.2).

3개의 정기 작업:
1. 매일 09:00 KST — 어제 1일치 수집 (collect_yesterday)
2. 매일 03:00 KST — 토큰 선제 갱신 (필요 시에만)
3. 매시간     — 최근 7일 내 누락분 보충 (backfill_missing)
"""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import alerts, collector, config, token_manager


def _job_daily_collect() -> None:
    """매일 09:00 KST: 전일자 1일 수집 워크플로우 실행."""
    try:
        collector.collect_yesterday()
    except Exception as e:  # noqa: BLE001 — 스케줄러 job은 절대 죽지 않도록 광범위 catch
        alerts.raise_alert(
            code="scheduler_daily_collect_error",
            level="error",
            message=f"일일 수집 스케줄 실행 중 예외: {e}",
        )


def _job_proactive_token_refresh() -> None:
    """매일 03:00 KST: Aqara·SmartThings 토큰을 만료 임박 시 선제 refresh.

    - Aqara: expires_at - now < 24h 이면 refresh.
    - SmartThings: access_token 수명이 짧아 expires_at - now < 6h 이면 OAuth refresh (DESIGN.md §15.2).
    두 갱신은 서로 독립적으로 처리해 한쪽 실패가 다른 쪽을 막지 않는다.
    """
    # ─ Aqara 토큰 ─
    try:
        if token_manager.should_refresh_proactively():
            # config.auth.refreshToken 호출은 access_token 없이 가능 → 직접 호출
            from . import aqara_client
            aqara_client._refresh_token()  # noqa: SLF001 (모듈 내부 함수 의도적 사용)
    except token_manager.TokenNotFoundError:
        # 아직 admin이 초기 토큰을 등록하지 않은 상태 → 조용히 패스
        pass
    except Exception:  # noqa: BLE001
        # _refresh_token 내부에서 alerts.raise_alert는 호출되지 않으므로 여기서 처리
        alerts.raise_alert(
            code="token_refresh_failed",
            level="error",
            message="선제 토큰 갱신(03:00 cron) 실패. /admin/token 확인 필요.",
        )

    # ─ SmartThings OAuth 토큰 ─
    try:
        if token_manager.smartthings_should_refresh_proactively():
            from . import smartthings_client
            smartthings_client.refresh_smartthings_tokens()
    except token_manager.SmartThingsTokenNotFoundError:
        # admin 이 아직 SmartThings 를 연결하지 않은 상태 → 조용히 패스
        pass
    except Exception:  # noqa: BLE001
        # refresh_smartthings_tokens 가 실패 시 이미 alert 를 등록하므로 추가 동작 불필요
        pass


def _job_healthcheck_backfill() -> None:
    """매시간: 최근 7일 내 failed/누락 (device, bundle, date) 재시도."""
    try:
        collector.backfill_missing()
    except Exception as e:  # noqa: BLE001
        alerts.raise_alert(
            code="scheduler_backfill_error",
            level="warning",
            message=f"누락 보충 헬스체크 중 예외: {e}",
        )


def _job_prune_old_jobs() -> None:
    """매일 03:30 KST: collection_jobs 보관 기간(`JOB_HISTORY_RETENTION_DAYS`, 기본 28일) 초과 행 삭제.

    수집된 CSV 파일은 보존된다 — `/data` 화면은 파일시스템 walk 기준이라 통계는 그대로 유지.
    실패해도 다음 일자에 다시 시도되므로 alert 는 warning 수준으로만 표면화.
    """
    try:
        deleted = collector.prune_old_jobs()
        if deleted:
            # 정상 정리는 조용히 통과 (수집 실패 alert 와 섞이지 않도록).
            # 디버깅 필요 시 로그로만 확인 가능.
            pass
    except Exception as e:  # noqa: BLE001
        alerts.raise_alert(
            code="scheduler_prune_error",
            level="warning",
            message=f"수집 작업 이력 정리(03:30 cron) 실패: {e}",
        )


# ─────────────────────────── 스케줄러 라이프사이클 ───────────────────────────

_scheduler: BackgroundScheduler | None = None


def start() -> BackgroundScheduler:
    """FastAPI startup 훅에서 호출. 멱등 (이미 시작했으면 기존 인스턴스 반환)."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    sched = BackgroundScheduler(timezone=config.KST_TZ)
    # NOTE: job name은 콘솔 dry-run에 그대로 출력되므로 영어로 작성 (Windows cp949 깨짐 방지).
    sched.add_job(
        _job_daily_collect,
        CronTrigger(hour=config.COLLECT_CRON_HOUR, minute=config.COLLECT_CRON_MINUTE),
        id="daily_collect",
        name="daily collection (09:00 KST)",
        replace_existing=True,
    )
    sched.add_job(
        _job_proactive_token_refresh,
        CronTrigger(hour=config.TOKEN_PROACTIVE_REFRESH_HOUR, minute=0),
        id="token_refresh",
        name="proactive token refresh (03:00 KST)",
        replace_existing=True,
    )
    sched.add_job(
        _job_healthcheck_backfill,
        IntervalTrigger(minutes=config.HEALTHCHECK_INTERVAL_MINUTES),
        id="healthcheck_backfill",
        name=f"healthcheck backfill (every {config.HEALTHCHECK_INTERVAL_MINUTES}min)",
        replace_existing=True,
    )
    sched.add_job(
        _job_prune_old_jobs,
        CronTrigger(hour=3, minute=30),
        id="prune_old_jobs",
        name=f"prune collection_jobs older than {config.JOB_HISTORY_RETENTION_DAYS}d (03:30 KST)",
        replace_existing=True,
    )
    sched.start()
    _scheduler = sched
    return sched


def stop() -> None:
    """FastAPI shutdown 훅에서 호출."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None


def list_jobs() -> list[dict]:
    """현재 등록된 job 메타데이터 (디버깅/UI용)."""
    if _scheduler is None:
        return []
    return [
        {"id": j.id, "name": j.name, "next_run": str(j.next_run_time)}
        for j in _scheduler.get_jobs()
    ]


def run_in_background(func, *args, id_prefix: str = "oneshot", name: str | None = None) -> str:
    """`func(*args)` 를 APScheduler 의 thread pool 에서 즉시 1회 실행한다 (DESIGN.md §7.4 일괄 수집).

    - 요청 응답 사이클을 막지 않기 위해 별도 워커 스레드에서 처리.
    - id 는 `<id_prefix>_<timestamp>` 형식으로 고유 보장.
    - 반환: 생성된 job id.

    스케줄러가 미시작 상태면 RuntimeError. FastAPI startup 훅이 항상 start() 를 호출하므로
    정상 라이프사이클에선 발생하지 않는다.
    """
    if _scheduler is None or not _scheduler.running:
        raise RuntimeError("스케줄러가 실행 중이 아닙니다. 앱이 정상 시작되었는지 확인하세요.")
    from datetime import datetime
    job_id = f"{id_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    _scheduler.add_job(
        func, args=list(args), id=job_id,
        name=name or job_id, max_instances=1,
    )
    return job_id
