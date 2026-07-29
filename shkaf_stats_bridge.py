#!/usr/bin/env python3
"""
shkaf_stats_bridge.py  (контейнер: shkaf-hud)

Собирает CPU/RAM/NET/DISK(%util) хоста + активность и статистику библиотек
через Tautulli, шлёт по USB-serial на Pro Micro одну строку за тик.

Формат строки (pipe-delimited, \n в конце), общие поля есть всегда:
    BAR0:<0-100>|BAR1:<0-100>|BAR2:<0-100>|BAR3:<0-100>|BRI:<0-100>|G0:<0|1>|G1:<0|1>|G2:<0|1>|G3:<0|1>|C0:<hex>|C1:<hex>|C2:<hex>|C3:<hex>|SCREEN:LIB|MOVIES:<n>|SERIES:<n>|TOTC:<TBx100>|FREEC:<TBx100>|ARRPCT:<0-100>
или
    BAR0:<0-100>|BAR1:<0-100>|BAR2:<0-100>|BAR3:<0-100>|BRI:<0-100>|G0:<0|1>|G1:<0|1>|G2:<0|1>|G3:<0|1>|C0:<hex>|C1:<hex>|C2:<hex>|C3:<hex>|SCREEN:STREAM|IDX:<n>|CNT:<n>|TITLE:<...>|USER:<...>|PROG:<0-100>

BAR0..BAR3 - значение каждого физического бара (0-100%), метрика для каждого
бара выбирается в веб-интерфейсе (CPU/RAM/NET/DISK %util/Array %/Cache %/CPU temp).
BRI - общая яркость ленты (0-100%), тоже настраивается в веб-интерфейсе.
C0..C3 - цвета баров в hex без "#".
Все три настройки сохраняются в CONFIG_DIR/settings.json между перезапусками.

Ротация экрана (LIB / STREAM 1..N) решается на хосте - Arduino просто
рендерит то, что прислали, никакой логики выбора экрана на нём нет.

Зависимости:
    pip install pyserial requests --break-system-packages
"""

import os
import copy

# Бампай эту строку при каждой значимой правке - так сразу видно в `docker logs`,
# какая версия реально запущена, без сверки digest'ов вручную.
SCRIPT_VERSION = "2026-07-23-7"
import re
import time
import serial
import requests

# ---------------- КОНФИГ (через переменные окружения) ----------------

SERIAL_PORT = os.environ.get("SERIAL_PORT", "/dev/ttyACM0")
BAUD = int(os.environ.get("BAUD", "115200"))

TAUTULLI_URL = os.environ.get("TAUTULLI_URL", "http://127.0.0.1:8181")
TAUTULLI_API_KEY = os.environ.get("TAUTULLI_API_KEY", "")

NET_IFACE = os.environ.get("NET_IFACE", "br0")
NET_MAX_MBPS = float(os.environ.get("NET_MAX_MBPS", "500"))
NET_STATS_PATH = os.environ.get("NET_STATS_PATH", "/proc/net/dev")

# Диски для %util: пусто = автоопределение всех sdX/nvmeXnY
DISK_DEVICES = os.environ.get("DISK_DEVICES", "")

ARRAY_PATH = os.environ.get("ARRAY_PATH", "/mnt/user")
ARRAY_REFRESH_SECONDS = float(os.environ.get("ARRAY_REFRESH_SECONDS", "60"))
LIBRARY_REFRESH_SECONDS = float(os.environ.get("LIBRARY_REFRESH_SECONDS", "300"))
SCREEN_ROTATE_SECONDS = float(os.environ.get("SCREEN_ROTATE_SECONDS", "4"))

BIGCACHE_PATH = os.environ.get("BIGCACHE_PATH", "/mnt/bigcache")
CACHE_REFRESH_SECONDS = float(os.environ.get("CACHE_REFRESH_SECONDS", "60"))

CPU_TEMP_MAX_C = float(os.environ.get("CPU_TEMP_MAX_C", "90"))

POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))

WEB_PORT = int(os.environ.get("WEB_PORT", "8189"))
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")

DEFAULT_COLORS = {
    "bar0": {"c1": "00FF42", "c2": "FFF600", "c3": "FF0000"},
    "bar1": {"c1": "00FF42", "c2": "FFF600", "c3": "FF0000"},
    "bar2": {"c1": "00FF42", "c2": "FFF600", "c3": "FF0000"},
    "bar3": {"c1": "00FF42", "c2": "FFF600", "c3": "FF0000"},
}  # c1=0%, c2=50%, c3=100% - три стопа градиента для каждого бара
DEFAULT_ASSIGNMENT = {"bar0": "cpu", "bar1": "ram", "bar2": "net", "bar3": "disk"}
DEFAULT_BRIGHTNESS = 15  # 0-100%, ~1% реального 0-255 диапазона FastLED уже безопасно для USB-питания
DEFAULT_SOLID = {"bar0": False, "bar1": False, "bar2": False, "bar3": False}
# per-bar: False = градиент через 3 стопа (c1->c2->c3) по позиции диода,
#          True  = вся полоска заливается одним цветом c3 (тем, что на 100%)

METRICS = {
    "cpu": "CPU",
    "ram": "RAM",
    "net": "NET",
    "disk": "DISK %util",
    "array": "Array %",
    "cache": "BigCache %",
    "cputemp": "CPU temp",
}

# -------------------------------------------------------------

import json
import threading
from flask import Flask, request, jsonify, Response

state_lock = threading.Lock()
state = {
    "bar0": 0, "bar1": 0, "bar2": 0, "bar3": 0,
    "screen": "LIB",
    "movies": 0, "series": 0, "total_tb": 0, "free_tb": 0, "arr_pct": 0,
    "stream_idx": 0, "stream_cnt": 0, "stream_title": "", "stream_user": "", "stream_prog": 0,
    "colors": copy.deepcopy(DEFAULT_COLORS),
    "assignment": dict(DEFAULT_ASSIGNMENT),
    "brightness": DEFAULT_BRIGHTNESS,
    "solid": dict(DEFAULT_SOLID),
    "serial_connected": False,
}


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
    except Exception:
        saved = {}

    colors = copy.deepcopy(DEFAULT_COLORS)
    saved_colors = saved.get("colors", {})
    if isinstance(saved_colors, dict):
        for bar_key, bar_val in saved_colors.items():
            if bar_key in colors and isinstance(bar_val, dict):
                colors[bar_key].update(bar_val)
            # если старый формат (просто hex-строка на бар) - игнорируем, остаёмся на дефолте
    assignment = dict(DEFAULT_ASSIGNMENT)
    assignment.update(saved.get("assignment", {}))
    brightness = saved.get("brightness", DEFAULT_BRIGHTNESS)
    solid = dict(DEFAULT_SOLID)
    saved_gradient = saved.get("solid", {})
    if isinstance(saved_gradient, dict):
        solid.update(saved_gradient)
    # если в settings.json старый формат (одно bool-значение на всё) - просто
    # игнорируем и остаёмся на дефолтах, ничего страшного

    return colors, assignment, brightness, solid


def save_settings(colors, assignment, brightness, solid):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump({"colors": colors, "assignment": assignment, "brightness": brightness, "solid": solid}, f)


# ---------------- веб-интерфейс ----------------

app = Flask(__name__)

PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>shkaf-hud</title>
<link rel="icon" type="image/png" href="https://raw.githubusercontent.com/RGCustom/shkaf-hud/main/favicon.png">
<style>
  * { box-sizing: border-box; }
  :root {
    --bg: #17181a;
    --panel: #1f2123;
    --border: #2c2e31;
    --text: #e6e6e6;
    --muted: #8a8d91;
    --accent: #ff8c2f;
    --accent-dim: #ff8c2f33;
    --danger: #e0483e;
  }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         margin:0; padding:44px 20px; }
  .wrap { max-width:480px; margin:0 auto; }
  .brand { display:flex; align-items:center; gap:10px; margin-bottom:4px; }
  .brand .dot { width:9px; height:9px; border-radius:50%; background:var(--accent);
                box-shadow:0 0 8px var(--accent); }
  h1 { font-size:19px; font-weight:600; margin:0; letter-spacing:.3px; }
  .sub { color:var(--muted); font-size:13px; margin:2px 0 24px 19px; }

  .banner { display:none; background:#3a2418; border:1px solid var(--danger); color:#ffb3ab;
            border-radius:10px; padding:12px 16px; margin-bottom:18px; font-size:13px;
            align-items:center; gap:10px; }
  .banner.show { display:flex; }
  .banner .b-dot { width:8px; height:8px; border-radius:50%; background:var(--danger); flex-shrink:0; }

  .card { background:var(--panel); border:1px solid var(--border); border-radius:14px;
          padding:22px; margin-bottom:18px; box-shadow:0 4px 16px rgba(0,0,0,.25); }
  .card h2 { font-size:11px; color:var(--muted);
             margin:0 0 18px; font-weight:600; white-space:nowrap; }

  .bars { display:flex; gap:20px; align-items:flex-start; margin-bottom:18px; }
  .bar-wrap { flex:1; display:flex; flex-direction:column; align-items:center; gap:8px; }
  .bar-track { width:100%; max-width:40px; height:130px; background:#101112; border-radius:6px;
               display:flex; flex-direction:column-reverse; overflow:hidden; border:1px solid var(--border); }
  .bar-fill { width:100%; transition:height .3s, background .2s; }
  .label { font-size:12px; color:var(--muted); text-align:center; }
  .label b { color:var(--text); font-size:13px; }
  input[type=color] { width:32px; height:24px; border:none; background:none; border-radius:6px;
                       cursor:pointer; padding:0; }
  .grad-colors { display:flex; gap:3px; }
  .grad-colors input[type=color] { width:20px; height:20px; }

  select.metric { background:#101112; color:var(--text); border:1px solid var(--border); border-radius:6px;
                  font-size:11px; padding:3px 4px; width:100%; }
  .grad-label { font-size:10px; color:var(--muted); display:flex; align-items:center; gap:4px; white-space:nowrap; }
  .grad-label input { width:12px; height:12px; margin:0; }
  .brightness-row { display:flex; align-items:center; gap:12px; margin-top:20px; padding-top:18px;
                    border-top:1px solid var(--border); }
  .brightness-row label { font-size:12px; color:var(--muted); white-space:nowrap; }
  .brightness-row input[type=range] { flex:1; }
  .brightness-row .val { font-size:12px; color:var(--text); min-width:32px; text-align:right; }

  .oled { background:#000; color:#7fd8ff; font-family:"SF Mono",Consolas,monospace; font-size:17px;
          overflow:hidden; padding:16px; border-radius:8px; line-height:1.5; }
  .oled .line { white-space:nowrap; overflow:hidden; }
  .oled .marquee { display:inline-block; animation: scroll 8s linear infinite; }

  footer { text-align:center; color:var(--border); font-size:11px; margin-top:8px; }
</style></head>
<body>
<div class="wrap">
  <div class="brand"><span class="dot"></span><h1>shkaf-hud</h1></div>
  <p class="sub">живое превью ленты и OLED</p>

  <div class="banner" id="banner"><span class="b-dot"></span>
    Pro Micro не подключена - лента и OLED не обновляются, статистика и настройки продолжают работать</div>

  <div class="card">
    <h2>SENSORS</h2>
    <div class="bars">
      <div class="bar-wrap"><div class="bar-track"><div class="bar-fill" id="fill-bar0"></div></div>
        <div class="grad-colors">
          <input type="color" id="c1-bar0" title="0%">
          <input type="color" id="c2-bar0" title="50%">
          <input type="color" id="c3-bar0" title="100%">
        </div>
        <select class="metric" id="metric-bar0"></select>
        <label class="grad-label"><input type="checkbox" id="solid-bar0"> цвет на 100%</label>
        <div class="label"><b><span id="val-bar0"></span>%</b></div></div>
      <div class="bar-wrap"><div class="bar-track"><div class="bar-fill" id="fill-bar1"></div></div>
        <div class="grad-colors">
          <input type="color" id="c1-bar1" title="0%">
          <input type="color" id="c2-bar1" title="50%">
          <input type="color" id="c3-bar1" title="100%">
        </div>
        <select class="metric" id="metric-bar1"></select>
        <label class="grad-label"><input type="checkbox" id="solid-bar1"> цвет на 100%</label>
        <div class="label"><b><span id="val-bar1"></span>%</b></div></div>
      <div class="bar-wrap"><div class="bar-track"><div class="bar-fill" id="fill-bar2"></div></div>
        <div class="grad-colors">
          <input type="color" id="c1-bar2" title="0%">
          <input type="color" id="c2-bar2" title="50%">
          <input type="color" id="c3-bar2" title="100%">
        </div>
        <select class="metric" id="metric-bar2"></select>
        <label class="grad-label"><input type="checkbox" id="solid-bar2"> цвет на 100%</label>
        <div class="label"><b><span id="val-bar2"></span>%</b></div></div>
      <div class="bar-wrap"><div class="bar-track"><div class="bar-fill" id="fill-bar3"></div></div>
        <div class="grad-colors">
          <input type="color" id="c1-bar3" title="0%">
          <input type="color" id="c2-bar3" title="50%">
          <input type="color" id="c3-bar3" title="100%">
        </div>
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
    <h2>OLED</h2>
    <div class="oled" id="oled"></div>
  </div>

  <footer>shkaf-hud</footer>
</div>

<script>
const bars = ["bar0","bar1","bar2","bar3"];
const stops = ["c1","c2","c3"];
let editingColor = {};
let editingBrightness = false;
let metricsPopulated = false;

bars.forEach(k => {
  stops.forEach(stop => {
    const el = document.getElementById(stop + "-" + k);
    el.addEventListener("input", () => editingColor[k] = true);
    el.addEventListener("change", () => sendColors(k));
  });
  document.getElementById("metric-" + k).addEventListener("change", sendAssignment);
});

const brightnessEl = document.getElementById("brightness");
brightnessEl.addEventListener("input", () => {
  editingBrightness = true;
  document.getElementById("brightness-val").textContent = brightnessEl.value + "%";
});
brightnessEl.addEventListener("change", () => {
  fetch("/api/brightness", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ value: parseInt(brightnessEl.value) }) })
    .then(() => editingBrightness = false);
});

let editingSolid = {};
bars.forEach(k => {
  const el = document.getElementById("solid-" + k);
  el.addEventListener("change", () => {
    editingSolid[k] = true;
    fetch("/api/solid", { method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ [k]: el.checked }) })
      .then(() => editingSolid[k] = false);
  });
});

function sendColors(k) {
  const body = {};
  body[k] = {};
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
      opt.value = id;
      opt.textContent = label;
      if (id === assignment[k]) opt.selected = true;
      sel.appendChild(opt);
    });
  });
  metricsPopulated = true;
}

function pad(s, n) { s = String(s); while (s.length < n) s += " "; return s.slice(0, n); }

function oledLines(s) {
  if (s.screen === "STREAM" && s.stream_cnt > 0) {
    return [
      `Stream ${s.stream_idx}/${s.stream_cnt}`,
      s.stream_title,
      `${pad(s.stream_user,7)}${s.stream_prog}%`,
    ];
  }
  return [
    `Movies  ${s.movies}`,
    `Series  ${s.series}`,
    `${s.total_tb.toFixed(2)}TB (${s.free_tb.toFixed(2)} free)`,
  ];
}

function renderOled(s) {
  const oled = document.getElementById("oled");
  oled.innerHTML = "";
  oledLines(s).forEach(text => {
    const div = document.createElement("div");
    div.className = "line";
    const span = document.createElement("span");
    span.textContent = text;
    div.appendChild(span);
    oled.appendChild(div);

    requestAnimationFrame(() => {
      if (span.scrollWidth > oled.clientWidth) {
        span.innerHTML = text + "&nbsp;&nbsp;&nbsp;&nbsp;" + text;
        span.classList.add("marquee");
      }
    });
  });
}

function refresh() {
  fetch("/api/state").then(r => r.json()).then(s => {
    document.getElementById("banner").classList.toggle("show", !s.serial_connected);

    if (!metricsPopulated) populateMetrics(s.metrics, s.assignment);

    bars.forEach(k => {
      const pct = s[k];
      const fill = document.getElementById("fill-" + k);
      const c = s.colors[k];
      const isSolidAt100 = s.solid[k];

      if (!editingSolid[k]) {
        document.getElementById("solid-" + k).checked = isSolidAt100;
      }
      if (!editingColor[k]) {
        stops.forEach(stop => document.getElementById(stop + "-" + k).value = "#" + c[stop]);
      }

      if (isSolidAt100 && pct >= 100) {
        fill.style.background = "#" + c.c3;
      } else {
        fill.style.background = `linear-gradient(to top, #${c.c1}, #${c.c2}, #${c.c3})`;
        fill.style.backgroundSize = "100% 130px";
        fill.style.backgroundPosition = "bottom";
      }
      fill.style.height = pct + "%";
      document.getElementById("val-" + k).textContent = pct;
    });

    if (!editingBrightness) {
      brightnessEl.value = s.brightness;
      document.getElementById("brightness-val").textContent = s.brightness + "%";
    }

    renderOled(s);
  });
}

const styleTag = document.createElement("style");
styleTag.textContent = "@keyframes scroll { from { transform: translateX(0); } to { transform: translateX(-50%); } }";
document.head.appendChild(styleTag);

setInterval(refresh, 1000);
refresh();
</script>
</body></html>
"""


@app.route("/")
def index():
    return Response(PAGE_HTML, mimetype="text/html")


@app.route("/api/state")
def api_state():
    with state_lock:
        d = dict(state)
    d["metrics"] = METRICS
    return jsonify(d)


@app.route("/api/colors", methods=["POST"])
def api_colors():
    body = request.get_json(force=True)
    with state_lock:
        for k in ("bar0", "bar1", "bar2", "bar3"):
            if k in body:
                for stop in ("c1", "c2", "c3"):
                    if stop in body[k]:
                        state["colors"][k][stop] = body[k][stop].upper()
        save_settings(state["colors"], state["assignment"], state["brightness"], state["solid"])
    return jsonify({"ok": True})


@app.route("/api/assignment", methods=["POST"])
def api_assignment():
    body = request.get_json(force=True)
    with state_lock:
        for k in ("bar0", "bar1", "bar2", "bar3"):
            if k in body and body[k] in METRICS:
                state["assignment"][k] = body[k]
        save_settings(state["colors"], state["assignment"], state["brightness"], state["solid"])
    return jsonify({"ok": True})


@app.route("/api/brightness", methods=["POST"])
def api_brightness():
    body = request.get_json(force=True)
    with state_lock:
        state["brightness"] = max(0, min(100, int(body.get("value", state["brightness"]))))
        save_settings(state["colors"], state["assignment"], state["brightness"], state["solid"])
    return jsonify({"ok": True})


@app.route("/api/solid", methods=["POST"])
def api_solid():
    body = request.get_json(force=True)
    with state_lock:
        for k in ("bar0", "bar1", "bar2", "bar3"):
            if k in body:
                state["solid"][k] = bool(body[k])
        save_settings(state["colors"], state["assignment"], state["brightness"], state["solid"])
    return jsonify({"ok": True})


def run_web():
    app.run(host="0.0.0.0", port=WEB_PORT, use_reloader=False)

DISK_NAME_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+)$")


TRANSLIT_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def transliterate(s: str) -> str:
    """Кириллица -> латиница - OLED-шрифт (Adafruit_GFX стандартный) не знает кириллицу вообще."""
    out = []
    for ch in s:
        lower = ch.lower()
        if lower in TRANSLIT_MAP:
            rep = TRANSLIT_MAP[lower]
            out.append(rep.capitalize() if ch.isupper() and rep else rep)
        else:
            out.append(ch)
    return "".join(out)


def sanitize(s: str) -> str:
    s = transliterate(s or "")
    return s.replace("|", "/").replace("\n", " ").replace("\r", "")[:48]


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
    tb = 10 ** 12
    total_tb = round(total / tb, 2)
    free_tb = round(free / tb, 2)
    used_pct = round((total - free) / total * 100) if total > 0 else 0
    return total_tb, free_tb, used_pct


def read_cache_usage_pct():
    try:
        st = os.statvfs(BIGCACHE_PATH)
        total = st.f_frsize * st.f_blocks
        free = st.f_frsize * st.f_bavail
        return round((total - free) / total * 100) if total > 0 else 0
    except Exception:
        return 0


import glob


def read_cpu_temp_pct():
    paths = glob.glob("/sys/class/hwmon/hwmon*/temp1_input") + ["/sys/class/thermal/thermal_zone0/temp"]
    for p in paths:
        try:
            with open(p) as f:
                milli_c = int(f.read().strip())
            temp_c = milli_c / 1000.0
            return max(0.0, min(100.0, temp_c / CPU_TEMP_MAX_C * 100.0))
        except Exception:
            continue
    return 0.0


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
    except Exception as e:
        print(f"[tautulli] get_activity failed: {e}", flush=True)
        return []


def get_library_counts():
    try:
        libs = tautulli_get("get_libraries")
        movies = sum(int(l.get("count", 0) or 0) for l in libs if l.get("section_type") == "movie")
        series = sum(int(l.get("count", 0) or 0) for l in libs if l.get("section_type") == "show")
        return movies, series
    except Exception as e:
        print(f"[tautulli] get_library_counts failed: {e}", flush=True)
        return 0, 0


# ---------------- главный цикл ----------------

def try_open_serial():
    try:
        s = serial.Serial(SERIAL_PORT, BAUD, timeout=1)
        time.sleep(2)
        with state_lock:
            state["serial_connected"] = True
        print(f"[serial] connected: {SERIAL_PORT}", flush=True)
        return s
    except (serial.SerialException, OSError) as e:
        with state_lock:
            state["serial_connected"] = False
        return None


def main():
    print(f"[shkaf-hud] starting, version {SCRIPT_VERSION}", flush=True)

    with state_lock:
        colors, assignment, brightness, solid = load_settings()
        state["colors"] = colors
        state["assignment"] = assignment
        state["brightness"] = brightness
        state["solid"] = solid

    threading.Thread(target=run_web, daemon=True).start()

    ser = try_open_serial()
    last_reconnect_attempt = time.time()

    prev_idle, prev_total = read_cpu_times()
    prev_net = read_net_bytes(NET_IFACE)
    disk_devices = list_disk_devices()
    prev_disk_ticks = read_disk_io_ticks(disk_devices)
    prev_time = time.time()

    movies, series = get_library_counts()
    total_tb, free_tb, arr_pct = read_array_usage_tb()
    cache_pct = read_cache_usage_pct()
    last_library_refresh = time.time()
    last_array_refresh = time.time()
    last_cache_refresh = time.time()

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
            total_tb, free_tb, arr_pct = read_array_usage_tb()
            last_array_refresh = now

        # Кэш-пул - редко обновляем
        if now - last_cache_refresh > CACHE_REFRESH_SECONDS:
            cache_pct = read_cache_usage_pct()
            last_cache_refresh = now

        # CPU температура - читаем каждый тик, это дёшево (просто /sys)
        cputemp_pct = read_cpu_temp_pct()

        # Активные стримы
        sessions = get_activity()
        count = len(sessions)

        # Ротация экранов: [LIB, stream1, stream2, ...]
        if count == 0:
            screen_index = 0
        elif now - last_screen_switch > SCREEN_ROTATE_SECONDS:
            screen_index = (screen_index + 1) % (count + 1)  # +1 за экран LIB
            last_screen_switch = now

        common_metrics = {
            "cpu": cpu_pct, "ram": ram_pct, "net": net_pct, "disk": disk_pct,
            "array": arr_pct, "cache": cache_pct, "cputemp": cputemp_pct,
        }

        with state_lock:
            colors = state["colors"]
            assignment = state["assignment"]
            brightness = state["brightness"]
            solid = state["solid"]

        bar_values = {b: round(common_metrics[assignment[b]]) for b in ("bar0", "bar1", "bar2", "bar3")}

        grad_part = "|".join(f"G{i}:{1 if solid[f'bar{i}'] else 0}" for i in range(4))

        common = (
            f"BAR0:{bar_values['bar0']}|BAR1:{bar_values['bar1']}|"
            f"BAR2:{bar_values['bar2']}|BAR3:{bar_values['bar3']}|BRI:{brightness}|{grad_part}"
        )
        color_part = "|" + "|".join(
            f"C{b}{s}:{colors[f'bar{b}'][f'c{s}']}"
            for b in range(4) for s in (1, 2, 3)
        )

        # TB с точностью до сотых - Arduino парсит только целые, поэтому шлём
        # "центи-терабайты" (умноженное на 100 целое число) и делим на месте
        totc = round(total_tb * 100)
        freec = round(free_tb * 100)

        if count == 0 or screen_index == 0:
            line = (
                f"{common}{color_part}|SCREEN:LIB|MOVIES:{movies}|SERIES:{series}|"
                f"TOTC:{totc}|FREEC:{freec}|ARRPCT:{arr_pct}\n"
            )
            with state_lock:
                state.update({
                    "bar0": bar_values["bar0"], "bar1": bar_values["bar1"],
                    "bar2": bar_values["bar2"], "bar3": bar_values["bar3"],
                    "screen": "LIB", "movies": movies, "series": series,
                    "total_tb": total_tb, "free_tb": free_tb, "arr_pct": arr_pct,
                    "stream_cnt": 0,
                })
        else:
            s = sessions[screen_index - 1]
            line = (
                f"{common}{color_part}|SCREEN:STREAM|IDX:{screen_index}|CNT:{count}|"
                f"TITLE:{sanitize(s['title'])}|USER:{sanitize(s['user'])}|PROG:{s['progress']}\n"
            )
            with state_lock:
                state.update({
                    "bar0": bar_values["bar0"], "bar1": bar_values["bar1"],
                    "bar2": bar_values["bar2"], "bar3": bar_values["bar3"],
                    "screen": "STREAM", "stream_idx": screen_index, "stream_cnt": count,
                    "stream_title": s["title"], "stream_user": s["user"], "stream_prog": s["progress"],
                })

        if ser is not None:
            try:
                ser.write(line.encode("utf-8"))
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
            # Arduino не подключена (или отвалилась) - пробуем раз в 5 секунд,
            # не блокируя при этом сбор статистики и веб-интерфейс
            if now - last_reconnect_attempt > 5:
                ser = try_open_serial()
                last_reconnect_attempt = now


if __name__ == "__main__":
    main()
