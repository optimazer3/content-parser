#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "Создаю виртуальное окружение..."
    python3 -m venv .venv
fi
source .venv/bin/activate
echo "Ставлю зависимости..."
pip install -q -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Заполни файл .env и запусти ./run.sh ещё раз."
    exit 0
fi

if [ ! -f data/userbot.session ] && grep -qE '^TG_API_ID=[0-9]' .env; then
    python -m bot.login
fi

exec python -m bot
