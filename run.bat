@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist .venv (
    echo Создаю виртуальное окружение...
    py -3 -m venv .venv 2>nul || python -m venv .venv
)
call .venv\Scripts\activate.bat
echo Ставлю зависимости...
pip install -q -r requirements.txt

if not exist .env (
    copy .env.example .env >nul
    echo.
    echo Заполни файл .env ^(откроется в Блокноте^), сохрани и запусти run.bat ещё раз.
    notepad .env
    pause
    exit /b
)

if not exist data\userbot.session (
    findstr /r "^TG_API_ID=[0-9]" .env >nul && python -m bot.login
)

python -m bot
pause
