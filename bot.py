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
import html as html_escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, ReplyKeyboardMarkup, KeyboardButton
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
            return False, f"Ошибка создания:\n<pre>{html_escape.escape(err)}</pre>", None
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


# ======================== БЛОКИРОВКА ========================

CCD_DIR = "/etc/openvpn/server/ccd"


def ensure_ccd():
    """Убедиться что CCD включён в конфиге и папка существует."""
    os.makedirs(CCD_DIR, exist_ok=True)
    # Проверить что client-config-dir есть в server.conf
    try:
        with open(SERVER_CONF) as f:
            conf = f.read()
        if "client-config-dir" not in conf:
            with open(SERVER_CONF, "a") as f:
                f.write(f"\nclient-config-dir {CCD_DIR}\n")
            # Перезапустить OpenVPN чтобы подхватил CCD
            subprocess.run(["systemctl", "restart", "openvpn-server@server"], timeout=15)
            log.info("CCD добавлен в server.conf, OpenVPN перезапущен")
    except Exception as e:
        log.error(f"ensure_ccd: {e}")


def is_client_disabled(name: str) -> bool:
    """Проверить отключён ли клиент."""
    ccd_file = os.path.join(CCD_DIR, name)
    if not os.path.exists(ccd_file):
        return False
    with open(ccd_file) as f:
        return "disable" in f.read()


def disable_client(name: str) -> tuple:
    """Отключить клиента (disable в CCD)."""
    ensure_ccd()
    try:
        ccd_file = os.path.join(CCD_DIR, name)
        with open(ccd_file, "w") as f:
            f.write("disable\n")
        # Кикнуть если онлайн — через management или kill
        kick_client(name)
        return True, f"Клиент <b>{name}</b> отключён"
    except Exception as e:
        return False, f"Ошибка: {e}"


def enable_client(name: str) -> tuple:
    """Включить клиента (убрать disable из CCD)."""
    try:
        ccd_file = os.path.join(CCD_DIR, name)
        if os.path.exists(ccd_file):
            os.remove(ccd_file)
        return True, f"Клиент <b>{name}</b> включён"
    except Exception as e:
        return False, f"Ошибка: {e}"


def kick_client(name: str):
    """Принудительно отключить клиента если он онлайн."""
    try:
        # Через management interface (если включён)
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect(("127.0.0.1", 7505))
        sock.recv(1024)
        sock.send(f"kill {name}\n".encode())
        sock.recv(1024)
        sock.close()
    except Exception:
        # Fallback: найти PID клиента и убить через ipp.txt не получится,
        # но disable сработает при следующем реконнекте
        pass


def get_disabled_clients() -> set:
    """Список отключённых клиентов."""
    disabled = set()
    if not os.path.exists(CCD_DIR):
        return disabled
    for name in os.listdir(CCD_DIR):
        if is_client_disabled(name):
            disabled.add(name)
    return disabled


# ======================== БЭКАП / ВОССТАНОВЛЕНИЕ ========================

import tarfile
import datetime

BACKUP_DIR = "/root/alertmob_backups"


def create_backup() -> tuple:
    """Создать бэкап OpenVPN + бота → tar.gz. Возвращает (success, msg, path)."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"alertmob_backup_{ts}.tar.gz")

    dirs_to_backup = [
        "/etc/openvpn/server",        # конфиги, сертификаты, easy-rsa, ccd
        "/root/alertmob/config.json",  # конфиг бота
    ]
    # Все .ovpn файлы в /root/
    ovpn_files = [f"/root/{f}" for f in os.listdir("/root") if f.endswith(".ovpn")]

    try:
        with tarfile.open(backup_path, "w:gz") as tar:
            for path in dirs_to_backup:
                if os.path.exists(path):
                    tar.add(path)
            for f in ovpn_files:
                if os.path.exists(f):
                    tar.add(f)
            # openvpn-install.sh
            if os.path.exists(INSTALL_SCRIPT):
                tar.add(INSTALL_SCRIPT)

        size = os.path.getsize(backup_path)
        return True, f"Бэкап создан ({fmt_bytes(size)})", backup_path
    except Exception as e:
        return False, f"Ошибка: {e}", None


def restore_backup(backup_path: str) -> tuple:
    """Восстановить из tar.gz бэкапа."""
    if not os.path.exists(backup_path):
        return False, "Файл не найден"

    try:
        with tarfile.open(backup_path, "r:gz") as tar:
            # Проверить что внутри есть /etc/openvpn
            members = tar.getnames()
            has_openvpn = any("openvpn" in m for m in members)
            if not has_openvpn:
                return False, "Не похоже на бэкап OpenVPN"
            tar.extractall("/", filter="data")

        # Перезапустить OpenVPN
        subprocess.run(["systemctl", "restart", "openvpn-server@server"], timeout=15)
        return True, "Восстановлено. OpenVPN перезапущен."
    except Exception as e:
        return False, f"Ошибка: {e}"


def list_backups() -> list:
    """Список бэкапов."""
    if not os.path.exists(BACKUP_DIR):
        return []
    files = []
    for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if f.endswith(".tar.gz"):
            path = os.path.join(BACKUP_DIR, f)
            size = os.path.getsize(path)
            files.append({"name": f, "path": path, "size": size})
    return files


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


BOTTOM_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Создать"), KeyboardButton("Удалить")],
        [KeyboardButton("Вкл/Выкл"), KeyboardButton("Мониторинг")],
        [KeyboardButton("Список"), KeyboardButton("Отправить .ovpn")],
        [KeyboardButton("Бэкап"), KeyboardButton("Обновить бот")],
    ],
    resize_keyboard=True
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text("Клавиатура активна", reply_markup=BOTTOM_KB)
    await show_menu(update, ctx)


async def handle_quick(update: Update, ctx: ContextTypes.DEFAULT_TYPE, action: str):
    """Обработка быстрых кнопок снизу (ReplyKeyboard)."""
    msg = update.message

    if action == "create":
        ctx.user_data["action"] = "create_name"
        await msg.reply_text("Введи имя клиента (латиница, без пробелов):")
        return

    if action == "delete":
        clients = list_clients()
        if not clients:
            await msg.reply_text("Нет клиентов.")
            return
        buttons = [[InlineKeyboardButton(f"× {c}", callback_data=f"del:{c}")] for c in clients]
        buttons.append([InlineKeyboardButton("Назад", callback_data="back")])
        await msg.reply_text("Выбери клиента:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if action == "list":
        clients = list_clients()
        if clients:
            text = "<b>Клиенты:</b>\n\n" + "\n".join(f"  {c}" for c in clients)
            text += f"\n\nВсего: {len(clients)}"
        else:
            text = "Нет клиентов."
        await msg.reply_text(text, parse_mode="HTML")
        return

    if action == "toggle":
        clients = list_clients()
        if not clients:
            await msg.reply_text("Нет клиентов.")
            return
        disabled = get_disabled_clients()
        buttons = []
        for c in clients:
            if c in disabled:
                buttons.append([InlineKeyboardButton(f"🔴 {c} → Включить", callback_data=f"enable:{c}")])
            else:
                buttons.append([InlineKeyboardButton(f"🟢 {c} → Отключить", callback_data=f"disable:{c}")])
        buttons.append([InlineKeyboardButton("Назад", callback_data="back")])
        await msg.reply_text("Вкл/Выкл клиентов:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if action == "monitor":
        online = parse_status_log()
        all_clients = list_clients()
        disabled = get_disabled_clients()
        lines = []
        total_rx, total_tx = 0, 0
        for c in sorted(all_clients):
            if c in disabled:
                lines.append(f"  ⛔ {c}  (отключён)")
            elif c in online:
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
            f"<b>Мониторинг</b>\n\nOnline: {len(online)} / {len(all_clients)}\n"
            f"Трафик сессии: ↓{fmt_bytes(total_rx)} ↑{fmt_bytes(total_tx)}\n\n"
            f"{client_block}\n\n<b>Сервер:</b>\n{server}"
        )
        buttons = [[InlineKeyboardButton("Обновить", callback_data="monitor")]]
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    if action == "send":
        clients = list_clients()
        if not clients:
            await msg.reply_text("Нет клиентов.")
            return
        buttons = [[InlineKeyboardButton(c, callback_data=f"sf:{c}")] for c in clients]
        await msg.reply_text("Выбери ключ:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if action == "backup":
        buttons = [
            [InlineKeyboardButton("Создать бэкап", callback_data="backup_create")],
            [InlineKeyboardButton("Скачать последний", callback_data="backup_download")],
            [InlineKeyboardButton("Восстановить", callback_data="backup_restore")],
            [InlineKeyboardButton("Назад", callback_data="back")],
        ]
        backups = list_backups()
        text = f"<b>Бэкап</b>\n\nСохранённых: {len(backups)}"
        if backups:
            text += f"\nПоследний: {backups[0]['name']} ({fmt_bytes(backups[0]['size'])})"
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    if action == "update":
        await msg.reply_text("Обновляю бот...")
        try:
            bot_dir = os.path.dirname(os.path.abspath(__file__))
            r = subprocess.run(
                ["git", "pull"], cwd=bot_dir,
                capture_output=True, text=True, timeout=30
            )
            output = (r.stdout + r.stderr).strip()[-300:]
            if "Already up to date" in output or "Already up-to-date" in output:
                await msg.reply_text("Уже последняя версия.")
            else:
                await msg.reply_text(f"<pre>{html_escape.escape(output)}</pre>\n\nПерезапускаю...", parse_mode="HTML")
                subprocess.Popen(["systemctl", "restart", "alertmob"])
        except Exception as e:
            await msg.reply_text(f"Ошибка: {e}")
        return


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
            InlineKeyboardButton("Обновить бот", callback_data="update"),
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

    # --- Установка (пошаговый мастер) ---
    if data == "install":
        ip = get_public_ip()
        ctx.user_data["vpn_setup"] = {"endpoint": ip}
        await q.edit_message_text(
            f"<b>Установка OpenVPN</b>\n\n"
            f"IP сервера: <code>{ip}</code>\n\n"
            f"Выбери протокол:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("UDP (рекомендуется)", callback_data="vs:protocol:udp")],
                [InlineKeyboardButton("TCP", callback_data="vs:protocol:tcp")],
                [InlineKeyboardButton("Отмена", callback_data="back")],
            ])
        )
        return

    if data.startswith("vs:"):
        setup = ctx.user_data.get("vpn_setup")
        if not setup:
            await show_menu(update, ctx, edit=True)
            return
        parts = data.split(":", 2)
        step = parts[1]
        val = parts[2] if len(parts) > 2 else ""

        # --- Шаг 1: протокол → порт ---
        if step == "protocol":
            setup["protocol"] = val
            await q.edit_message_text(
                f"Протокол: <b>{val.upper()}</b>\n\nВыбери порт:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("1194 (стандарт)", callback_data="vs:port:1194")],
                    [InlineKeyboardButton("443 (обход блокировок)", callback_data="vs:port:443")],
                    [InlineKeyboardButton("Случайный", callback_data="vs:port:random")],
                    [InlineKeyboardButton("Отмена", callback_data="back")],
                ])
            )
            return

        # --- Шаг 2: порт → DNS ---
        if step == "port":
            setup["port"] = val
            port_label = "случайный" if val == "random" else val
            await q.edit_message_text(
                f"Порт: <b>{port_label}</b>\n\nВыбери DNS:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("Cloudflare", callback_data="vs:dns:cloudflare"),
                        InlineKeyboardButton("Google", callback_data="vs:dns:google"),
                    ],
                    [
                        InlineKeyboardButton("Quad9", callback_data="vs:dns:quad9"),
                        InlineKeyboardButton("OpenDNS", callback_data="vs:dns:opendns"),
                    ],
                    [
                        InlineKeyboardButton("AdGuard", callback_data="vs:dns:adguard"),
                        InlineKeyboardButton("Системный", callback_data="vs:dns:system"),
                    ],
                    [InlineKeyboardButton("Отмена", callback_data="back")],
                ])
            )
            return

        # --- Шаг 3: DNS → шифр ---
        if step == "dns":
            setup["dns"] = val
            await q.edit_message_text(
                f"DNS: <b>{val}</b>\n\nВыбери шифрование:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("AES-128-GCM (быстрый)", callback_data="vs:cipher:AES-128-GCM")],
                    [InlineKeyboardButton("AES-256-GCM (надёжный)", callback_data="vs:cipher:AES-256-GCM")],
                    [InlineKeyboardButton("CHACHA20-POLY1305", callback_data="vs:cipher:CHACHA20-POLY1305")],
                    [InlineKeyboardButton("Отмена", callback_data="back")],
                ])
            )
            return

        # --- Шаг 4: шифр → тип сертификата ---
        if step == "cipher":
            setup["cipher"] = val
            await q.edit_message_text(
                f"Шифр: <b>{val}</b>\n\nТип сертификата:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("ECDSA (быстрый, современный)", callback_data="vs:cert:ecdsa")],
                    [InlineKeyboardButton("RSA (совместимый)", callback_data="vs:cert:rsa")],
                    [InlineKeyboardButton("Отмена", callback_data="back")],
                ])
            )
            return

        # --- Шаг 5: сертификат → TLS защита ---
        if step == "cert":
            setup["cert_type"] = val
            await q.edit_message_text(
                f"Сертификат: <b>{val.upper()}</b>\n\nЗащита TLS канала:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("tls-crypt-v2 (максимум)", callback_data="vs:tls:crypt-v2")],
                    [InlineKeyboardButton("tls-crypt", callback_data="vs:tls:crypt")],
                    [InlineKeyboardButton("tls-auth", callback_data="vs:tls:auth")],
                    [InlineKeyboardButton("Отмена", callback_data="back")],
                ])
            )
            return

        # --- Шаг 6: TLS → имя клиента ---
        if step == "tls":
            setup["tls_sig"] = val
            ctx.user_data["action"] = "vpn_client_name"
            await q.edit_message_text(
                f"TLS: <b>{val}</b>\n\n"
                f"Введи имя первого клиента (латиница, без пробелов).\n"
                f"Или нажми кнопку для стандартного имени:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("first-client", callback_data="vs:client:first-client")],
                    [InlineKeyboardButton("Отмена", callback_data="back")],
                ])
            )
            return

        # --- Шаг 7: имя клиента → подтверждение ---
        if step == "client":
            setup["client"] = val
            ctx.user_data.pop("action", None)
            summary = (
                f"<b>Параметры установки:</b>\n\n"
                f"IP: <code>{setup['endpoint']}</code>\n"
                f"Протокол: <b>{setup['protocol'].upper()}</b>\n"
                f"Порт: <b>{'случайный' if setup['port'] == 'random' else setup['port']}</b>\n"
                f"DNS: <b>{setup['dns']}</b>\n"
                f"Шифр: <b>{setup['cipher']}</b>\n"
                f"Сертификат: <b>{setup['cert_type'].upper()}</b>\n"
                f"TLS: <b>{setup['tls_sig']}</b>\n"
                f"Клиент: <b>{setup['client']}</b>\n\n"
                f"Начать установку?"
            )
            await q.edit_message_text(
                summary,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Установить", callback_data="vs:confirm:yes")],
                    [InlineKeyboardButton("Отмена", callback_data="back")],
                ])
            )
            return

        # --- Шаг 8: подтверждение → установка ---
        if step == "confirm" and val == "yes":
            await q.edit_message_text("Скачиваю скрипт и запускаю установку...\nЭто займёт 1-3 минуты.")

            try:
                # Скачать скрипт
                subprocess.run(
                    ["curl", "-sSL", "-o", INSTALL_SCRIPT,
                     "https://raw.githubusercontent.com/angristan/openvpn-install/master/openvpn-install.sh"],
                    capture_output=True, timeout=30
                )
                os.chmod(INSTALL_SCRIPT, 0o755)

                # Собрать аргументы из выбранных параметров
                cmd = ["bash", INSTALL_SCRIPT, "install"]
                cmd += ["--endpoint", setup["endpoint"]]
                cmd += ["--protocol", setup["protocol"]]
                if setup["port"] == "random":
                    cmd += ["--port-random"]
                else:
                    cmd += ["--port", setup["port"]]
                cmd += ["--dns", setup["dns"]]
                cmd += ["--cipher", setup["cipher"]]
                cmd += ["--cert-type", setup["cert_type"]]
                cmd += ["--tls-sig", setup["tls_sig"]]
                cmd += ["--hmac", "SHA256"]
                cmd += ["--client", setup["client"]]

                log.info("OpenVPN install cmd: %s", " ".join(cmd))

                r = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=300
                )

                if openvpn_installed():
                    port_label = "случайный" if setup["port"] == "random" else setup["port"]
                    await q.message.reply_text(
                        f"OpenVPN установлен!\n\n"
                        f"IP: <code>{setup['endpoint']}</code>\n"
                        f"Протокол: {setup['protocol'].upper()}\n"
                        f"Порт: {port_label}\n"
                        f"DNS: {setup['dns']}\n"
                        f"Шифр: {setup['cipher']}\n"
                        f"TLS: {setup['tls_sig']}\n\n"
                        f"Первый клиент: <b>{setup['client']}</b>",
                        parse_mode="HTML"
                    )
                    ovpn = os.path.join(OVPN_DIR, f"{setup['client']}.ovpn")
                    if os.path.exists(ovpn):
                        with open(ovpn, "rb") as f:
                            await q.message.reply_document(
                                document=InputFile(f, filename=f"{setup['client']}.ovpn"),
                                caption=f"{setup['client']}.ovpn"
                            )
                else:
                    err = (r.stderr or r.stdout or "нет вывода")[-800:]
                    await q.message.reply_text(
                        f"Ошибка установки:\n<pre>{html_escape.escape(err)}</pre>",
                        parse_mode="HTML"
                    )
            except subprocess.TimeoutExpired:
                await q.message.reply_text("Таймаут установки (>5 мин)")
            except Exception as e:
                await q.message.reply_text(f"Ошибка: {e}")

            ctx.user_data.pop("vpn_setup", None)
            await show_menu(update, ctx)
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

    # --- Вкл/Выкл ---
    if data == "toggle":
        clients = list_clients()
        if not clients:
            await q.edit_message_text("Нет клиентов.")
            await show_menu(update, ctx)
            return
        disabled = get_disabled_clients()
        buttons = []
        for c in clients:
            if c in disabled:
                buttons.append([InlineKeyboardButton(f"🔴 {c} → Включить", callback_data=f"enable:{c}")])
            else:
                buttons.append([InlineKeyboardButton(f"🟢 {c} → Отключить", callback_data=f"disable:{c}")])
        buttons.append([InlineKeyboardButton("Назад", callback_data="back")])
        await q.edit_message_text("Вкл/Выкл клиентов:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("disable:"):
        name = data[8:]
        ok, msg = disable_client(name)
        await q.edit_message_text(msg, parse_mode="HTML")
        await asyncio.sleep(1)
        # Показать обновлённый список toggle
        clients = list_clients()
        disabled = get_disabled_clients()
        buttons = []
        for c in clients:
            if c in disabled:
                buttons.append([InlineKeyboardButton(f"🔴 {c} → Включить", callback_data=f"enable:{c}")])
            else:
                buttons.append([InlineKeyboardButton(f"🟢 {c} → Отключить", callback_data=f"disable:{c}")])
        buttons.append([InlineKeyboardButton("Назад", callback_data="back")])
        await q.message.reply_text("Вкл/Выкл клиентов:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("enable:"):
        name = data[7:]
        ok, msg = enable_client(name)
        await q.edit_message_text(msg, parse_mode="HTML")
        await asyncio.sleep(1)
        clients = list_clients()
        disabled = get_disabled_clients()
        buttons = []
        for c in clients:
            if c in disabled:
                buttons.append([InlineKeyboardButton(f"🔴 {c} → Включить", callback_data=f"enable:{c}")])
            else:
                buttons.append([InlineKeyboardButton(f"🟢 {c} → Отключить", callback_data=f"disable:{c}")])
        buttons.append([InlineKeyboardButton("Назад", callback_data="back")])
        await q.message.reply_text("Вкл/Выкл клиентов:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # --- Бэкап ---
    if data == "backup_create":
        await q.edit_message_text("Создаю бэкап...")
        ok, msg, path = create_backup()
        await q.message.reply_text(msg, parse_mode="HTML")
        if ok and path:
            try:
                await q.message.reply_document(
                    document=InputFile(open(path, "rb"), filename=os.path.basename(path)),
                    caption="Бэкап OpenVPN"
                )
            except Exception:
                await q.message.reply_text(f"Файл на сервере: <code>{path}</code>", parse_mode="HTML")
        await show_menu(update, ctx)
        return

    if data == "backup_download":
        backups = list_backups()
        if not backups:
            await q.edit_message_text("Нет бэкапов. Сначала создай.")
            await show_menu(update, ctx)
            return
        latest = backups[0]
        try:
            await q.message.reply_document(
                document=InputFile(open(latest["path"], "rb"), filename=latest["name"]),
                caption=f"Последний бэкап ({fmt_bytes(latest['size'])})"
            )
        except Exception as e:
            await q.message.reply_text(f"Ошибка отправки: {e}")
        await show_menu(update, ctx)
        return

    if data == "backup_restore":
        backups = list_backups()
        if not backups:
            await q.edit_message_text("Нет бэкапов.")
            await show_menu(update, ctx)
            return
        buttons = [[InlineKeyboardButton(f"{b['name']} ({fmt_bytes(b['size'])})",
                     callback_data=f"restore:{b['name']}")] for b in backups[:5]]
        buttons.append([InlineKeyboardButton("Назад", callback_data="back")])
        await q.edit_message_text("Выбери бэкап для восстановления:",
                                  reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("restore:"):
        name = data[8:]
        path = os.path.join(BACKUP_DIR, name)
        await q.edit_message_text(f"Восстанавливаю {name}...")
        ok, msg = restore_backup(path)
        await q.message.reply_text(msg, parse_mode="HTML")
        await show_menu(update, ctx)
        return

    # --- Обновить бот ---
    if data == "update":
        await q.edit_message_text("Обновляю бот...")
        try:
            bot_dir = os.path.dirname(os.path.abspath(__file__))
            r = subprocess.run(
                ["git", "pull"], cwd=bot_dir,
                capture_output=True, text=True, timeout=30
            )
            output = (r.stdout + r.stderr).strip()[-300:]
            if "Already up to date" in output or "Already up-to-date" in output:
                await q.message.reply_text("Уже последняя версия.")
                await show_menu(update, ctx)
            else:
                await q.message.reply_text(
                    f"<pre>{html_escape.escape(output)}</pre>\n\nПерезапускаю...", parse_mode="HTML"
                )
                # Перезапуск через systemd
                subprocess.Popen(["systemctl", "restart", "alertmob"])
        except Exception as e:
            await q.message.reply_text(f"Ошибка: {e}")
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
        ctx.user_data.pop("vpn_setup", None)
        ctx.user_data.pop("action", None)
        await show_menu(update, ctx, edit=True)
        return


async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    action = ctx.user_data.get("action")
    text = update.message.text.strip()

    # Быстрые кнопки снизу
    quick = {
        "Создать": "create",
        "Удалить": "delete",
        "Вкл/Выкл": "toggle",
        "Список": "list",
        "Мониторинг": "monitor",
        "Отправить .ovpn": "send",
        "Бэкап": "backup",
        "Обновить бот": "update",
    }
    if text in quick and action != "create_name":
        # Имитируем нажатие inline-кнопки
        ctx.user_data["quick_action"] = quick[text]
        await handle_quick(update, ctx, quick[text])
        return

    # Ввод имени клиента при установке OpenVPN
    if action == "vpn_client_name":
        ctx.user_data.pop("action", None)
        name = text.replace(" ", "_")
        if not name or not name.isascii() or not name.replace("_", "").replace("-", "").isalnum():
            await update.message.reply_text("Имя: только латиница, цифры, дефис, подчёркивание.")
            ctx.user_data["action"] = "vpn_client_name"
            return
        if len(name) > 32:
            await update.message.reply_text("Макс 32 символа.")
            ctx.user_data["action"] = "vpn_client_name"
            return
        setup = ctx.user_data.get("vpn_setup")
        if not setup:
            await show_menu(update, ctx)
            return
        setup["client"] = name
        summary = (
            f"<b>Параметры установки:</b>\n\n"
            f"IP: <code>{setup['endpoint']}</code>\n"
            f"Протокол: <b>{setup['protocol'].upper()}</b>\n"
            f"Порт: <b>{'случайный' if setup['port'] == 'random' else setup['port']}</b>\n"
            f"DNS: <b>{setup['dns']}</b>\n"
            f"Шифр: <b>{setup['cipher']}</b>\n"
            f"Сертификат: <b>{setup['cert_type'].upper()}</b>\n"
            f"TLS: <b>{setup['tls_sig']}</b>\n"
            f"Клиент: <b>{name}</b>\n\n"
            f"Начать установку?"
        )
        await update.message.reply_text(
            summary,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Установить", callback_data="vs:confirm:yes")],
                [InlineKeyboardButton("Отмена", callback_data="back")],
            ])
        )
        return

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
