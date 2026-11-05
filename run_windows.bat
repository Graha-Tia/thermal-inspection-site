@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
if "%APP_PASSWORD%"=="" (
  echo [안내] 로컬 전용 실행: http://127.0.0.1:8000
  echo 휴대폰으로 접속하려면 비밀번호를 설정하고 --host 0.0.0.0으로 실행하세요.
)
python -m uvicorn main:app --host 127.0.0.1 --port 8000
pause
