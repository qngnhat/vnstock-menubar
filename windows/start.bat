@echo off
rem Chay nen khong console (pythonw). Log ghi vao vnstock.log.
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
  py -3 -m venv .venv || python -m venv .venv
)
.venv\Scripts\python.exe -c "import PySide6, requests" 2>nul || .venv\Scripts\pip.exe install -q -r requirements.txt
start "" /b .venv\Scripts\pythonw.exe -c "import sys,runpy; sys.stdout=sys.stderr=open('vnstock.log','a',encoding='utf-8',buffering=1); runpy.run_path('app.py', run_name='__main__')"
