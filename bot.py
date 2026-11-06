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
import socket
import base64
import re

import paramiko

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


# ======================== IP АДРЕСА (ФРОНТЫ) ========================


def get_client_remote(name: str) -> tuple:
    """Прочитать remote IP и порт из .ovpn файла. → (ip, port) или (None, None)."""
    ovpn = os.path.join(OVPN_DIR, f"{name}.ovpn")
    if not os.path.exists(ovpn):
        return None, None
    try:
        with open(ovpn) as f:
            for line in f:
                m = re.match(r'^remote\s+(\S+)\s+(\d+)', line)
                if m:
                    return m.group(1), m.group(2)
    except Exception:
        pass
    return None, None


def set_client_remote_ip(name: str, new_ip: str) -> tuple:
    """Заменить IP в строке remote у .ovpn файла, порт оставить. → (success, message)."""
    ovpn = os.path.join(OVPN_DIR, f"{name}.ovpn")
    if not os.path.exists(ovpn):
        return False, f"Файл {name}.ovpn не найден"
    try:
        with open(ovpn) as f:
            lines = f.readlines()
        found = False
        for i, line in enumerate(lines):
            m = re.match(r'^(remote\s+)\S+(\s+\d+.*)$', line)
            if m:
                lines[i] = f"{m.group(1)}{new_ip}{m.group(2)}\n"
                found = True
                break
        if not found:
            return False, "Строка remote не найдена в файле"
        with open(ovpn, "w") as f:
            f.writelines(lines)
        return True, f"IP клиента <b>{name}</b> изменён на <code>{new_ip}</code>"
    except Exception as e:
        return False, f"Ошибка: {e}"


def get_all_client_ips() -> list:
    """Список всех клиентов с их remote IP. → [(name, ip, port), ...]"""
    result = []
    for name in list_clients():
        ip, port = get_client_remote(name)
        result.append((name, ip or "—", port or "—"))
    return result


def format_ip_table(client_ips: list) -> str:
    """Форматировать список клиентов с IP в ровную таблицу."""
    if not client_ips:
        return ""
    max_name = max(len(name) for name, _, _ in client_ips)
    max_ip = max(len(ip) for _, ip, _ in client_ips)
    lines = []
    for name, ip, port in client_ips:
        lines.append(f"{name:<{max_name}}  {ip:>{max_ip}}:{port}")
    return "<b>📍 IP адреса клиентов</b>\n\n<pre>" + "\n".join(lines) + "</pre>"


# ======================== ФРОНТ-СЕРВЕРЫ / SSH / МОНИТОРИНГ ========================

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
FRONT_SERVERS_FILE = os.path.join(BOT_DIR, "front_servers.json")
IP_TRACK_FILE = os.path.join(BOT_DIR, "ip_track.json")
SSH_TIMEOUT = 10
SSH_CMD_TIMEOUT = 30

# IP-мониторинг (автоматический, по данным из .ovpn файлов)
TM_PROBE_IP = "217.174.235.161"
IP_FAIL_THRESHOLD = 3
IP_CHECK_INTERVAL = 60  # секунд
ip_fail_counts = {}  # {ip: int}

# GOST
GOST_BIN = "/usr/local/bin/gost"
GOST_SERVICE_PATH = "/usr/lib/systemd/system/gost.service"
GOST_REPO = "ginuerzh/gost"


def load_front_servers() -> dict:
    """Загрузить данные фронт-серверов (GOST). {client_name: {ip, ssh_user, ssh_pass, gost_rules}}"""
    if os.path.exists(FRONT_SERVERS_FILE):
        try:
            with open(FRONT_SERVERS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception):
            pass
    return {}


def save_front_servers(data: dict):
    tmp = FRONT_SERVERS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, FRONT_SERVERS_FILE)


# --- Автоматическое отслеживание IP из .ovpn ---

def load_ip_track() -> dict:
    """Загрузить файл отслеживания IP. {ip: [client_name, ...]}"""
    if os.path.exists(IP_TRACK_FILE):
        try:
            with open(IP_TRACK_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception):
            pass
    return {}


def save_ip_track(data: dict):
    tmp = IP_TRACK_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, IP_TRACK_FILE)


def sync_ip_track() -> dict:
    """Прочитать все .ovpn файлы и обновить ip_track.json.
    Возвращает {ip: [client_name, ...]} — группировка по IP."""
    ip_map = {}  # ip -> [names]
    for name in list_clients():
        ip, port = get_client_remote(name)
        if ip and ip != "—":
            ip_map.setdefault(ip, []).append(name)

    old = load_ip_track()
    if ip_map != old:
        save_ip_track(ip_map)
        # Лог изменений
        old_ips = set(old.keys())
        new_ips = set(ip_map.keys())
        added = new_ips - old_ips
        removed = old_ips - new_ips
        if added:
            log.info(f"[ip_track] Новые IP: {', '.join(added)}")
        if removed:
            log.info(f"[ip_track] Убраны IP: {', '.join(removed)}")

    return ip_map


def ssh_exec(ip: str, port: int, user: str, password: str, command: str,
             cmd_timeout: int = None) -> tuple:
    """Выполнить команду на удалённом сервере по SSH. → (success, output)"""
    _timeout = cmd_timeout or SSH_CMD_TIMEOUT
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ip, port=port, username=user, password=password,
                       timeout=SSH_TIMEOUT, look_for_keys=False, allow_agent=False)
        stdin, stdout, stderr = client.exec_command(command, timeout=_timeout)
        stdout.channel.settimeout(_timeout)
        try:
            out = stdout.read().decode("utf-8", errors="replace").strip()
        except socket.timeout:
            out = "(таймаут чтения)"
        try:
            err = stderr.read().decode("utf-8", errors="replace").strip()
        except socket.timeout:
            err = ""
        result = out if out else err
        return True, result if result else "(пустой вывод)"
    except paramiko.AuthenticationException:
        return False, "Ошибка авторизации (неверный логин/пароль)"
    except paramiko.SSHException as e:
        return False, f"SSH ошибка: {e}"
    except socket.timeout:
        return False, "Таймаут подключения"
    except Exception as e:
        return False, f"Ошибка: {e}"
    finally:
        client.close()


# Нейтральный IP для контрольного пинга — отделяет «у фронта нет сети»
# от «TM заблокировал именно этот IP».
NET_CONTROL_IP = "1.1.1.1"


def ping_via_ssh(host: str, user: str, password: str, target: str,
                 count: int = 5) -> bool:
    """SSH на host, пинг target несколькими пакетами.
    True = ответил хоть один пакет (цель достижима)."""
    ok, out = ssh_exec(host, 22, user, password, f"ping -c {count} -W 2 {target}")
    if not ok:
        return False
    # Любой полученный пакет = цель доступна. TM-зонд часто режет ICMP,
    # поэтому одного пакета мало — смотрим, вернулся ли хоть один из count.
    if "bytes from" in out:
        return True
    m = re.search(r"(\d+)\s+received", out)
    if m and int(m.group(1)) > 0:
        return True
    return False


def check_front_status(host: str, user: str, password: str) -> tuple:
    """Проверить фронт одной SSH-сессией.
    Возврат: (status, detail)
      'ok'         — фронт достучался до TM-зонда → IP НЕ заблокирован
      'blocked'    — у фронта есть интернет, но TM-зонд недоступен → похоже на блок
      'front_down' — SSH не прошёл или у фронта вообще нет сети → проблема фронта, не блок
    """
    cmd = (
        f"ping -c 5 -W 2 {TM_PROBE_IP} >/dev/null 2>&1 && echo PROBE_OK || echo PROBE_FAIL; "
        f"ping -c 3 -W 2 {NET_CONTROL_IP} >/dev/null 2>&1 && echo NET_OK || echo NET_FAIL"
    )
    ok, out = ssh_exec(host, 22, user, password, cmd)
    if not ok:
        return "front_down", out
    if "PROBE_OK" in out:
        return "ok", out
    if "NET_OK" not in out:
        # Фронт не достучался даже до нейтрального IP — это не блок TM,
        # а проблема сети самого фронта (или SSH-сессии).
        return "front_down", out
    return "blocked", out


# ======================== GOST ФУНКЦИИ ========================


def gost_install_script() -> str:
    """Скрипт установки GOST на удалённый сервер."""
    return (
        "#!/bin/bash\n"
        "apt-get update -qq && apt-get install -y -qq wget curl jq tar gzip > /dev/null 2>&1\n"
        f"VER=$(curl -s 'https://api.github.com/repos/{GOST_REPO}/releases/latest' | jq -r '.tag_name' | sed 's/^v//')\n"
        "ARCH=$(dpkg --print-architecture 2>/dev/null || echo amd64)\n"
        '[ "$ARCH" = "arm64" ] && ARCH="armv8"\n'
        '[ "$ARCH" = "armhf" ] && ARCH="armv7"\n'
        "cd /tmp\n"
        f"wget -q \"https://github.com/{GOST_REPO}/releases/download/v${{VER}}/gost_${{VER}}_linux_${{ARCH}}.tar.gz\" -O gost.tar.gz\n"
        "tar -xzf gost.tar.gz && chmod +x gost && mv gost /usr/local/bin/gost\n"
        "rm -f gost.tar.gz\n"
        'echo "GOST_OK:$VER"\n'
    )


def gost_build_service(rules: list) -> str:
    """Сгенерировать systemd unit для GOST с правилами."""
    gost_ls = " ".join(
        f"-L={r['proto']}://:{r['local_port']}/{r['remote_ip']}:{r['remote_port']}"
        for r in rules
    )
    return (
        "[Unit]\\n"
        "Description=GO Simple Tunnel\\n"
        "After=network.target\\n"
        "Wants=network.target\\n"
        "\\n"
        "[Service]\\n"
        "Type=simple\\n"
        f"ExecStart={GOST_BIN} {gost_ls}\\n"
        "Restart=on-failure\\n"
        "\\n"
        "[Install]\\n"
        "WantedBy=multi-user.target"
    )


# ======================== IP МОНИТОР (ФОНОВЫЙ) ========================


async def ip_block_monitor(app):
    """Фоновая задача: каждые 60 сек читает IP из .ovpn файлов,
    SSH-ится на каждый фронт и пингует TM-зонд (217.174.235.161).
    Если 3 раза подряд пинг не проходит — IP заблокирован, шлём уведомление.

    SSH-логин/пароль берутся из config.json (front_ssh_user / front_ssh_pass).
    IP автоматически берутся из .ovpn файлов — не нужно добавлять вручную."""
    global ip_fail_counts
    while True:
        try:
            await asyncio.sleep(IP_CHECK_INTERVAL)

            # Глобальные SSH-данные из конфига
            cfg = load_config()
            ssh_user = cfg.get("front_ssh_user", "").strip()
            ssh_pass = cfg.get("front_ssh_pass", "").strip()
            if not ssh_user or not ssh_pass:
                continue  # SSH фронтов не настроен — пропуск

            # Синхронизировать IP из .ovpn файлов
            ip_map = sync_ip_track()  # {ip: [client1, client2, ...]}
            if not ip_map:
                continue

            # Проверить каждый уникальный IP
            for front_ip, clients in ip_map.items():
                try:
                    status, _detail = await asyncio.to_thread(
                        check_front_status, front_ip, ssh_user, ssh_pass
                    )
                except Exception as e:
                    log.error(f"[ip_monitor] SSH error {front_ip}: {e}")
                    status = "front_down"

                if status == "ok":
                    if ip_fail_counts.get(front_ip, 0) > 0:
                        log.info(f"[ip_monitor] {front_ip} — зонд ОК, сброс ({', '.join(clients)})")
                    ip_fail_counts[front_ip] = 0
                    continue

                if status == "front_down":
                    # Не считаем блоком: нет сети на фронте или SSH-сбой.
                    # Сбрасываем счётчик, чтобы не копить ложные страйки.
                    log.warning(f"[ip_monitor] {front_ip} — фронт недоступен / нет сети (не блок)")
                    ip_fail_counts[front_ip] = 0
                    continue

                # status == "blocked": у фронта есть интернет, но TM-зонд недоступен
                ip_fail_counts[front_ip] = ip_fail_counts.get(front_ip, 0) + 1
                count = ip_fail_counts[front_ip]
                log.warning(f"[ip_monitor] {front_ip} — BLOCKED-страйк #{count}/{IP_FAIL_THRESHOLD} ({', '.join(clients)})")

                if count >= IP_FAIL_THRESHOLD:
                    ip_fail_counts[front_ip] = 0  # сбросить, чтобы не спамить
                    clients_str = ", ".join(f"<b>{c}</b>" for c in clients)
                    try:
                        await app.bot.send_message(
                            chat_id=ADMIN_ID,
                            text=(
                                f"🚫 <b>IP ЗАБЛОКИРОВАН</b>\n\n"
                                f"IP: <code>{front_ip}</code>\n"
                                f"Ключи: {clients_str}\n"
                                f"TM-зонд {TM_PROBE_IP} недоступен {IP_FAIL_THRESHOLD} раз подряд,\n"
                                f"при этом у фронта есть интернет.\n\n"
                                f"Нужна смена IP!"
                            ),
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        log.error(f"[ip_monitor] Не удалось отправить уведомление: {e}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"[ip_monitor] loop error: {e}")
            await asyncio.sleep(10)


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
        [KeyboardButton("📍 IP адреса"), KeyboardButton("🔒 GOST")],
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

    if action == "ip_list":
        client_ips = get_all_client_ips()
        if not client_ips:
            await msg.reply_text("Нет клиентов.")
            return
        text = format_ip_table(client_ips)
        buttons = [[InlineKeyboardButton(f"✏️ {name}", callback_data=f"ipchg:{name}")] for name, _, _ in client_ips]
        buttons.append([InlineKeyboardButton("Назад", callback_data="back")])
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    if action == "gost_menu":
        fronts = load_front_servers()
        cfg = load_config()
        ssh_ok = bool(cfg.get("front_ssh_user") and cfg.get("front_ssh_pass"))
        ip_map = sync_ip_track()

        text = f"<b>🔒 GOST / Фронты</b>\n\n"
        text += f"Мониторинг IP: {'✅ активен' if ssh_ok else '❌ SSH не настроен'}\n"
        text += f"Отслеживаемых IP: {len(ip_map)}\n"
        text += f"Фронт-серверов (GOST): {len(fronts)}"

        if ip_map:
            text += "\n\n<b>IP из .ovpn:</b>\n"
            for ip, clients in ip_map.items():
                text += f"  <code>{ip}</code> — {', '.join(clients)}\n"

        buttons = [
            [InlineKeyboardButton(
                f"🔑 SSH фронтов {'✅' if ssh_ok else '❌'}",
                callback_data="front_ssh_setup")],
            [InlineKeyboardButton("➕ Добавить фронт", callback_data="front_add")],
            [InlineKeyboardButton("📋 Список фронтов", callback_data="front_list")],
            [InlineKeyboardButton("⬇️ Установить GOST", callback_data="gost_install_select")],
            [InlineKeyboardButton("⚙️ Настроить правила GOST", callback_data="gost_rules_select")],
            [InlineKeyboardButton("Назад", callback_data="back")],
        ]
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
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
        status = "OpenVPN: не установлен\n\nУстановите OpenVPN вручную перед использованием бота."

    text = f"<b>AlertMob VPN</b>\n\n{status}"

    buttons = []
    if installed:
        buttons.append([
            InlineKeyboardButton("Создать", callback_data="create"),
            InlineKeyboardButton("Удалить", callback_data="delete"),
        ])
        buttons.append([
            InlineKeyboardButton("Список", callback_data="list"),
            InlineKeyboardButton("Отправить .ovpn", callback_data="send"),
        ])
        buttons.append([
            InlineKeyboardButton("📍 IP адреса", callback_data="ip_list"),
            InlineKeyboardButton("🔒 GOST", callback_data="gost_menu"),
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

    # --- GOST / Фронт-серверы ---
    if data == "gost_menu":
        fronts = load_front_servers()
        cfg = load_config()
        ssh_ok = bool(cfg.get("front_ssh_user") and cfg.get("front_ssh_pass"))
        ip_map = sync_ip_track()

        text = f"<b>🔒 GOST / Фронты</b>\n\n"
        text += f"Мониторинг IP: {'✅ активен' if ssh_ok else '❌ SSH не настроен'}\n"
        text += f"Отслеживаемых IP: {len(ip_map)}\n"
        text += f"Фронт-серверов (GOST): {len(fronts)}"

        if ip_map:
            text += "\n\n<b>IP из .ovpn:</b>\n"
            for ip, clients in ip_map.items():
                text += f"  <code>{ip}</code> — {', '.join(clients)}\n"

        buttons = [
            [InlineKeyboardButton(
                f"🔑 SSH фронтов {'✅' if ssh_ok else '❌'}",
                callback_data="front_ssh_setup")],
            [InlineKeyboardButton("➕ Добавить фронт", callback_data="front_add")],
            [InlineKeyboardButton("📋 Список фронтов", callback_data="front_list")],
            [InlineKeyboardButton("⬇️ Установить GOST", callback_data="gost_install_select")],
            [InlineKeyboardButton("⚙️ Настроить правила GOST", callback_data="gost_rules_select")],
            [InlineKeyboardButton("Назад", callback_data="back")],
        ]
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    if data == "front_ssh_setup":
        cfg = load_config()
        ssh_user = cfg.get("front_ssh_user", "")
        ssh_pass = cfg.get("front_ssh_pass", "")
        if ssh_user:
            text = (
                f"<b>🔑 SSH доступ к фронтам</b>\n\n"
                f"Логин: <code>{ssh_user}</code>\n"
                f"Пароль: <code>{'*' * len(ssh_pass)}</code>\n\n"
                "Эти данные используются для проверки блокировки IP.\n"
                "Бот SSH-ится на каждый фронт и пингует TM-зонд."
            )
        else:
            text = (
                "<b>🔑 SSH доступ к фронтам</b>\n\n"
                "Не настроен.\n\n"
                "Для мониторинга блокировки IP нужен SSH доступ к фронт-серверам.\n"
                "Бот будет SSH-иться на каждый IP из .ovpn файлов и проверять пинг."
            )
        buttons = [
            [InlineKeyboardButton("✏️ Задать логин/пароль", callback_data="front_ssh_edit")],
            [InlineKeyboardButton("Назад", callback_data="gost_menu")],
        ]
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    if data == "front_ssh_edit":
        ctx.user_data["action"] = "front_ssh_user_input"
        await q.edit_message_text(
            "<b>Настройка SSH фронтов</b>\n\n"
            "Введи SSH логин (общий для всех фронт-серверов):",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="gost_menu")]])
        )
        return

    if data == "front_add":
        clients = list_clients()
        fronts = load_front_servers()
        # Показать только клиентов без привязанного фронта
        available = [c for c in clients if c not in fronts]
        if not available:
            await q.edit_message_text(
                "Все клиенты уже имеют привязанный фронт-сервер.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="gost_menu")]])
            )
            return
        buttons = [[InlineKeyboardButton(c, callback_data=f"front_add_client:{c}")] for c in available]
        buttons.append([InlineKeyboardButton("Назад", callback_data="gost_menu")])
        await q.edit_message_text(
            "Выбери клиента для привязки фронт-сервера:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("front_add_client:"):
        client_name = data[17:]
        ctx.user_data["action"] = "front_add_ip"
        ctx.user_data["front_client"] = client_name
        await q.edit_message_text(
            f"<b>Добавление фронта для {client_name}</b>\n\n"
            "Введи IP адрес фронт-сервера:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="gost_menu")]])
        )
        return

    if data == "front_list":
        fronts = load_front_servers()
        if not fronts:
            await q.edit_message_text(
                "Нет фронт-серверов.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="gost_menu")]])
            )
            return
        buttons = [[InlineKeyboardButton(f"{cname} ({srv.get('ip', '?')})", callback_data=f"front_detail:{cname}")]
                    for cname, srv in fronts.items()]
        buttons.append([InlineKeyboardButton("Назад", callback_data="gost_menu")])
        await q.edit_message_text("Выбери фронт-сервер:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("front_detail:"):
        cname = data[13:]
        fronts = load_front_servers()
        srv = fronts.get(cname)
        if not srv:
            await q.edit_message_text("Фронт не найден.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="front_list")]]))
            return
        rules_text = ""
        if srv.get("gost_rules"):
            rules_text = "\n\n<b>GOST правила:</b>\n" + "\n".join(
                f"  <code>{r['proto']}://:{r['local_port']} → {r['remote_ip']}:{r['remote_port']}</code>"
                for r in srv["gost_rules"]
            )
        text = (
            f"<b>Фронт: {cname}</b>\n\n"
            f"IP: <code>{srv.get('ip', '?')}</code>\n"
            f"SSH: <code>{srv.get('ssh_user', '?')}</code>\n"
            f"Пароль: <code>{'*' * len(srv.get('ssh_pass', ''))}</code>"
            f"{rules_text}"
        )
        buttons = [
            [InlineKeyboardButton("🔍 Проверить пинг", callback_data=f"front_ping:{cname}")],
            [InlineKeyboardButton("⬇️ Установить GOST", callback_data=f"gost_install:{cname}")],
            [InlineKeyboardButton("⚙️ Правила GOST", callback_data=f"gost_rules:{cname}")],
            [InlineKeyboardButton("❌ Удалить фронт", callback_data=f"front_del:{cname}")],
            [InlineKeyboardButton("Назад", callback_data="front_list")],
        ]
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    if data.startswith("front_ping:"):
        cname = data[11:]
        fronts = load_front_servers()
        srv = fronts.get(cname)
        if not srv:
            await q.edit_message_text("Фронт не найден.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="front_list")]]))
            return
        await q.edit_message_text(f"🔍 Проверяю {srv['ip']}...")
        ssh_user = srv.get("ssh_user") or load_config().get("front_ssh_user", "")
        ssh_pass = srv.get("ssh_pass") or load_config().get("front_ssh_pass", "")
        try:
            status, _ = await asyncio.to_thread(
                check_front_status, srv["ip"], ssh_user, ssh_pass
            )
        except Exception:
            status = "front_down"
        if status == "ok":
            result = (f"✅ <code>{srv['ip']}</code> → зонд {TM_PROBE_IP} — <b>ОК</b>\n"
                      f"IP НЕ заблокирован.")
        elif status == "blocked":
            result = (f"🚫 <code>{srv['ip']}</code> → зонд {TM_PROBE_IP} — <b>НЕТ ОТВЕТА</b>\n"
                      f"У фронта есть интернет — похоже на блок TM.")
        else:
            result = (f"⚠️ <code>{srv['ip']}</code> — <b>ФРОНТ НЕДОСТУПЕН</b>\n"
                      f"Нет сети на фронте или SSH не прошёл. Это не блок TM.")
        await q.message.reply_text(result, parse_mode="HTML")
        # Вернуть детали фронта
        rules_text = ""
        if srv.get("gost_rules"):
            rules_text = "\n\n<b>GOST правила:</b>\n" + "\n".join(
                f"  <code>{r['proto']}://:{r['local_port']} → {r['remote_ip']}:{r['remote_port']}</code>"
                for r in srv["gost_rules"]
            )
        text = (
            f"<b>Фронт: {cname}</b>\n\n"
            f"IP: <code>{srv.get('ip', '?')}</code>\n"
            f"SSH: <code>{srv.get('ssh_user', '?')}</code>"
            f"{rules_text}"
        )
        buttons = [
            [InlineKeyboardButton("🔍 Проверить пинг", callback_data=f"front_ping:{cname}")],
            [InlineKeyboardButton("⬇️ Установить GOST", callback_data=f"gost_install:{cname}")],
            [InlineKeyboardButton("⚙️ Правила GOST", callback_data=f"gost_rules:{cname}")],
            [InlineKeyboardButton("❌ Удалить фронт", callback_data=f"front_del:{cname}")],
            [InlineKeyboardButton("Назад", callback_data="front_list")],
        ]
        await q.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    if data.startswith("front_del:"):
        cname = data[10:]
        fronts = load_front_servers()
        if cname in fronts:
            del fronts[cname]
            save_front_servers(fronts)
            await q.edit_message_text(f"✅ Фронт <b>{cname}</b> удалён.", parse_mode="HTML")
        else:
            await q.edit_message_text("Фронт не найден.")
        await asyncio.sleep(1)
        # Вернуться в gost_menu
        fronts = load_front_servers()
        text = f"<b>🔒 GOST / Фронты</b>\n\nФронт-серверов: {len(fronts)}"
        buttons = [
            [InlineKeyboardButton("➕ Добавить фронт", callback_data="front_add")],
            [InlineKeyboardButton("📋 Список фронтов", callback_data="front_list")],
            [InlineKeyboardButton("Назад", callback_data="back")],
        ]
        await q.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    # --- GOST установка ---
    if data == "gost_install_select":
        fronts = load_front_servers()
        if not fronts:
            await q.edit_message_text("Сначала добавь фронт-серверы.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="gost_menu")]]))
            return
        buttons = [[InlineKeyboardButton(f"{cname} ({srv.get('ip', '?')})", callback_data=f"gost_install:{cname}")]
                    for cname, srv in fronts.items()]
        buttons.append([InlineKeyboardButton("Назад", callback_data="gost_menu")])
        await q.edit_message_text("Выбери сервер для установки GOST:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("gost_install:"):
        cname = data[13:]
        fronts = load_front_servers()
        srv = fronts.get(cname)
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data=f"front_detail:{cname}")]])
        if not srv:
            await q.edit_message_text("Фронт не найден.", reply_markup=back_kb)
            return
        ip = srv["ip"]
        await q.edit_message_text(f"⏳ Устанавливаю GOST на <code>{ip}</code>...", parse_mode="HTML")
        script = gost_install_script()
        b64 = base64.b64encode(script.encode()).decode()
        start_cmd = (
            f"echo '{b64}' | base64 -d > /tmp/gost_install.sh && "
            "chmod +x /tmp/gost_install.sh && "
            "rm -f /tmp/gost_install.log && "
            "nohup bash /tmp/gost_install.sh > /tmp/gost_install.log 2>&1 & "
            "echo BG_OK"
        )
        ok, out = await asyncio.to_thread(ssh_exec, ip, 22, srv["ssh_user"], srv["ssh_pass"], start_cmd)
        if not ok or "BG_OK" not in out:
            await q.message.reply_text(
                f"❌ Не удалось запустить установку на {ip}:\n<pre>{html_escape.escape(out[:2000])}</pre>",
                parse_mode="HTML", reply_markup=back_kb)
            return
        check_cmd = (
            "cat /tmp/gost_install.log 2>/dev/null; "
            "pgrep -f gost_install.sh > /dev/null 2>&1 && echo __RUNNING__ || echo __FINISHED__"
        )
        for attempt in range(30):
            await asyncio.sleep(10)
            elapsed = (attempt + 1) * 10
            ok2, log_out = await asyncio.to_thread(ssh_exec, ip, 22, srv["ssh_user"], srv["ssh_pass"], check_cmd)
            if "GOST_OK:" in log_out:
                ver = log_out.split("GOST_OK:")[-1].split("\n")[0].split("__")[0].strip()
                await q.message.reply_text(
                    f"✅ GOST v{ver} установлен на <code>{ip}</code> ({elapsed} сек)",
                    parse_mode="HTML", reply_markup=back_kb)
                return
            if "__FINISHED__" in log_out and "GOST_OK:" not in log_out:
                clean = log_out.replace("__FINISHED__", "").replace("__RUNNING__", "").strip()
                await q.message.reply_text(
                    f"❌ Ошибка установки на {ip}:\n<pre>{html_escape.escape(clean[:2000])}</pre>",
                    parse_mode="HTML", reply_markup=back_kb)
                return
            bars = "▓" * (attempt % 4 + 1) + "░" * (3 - attempt % 4)
            try:
                await q.edit_message_text(
                    f"⏳ Устанавливаю GOST на <code>{ip}</code>... {bars} ({elapsed} сек)",
                    parse_mode="HTML")
            except Exception:
                pass
        await q.message.reply_text(
            f"⏳ Установка на <code>{ip}</code> идёт дольше 5 мин.\nGOST может установиться позже.",
            parse_mode="HTML", reply_markup=back_kb)
        return

    # --- GOST правила ---
    if data == "gost_rules_select":
        fronts = load_front_servers()
        if not fronts:
            await q.edit_message_text("Сначала добавь фронт-серверы.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="gost_menu")]]))
            return
        buttons = [[InlineKeyboardButton(f"{cname} ({srv.get('ip', '?')})", callback_data=f"gost_rules:{cname}")]
                    for cname, srv in fronts.items()]
        buttons.append([InlineKeyboardButton("Назад", callback_data="gost_menu")])
        await q.edit_message_text("Выбери сервер для настройки правил:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("gost_rules:"):
        cname = data[11:]
        fronts = load_front_servers()
        srv = fronts.get(cname)
        if not srv:
            await q.edit_message_text("Фронт не найден.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="gost_menu")]]))
            return
        rules = srv.get("gost_rules", [])
        if rules:
            text = f"<b>Правила GOST для {cname}</b> (<code>{srv['ip']}</code>)\n\n"
            text += "\n".join(
                f"  {i+1}. <code>{r['proto']}://:{r['local_port']} → {r['remote_ip']}:{r['remote_port']}</code>"
                for i, r in enumerate(rules)
            )
        else:
            text = f"<b>Правила GOST для {cname}</b>\n\nНет правил."
        buttons = [
            [InlineKeyboardButton("➕ Добавить правило", callback_data=f"gost_addrule:{cname}")],
        ]
        if rules:
            buttons.append([InlineKeyboardButton("🔄 Применить на сервер", callback_data=f"gost_apply:{cname}")])
            buttons.append([InlineKeyboardButton("🗑 Очистить правила", callback_data=f"gost_clearrules:{cname}")])
        buttons.append([InlineKeyboardButton("Назад", callback_data=f"front_detail:{cname}")])
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    if data.startswith("gost_addrule:"):
        cname = data[13:]
        ctx.user_data["action"] = "gost_rule_proto"
        ctx.user_data["gost_rule_client"] = cname
        await q.edit_message_text(
            f"<b>Новое правило для {cname}</b>\n\n"
            "Протоколы: tcp, udp, relay, socks5\n\n"
            "Введи протокол (например <code>tcp</code>):",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data=f"gost_rules:{cname}")]])
        )
        return

    if data.startswith("gost_clearrules:"):
        cname = data[16:]
        fronts = load_front_servers()
        if cname in fronts:
            fronts[cname]["gost_rules"] = []
            save_front_servers(fronts)
        await q.edit_message_text(f"✅ Правила для {cname} очищены.", parse_mode="HTML")
        await asyncio.sleep(1)
        # Вернуть в gost_rules
        fronts = load_front_servers()
        srv = fronts.get(cname, {})
        text = f"<b>Правила GOST для {cname}</b>\n\nНет правил."
        buttons = [
            [InlineKeyboardButton("➕ Добавить правило", callback_data=f"gost_addrule:{cname}")],
            [InlineKeyboardButton("Назад", callback_data=f"front_detail:{cname}")],
        ]
        await q.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    if data.startswith("gost_apply:"):
        cname = data[11:]
        fronts = load_front_servers()
        srv = fronts.get(cname)
        if not srv or not srv.get("gost_rules"):
            await q.edit_message_text("Нет правил для применения.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data=f"gost_rules:{cname}")]]))
            return
        ip = srv["ip"]
        rules = srv["gost_rules"]
        await q.edit_message_text(f"⏳ Применяю {len(rules)} правил на <code>{ip}</code>...", parse_mode="HTML")
        service_content = gost_build_service(rules)
        cmd = (
            f"echo -e '{service_content}' > {GOST_SERVICE_PATH} && "
            "systemctl daemon-reload && systemctl enable gost && systemctl restart gost && echo GOST_CONF_OK"
        )
        ok, out = await asyncio.to_thread(ssh_exec, ip, 22, srv["ssh_user"], srv["ssh_pass"], cmd)
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data=f"gost_rules:{cname}")]])
        if ok and "GOST_CONF_OK" in out:
            rules_txt = "\n".join(
                f"  <code>{r['proto']}://:{r['local_port']} → {r['remote_ip']}:{r['remote_port']}</code>"
                for r in rules)
            await q.message.reply_text(
                f"✅ GOST настроен на <code>{ip}</code>:\n{rules_txt}",
                parse_mode="HTML", reply_markup=back_kb)
        else:
            await q.message.reply_text(
                f"❌ Ошибка:\n<pre>{html_escape.escape(out[:2000])}</pre>",
                parse_mode="HTML", reply_markup=back_kb)
        return

    # --- IP адреса (фронты) ---
    if data == "ip_list":
        client_ips = get_all_client_ips()
        if not client_ips:
            await q.edit_message_text("Нет клиентов.")
            await asyncio.sleep(1)
            await show_menu(update, ctx)
            return
        text = format_ip_table(client_ips)
        buttons = [[InlineKeyboardButton(f"✏️ {name}", callback_data=f"ipchg:{name}")] for name, _, _ in client_ips]
        buttons.append([InlineKeyboardButton("Назад", callback_data="back")])
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    if data.startswith("ipchg:"):
        name = data[6:]
        ip, port = get_client_remote(name)
        ctx.user_data["action"] = "change_ip"
        ctx.user_data["change_ip_client"] = name
        await q.edit_message_text(
            f"<b>{name}</b>\n\n"
            f"Текущий IP: <code>{ip or '—'}</code>\n"
            f"Порт: <code>{port or '—'}</code>\n\n"
            f"Введи новый IP адрес:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Отмена", callback_data="ip_list")],
            ])
        )
        return

    # --- Назад ---
    if data == "back":
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
        "📍 IP адреса": "ip_list",
        "🔒 GOST": "gost_menu",
    }
    if text in quick and action != "create_name":
        # Имитируем нажатие inline-кнопки
        ctx.user_data["quick_action"] = quick[text]
        await handle_quick(update, ctx, quick[text])
        return

    # --- Настройка глобального SSH для фронтов ---
    if action == "front_ssh_user_input":
        ctx.user_data.pop("action", None)
        ctx.user_data["front_ssh_user_new"] = text
        ctx.user_data["action"] = "front_ssh_pass_input"
        await update.message.reply_text(
            f"Логин: <code>{text}</code>\n\nВведи SSH пароль:",
            parse_mode="HTML"
        )
        return

    if action == "front_ssh_pass_input":
        ctx.user_data.pop("action", None)
        ssh_user = ctx.user_data.pop("front_ssh_user_new", "")
        ssh_pass = text
        if not ssh_user:
            await show_menu(update, ctx)
            return
        # Сохранить в конфиг
        cfg = load_config()
        cfg["front_ssh_user"] = ssh_user
        cfg["front_ssh_pass"] = ssh_pass
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)

        # Проверить на первом доступном IP
        ip_map = sync_ip_track()
        test_ip = next(iter(ip_map), None) if ip_map else None
        if test_ip:
            await update.message.reply_text(f"Проверяю SSH {ssh_user}@{test_ip}...")
            ok, out = await asyncio.to_thread(ssh_exec, test_ip, 22, ssh_user, ssh_pass, "echo SSH_OK")
            if ok and "SSH_OK" in out:
                await update.message.reply_text(
                    f"✅ SSH настроен и проверен!\n\n"
                    f"Логин: <code>{ssh_user}</code>\n"
                    f"Тест: <code>{test_ip}</code> — ОК\n\n"
                    f"Мониторинг {len(ip_map)} IP активирован.",
                    parse_mode="HTML"
                )
            else:
                await update.message.reply_text(
                    f"⚠️ SSH сохранён, но тест не прошёл:\n"
                    f"<pre>{html_escape.escape(out[:500])}</pre>\n\n"
                    f"Проверь логин/пароль. Мониторинг может не работать.",
                    parse_mode="HTML"
                )
        else:
            await update.message.reply_text(
                f"✅ SSH сохранён: <code>{ssh_user}</code>\n\n"
                f"Нет .ovpn ключей для проверки.",
                parse_mode="HTML"
            )
        await show_menu(update, ctx)
        return

    # --- Добавление фронт-сервера (пошаговый ввод) ---
    if action == "front_add_ip":
        ctx.user_data.pop("action", None)
        client_name = ctx.user_data.get("front_client")
        parts = text.split(".")
        if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            ctx.user_data["action"] = "front_add_ip"
            await update.message.reply_text("❌ Неверный IP. Введи IPv4 (например 1.2.3.4):")
            return
        ctx.user_data["front_ip"] = text
        ctx.user_data["action"] = "front_add_user"
        await update.message.reply_text(f"IP: <code>{text}</code>\n\nВведи SSH логин:", parse_mode="HTML")
        return

    if action == "front_add_user":
        ctx.user_data.pop("action", None)
        ctx.user_data["front_ssh_user"] = text
        ctx.user_data["action"] = "front_add_pass"
        await update.message.reply_text("Введи SSH пароль:")
        return

    if action == "front_add_pass":
        ctx.user_data.pop("action", None)
        client_name = ctx.user_data.pop("front_client", None)
        front_ip = ctx.user_data.pop("front_ip", None)
        ssh_user = ctx.user_data.pop("front_ssh_user", None)
        ssh_pass = text
        if not client_name or not front_ip:
            await show_menu(update, ctx)
            return
        # Проверить SSH подключение
        await update.message.reply_text(f"Проверяю SSH {ssh_user}@{front_ip}...")
        ok, out = await asyncio.to_thread(ssh_exec, front_ip, 22, ssh_user, ssh_pass, "echo SSH_OK")
        if ok and "SSH_OK" in out:
            fronts = load_front_servers()
            fronts[client_name] = {
                "ip": front_ip,
                "ssh_user": ssh_user,
                "ssh_pass": ssh_pass,
                "gost_rules": [],
            }
            save_front_servers(fronts)
            await update.message.reply_text(
                f"✅ Фронт-сервер добавлен:\n\n"
                f"Клиент: <b>{client_name}</b>\n"
                f"IP: <code>{front_ip}</code>\n"
                f"SSH: <code>{ssh_user}</code>",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                f"❌ SSH не удался:\n<pre>{html_escape.escape(out[:500])}</pre>\n\n"
                f"Фронт НЕ добавлен. Попробуй снова.",
                parse_mode="HTML"
            )
        await show_menu(update, ctx)
        return

    # --- GOST правила (пошаговый ввод) ---
    if action == "gost_rule_proto":
        ctx.user_data.pop("action", None)
        proto = text.lower().strip()
        valid_protos = ["tcp", "udp", "relay", "socks5", "http", "tls", "ws"]
        if proto not in valid_protos:
            ctx.user_data["action"] = "gost_rule_proto"
            await update.message.reply_text(f"❌ Неверный протокол. Доступны: {', '.join(valid_protos)}")
            return
        ctx.user_data["gost_rule_proto"] = proto
        ctx.user_data["action"] = "gost_rule_local_port"
        await update.message.reply_text(f"Протокол: <code>{proto}</code>\n\nВведи локальный порт:", parse_mode="HTML")
        return

    if action == "gost_rule_local_port":
        ctx.user_data.pop("action", None)
        if not text.isdigit() or not (1 <= int(text) <= 65535):
            ctx.user_data["action"] = "gost_rule_local_port"
            await update.message.reply_text("❌ Порт: число 1-65535")
            return
        ctx.user_data["gost_rule_local_port"] = text
        ctx.user_data["action"] = "gost_rule_remote"
        await update.message.reply_text(
            f"Локальный порт: <code>{text}</code>\n\n"
            "Введи удалённый адрес и порт (формат: <code>IP:PORT</code>):",
            parse_mode="HTML"
        )
        return

    if action == "gost_rule_remote":
        ctx.user_data.pop("action", None)
        if ":" not in text:
            ctx.user_data["action"] = "gost_rule_remote"
            await update.message.reply_text("❌ Формат: IP:PORT (например 10.8.0.1:443)")
            return
        remote_ip, remote_port = text.rsplit(":", 1)
        if not remote_port.isdigit() or not (1 <= int(remote_port) <= 65535):
            ctx.user_data["action"] = "gost_rule_remote"
            await update.message.reply_text("❌ Порт: число 1-65535")
            return
        cname = ctx.user_data.pop("gost_rule_client", None)
        proto = ctx.user_data.pop("gost_rule_proto", "tcp")
        local_port = ctx.user_data.pop("gost_rule_local_port", "0")
        if not cname:
            await show_menu(update, ctx)
            return
        rule = {
            "proto": proto,
            "local_port": local_port,
            "remote_ip": remote_ip,
            "remote_port": remote_port,
        }
        fronts = load_front_servers()
        if cname in fronts:
            if "gost_rules" not in fronts[cname]:
                fronts[cname]["gost_rules"] = []
            fronts[cname]["gost_rules"].append(rule)
            save_front_servers(fronts)
            await update.message.reply_text(
                f"✅ Правило добавлено:\n"
                f"<code>{proto}://:{local_port} → {remote_ip}:{remote_port}</code>",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text("❌ Фронт не найден.")
        await show_menu(update, ctx)
        return

    # Ввод нового IP для смены адреса фронта
    if action == "change_ip":
        ctx.user_data.pop("action", None)
        client_name = ctx.user_data.pop("change_ip_client", None)
        if not client_name:
            await show_menu(update, ctx)
            return
        new_ip = text
        # Валидация IP
        parts = new_ip.split(".")
        if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            ctx.user_data["action"] = "change_ip"
            ctx.user_data["change_ip_client"] = client_name
            await update.message.reply_text("❌ Неверный формат IP. Введи IPv4 (например 1.2.3.4):")
            return
        ok, msg_text = set_client_remote_ip(client_name, new_ip)
        if ok:
            await update.message.reply_text(f"✅ {msg_text}", parse_mode="HTML")
            # Отправить обновлённый .ovpn файл
            ovpn = os.path.join(OVPN_DIR, f"{client_name}.ovpn")
            if os.path.exists(ovpn):
                with open(ovpn, "rb") as f:
                    await update.message.reply_document(
                        document=InputFile(f, filename=f"{client_name}.ovpn"),
                        caption=f"🔑 {client_name}.ovpn (новый IP: {new_ip})"
                    )
        else:
            await update.message.reply_text(f"❌ {msg_text}", parse_mode="HTML")
        await show_menu(update, ctx)
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

    # Запустить фоновый мониторинг IP после старта бота
    async def post_init(application):
        application.create_task(ip_block_monitor(application))

    app.post_init = post_init
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
