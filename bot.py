#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AlertMob — Mini OpenVPN Telegram Bot
Управление клиентами OpenVPN tls-crypt-v2 через Telegram.
Первый запуск: интерактивная настройка TOKEN + ADMIN_ID.
"""

import asyncio
import json
import os
import subprocess
import sys
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# ======================== ПУТИ ========================
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
INSTALL_SCRIPT = "/root/openvpn-install.sh"
OVPN_DIR = "/root"
SERVER_CONF = "/etc/openvpn/server/server.conf"
EASYRSA_DIR = "/etc/openvpn/server/easy-rsa"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("alertmob")


# ======================== КОНФИГ ========================

def load_config() -> dict:
    """Загрузить конфиг из JSON. Если нет — интерактивная настройка."""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        if cfg.get("token") and cfg.get("admin_id"):
            return cfg

    # Проверить config.env (от setup.sh)
    env_path = "/root/alertmob_config.env"
    if os.path.exists(env_path):
        token, admin_id = None, None
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                elif line.startswith("ADMIN_ID="):
                    try:
                        admin_id = int(line.split("=", 1)[1].strip())
                    except ValueError:
                        pass
        if token and admin_id:
            cfg = {"token": token, "admin_id": admin_id}
            save_config(cfg)
            log.info("Конфиг импортирован из config.env")
            return cfg

    # Интерактивная настройка
    print("\n" + "=" * 40)
    print("  AlertMob — Первый запуск")
    print("=" * 40 + "\n")

    while True:
        token = input("  Введи TOKEN бота: ").strip()
        if token and ":" in token:
            break
        print("  ❌ Неверный формат токена (пример: 123456:ABC...)")

    while True:
        raw = input("  Введи свой Telegram ID: ").strip()
        try:
            admin_id = int(raw)
            break
        except ValueError:
            print("  ❌ ID должен быть числом")

    cfg = {"token": token, "admin_id": admin_id}
    save_config(cfg)
    print(f"\n  ✓ Конфиг сохранён: {CONFIG_PATH}\n")
    return cfg


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ======================== OPENVPN ========================

def openvpn_installed() -> bool:
    return os.path.exists(SERVER_CONF)


def get_public_ip() -> str:
    try:
        import urllib.request
        return urllib.request.urlopen("https://api.ipify.org", timeout=5).read().decode().strip()
    except Exception:
        return "0.0.0.0"


def list_clients() -> list:
    """Список клиентов через angristan CLI, fallback на index.txt."""
    # Метод 1: CLI
    try:
        r = subprocess.run(
            ["bash", INSTALL_SCRIPT, "client", "list"],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0 and r.stdout.strip():
            clients = []
            for line in r.stdout.strip().splitlines():
                line = line.strip()
                # Пропускаем заголовки, инфо-строки и пустые
                if not line or line.startswith("===") or line.startswith("[") or line.startswith("---") or line.startswith("Name"):
                    continue
                # Формат: "name  Valid  2036-10-30  3649 days"
                parts = line.split()
                if parts and parts[0] != "server":
                    clients.append(parts[0])
            if clients:
                return sorted(set(clients))
    except Exception:
        pass

    # Метод 2: index.txt
    index = os.path.join(EASYRSA_DIR, "pki/index.txt")
    if not os.path.exists(index):
        return []
    clients = []
    with open(index) as f:
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) >= 6 and parts[0] == "V":
                cn = parts[5].split("CN=")[-1] if "CN=" in parts[5] else ""
                if cn and cn != "server":
                    clients.append(cn)
    return sorted(set(clients))


def create_client(name: str) -> tuple:
    """Создать клиента. → (success, message, ovpn_path)"""
    if not openvpn_installed():
        return False, "OpenVPN не установлен!", None

    try:
        # "1" = без пароля (ответ на интерактивный вопрос)
        r = subprocess.run(
            ["bash", INSTALL_SCRIPT, "client", "add", name],
            input="1\n", capture_output=True, text=True, timeout=60
        )
        ovpn = os.path.join(OVPN_DIR, f"{name}.ovpn")
        if os.path.exists(ovpn):
            return True, f"Клиент <b>{name}</b> создан", ovpn
        else:
            err = (r.stderr or r.stdout or "нет вывода")[-500:]
            return False, f"Ошибка создания:\n<pre>{err}</pre>", None
    except subprocess.TimeoutExpired:
        return False, "Таймаут (>60 сек)", None
    except Exception as e:
        return False, f"Ошибка: {e}", None


def revoke_client(name: str) -> tuple:
    """Удалить клиента. → (success, message)"""
    if not openvpn_installed():
        return False, "OpenVPN не установлен!"

    try:
        r = subprocess.run(
            ["bash", INSTALL_SCRIPT, "client", "revoke", name],
            capture_output=True, text=True, timeout=60
        )
        ovpn = os.path.join(OVPN_DIR, f"{name}.ovpn")
        if os.path.exists(ovpn):
            os.remove(ovpn)
        return True, f"Клиент <b>{name}</b> удалён"
    except Exception as e:
        return False, f"Ошибка: {e}"


# ======================== МОНИТОРИНГ ========================

STATUS_LOGS = [
    "/var/log/openvpn/status.log",
    "/etc/openvpn/server/openvpn-status.log",
    "/run/openvpn-server/status-server.log",
]


def find_status_log() -> str:
    for p in STATUS_LOGS:
        if os.path.exists(p):
            return p
    return ""


def parse_status_log() -> dict:
    """Парсить status.log (v2/v3 формат с префиксами)."""
    clients = {}
    path = find_status_log()
    if not path:
        return clients

    try:
        with open(path) as f:
            for line in f:
                line = line.strip()

                # CLIENT_LIST,name,real_addr,virtual_addr,ipv6,bytes_rx,bytes_tx,connected,...
                if line.startswith("CLIENT_LIST,"):
                    parts = line.split(",")
                    if len(parts) >= 7:
                        name = parts[1]
                        real_addr = parts[2].replace("udp4:", "").replace("tcp4:", "").split(":")[0]
                        vaddr = parts[3]
                        try:
                            rx = int(parts[5])
                        except (ValueError, IndexError):
                            rx = 0
                        try:
                            tx = int(parts[6])
                        except (ValueError, IndexError):
                            tx = 0
                        connected = parts[7] if len(parts) > 7 else ""
                        clients[name] = {
                            "real_addr": real_addr,
                            "virtual_addr": vaddr,
                            "rx": rx,
                            "tx": tx,
                            "connected": connected,
                        }

                # Fallback: старый формат без префиксов
                elif not line.startswith(("HEADER,", "ROUTING_TABLE,", "GLOBAL_STATS,",
                                          "TITLE,", "TIME,", "END")):
                    parts = line.split(",")
                    if len(parts) >= 5 and parts[0] not in ("Common Name", "Virtual Address", ""):
                        # Может быть строка клиента старого формата
                        name = parts[0]
                        if "." in parts[1] or ":" in parts[1]:  # похоже на адрес
                            try:
                                clients[name] = {
                                    "real_addr": parts[1].split(":")[0],
                                    "virtual_addr": "",
                                    "rx": int(parts[2]) if parts[2].isdigit() else 0,
                                    "tx": int(parts[3]) if parts[3].isdigit() else 0,
                                    "connected": parts[4] if len(parts) > 4 else "",
                                }
                            except (ValueError, IndexError):
                                pass

        # Добить virtual_addr из ROUTING_TABLE
        with open(path) as f:
            for line in f:
                if line.startswith("ROUTING_TABLE,"):
                    parts = line.split(",")
                    if len(parts) >= 3:
                        vaddr = parts[1]
                        name = parts[2]
                        if name in clients and not clients[name].get("virtual_addr"):
                            clients[name]["virtual_addr"] = vaddr

    except Exception as e:
        log.error(f"Ошибка чтения status.log: {e}")

    return clients


def fmt_bytes(b: int) -> str:
    """Форматировать байты в читаемый вид."""
    if b < 1024:
        return f"{b} B"
    elif b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    elif b < 1024 * 1024 * 1024:
        return f"{b / (1024*1024):.1f} MB"
    else:
        return f"{b / (1024*1024*1024):.2f} GB"


def get_server_stats() -> str:
    """Статистика сервера: uptime, нагрузка, трафик интерфейса."""
    lines = []

    # Uptime
    try:
        with open("/proc/uptime") as f:
            sec = int(float(f.read().split()[0]))
            d, rem = divmod(sec, 86400)
            h, rem = divmod(rem, 3600)
            m = rem // 60
            lines.append(f"Uptime: {d}d {h}h {m}m")
    except Exception:
        pass

    # Load average
    try:
        with open("/proc/loadavg") as f:
            load = f.read().split()[:3]
            lines.append(f"Load: {' '.join(load)}")
    except Exception:
        pass

    # tun0 traffic
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                if "tun0" in line:
                    parts = line.split()
                    rx = int(parts[1])
                    tx = int(parts[9])
                    lines.append(f"tun0 RX: {fmt_bytes(rx)} | TX: {fmt_bytes(tx)}")
                    break
    except Exception:
        pass

    return "\n".join(lines) if lines else "Нет данных"


# ======================== TELEGRAM BOT ========================

ADMIN_ID = 0  # Заполняется при старте


def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await show_menu(update, ctx)


async def show_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE, edit=False):
    installed = openvpn_installed()
    clients = list_clients() if installed else []

    if installed:
        status = f"OpenVPN: работает\nКлиентов: {len(clients)}"
    else:
        status = "OpenVPN: не установлен"

    text = f"<b>AlertMob VPN</b>\n\n{status}"

    buttons = []
    if not installed:
        buttons.append([InlineKeyboardButton("Установить OpenVPN", callback_data="install")])
    else:
        buttons.append([
            InlineKeyboardButton("Создать", callback_data="create"),
            InlineKeyboardButton("Удалить", callback_data="delete"),
        ])
        buttons.append([
            InlineKeyboardButton("Список", callback_data="list"),
            InlineKeyboardButton("Отправить .ovpn", callback_data="send"),
        ])
        buttons.append([
            InlineKeyboardButton("Мониторинг", callback_data="monitor"),
        ])

    kb = InlineKeyboardMarkup(buttons)

    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await update.callback_query.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        msg = update.callback_query.message if update.callback_query else update.message
        await msg.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    q = update.callback_query
    await q.answer()
    data = q.data

    # --- Установка ---
    if data == "install":
        await q.edit_message_text(
            "Запускаю установку OpenVPN...\n\n"
            "Используй <code>bash /root/openvpn-install.sh interactive</code> "
            "в терминале для интерактивной настройки.\n\n"
            "После установки нажми /start",
            parse_mode="HTML"
        )
        return

    # --- Создать ---
    if data == "create":
        ctx.user_data["action"] = "create_name"
        await q.edit_message_text("Введи имя клиента (латиница, без пробелов):")
        return

    # --- Удалить ---
    if data == "delete":
        clients = list_clients()
        if not clients:
            await q.edit_message_text("Нет клиентов.")
            await asyncio.sleep(1)
            await show_menu(update, ctx)
            return
        buttons = [[InlineKeyboardButton(f"× {c}", callback_data=f"del:{c}")] for c in clients]
        buttons.append([InlineKeyboardButton("Назад", callback_data="back")])
        await q.edit_message_text(
            "Выбери клиента для удаления:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("del:"):
        name = data[4:]
        ok, msg = revoke_client(name)
        await q.edit_message_text(msg, parse_mode="HTML")
        await asyncio.sleep(1)
        await show_menu(update, ctx)
        return

    # --- Список ---
    if data == "list":
        clients = list_clients()
        if clients:
            text = "<b>Клиенты:</b>\n\n" + "\n".join(f"  {c}" for c in clients)
            text += f"\n\nВсего: {len(clients)}"
        else:
            text = "Нет клиентов."
        buttons = [[InlineKeyboardButton("Назад", callback_data="back")]]
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    # --- Отправить ---
    if data == "send":
        clients = list_clients()
        if not clients:
            await q.edit_message_text("Нет клиентов.")
            await asyncio.sleep(1)
            await show_menu(update, ctx)
            return
        buttons = [[InlineKeyboardButton(c, callback_data=f"sf:{c}")] for c in clients]
        buttons.append([InlineKeyboardButton("Назад", callback_data="back")])
        await q.edit_message_text(
            "Выбери ключ для отправки:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("sf:"):
        name = data[3:]
        ovpn = os.path.join(OVPN_DIR, f"{name}.ovpn")
        if os.path.exists(ovpn):
            await q.message.reply_document(
                document=InputFile(open(ovpn, "rb"), filename=f"{name}.ovpn"),
                caption=f"{name}.ovpn"
            )
        else:
            await q.message.reply_text(f"Файл {name}.ovpn не найден в {OVPN_DIR}")
        await show_menu(update, ctx)
        return

    # --- Мониторинг ---
    if data == "monitor":
        online = parse_status_log()
        all_clients = list_clients()

        lines = []
        total_rx, total_tx = 0, 0

        for c in sorted(all_clients):
            if c in online:
                info = online[c]
                vip = info.get("virtual_addr", "?")
                rx, tx = info.get("rx", 0), info.get("tx", 0)
                total_rx += rx
                total_tx += tx
                lines.append(f"  🟢 <b>{c}</b>  {vip}  ↓{fmt_bytes(rx)} ↑{fmt_bytes(tx)}")
            else:
                lines.append(f"  🔴 {c}")

        client_block = "\n".join(lines) if lines else "Нет клиентов"
        server = get_server_stats()

        text = (
            f"<b>Мониторинг</b>\n\n"
            f"Online: {len(online)} / {len(all_clients)}\n"
            f"Трафик сессии: ↓{fmt_bytes(total_rx)} ↑{fmt_bytes(total_tx)}\n\n"
            f"{client_block}\n\n"
            f"<b>Сервер:</b>\n{server}"
        )
        buttons = [
            [InlineKeyboardButton("Обновить", callback_data="monitor")],
            [InlineKeyboardButton("Назад", callback_data="back")],
        ]
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    # --- Назад ---
    if data == "back":
        await show_menu(update, ctx, edit=True)
        return


async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    action = ctx.user_data.get("action")

    if action == "create_name":
        ctx.user_data.pop("action", None)
        name = update.message.text.strip().replace(" ", "_")

        # Валидация имени
        if not name or not name.isascii() or not name.replace("_", "").replace("-", "").isalnum():
            await update.message.reply_text("Имя: только латиница, цифры, дефис, подчёркивание.")
            await show_menu(update, ctx)
            return

        if len(name) > 32:
            await update.message.reply_text("Имя слишком длинное (макс 32 символа).")
            await show_menu(update, ctx)
            return

        # Проверить дубликат
        if name in list_clients():
            await update.message.reply_text(
                f"Клиент <b>{name}</b> уже существует.", parse_mode="HTML"
            )
            await show_menu(update, ctx)
            return

        await update.message.reply_text(f"Создаю <b>{name}</b>...", parse_mode="HTML")

        ok, msg, ovpn_path = create_client(name)
        await update.message.reply_text(msg, parse_mode="HTML")

        if ok and ovpn_path:
            await update.message.reply_document(
                document=InputFile(open(ovpn_path, "rb"), filename=f"{name}.ovpn"),
                caption=f"{name}.ovpn"
            )

        await show_menu(update, ctx)
        return


# ======================== MAIN ========================

def main():
    global ADMIN_ID

    cfg = load_config()
    token = cfg["token"]
    ADMIN_ID = cfg["admin_id"]

    log.info(f"AlertMob запущен | Admin: {ADMIN_ID}")
    log.info(f"OpenVPN: {'да' if openvpn_installed() else 'нет'}")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
