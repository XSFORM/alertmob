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

echo "======================================"
echo "  AlertMob — Setup"
echo "======================================"
echo ""

# --- 1. Фикс репозиториев (Debian EOL) ---
echo "[1/4] Проверяю репозитории..."

# Определяем кодовое имя дистрибутива
CODENAME=$(. /etc/os-release 2>/dev/null && echo "$VERSION_CODENAME")

# Debian bullseye и старше — security.debian.org больше не работает
# Переключаем на archive.debian.org
fix_eol_repos() {
    echo "  Debian $CODENAME — переключаю на archive.debian.org..."
    # Бэкап
    cp /etc/apt/sources.list /etc/apt/sources.list.bak 2>/dev/null

    # Заменяем все зеркала на archive
    sed -i 's|http://security.debian.org[^ ]*|http://archive.debian.org/debian-security|g' /etc/apt/sources.list
    sed -i 's|http://deb.debian.org/debian |http://archive.debian.org/debian |g' /etc/apt/sources.list
    sed -i 's|http://httpredir.debian.org/debian |http://archive.debian.org/debian |g' /etc/apt/sources.list

    # Тоже в sources.list.d
    for f in /etc/apt/sources.list.d/*.list; do
        [ -f "$f" ] || continue
        sed -i 's|http://security.debian.org[^ ]*|http://archive.debian.org/debian-security|g' "$f"
        sed -i 's|http://deb.debian.org/debian |http://archive.debian.org/debian |g' "$f"
    done
    echo "  ✓ Репозитории переключены на архив"
}

# Пробуем обновить; если 404 — чиним
apt_ok=0
if apt update -qq 2>&1 | grep -qi "404\|failed to fetch"; then
    fix_eol_repos
    apt -o Acquire::Check-Valid-Until=false update -qq 2>&1 | tail -1
    apt_ok=$?
else
    apt_ok=0
fi

# Для archive.debian.org нужен этот флаг постоянно
if grep -q "archive.debian.org" /etc/apt/sources.list 2>/dev/null; then
    echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99no-check-valid
fi

echo "  ✓ Репозитории готовы"

# --- 2. Зависимости ---
echo "[2/4] Устанавливаю зависимости..."

apt install -y git python3-pip 2>&1 | tail -3
if ! command -v git &>/dev/null; then
    echo "  ❌ Не удалось установить git"
    echo "  Проверь вывод выше и попробуй: apt install -y git python3-pip"
    exit 1
fi

pip install python-telegram-bot --break-system-packages -q 2>/dev/null \
  || pip install python-telegram-bot -q 2>/dev/null \
  || pip3 install python-telegram-bot --break-system-packages -q 2>/dev/null \
  || pip3 install python-telegram-bot -q 2>/dev/null
if ! python3 -c "import telegram" 2>/dev/null; then
    echo "  ❌ Не удалось установить python-telegram-bot"
    exit 1
fi
echo "  ✓ Python + telegram-bot + git установлены"

# --- 3. Скачать бота ---
echo "[3/4] Скачиваю бота..."
if [ -d "/root/alertmob" ]; then
    cd /root/alertmob && git pull -q
else
    git clone -q https://github.com/XSFORM/alertmob.git /root/alertmob
fi
if [ ! -f "/root/alertmob/bot.py" ]; then
    echo "  ❌ Ошибка скачивания бота"
    exit 1
fi
echo "  ✓ alertmob готов"

# --- 4. Настройка ---
echo "[4/4] Настройка Telegram бота..."
echo ""

BOT_TOKEN="${1:-}"
ADMIN_ID="${2:-}"

if [ -z "$BOT_TOKEN" ] || [ -z "$ADMIN_ID" ]; then
    if [ ! -e /dev/tty ]; then
        echo "  ❌ Не могу спросить TOKEN/ID — /dev/tty недоступен."
        echo "  Используй: curl ... | bash -s -- TOKEN ADMIN_ID"
        exit 1
    fi

    if [ -z "$BOT_TOKEN" ]; then
        while true; do
            read -p "  Введи TOKEN бота: " BOT_TOKEN < /dev/tty
            if [ -z "$BOT_TOKEN" ]; then
                echo "  ❌ Токен не может быть пустым"
            elif [[ "$BOT_TOKEN" != *":"* ]]; then
                echo "  ❌ Неверный формат (пример: 123456:ABC...)"
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

if [ -z "$BOT_TOKEN" ] || [ -z "$ADMIN_ID" ]; then
    echo "  ❌ TOKEN и ADMIN_ID обязательны!"
    exit 1
fi

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
echo "  OpenVPN должен быть уже установлен."
echo "  Управление клиентами — через бота."
echo "======================================"
