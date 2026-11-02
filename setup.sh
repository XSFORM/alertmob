#!/bin/bash
# ============================================================
# AlertMob — быстрая установка OpenVPN + Telegram бот
# Запуск: bash setup.sh
# ============================================================
set -e

echo "======================================"
echo "  AlertMob — Setup"
echo "======================================"
echo ""

# --- 1. Системные пакеты ---
echo "[1/4] Устанавливаю зависимости..."
apt update -qq
apt install -y -qq python3 python3-pip curl > /dev/null 2>&1
pip install python-telegram-bot --break-system-packages -q 2>/dev/null || pip install python-telegram-bot -q
echo "  ✓ Python + telegram-bot установлены"

# --- 2. Скачать angristan ---
echo "[2/4] Скачиваю OpenVPN installer..."
curl -sSL -o /root/openvpn-install.sh https://raw.githubusercontent.com/angristan/openvpn-install/master/openvpn-install.sh
chmod +x /root/openvpn-install.sh
echo "  ✓ openvpn-install.sh готов"

# --- 3. Настройка бота ---
echo "[3/4] Настройка Telegram бота..."
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

# Записать config.env
cat > /root/alertmob_config.env << EOF
BOT_TOKEN=${BOT_TOKEN}
ADMIN_ID=${ADMIN_ID}
EOF
echo "  ✓ Конфиг сохранён в /root/alertmob_config.env"

# --- 4. Установка OpenVPN ---
echo "[4/4] Запускаю установку OpenVPN..."
echo ""
echo "  Выбирай: tls-crypt-v2, AES-128-GCM, ECDSA, UDP 1194"
echo "  ─────────────────────────────────────────────────────"
echo ""
bash /root/openvpn-install.sh interactive

echo ""
echo "======================================"
echo "  Установка завершена!"
echo ""
echo "  Запуск бота:"
echo "    python3 /root/bot.py"
echo ""
echo "  Фоновый запуск:"
echo "    nohup python3 /root/bot.py &> /root/bot.log &"
echo ""
echo "  Systemd (автозапуск):"
echo "    cp alertmob.service /etc/systemd/system/"
echo "    systemctl enable --now alertmob"
echo "======================================"
