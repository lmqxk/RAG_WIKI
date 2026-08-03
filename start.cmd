@echo off
setlocal
cd /d "%~dp0"
"backend\.venv\Scripts\python.exe" "scripts\run.py"
endlocal
