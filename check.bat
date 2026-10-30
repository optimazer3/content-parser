@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
    echo Сначала запусти run.bat
    pause
    exit /b
)
call .venv\Scripts\activate.bat
python -m bot.check
pause
