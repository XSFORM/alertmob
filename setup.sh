#!/bin/bash
# ============================================================
# AlertMob — установка бота на чистый сервер
# Один файл: bash setup.sh
# OpenVPN ставится потом через кнопку в боте
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

# Запросить TOKEN
while true; do
    read -p "  Введи TOKEN бота: " BOT_TOKEN
    if [ -z "$BOT_TOKEN" ]; then
        echo "  ❌ Токен не может быть пустым"
    else
        break
    fi
done

# Запросить ADMIN_ID
while true; do
    read -p "  Введи свой Telegram ID: " ADMIN_ID
    if [[ "$ADMIN_ID" =~ ^[0-9]+$ ]]; then
        break
    else
        echo "  ❌ ID должен быть числом"
    fi
done

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
echo "  Бот запущен!"
echo ""
echo "  Открой бота в Telegram → /start"
echo "  Нажми «Установить OpenVPN»"
echo "  Всё остальное — через бота."
echo "======================================"
