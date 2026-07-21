#!/usr/bin/env python3
"""
shkaf_stats_bridge.py  (контейнер: shkaf-hud)

Собирает CPU/RAM/NET/DISK(%util) хоста + активность и статистику библиотек
через Tautulli, шлёт по USB-serial на Pro Micro одну строку за тик.

Формат строки (pipe-delimited, \n в конце), общие поля есть всегда:
    CPU:<0-100>|RAM:<0-100>|NET:<0-100>|DISK:<0-100>|SCREEN:LIB|MOVIES:<n>|SERIES:<n>|USED:<TB>|FREE:<TB>
или
    CPU:<0-100>|RAM:<0-100>|NET:<0-100>|DISK:<0-100>|SCREEN:STREAM|IDX:<n>|CNT:<n>|TITLE:<...>|USER:<...>|PROG:<0-100>

Ротация экрана (LIB / STREAM 1..N) решается на хосте - Arduino просто
рендерит то, что прислали, никакой логики выбора экрана на нём нет.

Зависимости:
    pip install pyserial requests --break-system-packages
"""

import os
import re
import time
import serial
import requests

# ---------------- КОНФИГ (через переменные окружения) ----------------

SERIAL_PORT = os.environ.get("SERIAL_PORT", "/dev/ttyACM0")
BAUD = int(os.environ.get("BAUD", "115200"))

TAUTULLI_URL = os.environ.get("TAUTULLI_URL", "http://127.0.0.1:8181")
TAUTULLI_API_KEY = os.environ.get("TAUTULLI_API_KEY", "")

NET_IFACE = os.environ.get("NET_IFACE", "eth0")
NET_MAX_MBPS = float(os.environ.get("NET_MAX_MBPS", "500"))

# Диски для %util: пусто = автоопределение всех sdX/nvmeXnY
DISK_DEVICES = os.environ.get("DISK_DEVICES", "")

ARRAY_PATH = os.environ.get("ARRAY_PATH", "/mnt/user")
ARRAY_REFRESH_SECONDS = float(os.environ.get("ARRAY_REFRESH_SECONDS", "60"))
LIBRARY_REFRESH_SECONDS = float(os.environ.get("LIBRARY_REFRESH_SECONDS", "300"))
SCREEN_ROTATE_SECONDS = float(os.environ.get("SCREEN_ROTATE_SECONDS", "4"))

POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))

# -------------------------------------------------------------

DISK_NAME_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+)$")


def sanitize(s: str) -> str:
    return (s or "").replace("|", "/").replace("\n", " ").replace("\r", "")[:24]


# ---------- CPU / RAM / NET ----------

def read_cpu_times():
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    vals = list(map(int, parts))
    idle = vals[3] + vals[4]
    total = sum(vals)
    return idle, total


def read_ram_percent():
    meminfo = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, val = line.split(":")
            meminfo[key.strip()] = int(val.strip().split()[0])
    total = meminfo.get("MemTotal", 1)
    available = meminfo.get("MemAvailable", total)
    return max(0.0, min(100.0, (total - available) / total * 100.0))


def read_net_bytes(iface: str):
    with open("/proc/net/dev") as f:
        for line in f:
            if ":" not in line:
                continue
            name, rest = line.split(":", 1)
            if name.strip() != iface:
                continue
            fields = rest.split()
            return int(fields[0]) + int(fields[8])
    return None


# ---------- DISK %util ----------

def list_disk_devices():
    if DISK_DEVICES.strip():
        return set(x.strip() for x in DISK_DEVICES.split(","))
    found = set()
    with open("/proc/diskstats") as f:
        for line in f:
            fields = line.split()
            if DISK_NAME_RE.match(fields[2]):
                found.add(fields[2])
    return found


def read_disk_io_ticks(devices):
    """io_ticks (мс, потраченные на I/O) по каждому диску - для %util."""
    ticks = {}
    with open("/proc/diskstats") as f:
        for line in f:
            fields = line.split()
            name = fields[2]
            if name in devices:
                ticks[name] = int(fields[12])  # io_ticks
    return ticks


# ---------- Array usage (df) ----------

def read_array_usage_tb():
    st = os.statvfs(ARRAY_PATH)
    total = st.f_frsize * st.f_blocks
    free = st.f_frsize * st.f_bavail
    used = total - free
    tb = 1024 ** 4
    return round(used / tb), round(free / tb)


# ---------- Tautulli ----------

def tautulli_get(cmd, **params):
    params.update({"apikey": TAUTULLI_API_KEY, "cmd": cmd})
    r = requests.get(f"{TAUTULLI_URL}/api/v2", params=params, timeout=5)
    return r.json()["response"]["data"]


def get_activity():
    try:
        data = tautulli_get("get_activity")
        sessions = data.get("sessions", [])
        out = []
        for s in sessions:
            out.append({
                "title": s.get("full_title") or s.get("title", ""),
                "user": s.get("friendly_name") or s.get("user", ""),
                "progress": int(s.get("progress_percent", 0) or 0),
            })
        return out
    except Exception:
        return []


def get_library_counts():
    try:
        libs = tautulli_get("get_libraries")
        movies = sum(int(l.get("count", 0) or 0) for l in libs if l.get("section_type") == "movie")
        series = sum(int(l.get("count", 0) or 0) for l in libs if l.get("section_type") == "show")
        return movies, series
    except Exception:
        return 0, 0


# ---------------- главный цикл ----------------

def main():
    ser = serial.Serial(SERIAL_PORT, BAUD, timeout=1)
    time.sleep(2)

    prev_idle, prev_total = read_cpu_times()
    prev_net = read_net_bytes(NET_IFACE)
    disk_devices = list_disk_devices()
    prev_disk_ticks = read_disk_io_ticks(disk_devices)
    prev_time = time.time()

    movies, series = get_library_counts()
    used_tb, free_tb = read_array_usage_tb()
    last_library_refresh = time.time()
    last_array_refresh = time.time()

    screen_index = 0
    last_screen_switch = time.time()

    while True:
        time.sleep(POLL_INTERVAL)
        now = time.time()
        dt = now - prev_time

        # CPU %
        idle, total = read_cpu_times()
        d_idle = idle - prev_idle
        d_total = total - prev_total
        cpu_pct = 0.0 if d_total == 0 else (1 - d_idle / d_total) * 100.0
        prev_idle, prev_total = idle, total

        # RAM %
        ram_pct = read_ram_percent()

        # NET %
        net_bytes = read_net_bytes(NET_IFACE)
        net_pct = 0.0
        if net_bytes is not None and prev_net is not None and dt > 0:
            mbps = (net_bytes - prev_net) * 8 / 1_000_000 / dt
            net_pct = max(0.0, min(100.0, mbps / NET_MAX_MBPS * 100.0))
        prev_net = net_bytes

        # DISK %util (максимум по дискам - самый занятый диск в моменте)
        disk_ticks = read_disk_io_ticks(disk_devices)
        disk_pct = 0.0
        if dt > 0:
            utils = []
            for name, ticks in disk_ticks.items():
                prev = prev_disk_ticks.get(name, ticks)
                util = (ticks - prev) / (dt * 1000) * 100.0
                utils.append(max(0.0, min(100.0, util)))
            if utils:
                disk_pct = max(utils)
        prev_disk_ticks = disk_ticks

        prev_time = now

        # Библиотека - редко обновляем
        if now - last_library_refresh > LIBRARY_REFRESH_SECONDS:
            movies, series = get_library_counts()
            last_library_refresh = now

        # Массив - редко обновляем
        if now - last_array_refresh > ARRAY_REFRESH_SECONDS:
            used_tb, free_tb = read_array_usage_tb()
            last_array_refresh = now

        # Активные стримы
        sessions = get_activity()
        count = len(sessions)

        # Ротация экранов: [LIB, stream1, stream2, ...]
        if count == 0:
            screen_index = 0
        elif now - last_screen_switch > SCREEN_ROTATE_SECONDS:
            screen_index = (screen_index + 1) % (count + 1)  # +1 за экран LIB
            last_screen_switch = now

        common = f"CPU:{cpu_pct:.0f}|RAM:{ram_pct:.0f}|NET:{net_pct:.0f}|DISK:{disk_pct:.0f}"

        if count == 0 or screen_index == 0:
            line = f"{common}|SCREEN:LIB|MOVIES:{movies}|SERIES:{series}|USED:{used_tb}|FREE:{free_tb}\n"
        else:
            s = sessions[screen_index - 1]
            line = (
                f"{common}|SCREEN:STREAM|IDX:{screen_index}|CNT:{count}|"
                f"TITLE:{sanitize(s['title'])}|USER:{sanitize(s['user'])}|PROG:{s['progress']}\n"
            )

        try:
            ser.write(line.encode("utf-8"))
        except (serial.SerialException, OSError):
            try:
                ser.close()
            except Exception:
                pass
            time.sleep(2)
            ser = serial.Serial(SERIAL_PORT, BAUD, timeout=1)
            time.sleep(2)


if __name__ == "__main__":
    main()
