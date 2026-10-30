#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "Создаю виртуальное окружение..."
    python3 -m venv .venv
fi
source .venv/bin/activate
echo "Ставлю зависимости..."
pip install -q --disable-pip-version-check -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Заполни файл .env и запусти ./run.sh ещё раз."
    exit 0
fi

python -m bot.login --if-needed

exec python -m bot
