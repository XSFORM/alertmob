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

# --- 1. Зависимости ---
echo "[1/4] Проверяю репозитории..."

# Фикс для старых Debian (bullseye и т.д.) — security.debian.org отдаёт 404
# Переключаем на архивное зеркало
fix_repos() {
    local changed=0
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
        [ -f "$f" ] || continue
        if grep -q "security.debian.org" "$f" 2>/dev/null; then
            sed -i 's|http://security.debian.org/debian-security|http://deb.debian.org/debian-security|g' "$f"
            changed=1
        fi
    done
    if [ "$changed" -eq 1 ]; then
        echo "  ✓ Репозитории обновлены"
    fi
}

# Пробуем apt update, если 404 — чиним
apt update -qq 2>&1 | tail -1
if [ ${PIPESTATUS[0]} -ne 0 ]; then
    echo "  Репозитории устарели, исправляю..."
    fix_repos
    apt update -qq 2>&1 | tail -1
fi

echo "[2/4] Устанавливаю зависимости..."

# python3 и curl скорее всего есть, git и pip — нет
# Ставим только то что реально нужно, без -dev пакетов
apt install -y git python3-pip 2>&1 | tail -5
if ! command -v git &>/dev/null; then
    echo "  ❌ Не удалось установить git"
    echo "  Попробуй вручную: apt update && apt install -y git python3-pip"
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

# Принять из аргументов (bash -s -- TOKEN ID) или спросить
BOT_TOKEN="${1:-}"
ADMIN_ID="${2:-}"

# Если не переданы — спросить интерактивно через /dev/tty
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
