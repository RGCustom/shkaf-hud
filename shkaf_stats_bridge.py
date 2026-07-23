#!/usr/bin/env python3
"""
shkaf_stats_bridge.py  (контейнер: shkaf-hud)

Собирает CPU/RAM/NET/DISK(%util) хоста + активность и статистику библиотек
через Tautulli, шлёт по USB-serial на Pro Micro одну строку за тик.

Формат строки (pipe-delimited, \n в конце), общие поля есть всегда:
    CPU:<0-100>|RAM:<0-100>|NET:<0-100>|DISK:<0-100>|C0:<hex>|C1:<hex>|C2:<hex>|C3:<hex>|SCREEN:LIB|MOVIES:<n>|SERIES:<n>|TOTC:<TBx100>|FREEC:<TBx100>|ARRPCT:<0-100>
или
    CPU:<0-100>|RAM:<0-100>|NET:<0-100>|DISK:<0-100>|C0:<hex>|C1:<hex>|C2:<hex>|C3:<hex>|SCREEN:STREAM|IDX:<n>|CNT:<n>|TITLE:<...>|USER:<...>|PROG:<0-100>

C0..C3 - цвета баров (CPU/RAM/NET/DISK) в hex без "#", настраиваются через
веб-интерфейс контейнера (порт WEB_PORT, по умолчанию 8189) и сохраняются
в CONFIG_DIR/colors.json между перезапусками.

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

NET_IFACE = os.environ.get("NET_IFACE", "br0")
NET_MAX_MBPS = float(os.environ.get("NET_MAX_MBPS", "500"))
NET_STATS_PATH = os.environ.get("NET_STATS_PATH", "/proc/net/dev")

# Диски для %util: пусто = автоопределение всех sdX/nvmeXnY
DISK_DEVICES = os.environ.get("DISK_DEVICES", "")

ARRAY_PATH = os.environ.get("ARRAY_PATH", "/mnt/user")
ARRAY_REFRESH_SECONDS = float(os.environ.get("ARRAY_REFRESH_SECONDS", "60"))
LIBRARY_REFRESH_SECONDS = float(os.environ.get("LIBRARY_REFRESH_SECONDS", "300"))
SCREEN_ROTATE_SECONDS = float(os.environ.get("SCREEN_ROTATE_SECONDS", "4"))

POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))

WEB_PORT = int(os.environ.get("WEB_PORT", "8189"))
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
COLORS_FILE = os.path.join(CONFIG_DIR, "colors.json")

DEFAULT_COLORS = {"cpu": "FF0000", "ram": "00FF00", "net": "0000FF", "disk": "FFFF00"}

# -------------------------------------------------------------

import json
import threading
from flask import Flask, request, jsonify, Response

state_lock = threading.Lock()
state = {
    "cpu": 0, "ram": 0, "net": 0, "disk": 0,
    "screen": "LIB",
    "movies": 0, "series": 0, "total_tb": 0, "free_tb": 0, "arr_pct": 0,
    "stream_idx": 0, "stream_cnt": 0, "stream_title": "", "stream_user": "", "stream_prog": 0,
    "colors": dict(DEFAULT_COLORS),
    "serial_connected": False,
}


def load_colors():
    try:
        with open(COLORS_FILE) as f:
            saved = json.load(f)
        colors = dict(DEFAULT_COLORS)
        colors.update(saved)
        return colors
    except Exception:
        return dict(DEFAULT_COLORS)


def save_colors(colors):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(COLORS_FILE, "w") as f:
        json.dump(colors, f)


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
  .card h2 { font-size:11px; text-transform:uppercase; letter-spacing:.8px; color:var(--muted);
             margin:0 0 18px; font-weight:600; }

  .bars { display:flex; gap:20px; align-items:flex-end; height:150px; margin-bottom:18px; }
  .bar-wrap { flex:1; display:flex; flex-direction:column; align-items:center; gap:10px; }
  .bar-track { width:100%; max-width:40px; height:130px; background:#101112; border-radius:6px;
               display:flex; flex-direction:column-reverse; overflow:hidden; border:1px solid var(--border); }
  .bar-fill { width:100%; transition:height .3s, background .2s; }
  .label { font-size:12px; color:var(--muted); text-align:center; }
  .label b { color:var(--text); font-size:13px; }
  input[type=color] { width:32px; height:24px; border:none; background:none; border-radius:6px;
                       cursor:pointer; padding:0; }

  .oled { background:#000; color:#7fd8ff; font-family:"SF Mono",Consolas,monospace; font-size:17px;
          overflow:hidden; padding:16px; border-radius:8px; line-height:1.5; }
  .oled .line { white-space:nowrap; }
  .oled .marquee { display:inline-block; padding-right:40px; }

  footer { text-align:center; color:var(--border); font-size:11px; margin-top:8px; }
</style></head>
<body>
<div class="wrap">
  <div class="brand"><span class="dot"></span><h1>shkaf-hud</h1></div>
  <p class="sub">живое превью ленты и OLED</p>

  <div class="banner" id="banner"><span class="b-dot"></span>
    Pro Micro не подключена - лента и OLED не обновляются, статистика и настройки продолжают работать</div>

  <div class="card">
    <h2>Индикаторы</h2>
    <div class="bars">
      <div class="bar-wrap"><div class="bar-track"><div class="bar-fill" id="fill-cpu"></div></div>
        <input type="color" id="color-cpu"><div class="label">CPU<br><b><span id="val-cpu"></span>%</b></div></div>
      <div class="bar-wrap"><div class="bar-track"><div class="bar-fill" id="fill-ram"></div></div>
        <input type="color" id="color-ram"><div class="label">RAM<br><b><span id="val-ram"></span>%</b></div></div>
      <div class="bar-wrap"><div class="bar-track"><div class="bar-fill" id="fill-net"></div></div>
        <input type="color" id="color-net"><div class="label">NET<br><b><span id="val-net"></span>%</b></div></div>
      <div class="bar-wrap"><div class="bar-track"><div class="bar-fill" id="fill-disk"></div></div>
        <input type="color" id="color-disk"><div class="label">DISK<br><b><span id="val-disk"></span>%</b></div></div>
    </div>
  </div>

  <div class="card">
    <h2>OLED</h2>
    <div class="oled" id="oled"></div>
  </div>

  <footer>shkaf-hud</footer>
</div>

<script>
const keys = ["cpu","ram","net","disk"];
let editingColor = false;

keys.forEach(k => {
  document.getElementById("color-" + k).addEventListener("input", () => editingColor = true);
  document.getElementById("color-" + k).addEventListener("change", sendColors);
});

function sendColors() {
  const body = {};
  keys.forEach(k => body[k] = document.getElementById("color-" + k).value.slice(1));
  fetch("/api/colors", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) })
    .then(() => editingColor = false);
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
    if (text.length > 16) {
      div.innerHTML = `<span class="marquee">${text}&nbsp;&nbsp;&nbsp;&nbsp;${text}</span>`;
      div.style.animation = "scroll 8s linear infinite";
      div.style.width = "100%";
    } else {
      div.textContent = text;
    }
    oled.appendChild(div);
  });
}

function refresh() {
  fetch("/api/state").then(r => r.json()).then(s => {
    document.getElementById("banner").classList.toggle("show", !s.serial_connected);
    keys.forEach(k => {
      const pct = s[k];
      const color = pct >= 100 ? "#e0483e" : "#" + s.colors[k];
      document.getElementById("fill-" + k).style.height = pct + "%";
      document.getElementById("fill-" + k).style.background = color;
      document.getElementById("val-" + k).textContent = pct;
      if (!editingColor) document.getElementById("color-" + k).value = "#" + s.colors[k];
    });
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
        return jsonify(dict(state))


@app.route("/api/colors", methods=["POST"])
def api_colors():
    body = request.get_json(force=True)
    with state_lock:
        for k in ("cpu", "ram", "net", "disk"):
            if k in body:
                state["colors"][k] = body[k].upper()
        save_colors(state["colors"])
    return jsonify({"ok": True})


def run_web():
    app.run(host="0.0.0.0", port=WEB_PORT, use_reloader=False)

DISK_NAME_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+)$")


def sanitize(s: str) -> str:
    return (s or "").replace("|", "/").replace("\n", " ").replace("\r", "")[:48]


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
    with state_lock:
        state["colors"] = load_colors()

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
            total_tb, free_tb, arr_pct = read_array_usage_tb()
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

        with state_lock:
            colors = state["colors"]
        color_part = f"|C0:{colors['cpu']}|C1:{colors['ram']}|C2:{colors['net']}|C3:{colors['disk']}"

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
                    "cpu": round(cpu_pct), "ram": round(ram_pct), "net": round(net_pct), "disk": round(disk_pct),
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
                    "cpu": round(cpu_pct), "ram": round(ram_pct), "net": round(net_pct), "disk": round(disk_pct),
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
