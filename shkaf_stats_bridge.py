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
import threading

import serial
import requests
from flask import Flask, request, jsonify, Response

import variables
import templates
import screens
import screens_webui
import protocol
import ledbar

SCRIPT_VERSION = "2026-07-25-1"

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

BIGCACHE_PATH = os.environ.get("BIGCACHE_PATH", "/mnt/bigcache")
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
DEFAULT_ASSIGNMENT = {"bar0": "cpu", "bar1": "ram", "bar2": "net", "bar3": "disk"}
DEFAULT_BRIGHTNESS = 15
DEFAULT_SOLID = {"bar0": False, "bar1": False, "bar2": False, "bar3": False}

BAR_METRICS = {
    "cpu": "CPU",
    "ram": "RAM",
    "net": "NET (общий, для LED)",
    "disk": "DISK %util",
    "array": "Array %",
    "cache": "BigCache %",
    "cputemp": "CPU temp",
}

DEFAULT_SETTINGS = {
    "colors": DEFAULT_COLORS,
    "assignment": DEFAULT_ASSIGNMENT,
    "brightness": DEFAULT_BRIGHTNESS,
    "solid": DEFAULT_SOLID,
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
    "bar0": 0, "bar1": 0, "bar2": 0, "bar3": 0,
    "cfg": load_settings(),
    "serial_connected": False,
    "oled_lines": ["", "", ""],
}

# последний известный context - для /api/preview на странице /screens
_last_context = {}
_context_lock = threading.Lock()


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


def read_ram_percent():
    meminfo = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, val = line.split(":")
            meminfo[key.strip()] = int(val.strip().split()[0])
    total = meminfo.get("MemTotal", 1)
    available = meminfo.get("MemAvailable", total)
    return max(0.0, min(100.0, (total - available) / total * 100.0))


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
    try:
        st = os.statvfs(BIGCACHE_PATH)
        total = st.f_frsize * st.f_blocks
        free = st.f_frsize * st.f_bavail
        tb = 10 ** 12
        free_tb = round(free / tb, 2)
        pct = round((total - free) / total * 100) if total > 0 else 0
        return pct, free_tb
    except Exception:
        return 0, 0.0


# ---------------- Tautulli ----------------

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
            transcode_decision = (s.get("transcode_decision") or "").lower()
            mode = "D" if transcode_decision in ("", "direct play", "copy") else "T"
            user = s.get("friendly_name") or s.get("user", "") or ""
            out.append({
                "title": s.get("full_title") or s.get("title", ""),
                "user": user,
                "progress": int(s.get("progress_percent", 0) or 0),
                "mode": mode,
            })
        return out
    except Exception as e:
        print(f"[tautulli] get_activity failed: {e}", flush=True)
        return []


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
  .nav { display:flex; gap:16px; margin:14px 0 24px; }
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
               display:flex; flex-direction:column-reverse; overflow:hidden; border:1px solid var(--border); }
  .bar-fill { width:100%; transition:height .3s, background .2s; }
  .label { font-size:12px; color:var(--muted); text-align:center; }
  .label b { color:var(--text); font-size:13px; }
  input[type=color] { width:20px; height:20px; border:none; background:none; border-radius:6px; cursor:pointer; padding:0; }
  .grad-colors { display:flex; gap:3px; }
  select.metric, select.iface { background:#101112; color:var(--text); border:1px solid var(--border);
                  border-radius:6px; font-size:11px; padding:3px 4px; width:100%; }
  .grad-label { font-size:10px; color:var(--muted); display:flex; align-items:center; gap:4px; white-space:nowrap; }
  .grad-label input { width:12px; height:12px; margin:0; }

  .brightness-row, .field-row { display:flex; align-items:center; gap:10px; margin-top:12px; font-size:13px; }
  .brightness-row label, .field-row label { color:var(--muted); min-width:100px; }
  .brightness-row input[type=range] { flex:1; }
  .brightness-row .val { min-width:36px; text-align:right; color:var(--text); }

  footer { text-align:center; color:var(--border); font-size:11px; margin-top:20px; }
</style></head>
<body>
<div class="wrap">
  <div class="brand"><span class="dot"></span><h1>shkaf-hud</h1></div>
  <div class="nav"><a href="/" class="active">Sensors</a><a href="/screens">OLED screens</a></div>

  <div class="banner" id="banner"><span class="b-dot"></span>
    Pro Micro не подключена - лента и OLED не обновляются, статистика продолжает собираться</div>

  <div class="card">
    <h2>LED БАРЫ</h2>
    <div class="bars">
      <div class="bar-wrap"><div class="bar-track"><div class="bar-fill" id="fill-bar0"></div></div>
        <div class="grad-colors"><input type="color" id="c1-bar0"><input type="color" id="c2-bar0"><input type="color" id="c3-bar0"></div>
        <select class="metric" id="metric-bar0"></select>
        <label class="grad-label"><input type="checkbox" id="solid-bar0"> цвет на 100%</label>
        <div class="label"><b><span id="val-bar0"></span>%</b></div></div>
      <div class="bar-wrap"><div class="bar-track"><div class="bar-fill" id="fill-bar1"></div></div>
        <div class="grad-colors"><input type="color" id="c1-bar1"><input type="color" id="c2-bar1"><input type="color" id="c3-bar1"></div>
        <select class="metric" id="metric-bar1"></select>
        <label class="grad-label"><input type="checkbox" id="solid-bar1"> цвет на 100%</label>
        <div class="label"><b><span id="val-bar1"></span>%</b></div></div>
      <div class="bar-wrap"><div class="bar-track"><div class="bar-fill" id="fill-bar2"></div></div>
        <div class="grad-colors"><input type="color" id="c1-bar2"><input type="color" id="c2-bar2"><input type="color" id="c3-bar2"></div>
        <select class="metric" id="metric-bar2"></select>
        <label class="grad-label"><input type="checkbox" id="solid-bar2"> цвет на 100%</label>
        <div class="label"><b><span id="val-bar2"></span>%</b></div></div>
      <div class="bar-wrap"><div class="bar-track"><div class="bar-fill" id="fill-bar3"></div></div>
        <div class="grad-colors"><input type="color" id="c1-bar3"><input type="color" id="c2-bar3"><input type="color" id="c3-bar3"></div>
        <select class="metric" id="metric-bar3"></select>
        <label class="grad-label"><input type="checkbox" id="solid-bar3"> цвет на 100%</label>
        <div class="label"><b><span id="val-bar3"></span>%</b></div></div>
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
const stops = ["c1","c2","c3"];
let editingColor = {}, editingSolid = {}, editingBrightness = false, editingIfaces = false;
let metricsPopulated = false, ifacesPopulated = false;

bars.forEach(k => {
  stops.forEach(stop => {
    const el = document.getElementById(stop + "-" + k);
    el.addEventListener("input", () => editingColor[k] = true);
    el.addEventListener("change", () => sendColors(k));
  });
  document.getElementById("metric-" + k).addEventListener("change", sendAssignment);
  const solidEl = document.getElementById("solid-" + k);
  solidEl.addEventListener("change", () => {
    editingSolid[k] = true;
    fetch("/api/solid", { method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ [k]: solidEl.checked }) }).then(() => editingSolid[k] = false);
  });
});

const brightnessEl = document.getElementById("brightness");
brightnessEl.addEventListener("input", () => {
  editingBrightness = true;
  document.getElementById("brightness-val").textContent = brightnessEl.value + "%";
});
brightnessEl.addEventListener("change", () => {
  fetch("/api/brightness", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ value: parseInt(brightnessEl.value) }) }).then(() => editingBrightness = false);
});

function sendColors(k) {
  const body = {}; body[k] = {};
  stops.forEach(stop => body[k][stop] = document.getElementById(stop + "-" + k).value.slice(1));
  fetch("/api/colors", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) })
    .then(() => editingColor[k] = false);
}

function sendAssignment() {
  const body = {};
  bars.forEach(k => body[k] = document.getElementById("metric-" + k).value);
  fetch("/api/assignment", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
}

function populateMetrics(metrics, assignment) {
  bars.forEach(k => {
    const sel = document.getElementById("metric-" + k);
    sel.innerHTML = "";
    Object.entries(metrics).forEach(([id, label]) => {
      const opt = document.createElement("option");
      opt.value = id; opt.textContent = label;
      if (id === assignment[k]) opt.selected = true;
      sel.appendChild(opt);
    });
  });
  metricsPopulated = true;
}

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

function refresh() {
  fetch("/api/state").then(r => r.json()).then(s => {
    document.getElementById("banner").classList.toggle("show", !s.serial_connected);
    if (!metricsPopulated) populateMetrics(s.metrics, s.cfg.assignment);
    if (!ifacesPopulated) populateIfaces(s.available_interfaces, s.cfg.net1_iface, s.cfg.net2_iface);

    bars.forEach(k => {
      const pct = s[k];
      const fill = document.getElementById("fill-" + k);
      const c = s.cfg.colors[k];
      const isSolidAt100 = s.cfg.solid[k];
      if (!editingSolid[k]) document.getElementById("solid-" + k).checked = isSolidAt100;
      if (!editingColor[k]) stops.forEach(stop => document.getElementById(stop + "-" + k).value = "#" + c[stop]);
      if (isSolidAt100 && pct >= 100) {
        // solid стоит и бар полон - сплошная заливка третьим цветом
        fill.style.background = "#" + c.c3;
      } else if (isSolidAt100) {
        // solid стоит, но ещё не 100% - градиент только c1 -> c2, c3 тут не участвует
        fill.style.background = `linear-gradient(to top, #${c.c1}, #${c.c2})`;
        fill.style.backgroundSize = "100% 130px";
        fill.style.backgroundPosition = "bottom";
      } else {
        // solid снята - как и раньше, обычный 3-стопный градиент c1 -> c2 -> c3
        fill.style.background = `linear-gradient(to top, #${c.c1}, #${c.c2}, #${c.c3})`;
        fill.style.backgroundSize = "100% 130px";
        fill.style.backgroundPosition = "bottom";
      }
      fill.style.height = pct + "%";
      document.getElementById("val-" + k).textContent = pct;
    });

    if (!editingBrightness) {
      brightnessEl.value = s.cfg.brightness;
      document.getElementById("brightness-val").textContent = s.cfg.brightness + "%";
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


@app.route("/api/solid", methods=["POST"])
def api_solid():
    body = request.get_json(force=True)
    with state_lock:
        for k in ("bar0", "bar1", "bar2", "bar3"):
            if k in body:
                state["cfg"]["solid"][k] = bool(body[k])
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
    cache_pct, free_tb_cache = read_cache_usage()
    last_library_refresh = time.time()
    last_array_refresh = time.time()
    last_cache_refresh = time.time()

    recent_items = []
    last_recent_refresh = 0.0

    prev_net1_iface, prev_net2_iface = None, None
    prev_net1_rx = prev_net1_tx = prev_net2_rx = prev_net2_tx = None

    rotation = screens.RotationState()
    proto = protocol.ProtocolState(full_resync_seconds=FULL_RESYNC_SECONDS)

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

        # RAM %
        ram_pct = read_ram_percent()

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
            cache_pct, free_tb_cache = read_cache_usage()
            last_cache_refresh = now
        if now - last_recent_refresh > RECENT_REFRESH_SECONDS:
            recent_items = get_recently_added(RECENT_COUNT, RECENT_MAX_AGE_DAYS)
            last_recent_refresh = now

        sessions = get_activity()

        # сеть по интерфейсам (net1/net2, для OLED-экранов)
        with state_lock:
            net1_iface = state["cfg"]["net1_iface"]
            net2_iface = state["cfg"]["net2_iface"]

        if net1_iface != prev_net1_iface:
            prev_net1_rx = prev_net1_tx = None
            prev_net1_iface = net1_iface
        if net2_iface != prev_net2_iface:
            prev_net2_rx = prev_net2_tx = None
            prev_net2_iface = net2_iface

        net_info = {"net1": None, "net2": None}
        for slot, iface, prev_rx_attr, prev_tx_attr in (
            ("net1", net1_iface, "prev_net1_rx", "prev_net1_tx"),
            ("net2", net2_iface, "prev_net2_rx", "prev_net2_tx"),
        ):
            if not iface:
                continue
            rx, tx = read_iface_rx_tx(iface)
            if rx is None:
                continue
            prev_rx = prev_net1_rx if slot == "net1" else prev_net2_rx
            prev_tx = prev_net1_tx if slot == "net1" else prev_net2_tx
            rx_str = format_rate(rx - prev_rx, dt) if prev_rx is not None and dt > 0 else "0Kbps"
            tx_str = format_rate(tx - prev_tx, dt) if prev_tx is not None and dt > 0 else "0Kbps"
            if slot == "net1":
                prev_net1_rx, prev_net1_tx = rx, tx
            else:
                prev_net2_rx, prev_net2_tx = rx, tx
            net_info[slot] = {
                "name": iface, "speed": read_iface_speed(iface), "ip": read_iface_ip(iface),
                "rx": rx_str, "tx": tx_str,
            }

        prev_time = now

        # ---- собрать context для variables.py/templates.py/screens.py ----
        context = {
            "cpu_pct": round(cpu_pct), "ram_pct": round(ram_pct),
            "cpu_temp_c": cpu_temp_c, "disk_pct": round(disk_pct),
            "array_pct": arr_pct, "cache_pct": cache_pct,
            "free_tb": round(free_tb_array + free_tb_cache, 2),
            "net": net_info,
            "plex": {"movies": movies, "series": series, "songs": songs},
            "streams": sessions,
            "recent": recent_items,
            "qbt": [],
        }
        with _context_lock:
            _last_context.clear()
            _last_context.update(context)

        # ---- OLED: текущие 3 строки по ротации ----
        current_screens = screens_webui.get_screens()
        lines = rotation.current_lines(current_screens, context, now=now)
        with state_lock:
            state["oled_lines"] = lines

        # ---- LED бары: посчитать значение метрики для каждого бара ----
        common_metrics = {
            "cpu": cpu_pct, "ram": ram_pct, "net": net_pct, "disk": disk_pct,
            "array": arr_pct, "cache": cache_pct, "cputemp": cputemp_pct,
        }
        with state_lock:
            colors = state["cfg"]["colors"]
            assignment = state["cfg"]["assignment"]
            brightness = state["cfg"]["brightness"]
            solid = state["cfg"]["solid"]

        bar_pcts = {}
        for b in ("bar0", "bar1", "bar2", "bar3"):
            bar_pcts[b] = round(common_metrics.get(assignment[b], 0))

        with state_lock:
            state["bar0"], state["bar1"] = bar_pcts["bar0"], bar_pcts["bar1"]
            state["bar2"], state["bar3"] = bar_pcts["bar2"], bar_pcts["bar3"]

        # ---- собрать протокол (только изменившееся) ----
        # Градиент/solid-логика считается тут, на сервере (ledbar.py) - Arduino
        # получает уже готовый цвет каждого светодиода и просто зажигает его.
        proto_values = {}
        for i, b in enumerate(("bar0", "bar1", "bar2", "bar3"), start=1):
            pixels = ledbar.compute_bar_pixels(
                bar_pcts[b], colors[b]["c1"], colors[b]["c2"], colors[b]["c3"], solid[b]
            )
            proto_values[f"BAR{i}"] = protocol.pack_bar_pixels(pixels)
        proto_values["BRI"] = str(brightness)
        proto_values["L1"], proto_values["L2"], proto_values["L3"] = lines

        line_to_send = proto.build(proto_values, now=now)

        if ser is not None:
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
