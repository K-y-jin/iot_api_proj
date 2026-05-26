@echo off
REM Aqara 데이터 자동 수집 시스템 실행 스크립트 (DESIGN.md §11)
REM 의존성 설치: pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
