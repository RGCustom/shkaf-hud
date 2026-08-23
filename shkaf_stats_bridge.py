#!/usr/bin/env python3
"""
shkaf_stats_bridge.py  (контейнер: shkaf-hud)

Финальная сборка: собирает статистику хоста + Tautulli, отдаёт веб-интерфейс
(страница "/" - лента/бары, "/screens" - редактор OLED-экранов), шлёт на
Arduino Pro Micro минимальный протокол (BAR1-4, BRI, L1-3), только то, что
реально изменилось.

Логика LED-баров (цвета/яркость/что на каком баре) - неизменная часть проекта.
Логика OLED (какие экраны, что на них, ротация) - новая, целиком через
variables.py/templates.py/screens.py/protocol.py, настраивается на /screens.

Зависимости:
    pip install pyserial requests flask --break-system-packages
"""

import os
import re
import copy
import glob
import json
import time
import socket
import fcntl
import struct
import datetime
import threading

import serial
import requests
from flask import Flask, request, jsonify, Response

import variables
import templates
import screens
import screens_webui
import settings_webui
import protocol
import ledbar
import qbittorrent
import flash_webui

SCRIPT_VERSION = "2026-08-22-1"

# Момент старта процесса - для переменной container_uptime (аптайм самого
# контейнера, в отличие от uptime - аптайма хоста из /proc/uptime).
CONTAINER_START_TIME = time.time()

# ---------------- КОНФИГ (через переменные окружения) ----------------

SERIAL_PORT = os.environ.get("SERIAL_PORT", "/dev/ttyACM0")
BAUD = int(os.environ.get("BAUD", "115200"))

TAUTULLI_URL = os.environ.get("TAUTULLI_URL", "http://127.0.0.1:8181")
TAUTULLI_API_KEY = os.environ.get("TAUTULLI_API_KEY", "")

NET_IFACE = os.environ.get("NET_IFACE", "br0")  # для LED-бара, если на него назначена метрика "net"
NET_MAX_MBPS = float(os.environ.get("NET_MAX_MBPS", "500"))

DISK_DEVICES = os.environ.get("DISK_DEVICES", "")

ARRAY_PATH = os.environ.get("ARRAY_PATH", "/mnt/user")
ARRAY_REFRESH_SECONDS = float(os.environ.get("ARRAY_REFRESH_SECONDS", "60"))

CACHE_PATH = os.environ.get("CACHE_PATH", "/mnt/cache")
CACHE_REFRESH_SECONDS = float(os.environ.get("CACHE_REFRESH_SECONDS", "60"))

LIBRARY_REFRESH_SECONDS = float(os.environ.get("LIBRARY_REFRESH_SECONDS", "300"))
RECENT_REFRESH_SECONDS = float(os.environ.get("RECENT_REFRESH_SECONDS", "120"))
RECENT_MAX_AGE_DAYS = float(os.environ.get("RECENT_MAX_AGE_DAYS", "5"))
RECENT_COUNT = int(os.environ.get("RECENT_COUNT", "5"))

CPU_TEMP_MAX_C = float(os.environ.get("CPU_TEMP_MAX_C", "90"))

POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))
FULL_RESYNC_SECONDS = float(os.environ.get("FULL_RESYNC_SECONDS", "30"))

WEB_PORT = int(os.environ.get("WEB_PORT", "8189"))
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")

DISK_NAME_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+)$")

# ---------------- настройки баров (цвета/яркость/назначение/сеть для net1-net2) ----------------

DEFAULT_COLORS = {
    "bar0": {"c1": "00FF42", "c2": "FFF600", "c3": "FF0000"},
    "bar1": {"c1": "00FF42", "c2": "FFF600", "c3": "FF0000"},
    "bar2": {"c1": "00FF42", "c2": "FFF600", "c3": "FF0000"},
    "bar3": {"c1": "00FF42", "c2": "FFF600", "c3": "FF0000"},
}
# colors_top - отдельный градиент для верхней половины в режимах "center"/
# "edges". Дефолт = DEFAULT_COLORS - так получается "зеркало" без специальной
# mirror-логики: юзер просто не трогает colors_top, и обе половины одинаковые.
DEFAULT_COLORS_TOP = copy.deepcopy(DEFAULT_COLORS)

DEFAULT_ASSIGNMENT = {"bar0": "cpu", "bar1": "ram", "bar2": "net", "bar3": "disk"}
# assignment_top - метрика верхней половины в режимах "center"/"edges".
# Дефолт = та же метрика, что и снизу (опять же зеркальность "из коробки").
DEFAULT_ASSIGNMENT_TOP = dict(DEFAULT_ASSIGNMENT)

DEFAULT_BRIGHTNESS = 15
DEFAULT_SOLID = {"bar0": False, "bar1": False, "bar2": False, "bar3": False}
DEFAULT_SOLID_TOP = {"bar0": False, "bar1": False, "bar2": False, "bar3": False}

# "classic"          - градиент снизу вверх (как было всегда).
# "classic_reverse"  - тот же classic, только перевёрнутый - бар растёт
#                       сверху вниз. Использует те же assignment/colors/solid,
#                       что и classic (никаких отдельных полей не заводим -
#                       это просто другое физическое направление рендера
#                       того же одиночного бара).
# "center"  - бар растёт от центра в обе стороны, у каждой половины своя
#             метрика/цвета/solid (assignment_top/colors_top/solid_top).
# "edges"   - зеркало center: бар растёт от краёв к центру, каждая половина
#             от своего края. Использует ТЕ ЖЕ поля, что и center
#             (assignment_top/colors_top/solid_top) - разница только в
#             направлении рендера (см. ledbar.compute_bar_pixels_edges).
DEFAULT_MODE = {"bar0": "classic", "bar1": "classic", "bar2": "classic", "bar3": "classic"}

# Peak hold - независимый тумблер поверх ЛЮБОГО режима (см. ledbar.PeakHold).
# style: "hold" - точка держится и гаснет, "fade" - плавно затухает.
# Тайминги (сколько держится/затухает) - ОБЩИЕ на все бары, см.
# DEFAULT_PEAK_HOLD_SECONDS/DEFAULT_PEAK_FADE_SECONDS ниже - один слайдер
# в /settings на оба параметра, не по одному на бар.
DEFAULT_PEAK = {
    "bar0": {"enabled": False, "style": "hold"},
    "bar1": {"enabled": False, "style": "hold"},
    "bar2": {"enabled": False, "style": "hold"},
    "bar3": {"enabled": False, "style": "hold"},
}

# Общие тайминги peak hold - одни на весь проект (все бары/половины),
# слайдеры 0-10 сек с шагом 0.1 в /settings. Дефолты как раньше.
DEFAULT_PEAK_HOLD_SECONDS = 2.0
DEFAULT_PEAK_FADE_SECONDS = 1.5

BAR_METRICS = {
    "cpu": "CPU",
    "ram": "RAM",
    "net": "NET (общий, для LED)",
    "disk": "DISK %util",
    "array": "Array %",
    "cache": "Cache %",
    "cputemp": "CPU temp",
    "swap": "SWAP %",
    "qbt_dl": "qBittorrent ↓ (DL)",
    "qbt_ul": "qBittorrent ↑ (UL)",
}

# Все допустимые значения "режим бара" - используется и в /api/mode для
# валидации, и как единый источник правды (не дублировать список руками
# в нескольких местах).
BAR_MODES = ("classic", "classic_reverse", "center", "edges")

DEFAULT_SETTINGS = {
    "colors": DEFAULT_COLORS,
    "colors_top": DEFAULT_COLORS_TOP,
    "assignment": DEFAULT_ASSIGNMENT,
    "assignment_top": DEFAULT_ASSIGNMENT_TOP,
    "mode": DEFAULT_MODE,
    "brightness": DEFAULT_BRIGHTNESS,
    "solid": DEFAULT_SOLID,
    "solid_top": DEFAULT_SOLID_TOP,
    "peak": DEFAULT_PEAK,
    "peak_hold_seconds": DEFAULT_PEAK_HOLD_SECONDS,
    "peak_fade_seconds": DEFAULT_PEAK_FADE_SECONDS,
    "contrast": 255,
    "net1_iface": "",
    "net2_iface": "",
}


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
    except Exception:
        saved = {}

    cfg = copy.deepcopy(DEFAULT_SETTINGS)
    for key, default_val in DEFAULT_SETTINGS.items():
        if key not in saved:
            continue
        saved_val = saved[key]
        if isinstance(default_val, dict):
            if isinstance(saved_val, dict):
                for sub_key, sub_val in saved_val.items():
                    if sub_key in cfg[key] and isinstance(cfg[key][sub_key], dict) and isinstance(sub_val, dict):
                        cfg[key][sub_key].update(sub_val)
                    elif sub_key in cfg[key] and not isinstance(cfg[key][sub_key], dict):
                        cfg[key][sub_key] = sub_val
            # если сохранённое не dict там, где ожидается dict (старый формат) - игнорируем
        else:
            cfg[key] = saved_val
    return cfg


def save_settings(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(cfg, f)


state_lock = threading.Lock()
state = {
    "bars": {},  # {"bar0": {"mode":..., "pixels":[...], "pct_bottom":..., "pct_top":...}, ...}
    "cfg": load_settings(),
    "serial_connected": False,
    "oled_lines": ["", "", ""],
}

# последний известный context - для /api/preview на странице /screens
_last_context = {}
_context_lock = threading.Lock()

# Установлен, пока идёт заливка прошивки через /api/flash - на это время
# главный цикл не открывает и не пишет в serial-порт платы (см. main()),
# чтобы avrdude и bridge не дрались за один и тот же USB-порт одновременно.
flashing_event = threading.Event()


def get_context():
    with _context_lock:
        return dict(_last_context)


# ---------------- метрики хоста ----------------

def read_cpu_times():
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    vals = list(map(int, parts))
    idle = vals[3] + vals[4]
    total = sum(vals)
    return idle, total


def read_per_core_times():
    """{'cpu0': (idle, total), 'cpu1': (...), ...} - как read_cpu_times(), но
    по каждому ядру отдельно (в /proc/stat помимо общей строки 'cpu' идут
    построчно 'cpu0', 'cpu1', ...)."""
    times = {}
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu") and len(line) > 3 and line[3].isdigit():
                    parts = line.split()
                    vals = list(map(int, parts[1:]))
                    idle = vals[3] + vals[4]
                    total = sum(vals)
                    times[parts[0]] = (idle, total)
    except Exception:
        pass
    return times


def compute_max_core_pct(prev_times, curr_times):
    """Загрузка самого нагруженного ядра между двумя снятыми снапшотами -
    в отличие от cpu_pct (среднее по всем ядрам), показывает, есть ли
    процесс, упирающийся в одно ядро."""
    max_pct = 0.0
    for name, (curr_idle, curr_total) in curr_times.items():
        prev = prev_times.get(name)
        if prev is None:
            continue
        prev_idle, prev_total = prev
        d_idle = curr_idle - prev_idle
        d_total = curr_total - prev_total
        if d_total <= 0:
            continue
        pct = (1 - d_idle / d_total) * 100.0
        max_pct = max(max_pct, pct)
    return max_pct


def read_ram_details():
    """Возвращает (pct, used_gb, total_gb) - раньше была только read_ram_percent()."""
    meminfo = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, val = line.split(":")
            meminfo[key.strip()] = int(val.strip().split()[0])
    total_kb = meminfo.get("MemTotal", 1)
    available_kb = meminfo.get("MemAvailable", total_kb)
    used_kb = max(0, total_kb - available_kb)
    pct = max(0.0, min(100.0, used_kb / total_kb * 100.0)) if total_kb else 0.0
    return pct, round(used_kb / 1_048_576, 1), round(total_kb / 1_048_576, 1)


def read_swap_percent():
    meminfo = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, val = line.split(":")
                meminfo[key.strip()] = int(val.strip().split()[0])
    except Exception:
        return 0.0
    total = meminfo.get("SwapTotal", 0)
    free = meminfo.get("SwapFree", 0)
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, (total - free) / total * 100.0))


def read_load_avg():
    """(load1, load5, load15) из /proc/loadavg."""
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        return float(parts[0]), float(parts[1]), float(parts[2])
    except Exception:
        return 0.0, 0.0, 0.0


def read_cpu_freq_mhz():
    """Средняя частота по ядрам, МГц (None, если /proc/cpuinfo не отдаёт cpu MHz)."""
    try:
        freqs = []
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("cpu MHz"):
                    freqs.append(float(line.split(":")[1].strip()))
        if freqs:
            return round(sum(freqs) / len(freqs))
    except Exception:
        pass
    return None


def read_uptime_seconds():
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def format_duration(seconds):
    """Компактный формат аптайма: '5d 3h', '2h 14m', '47m'."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def read_cpu_temp_c():
    paths = glob.glob("/sys/class/hwmon/hwmon*/temp1_input") + ["/sys/class/thermal/thermal_zone0/temp"]
    for p in paths:
        try:
            with open(p) as f:
                milli_c = int(f.read().strip())
            return milli_c / 1000.0
        except Exception:
            continue
    return None


def read_cpu_temp_pct():
    temp_c = read_cpu_temp_c()
    if temp_c is None:
        return 0.0
    return max(0.0, min(100.0, temp_c / CPU_TEMP_MAX_C * 100.0))


def read_net_bytes(iface):
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                if ":" not in line:
                    continue
                name, rest = line.split(":", 1)
                if name.strip() != iface:
                    continue
                fields = rest.split()
                return int(fields[0]) + int(fields[8])
    except Exception:
        pass
    return None


def list_real_interfaces():
    ifaces = []
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()[2:]
        for line in lines:
            name = line.split(":")[0].strip()
            if not name or name == "lo":
                continue
            if name.startswith(("veth", "docker", "br-", "virbr", "vnet", "tunl", "tun", "wg")):
                continue
            ifaces.append(name)
    except Exception:
        pass
    return ifaces


def read_iface_rx_tx(iface):
    if not iface:
        return None, None
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                if ":" not in line:
                    continue
                name, rest = line.split(":", 1)
                if name.strip() != iface:
                    continue
                fields = rest.split()
                return int(fields[0]), int(fields[8])
    except Exception:
        pass
    return None, None


def read_iface_speed(iface):
    try:
        with open(f"/sys/class/net/{iface}/speed") as f:
            mbps = int(f.read().strip())
        if mbps <= 0:
            return "?"
        if mbps >= 1000:
            g = mbps / 1000
            return f"{g:g}Gbit"
        return f"{mbps}Mbit"
    except Exception:
        return "?"


def read_iface_ip(iface):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        packed = struct.pack("256s", iface[:15].encode())
        return socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, packed)[20:24])
    except Exception:
        return ""


def format_rate(bytes_delta, dt):
    if dt <= 0 or bytes_delta < 0:
        return "0Kbps"
    bits_per_sec = bytes_delta * 8 / dt
    mbps = bits_per_sec / 1_000_000
    if mbps >= 1:
        return f"{mbps:.1f}Mbps"
    kbps = bits_per_sec / 1000
    return f"{kbps:.0f}Kbps"


def format_bytes_total(bytes_val):
    """Накопленный трафик (не скорость) - для net1_total_rx/tx и т.п."""
    if bytes_val is None or bytes_val < 0:
        return "0MB"
    gb = bytes_val / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f}GB"
    mb = bytes_val / (1024 ** 2)
    return f"{mb:.0f}MB"


def _ipv4_to_proc_hex(ip):
    """127.0.0.1 -> '0100007F' - формат локального адреса в /proc/net/tcp
    (little-endian hex, как ядро его пишет)."""
    parts = ip.split(".")
    if len(parts) != 4:
        return None
    try:
        return "".join(f"{int(p):02X}" for p in reversed(parts))
    except ValueError:
        return None


def count_established_connections(ip):
    """Сколько сейчас ESTABLISHED TCP-соединений с локальным адресом ip
    (то есть привязанных к конкретному сетевому интерфейсу). Только IPv4 -
    для host-режима на типичном Unraid-сетапе этого достаточно."""
    hex_ip = _ipv4_to_proc_hex(ip) if ip else None
    if hex_ip is None:
        return None
    count = 0
    try:
        with open("/proc/net/tcp") as f:
            next(f)  # заголовок
            for line in f:
                fields = line.split()
                if len(fields) < 4:
                    continue
                local_ip, state = fields[1].split(":")[0], fields[3]
                if state == "01" and local_ip == hex_ip:  # 01 = ESTABLISHED
                    count += 1
    except Exception:
        return None
    return count


def list_disk_devices():
    if DISK_DEVICES.strip():
        return set(x.strip() for x in DISK_DEVICES.split(","))
    found = set()
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                fields = line.split()
                if DISK_NAME_RE.match(fields[2]):
                    found.add(fields[2])
    except Exception:
        pass
    return found


def read_disk_io_ticks(devices):
    ticks = {}
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                fields = line.split()
                name = fields[2]
                if name in devices:
                    ticks[name] = int(fields[12])
    except Exception:
        pass
    return ticks


def read_array_usage_tb():
    st = os.statvfs(ARRAY_PATH)
    total = st.f_frsize * st.f_blocks
    free = st.f_frsize * st.f_bavail
    tb = 10 ** 12
    total_tb = round(total / tb, 2)
    free_tb = round(free / tb, 2)
    used_pct = round((total - free) / total * 100) if total > 0 else 0
    return total_tb, free_tb, used_pct


def read_cache_usage():
    """Возвращает (pct, free_tb, total_tb)."""
    try:
        st = os.statvfs(CACHE_PATH)
        total = st.f_frsize * st.f_blocks
        free = st.f_frsize * st.f_bavail
        tb = 10 ** 12
        total_tb = round(total / tb, 2)
        free_tb = round(free / tb, 2)
        pct = round((total - free) / total * 100) if total > 0 else 0
        return pct, free_tb, total_tb
    except Exception:
        return 0, 0.0, 0.0


# ---------------- Tautulli ----------------

def tautulli_get(cmd, **params):
    params.update({"apikey": TAUTULLI_API_KEY, "cmd": cmd})
    r = requests.get(f"{TAUTULLI_URL}/api/v2", params=params, timeout=5)
    return r.json()["response"]["data"]


def format_bandwidth(kbps):
    """Tautulli отдаёт bandwidth сессии в Kbps."""
    kbps = kbps or 0
    if kbps <= 0:
        return "0 Kbps"
    mbps = kbps / 1000
    if mbps >= 1:
        return f"{mbps:.1f} Mbps"
    return f"{kbps:.0f} Kbps"


def get_activity():
    """Возвращает (sessions, ok). ok=False означает, что Tautulli сейчас
    недоступен (см. plex_server_status) - в отличие от ok=True с пустым
    sessions, что означает 'сервер жив, просто никто не смотрит'."""
    try:
        data = tautulli_get("get_activity")
        sessions = data.get("sessions", [])
        out = []
        for s in sessions:
            transcode_decision = (s.get("transcode_decision") or "").lower()
            mode = "D" if transcode_decision in ("", "direct play", "copy") else "T"
            user = s.get("friendly_name") or s.get("user", "") or ""
            out.append({
                "title": s.get("full_title") or s.get("title", ""),
                "user": user,
                "progress": int(s.get("progress_percent", 0) or 0),
                "mode": mode,
                "bandwidth": format_bandwidth(float(s.get("bandwidth") or 0)),
            })
        return out, True
    except Exception as e:
        print(f"[tautulli] get_activity failed: {e}", flush=True)
        return [], False


def get_library_counts():
    try:
        libs = tautulli_get("get_libraries")
        movies = sum(int(l.get("count", 0) or 0) for l in libs if l.get("section_type") == "movie")
        series = sum(int(l.get("count", 0) or 0) for l in libs if l.get("section_type") == "show")
        songs = sum(
            int(l.get("child_count", 0) or l.get("count", 0) or 0)
            for l in libs if l.get("section_type") == "artist"
        )
        return movies, series, songs
    except Exception as e:
        print(f"[tautulli] get_library_counts failed: {e}", flush=True)
        return 0, 0, 0


def format_ago(added_at):
    secs = max(0, time.time() - added_at)
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def get_recently_added(count, max_age_days):
    try:
        data = tautulli_get("get_recently_added", count=max(count * 4, 10))
        items = data.get("recently_added", [])
        now = time.time()
        out = []
        for it in items:
            added_at = int(it.get("added_at", 0) or 0)
            if added_at <= 0:
                continue
            if (now - added_at) / 86400 > max_age_days:
                continue
            media_type = it.get("media_type")
            if media_type == "episode":
                season = int(it.get("parent_media_index", 0) or 0)
                episode = int(it.get("media_index", 0) or 0)
                code = f"s{season:02d}e{episode:02d}"
                title = it.get("grandparent_title") or it.get("title", "")
            elif media_type == "movie":
                code = str(it.get("year", "") or "")
                title = it.get("title", "")
            else:
                continue
            out.append({"ago": format_ago(added_at), "code": code, "title": title})
            if len(out) >= count:
                break
        return out
    except Exception as e:
        print(f"[tautulli] get_recently_added failed: {e}", flush=True)
        return []


# ---------------- serial ----------------

def try_open_serial():
    try:
        s = serial.Serial(SERIAL_PORT, BAUD, timeout=1)
        time.sleep(2)
        with state_lock:
            state["serial_connected"] = True
        print(f"[serial] connected: {SERIAL_PORT}", flush=True)
        return s
    except (serial.SerialException, OSError):
        with state_lock:
            state["serial_connected"] = False
        return None


# ---------------- веб-интерфейс (Sensors: бары/яркость/net1-net2) ----------------

app = Flask(__name__)

SENSORS_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>shkaf-hud</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#ff8c2f">
<link rel="icon" type="image/png" href="https://raw.githubusercontent.com/RGCustom/shkaf-hud/main/favicon.png">
<style>
  * { box-sizing: border-box; }
  :root {
    --bg: #17181a; --panel: #1f2123; --border: #2c2e31;
    --text: #e6e6e6; --muted: #8a8d91; --accent: #ff8c2f; --danger: #e0483e;
  }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         margin:0; padding:24px 16px 60px; }
  .wrap { max-width:520px; margin:0 auto; }
  .brand { display:flex; align-items:center; gap:10px; margin-bottom:4px; }
  .brand .dot { width:9px; height:9px; border-radius:50%; background:var(--accent); box-shadow:0 0 8px var(--accent); }
  h1 { font-size:19px; font-weight:600; margin:0; }
  .nav { display:flex; gap:16px; margin:14px 0 24px; flex-wrap:wrap; }
  .nav a { color:var(--muted); text-decoration:none; font-size:13px; padding:6px 0; border-bottom:2px solid transparent; }
  .nav a.active { color:var(--text); border-bottom-color:var(--accent); }

  .banner { display:none; background:#3a2418; border:1px solid var(--danger); color:#ffb3ab;
            border-radius:10px; padding:12px 16px; margin-bottom:18px; font-size:13px;
            align-items:center; gap:10px; }
  .banner.show { display:flex; }
  .banner .b-dot { width:8px; height:8px; border-radius:50%; background:var(--danger); flex-shrink:0; }

  .card { background:var(--panel); border:1px solid var(--border); border-radius:14px;
          padding:22px; margin-bottom:18px; }
  .card h2 { font-size:11px; color:var(--muted); margin:0 0 18px; font-weight:600; }

  .bars { display:flex; gap:16px; align-items:flex-start; margin-bottom:18px; }
  .bar-wrap { flex:1; display:flex; flex-direction:column; align-items:center; gap:6px; min-width:0; }
  .bar-track { width:100%; max-width:40px; height:130px; background:#101112; border-radius:6px;
               display:flex; flex-direction:column-reverse; overflow:hidden; border:1px solid var(--border);
               padding:2px; gap:1px; }
  .led-px { flex:1 1 auto; min-height:1px; border-radius:1px; background:#101112; transition:background .2s; }
  .label { font-size:12px; color:var(--muted); text-align:center; }
  .label b { color:var(--text); font-size:13px; }

  .brightness-row, .field-row { display:flex; align-items:center; gap:10px; margin-top:12px; font-size:13px; }
  .brightness-row label, .field-row label { color:var(--muted); min-width:100px; }
  .brightness-row input[type=range] { flex:1; }
  .brightness-row .val { min-width:36px; text-align:right; color:var(--text); }

  select.iface { background:#101112; color:var(--text); border:1px solid var(--border);
                  border-radius:6px; font-size:11px; padding:3px 4px; width:100%; }

  footer { text-align:center; color:var(--border); font-size:11px; margin-top:20px; }
</style></head>
<body>
<div class="wrap">
  <div class="brand"><span class="dot"></span><h1>shkaf-hud</h1></div>
  <div class="nav"><a href="/" class="active">Sensors</a><a href="/settings">Settings</a><a href="/screens">OLED screens</a><a href="/flash">Flash</a></div>

  <div class="banner" id="banner"><span class="b-dot"></span>
    Pro Micro не подключена - лента и OLED не обновляются, статистика продолжает собираться</div>

  <div class="card">
    <h2>LED БАРЫ</h2>
    <div class="bars">
      <div class="bar-wrap"><div class="bar-track" id="pixels-bar0"></div>
        <div class="label"><b><span id="val-bar0"></span></b></div></div>
      <div class="bar-wrap"><div class="bar-track" id="pixels-bar1"></div>
        <div class="label"><b><span id="val-bar1"></span></b></div></div>
      <div class="bar-wrap"><div class="bar-track" id="pixels-bar2"></div>
        <div class="label"><b><span id="val-bar2"></span></b></div></div>
      <div class="bar-wrap"><div class="bar-track" id="pixels-bar3"></div>
        <div class="label"><b><span id="val-bar3"></span></b></div></div>
    </div>
    <div class="brightness-row">
      <label>Яркость</label>
      <input type="range" id="brightness" min="0" max="100" value="15">
      <span class="val" id="brightness-val">15%</span>
    </div>
  </div>

  <div class="card">
    <h2>OLED (текущий экран)</h2>
    <div style="background:#000;color:#7fd8ff;font-family:monospace;font-size:18px;padding:16px;border-radius:8px;line-height:1.5" id="oled"></div>
    <div class="brightness-row">
      <label>Контраст</label>
      <input type="range" id="contrast" min="0" max="255" value="255">
      <span class="val" id="contrast-val">255</span>
    </div>
  </div>

  <div class="card">
    <h2>СЕТЕВЫЕ ИНТЕРФЕЙСЫ (для экранов Network 1/2)</h2>
    <div class="field-row"><label>Network 1</label><select class="iface" id="net1-iface"></select></div>
    <div class="field-row"><label>Network 2</label><select class="iface" id="net2-iface"><option value="">(не выбран)</option></select></div>
  </div>

  <footer>shkaf-hud</footer>
</div>

<script>
const bars = ["bar0","bar1","bar2","bar3"];
let editingBrightness = false, editingContrast = false, editingIfaces = false;
let ifacesPopulated = false, pixelsBuilt = false;

const brightnessEl = document.getElementById("brightness");
brightnessEl.addEventListener("input", () => {
  editingBrightness = true;
  document.getElementById("brightness-val").textContent = brightnessEl.value + "%";
});
brightnessEl.addEventListener("change", () => {
  fetch("/api/brightness", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ value: parseInt(brightnessEl.value) }) }).then(() => editingBrightness = false);
});

const contrastEl = document.getElementById("contrast");
contrastEl.addEventListener("input", () => {
  editingContrast = true;
  document.getElementById("contrast-val").textContent = contrastEl.value;
});
contrastEl.addEventListener("change", () => {
  fetch("/api/contrast", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ value: parseInt(contrastEl.value) }) }).then(() => editingContrast = false);
});

function populateIfaces(ifaces, net1, net2) {
  const sel1 = document.getElementById("net1-iface");
  const sel2 = document.getElementById("net2-iface");
  sel1.innerHTML = "";
  sel2.innerHTML = '<option value="">(не выбран)</option>';
  ifaces.forEach(name => {
    const o1 = document.createElement("option");
    o1.value = name; o1.textContent = name;
    if (name === net1) o1.selected = true;
    sel1.appendChild(o1);
    const o2 = document.createElement("option");
    o2.value = name; o2.textContent = name;
    if (name === net2) o2.selected = true;
    sel2.appendChild(o2);
  });
  ifacesPopulated = true;

  [sel1, sel2].forEach(sel => sel.addEventListener("change", () => {
    editingIfaces = true;
    fetch("/api/net-ifaces", { method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ net1_iface: sel1.value, net2_iface: sel2.value }) })
      .then(() => editingIfaces = false);
  }));
}

// Превью строится динамически по leds_per_bar, пришедшему от бэкенда -
// число диодов на бар нигде на фронте не захардкожено.
function buildPixelGrid(ledsPerBar) {
  bars.forEach(k => {
    const track = document.getElementById("pixels-" + k);
    track.innerHTML = "";
    for (let i = 0; i < ledsPerBar; i++) {
      const sq = document.createElement("div");
      sq.className = "led-px";
      sq.id = "px-" + k + "-" + i;
      track.appendChild(sq);
    }
  });
  pixelsBuilt = true;
}

function refresh() {
  fetch("/api/state").then(r => r.json()).then(s => {
    document.getElementById("banner").classList.toggle("show", !s.serial_connected);
    if (!ifacesPopulated) populateIfaces(s.available_interfaces, s.cfg.net1_iface, s.cfg.net2_iface);
    if (!pixelsBuilt) buildPixelGrid(s.leds_per_bar);

    bars.forEach(k => {
      const bar = s.bars[k];
      if (!bar) return;
      bar.pixels.forEach((hex, i) => {
        const px = document.getElementById("px-" + k + "-" + i);
        if (px) px.style.background = "#" + hex;
      });
      const label = (bar.mode === "center" || bar.mode === "edges")
        ? (bar.pct_bottom + "% / " + bar.pct_top + "%")
        : (bar.pct_bottom + "%");
      document.getElementById("val-" + k).textContent = label;
    });

    if (!editingBrightness) {
      brightnessEl.value = s.cfg.brightness;
      document.getElementById("brightness-val").textContent = s.cfg.brightness + "%";
    }

    if (!editingContrast) {
      contrastEl.value = s.cfg.contrast;
      document.getElementById("contrast-val").textContent = s.cfg.contrast;
    }

    document.getElementById("oled").innerHTML = s.oled_lines.map(l => l || "&nbsp;").join("<br>");
  });
}

if ('serviceWorker' in navigator) { navigator.serviceWorker.register('/sw.js').catch(() => {}); }

setInterval(refresh, 1000);
refresh();
</script>
</body></html>
"""


@app.route("/")
def index():
    return Response(SENSORS_PAGE_HTML, mimetype="text/html")


@app.route("/api/state")
def api_state():
    with state_lock:
        out = dict(state)
        out["metrics"] = BAR_METRICS
        out["available_interfaces"] = list_real_interfaces()
        out["leds_per_bar"] = ledbar.LEDS_PER_BAR
        return jsonify(out)


@app.route("/api/colors", methods=["POST"])
def api_colors():
    body = request.get_json(force=True)
    with state_lock:
        for k in ("bar0", "bar1", "bar2", "bar3"):
            if k in body:
                for stop in ("c1", "c2", "c3"):
                    if stop in body[k]:
                        state["cfg"]["colors"][k][stop] = body[k][stop].upper()
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/assignment", methods=["POST"])
def api_assignment():
    body = request.get_json(force=True)
    with state_lock:
        for k in ("bar0", "bar1", "bar2", "bar3"):
            if k in body and body[k] in BAR_METRICS:
                state["cfg"]["assignment"][k] = body[k]
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/brightness", methods=["POST"])
def api_brightness():
    body = request.get_json(force=True)
    with state_lock:
        state["cfg"]["brightness"] = max(0, min(100, int(body.get("value", state["cfg"]["brightness"]))))
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/contrast", methods=["POST"])
def api_contrast():
    body = request.get_json(force=True)
    with state_lock:
        state["cfg"]["contrast"] = max(0, min(255, int(body.get("value", state["cfg"]["contrast"]))))
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/solid", methods=["POST"])
def api_solid():
    body = request.get_json(force=True)
    with state_lock:
        for k in ("bar0", "bar1", "bar2", "bar3"):
            if k in body:
                state["cfg"]["solid"][k] = bool(body[k])
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/solid_top", methods=["POST"])
def api_solid_top():
    body = request.get_json(force=True)
    with state_lock:
        for k in ("bar0", "bar1", "bar2", "bar3"):
            if k in body:
                state["cfg"]["solid_top"][k] = bool(body[k])
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/colors_top", methods=["POST"])
def api_colors_top():
    body = request.get_json(force=True)
    with state_lock:
        for k in ("bar0", "bar1", "bar2", "bar3"):
            if k in body:
                for stop in ("c1", "c2", "c3"):
                    if stop in body[k]:
                        state["cfg"]["colors_top"][k][stop] = body[k][stop].upper()
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/assignment_top", methods=["POST"])
def api_assignment_top():
    body = request.get_json(force=True)
    with state_lock:
        for k in ("bar0", "bar1", "bar2", "bar3"):
            if k in body and body[k] in BAR_METRICS:
                state["cfg"]["assignment_top"][k] = body[k]
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/mode", methods=["POST"])
def api_mode():
    body = request.get_json(force=True)
    with state_lock:
        for k in ("bar0", "bar1", "bar2", "bar3"):
            if k in body and body[k] in BAR_MODES:
                state["cfg"]["mode"][k] = body[k]
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/peak", methods=["POST"])
def api_peak():
    body = request.get_json(force=True)
    with state_lock:
        for k in ("bar0", "bar1", "bar2", "bar3"):
            if k in body:
                entry = body[k]
                if "enabled" in entry:
                    state["cfg"]["peak"][k]["enabled"] = bool(entry["enabled"])
                if "style" in entry and entry["style"] in ("hold", "fade"):
                    state["cfg"]["peak"][k]["style"] = entry["style"]
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/peak_timing", methods=["POST"])
def api_peak_timing():
    """Общие тайминги peak hold (один слайдер на hold, один на fade - не
    по одному на бар). Диапазон 0-10 сек, шаг 0.1 - клампим и округляем
    тут же, чтобы в settings.json не залетело мусора при кривом запросе."""
    body = request.get_json(force=True)
    with state_lock:
        if "hold_seconds" in body:
            state["cfg"]["peak_hold_seconds"] = round(max(0.0, min(10.0, float(body["hold_seconds"]))), 1)
        if "fade_seconds" in body:
            state["cfg"]["peak_fade_seconds"] = round(max(0.0, min(10.0, float(body["fade_seconds"]))), 1)
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/net-ifaces", methods=["POST"])
def api_net_ifaces():
    body = request.get_json(force=True)
    with state_lock:
        if "net1_iface" in body:
            state["cfg"]["net1_iface"] = body["net1_iface"]
        if "net2_iface" in body:
            state["cfg"]["net2_iface"] = body["net2_iface"]
        save_settings(state["cfg"])
    return jsonify({"ok": True})


screens_webui.register_screens_routes(app, get_context)
settings_webui.register_settings_routes(app)
flash_webui.register_flash_routes(app, SERIAL_PORT, flashing_event)


def run_web():
    app.run(host="0.0.0.0", port=WEB_PORT, use_reloader=False)


# ---------------- главный цикл ----------------

def main():
    print(f"[shkaf-hud] starting, version {SCRIPT_VERSION}", flush=True)

    with state_lock:
        state["cfg"] = load_settings()

    threading.Thread(target=run_web, daemon=True).start()

    ser = try_open_serial()
    last_reconnect_attempt = time.time()

    prev_idle, prev_total = read_cpu_times()
    prev_net = read_net_bytes(NET_IFACE)
    disk_devices = list_disk_devices()
    prev_disk_ticks = read_disk_io_ticks(disk_devices)

    movies, series, songs = get_library_counts()
    total_tb, free_tb_array, arr_pct = read_array_usage_tb()
    cache_pct, free_tb_cache, cache_total_tb = read_cache_usage()
    last_library_refresh = time.time()
    last_array_refresh = time.time()
    last_cache_refresh = time.time()

    recent_items = []
    last_recent_refresh = 0.0

    prev_core_times = read_per_core_times()

    prev_net1_iface, prev_net2_iface = None, None
    prev_net1_rx = prev_net1_tx = prev_net2_rx = prev_net2_tx = None
    # base_rx/base_tx - точка отсчёта для накопленного трафика (net1_total_rx и
    # т.п.): фиксируется на первом успешном чтении интерфейса и сбрасывается
    # при смене выбранного интерфейса (аналогично prev_net1_rx выше).
    net1_base_rx = net1_base_tx = net2_base_rx = net2_base_tx = None

    rotation = screens.RotationState()
    proto = protocol.ProtocolState(full_resync_seconds=FULL_RESYNC_SECONDS)

    # Peak hold - по трекеру на "низ" и "верх" каждого бара (для classic/
    # classic_reverse используется только *_bottom, *_top просто простаивает).
    # Стейт живёт тут, в главном цикле, а не в state["cfg"] - это не
    # настройка, а текущее физическое положение точки во времени.
    peak_trackers = {}
    for b in ("bar0", "bar1", "bar2", "bar3"):
        peak_trackers[f"{b}_bottom"] = ledbar.PeakHold()
        peak_trackers[f"{b}_top"] = ledbar.PeakHold()

    prev_time = time.time()

    while True:
        time.sleep(POLL_INTERVAL)
        now = time.time()
        dt = now - prev_time

        # CPU %
        idle, total = read_cpu_times()
        d_idle, d_total = idle - prev_idle, total - prev_total
        cpu_pct = 0.0 if d_total == 0 else (1 - d_idle / d_total) * 100.0
        prev_idle, prev_total = idle, total

        # RAM % + абсолютные ГБ
        ram_pct, ram_used_gb, ram_total_gb = read_ram_details()

        # SWAP %
        swap_pct = read_swap_percent()

        # Load average (1/5/15 мин)
        load1, load5, load15 = read_load_avg()

        # Частота CPU (среднее по ядрам, МГц)
        cpu_freq_mhz = read_cpu_freq_mhz()

        # Загрузка самого нагруженного ядра (в отличие от cpu_pct - среднего по всем)
        curr_core_times = read_per_core_times()
        cpu_pct_core_max = compute_max_core_pct(prev_core_times, curr_core_times)
        prev_core_times = curr_core_times

        # NET % (общий, для LED бара, если на него назначена метрика "net")
        net_bytes = read_net_bytes(NET_IFACE)
        net_pct = 0.0
        if net_bytes is not None and prev_net is not None and dt > 0:
            mbps = (net_bytes - prev_net) * 8 / 1_000_000 / dt
            net_pct = max(0.0, min(100.0, mbps / NET_MAX_MBPS * 100.0))
        prev_net = net_bytes

        # DISK %util
        disk_ticks = read_disk_io_ticks(disk_devices)
        disk_pct = 0.0
        if dt > 0:
            utils = []
            for name, ticks in disk_ticks.items():
                prev = prev_disk_ticks.get(name, ticks)
                utils.append(max(0.0, min(100.0, (ticks - prev) / (dt * 1000) * 100.0)))
            if utils:
                disk_pct = max(utils)
        prev_disk_ticks = disk_ticks

        cpu_temp_c = read_cpu_temp_c()
        cputemp_pct = read_cpu_temp_pct()

        # периодические обновления
        if now - last_library_refresh > LIBRARY_REFRESH_SECONDS:
            movies, series, songs = get_library_counts()
            last_library_refresh = now
        if now - last_array_refresh > ARRAY_REFRESH_SECONDS:
            total_tb, free_tb_array, arr_pct = read_array_usage_tb()
            last_array_refresh = now
        if now - last_cache_refresh > CACHE_REFRESH_SECONDS:
            cache_pct, free_tb_cache, cache_total_tb = read_cache_usage()
            last_cache_refresh = now
        if now - last_recent_refresh > RECENT_REFRESH_SECONDS:
            recent_items = get_recently_added(RECENT_COUNT, RECENT_MAX_AGE_DAYS)
            last_recent_refresh = now

        sessions, plex_online = get_activity()
        plex_server_status = "online" if plex_online else "offline"
        plex_transcode_count = sum(1 for s in sessions if s["mode"] == "T")
        plex_users_count = len({s["user"] for s in sessions if s["user"]})

        # qBittorrent: суммарная статистика по ВСЕМ торрентам (в т.ч. для LED-баров)
        qbt_totals = qbittorrent.get_qbt_totals()

        # сеть по интерфейсам (net1/net2, для OLED-экранов)
        with state_lock:
            net1_iface = state["cfg"]["net1_iface"]
            net2_iface = state["cfg"]["net2_iface"]

        if net1_iface != prev_net1_iface:
            prev_net1_rx = prev_net1_tx = None
            net1_base_rx = net1_base_tx = None
            prev_net1_iface = net1_iface
        if net2_iface != prev_net2_iface:
            prev_net2_rx = prev_net2_tx = None
            net2_base_rx = net2_base_tx = None
            prev_net2_iface = net2_iface

        net_info = {"net1": None, "net2": None}
        for slot, iface in (("net1", net1_iface), ("net2", net2_iface)):
            if not iface:
                continue
            rx, tx = read_iface_rx_tx(iface)
            if rx is None:
                continue
            prev_rx = prev_net1_rx if slot == "net1" else prev_net2_rx
            prev_tx = prev_net1_tx if slot == "net1" else prev_net2_tx
            rx_str = format_rate(rx - prev_rx, dt) if prev_rx is not None and dt > 0 else "0Kbps"
            tx_str = format_rate(tx - prev_tx, dt) if prev_tx is not None and dt > 0 else "0Kbps"

            # Накопленный трафик с момента старта контейнера (или смены
            # интерфейса) - base_rx/base_tx фиксируются один раз на первом
            # успешном чтении, дальше total = текущее - база.
            base_rx = net1_base_rx if slot == "net1" else net2_base_rx
            base_tx = net1_base_tx if slot == "net1" else net2_base_tx
            if base_rx is None:
                base_rx, base_tx = rx, tx
            total_rx_str = format_bytes_total(rx - base_rx)
            total_tx_str = format_bytes_total(tx - base_tx)

            iface_ip = read_iface_ip(iface)
            conn_count = count_established_connections(iface_ip)

            if slot == "net1":
                prev_net1_rx, prev_net1_tx = rx, tx
                net1_base_rx, net1_base_tx = base_rx, base_tx
            else:
                prev_net2_rx, prev_net2_tx = rx, tx
                net2_base_rx, net2_base_tx = base_rx, base_tx

            net_info[slot] = {
                "name": iface, "speed": read_iface_speed(iface), "ip": iface_ip,
                "rx": rx_str, "tx": tx_str,
                "total_rx": total_rx_str, "total_tx": total_tx_str,
                "conn_count": conn_count if conn_count is not None else 0,
            }

        prev_time = now

        # ---- собрать context для variables.py/templates.py/screens.py ----
        context = {
            "cpu_pct": round(cpu_pct), "ram_pct": round(ram_pct),
            "cpu_temp_c": cpu_temp_c, "disk_pct": round(disk_pct),
            "array_pct": arr_pct, "cache_pct": cache_pct,
            "free_tb": round(free_tb_array + free_tb_cache, 2),

            "array_used_tb": round(total_tb - free_tb_array, 2),
            "array_total_tb": total_tb,
            "cache_free_tb": free_tb_cache,
            "cache_total_tb": cache_total_tb,

            "ram_used_gb": ram_used_gb,
            "ram_total_gb": ram_total_gb,
            "swap_pct": round(swap_pct),

            "load1": round(load1, 2), "load5": round(load5, 2), "load15": round(load15, 2),
            "cpu_freq_mhz": cpu_freq_mhz,
            "cpu_pct_core_max": round(cpu_pct_core_max),

            "uptime": format_duration(read_uptime_seconds()),
            "container_uptime": format_duration(now - CONTAINER_START_TIME),
            "time_now": datetime.datetime.now().strftime("%H:%M"),

            "plex_server_status": plex_server_status,
            "plex_transcode_count": plex_transcode_count,
            "plex_users_count": plex_users_count,

            "qbt_total_dl": qbt_totals["total_dl"],
            "qbt_total_ul": qbt_totals["total_ul"],
            "qbt_count_all": qbt_totals["count_all"],
            "qbt_ratio": qbt_totals["ratio"],
            "qbt_free_space_gb": qbt_totals["free_space"],

            "net": net_info,
            "plex": {"movies": movies, "series": series, "songs": songs},
            "streams": sessions,
            "recent": recent_items,
            "qbt": qbittorrent.get_qbt_active(),
        }
        with _context_lock:
            _last_context.clear()
            _last_context.update(context)

        # ---- OLED: текущие 3 строки по ротации ----
        current_screens = screens_webui.get_screens()
        lines = rotation.current_lines(current_screens, context, now=now)
        with state_lock:
            state["oled_lines"] = lines

        # ---- LED бары: посчитать значение метрики(к) для каждого бара ----
        common_metrics = {
            "cpu": cpu_pct, "ram": ram_pct, "net": net_pct, "disk": disk_pct,
            "array": arr_pct, "cache": cache_pct, "cputemp": cputemp_pct,
            "swap": swap_pct,
            "qbt_dl": qbt_totals["dl_pct"], "qbt_ul": qbt_totals["ul_pct"],
        }
        with state_lock:
            colors = state["cfg"]["colors"]
            colors_top = state["cfg"]["colors_top"]
            assignment = state["cfg"]["assignment"]
            assignment_top = state["cfg"]["assignment_top"]
            mode_cfg = state["cfg"]["mode"]
            solid = state["cfg"]["solid"]
            solid_top = state["cfg"]["solid_top"]
            peak_cfg = state["cfg"]["peak"]
            peak_hold_seconds = state["cfg"]["peak_hold_seconds"]
            peak_fade_seconds = state["cfg"]["peak_fade_seconds"]
            brightness = state["cfg"]["brightness"]
            contrast = state["cfg"]["contrast"]

        leds_per_bar = ledbar.LEDS_PER_BAR

        # ---- собрать пиксели + протокол (только изменившееся) ----
        # Градиент/solid/center/edges/reverse/peak-логика считается тут, на
        # сервере (ledbar.py) - Arduino получает уже готовый цвет каждого
        # диода и просто зажигает его, ему всё равно, какой это режим.
        bars_state = {}
        proto_values = {}
        for i, b in enumerate(("bar0", "bar1", "bar2", "bar3"), start=1):
            bar_mode = mode_cfg.get(b, "classic")
            if bar_mode not in BAR_MODES:
                bar_mode = "classic"
            peak_info = peak_cfg.get(b, {"enabled": False, "style": "hold"})
            peak_enabled = peak_info.get("enabled", False)

            bottom_tracker = peak_trackers[f"{b}_bottom"]
            bottom_tracker.set_style(peak_info.get("style", "hold"))
            bottom_tracker.set_timings(peak_hold_seconds, peak_fade_seconds)

            pct_bottom = round(common_metrics.get(assignment[b], 0))
            bottom_peak = bottom_tracker.update(pct_bottom, now)

            if bar_mode in ("center", "edges"):
                top_tracker = peak_trackers[f"{b}_top"]
                top_tracker.set_style(peak_info.get("style", "hold"))
                top_tracker.set_timings(peak_hold_seconds, peak_fade_seconds)

                pct_top = round(common_metrics.get(assignment_top[b], 0))
                top_peak = top_tracker.update(pct_top, now)

                compute_fn = ledbar.compute_bar_pixels_center if bar_mode == "center" else ledbar.compute_bar_pixels_edges
                pixels = compute_fn(
                    pct_bottom, pct_top,
                    colors[b]["c1"], colors[b]["c2"], colors[b]["c3"], solid[b],
                    colors_top[b]["c1"], colors_top[b]["c2"], colors_top[b]["c3"], solid_top[b],
                    leds_per_bar=leds_per_bar,
                    peak_pct_bottom=bottom_peak if peak_enabled else None,
                    peak_pct_top=top_peak if peak_enabled else None,
                )
                bars_state[b] = {"mode": bar_mode, "pixels": pixels, "pct_bottom": pct_bottom, "pct_top": pct_top}
            else:
                pixels = ledbar.compute_bar_pixels(
                    pct_bottom, colors[b]["c1"], colors[b]["c2"], colors[b]["c3"], solid[b],
                    leds_per_bar=leds_per_bar,
                    peak_pct=bottom_peak if peak_enabled else None,
                    reverse=(bar_mode == "classic_reverse"),
                )
                bars_state[b] = {"mode": bar_mode, "pixels": pixels, "pct_bottom": pct_bottom, "pct_top": None}

            proto_values[f"BAR{i}"] = protocol.pack_bar_pixels(pixels)

        with state_lock:
            state["bars"] = bars_state

        proto_values["BRI"] = str(brightness)
        proto_values["CON"] = str(contrast)
        proto_values["L1"], proto_values["L2"], proto_values["L3"] = lines

        line_to_send = proto.build(proto_values, now=now)

        if flashing_event.is_set():
            # идёт заливка прошивки через /api/flash - avrdude сейчас сам
            # владеет USB-портом платы, ни писать, ни переподключаться нельзя.
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                with state_lock:
                    state["serial_connected"] = False
            last_reconnect_attempt = now
        elif ser is not None:
            if line_to_send is not None:
                try:
                    ser.write((line_to_send + "\n").encode("utf-8"))
                except (serial.SerialException, OSError):
                    print("[serial] write failed, will retry connecting", flush=True)
                    try:
                        ser.close()
                    except Exception:
                        pass
                    ser = None
                    with state_lock:
                        state["serial_connected"] = False
                    last_reconnect_attempt = now
        else:
            if now - last_reconnect_attempt > 5:
                ser = try_open_serial()
                if ser is not None:
                    proto.reset()  # только что переподключились - шлём полное состояние
                last_reconnect_attempt = now


if __name__ == "__main__":
    main()
