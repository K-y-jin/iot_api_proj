"""FastAPI 앱 진입점 (DESIGN.md §4).

- startup 훅: DB 초기화 + admin 시드 + APScheduler 시작
- shutdown 훅: 스케줄러 정리
- 미들웨어: SessionMiddleware (서명 쿠키)
- 정적 파일 + Jinja2 템플릿 마운트
"""

from __future__ import annotations

import argparse

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, db, scheduler
from .routes import api as api_routes
from .routes import display as display_routes
from .routes import pages as page_routes


def create_app() -> FastAPI:
    """FastAPI 인스턴스 생성. 테스트에서도 직접 호출 가능."""
    app = FastAPI(title="Aqara 데이터 자동 수집 시스템")

    # 세션 쿠키 (DESIGN.md §6.3)
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.SESSION_SECRET,
        max_age=config.SESSION_MAX_AGE_SECONDS,
        same_site="lax",
    )

    # 정적 파일
    static_dir = config.PROJECT_ROOT / "app" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # 라우트
    app.include_router(page_routes.router)
    app.include_router(display_routes.router)   # /display/{device_id} (DISPLAY.md SSOT)
    app.include_router(api_routes.router)

    @app.on_event("startup")
    def _startup() -> None:
        """앱 부팅 시 1회: DB 초기화 + admin 시드 + 디렉토리 생성 + 스케줄러 시작."""
        config.ensure_dirs()
        db.init_db()
        auth.seed_admin_if_missing()
        scheduler.start()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        scheduler.stop()

    return app


app = create_app()


def _cli() -> None:
    """`python -m app.main --dry-run` 으로 부팅 검증 (CLAUDE.md §3.2)."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="DB 초기화 + 스케줄러 등록까지만 수행하고 종료")
    args = parser.parse_args()
    if args.dry_run:
        config.ensure_dirs()
        db.init_db()
        auth.seed_admin_if_missing()
        sched = scheduler.start()
        # NOTE: console output in English to avoid Windows cp949 mojibake.
        print("[dry-run] registered scheduler jobs:")
        for j in sched.get_jobs():
            print(f"  - {j.id:25} next={j.next_run_time}  name={j.name}")
        scheduler.stop()


if __name__ == "__main__":
    _cli()
