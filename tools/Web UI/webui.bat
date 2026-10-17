@echo off
chcp 65001 >nul
rem 웹 UI 실행기: 더블클릭 -> 처음이면 설치 -> 엔진 서버 켜기 -> 웹 UI http://127.0.0.1:7860
rem 끌 때는 이 창에서 Ctrl+C 또는 창 닫기. 여기서 켠 엔진 서버도 같이 꺼진다.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if exist ".venv\Scripts\python.exe" goto run
echo 처음 실행이라 웹 UI 환경을 설치합니다. .venv 폴더, 몇 분 걸려요.
py -3.11 -m venv .venv 2>nul
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if not exist ".venv\Scripts\python.exe" (
  echo Python 3.11 이 필요합니다: https://www.python.org/downloads/
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo 설치에 실패했습니다. 위 오류를 확인해 주세요. 다시 실행하기 전에 .venv 폴더를 지워 주세요.
  pause
  exit /b 1
)

:run
".venv\Scripts\python.exe" loop\launch.py
if errorlevel 1 pause
