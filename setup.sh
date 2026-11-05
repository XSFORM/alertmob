#!/bin/bash
# ============================================================
# AlertMob — установка бота на чистый сервер
# Один файл: bash setup.sh
# OpenVPN ставится потом через кнопку в боте
#
# Варианты запуска:
#   curl -sSL .../setup.sh | bash
#   curl -sSL .../setup.sh | bash -s -- TOKEN ADMIN_ID
#   bash setup.sh
#   bash setup.sh TOKEN ADMIN_ID
# ============================================================
set -e

echo "======================================"
echo "  AlertMob — Setup"
echo "======================================"
echo ""

# --- 1. Зависимости ---
echo "[1/3] Устанавливаю зависимости..."
apt update -qq
apt install -y -qq python3 python3-pip curl git > /dev/null 2>&1
pip install python-telegram-bot --break-system-packages -q 2>/dev/null || pip install python-telegram-bot -q
echo "  ✓ Python + telegram-bot + git установлены"

# --- 2. Скачать бота ---
echo "[2/3] Скачиваю бота..."
if [ -d "/root/alertmob" ]; then
    cd /root/alertmob && git pull -q
else
    git clone -q https://github.com/XSFORM/alertmob.git /root/alertmob
fi
echo "  ✓ alertmob готов"

# --- 3. Настройка ---
echo "[3/3] Настройка Telegram бота..."
echo ""

# Принять из аргументов (bash -s -- TOKEN ID) или спросить
BOT_TOKEN="${1:-}"
ADMIN_ID="${2:-}"

# Если не переданы — спросить интерактивно через /dev/tty
# (работает даже когда stdin занят пайпом от curl)
if [ -z "$BOT_TOKEN" ] || [ -z "$ADMIN_ID" ]; then
    if [ ! -t 0 ] && [ ! -e /dev/tty ]; then
        echo "  ❌ Не могу спросить TOKEN/ID — stdin занят пайпом и /dev/tty недоступен."
        echo "  Используй: curl ... | bash -s -- TOKEN ADMIN_ID"
        exit 1
    fi

    if [ -z "$BOT_TOKEN" ]; then
        while true; do
            read -p "  Введи TOKEN бота: " BOT_TOKEN < /dev/tty
            if [ -z "$BOT_TOKEN" ]; then
                echo "  ❌ Токен не может быть пустым"
            elif [[ "$BOT_TOKEN" != *":"* ]]; then
                echo "  ❌ Неверный формат токена (пример: 123456:ABC...)"
            else
                break
            fi
        done
    fi

    if [ -z "$ADMIN_ID" ]; then
        while true; do
            read -p "  Введи свой Telegram ID: " ADMIN_ID < /dev/tty
            if [[ "$ADMIN_ID" =~ ^[0-9]+$ ]]; then
                break
            else
                echo "  ❌ ID должен быть числом"
            fi
        done
    fi
fi

# Финальная проверка
if [ -z "$BOT_TOKEN" ] || [ -z "$ADMIN_ID" ]; then
    echo "  ❌ TOKEN и ADMIN_ID обязательны!"
    exit 1
fi

# Записать config.json
cat > /root/alertmob/config.json << EOF
{
  "token": "${BOT_TOKEN}",
  "admin_id": ${ADMIN_ID}
}
EOF
echo "  ✓ Конфиг сохранён"

# --- Systemd ---
cp /root/alertmob/alertmob.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now alertmob

echo ""
echo "======================================"
echo "  ✓ Бот запущен!"
echo ""
echo "  Открой бота в Telegram → /start"
echo "  Нажми «Установить OpenVPN»"
echo "  Всё остальное — через бота."
echo "======================================"
