@echo off
REM Windows: restart collect.py 30 seconds after it stops. Close this window to stop.
cd /d "%~dp0"
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
:loop
python collect.py
echo collector stopped. restarting in 30 seconds. close this window to stop.
timeout /t 30 /nobreak > nul
goto loop
