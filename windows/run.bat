@echo off
rem Chay app co cua so console (xem log). Tu tao venv + cai deps lan dau.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv || python -m venv .venv
)
.venv\Scripts\python.exe -c "import PySide6, requests" 2>nul || .venv\Scripts\pip.exe install -q -r requirements.txt
.venv\Scripts\python.exe app.py
