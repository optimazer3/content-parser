@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist .venv (
    echo Создаю виртуальное окружение...
    py -3 -m venv .venv 2>nul || python -m venv .venv
)
call .venv\Scripts\activate.bat
echo Ставлю зависимости...
pip install -q --disable-pip-version-check -r requirements.txt

if not exist .env (
    copy .env.example .env >nul
    echo.
    echo Заполни файл .env ^(откроется в Блокноте^), сохрани и запусти run.bat ещё раз.
    notepad .env
    pause
    exit /b
)

python -m bot.login --if-needed || (pause & exit /b 1)
python -m bot
pause
