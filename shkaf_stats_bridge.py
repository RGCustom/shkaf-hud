"""
shkaf_stats_bridge.py
Главный скрипт контейнера shkaf-hud.
Собирает метрики хоста (Unraid) и внешних сервисов (Tautulli, qBittorrent),
формирует context, рендерит экраны через screens.py и шлёт diff на Arduino через protocol.py.
Также поднимает Flask-сервер для веб-интерфейса.
"""
import os
import sys
import time
import json
import logging
import threading
import socket
import shutil
from datetime import datetime, timedelta

import psutil
import requests
import serial
from flask import Flask, request, jsonify

import variables
import templates
import screens
import screens_webui
import protocol

# ---------------- Логирование ----------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    stream=sys.stdout
)
log = logging.getLogger("shkaf-hud")

# ---------------- Конфигурация из ENV ----------------
SERIAL_PORT = os.environ.get("SERIAL_PORT", "/dev/ttyACM0")
BAUD_RATE = int(os.environ.get("BAUD", "115200"))
WEB_PORT = int(os.environ.get("WEB_PORT", "8189"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))

TAUTULLI_URL = os.environ.get("TAUTULLI_URL", "").rstrip("/")
TAUTULLI_API_KEY = os.environ.get("TAUTULLI_API_KEY", "")

QBT_URL = os.environ.get("QBT_URL", "").rstrip("/")
QBT_USER = os.environ.get("QBT_USER", "")
QBT_PASS = os.environ.get("QBT_PASS", "")

NET_IFACE1 = os.environ.get("NET_IFACE1", "br0")
NET_IFACE2 = os.environ.get("NET_IFACE2", "")
NET_MAX_MBPS = float(os.environ.get("NET_MAX_MBPS", "1000"))

# ---------------- Глобальное состояние ----------------
context_lock = threading.Lock()
current_context = {}
rotation_state = screens.RotationState()
protocol_state = protocol.ProtocolState(full_resync_seconds=30)

# Состояние баров (пока в памяти, можно расширить до JSON-конфига)
bar_config = {
    "BAR1": {"pct": 0, "c1": "FF0000", "c2": "FFFF00", "c3": "FF0000", "solid": True}, # CPU
    "BAR2": {"pct": 0, "c1": "00FF00", "c2": "FFFF00", "c3": "FF0000", "solid": True}, # RAM
    "BAR3": {"pct": 0, "c1": "0000FF", "c2": "00FFFF", "c3": "FF0000", "solid": True}, # NET
    "BAR4": {"pct": 0, "c1": "FFFF00", "c2": "FF8C00", "c3": "FF0000", "solid": True}, # DISK
}
brightness = 15

# ---------------- Сборщики метрик ----------------

def collect_cpu_ram():
    """CPU% (за интервал) и RAM%."""
    cpu = psutil.cpu_percent(interval=0.1) # Неблокирующий вызов, интервал минимальный
    mem = psutil.virtual_memory()
    return {"cpu_pct": cpu, "ram_pct": mem.percent}

def collect_cpu_temp():
    """Температура CPU. Ищем в /sys/class/thermal."""
    try:
        # Unraid обычно кладёт CPU temp в thermal_zone0 или hwmon
        for zone in psutil.sensors_temperatures():
            for entry in psutil.sensors_temperatures()[zone]:
                if entry.current > 0:
                    return entry.current
    except Exception:
        pass
    return None

def collect_disks():
    """Занято/свободно на массиве и кэше."""
    array_pct = 0.0
    cache_pct = 0.0
    free_tb = 0.0
    
    try:
        array = shutil.disk_usage("/mnt/user")
        array_pct = (array.used / array.total) * 100 if array.total > 0 else 0
        free_tb += array.free / (1024**4)
    except Exception as e:
        log.warning(f"Ошибка чтения /mnt/user: {e}")

    try:
        cache = shutil.disk_usage("/mnt/cache")
        cache_pct = (cache.used / cache.total) * 100 if cache.total > 0 else 0
        free_tb += cache.free / (1024**4)
    except Exception:
        # Если cache не проброшен, игнорируем
        pass

    return {
        "array_pct": round(array_pct, 1),
        "cache_pct": round(cache_pct, 1),
        "free_tb": round(free_tb, 2)
    }

def get_iface_ip(iface):
    """Получить IP интерфейса через socket."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, 25, iface.encode('utf-8')) # SO_BINDTODEVICE
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None

def collect_network(iface_name):
    """Статус, скорость линка, IP, rx/tx."""
    if not iface_name:
        return None
    
    # Статус
    try:
        with open(f"/sys/class/net/{iface_name}/operstate", "r") as f:
            status = f.read().strip().upper()
    except Exception:
        status = "DOWN"
        
    if status != "UP":
        return {"name": iface_name, "status": "DOWN", "speed": "-", "ip": "-", "rx": "0 B/s", "tx": "0 B/s"}

    # Скорость линка (Мбит/с)
    try:
        with open(f"/sys/class/net/{iface_name}/speed", "r") as f:
            speed_mbps = int(f.read().strip())
            speed_str = f"{speed_mbps}M" if speed_mbps < 1000 else f"{speed_mbps//1000}G"
    except Exception:
        speed_str = "?"

    # IP
    ip = get_iface_ip(iface_name) or "-"

    # Трафик (берём мгновенные счётчики, скорость посчитаем в главном цикле через дельту)
    try:
        net_io = psutil.net_io_counters(pernic=True).get(iface_name)
        rx_bytes = net_io.bytes_recv if net_io else 0
        tx_bytes = net_io.bytes_sent if net_io else 0
    except Exception:
        rx_bytes, tx_bytes = 0, 0

    return {
        "name": iface_name,
        "status": "UP",
        "speed": speed_str,
        "ip": ip,
        "rx_bytes": rx_bytes,
        "tx_bytes": tx_bytes
    }

def format_speed(bytes_per_sec):
    """Форматирование скорости для отображения."""
    if bytes_per_sec < 1024:
        return f"{int(bytes_per_sec)} B/s"
    elif bytes_per_sec < 1024 * 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    else:
        return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"

def collect_tautulli():
    """Данные из Tautulli: библиотека, стримы, недавние добавления."""
    plex = {"movies": 0, "series": 0, "songs": 0}
    streams = []
    recent = []

    if not TAUTULLI_URL or not TAUTULLI_API_KEY:
        return plex, streams, recent

    headers = {"Accept": "application/json"}
    params = {"apikey": TAUTULLI_API_KEY}

    # 1. Библиотека (упрощённо: берём из get_library_media_info или парсим homepage)
    # Для надёжности используем get_libraries_info
    try:
        r = requests.get(f"{TAUTULLI_URL}/api/v2", params={**params, "cmd": "get_libraries_info"}, headers=headers, timeout=5)
        if r.ok:
            libs = r.json().get("response", {}).get("data", {}).get("data", [])
            for lib in libs:
                ltype = lib.get("section_type", "")
                count = int(lib.get("count", 0))
                if ltype == "movie": plex["movies"] += count
                elif ltype == "show": plex["series"] += count
                elif ltype == "artist": plex["songs"] += count
    except Exception as e:
        log.warning(f"Tautulli library error: {e}")

    # 2. Активные стримы
    try:
        r = requests.get(f"{TAUTULLI_URL}/api/v2", params={**params, "cmd": "get_activity"}, headers=headers, timeout=5)
        if r.ok:
            sessions = r.json().get("response", {}).get("data", {}).get("sessions", [])
            for s in sessions[:5]: # Максимум 5
                user = s.get("user", "Unknown")[:10]
                progress = int(s.get("progress_percent", 0))
                state = s.get("state", "")
                mode = "T" if "transcode" in s.get("transcode_decision", "").lower() else "D"
                title = s.get("full_title", s.get("title", "Unknown"))
                streams.append({"user": user, "progress": progress, "mode": mode, "title": title})
    except Exception as e:
        log.warning(f"Tautulli activity error: {e}")

    # 3. Недавно добавленные
    try:
        r = requests.get(f"{TAUTULLI_URL}/api/v2", params={**params, "cmd": "get_recently_added", "length": 10}, headers=headers, timeout=5)
        if r.ok:
            items = r.json().get("response", {}).get("data", {}).get("data", [])
            for item in items[:10]:
                added = int(item.get("added_at", 0))
                ago_dt = datetime.now() - datetime.fromtimestamp(added)
                # Форматирование "сколько времени назад"
                if ago_dt.days > 30: continue # Пропускаем старые
                if ago_dt.days > 0: ago_str = f"{ago_dt.days}d"
                elif ago_dt.seconds > 3600: ago_str = f"{ago_dt.seconds//3600}h"
                else: ago_str = f"{ago_dt.seconds//60}m"
                
                media_type = item.get("media_type", "")
                if media_type == "episode":
                    code = f"s{int(item.get('season_num', 0)):02d}e{int(item.get('episode_num', 0)):02d}"
                    title = item.get("grandparent_title", item.get("title", "Unknown"))
                else:
                    code = item.get("year", "")
                    title = item.get("title", "Unknown")
                    
                recent.append({"ago": ago_str, "code": str(code), "title": title})
    except Exception as e:
        log.warning(f"Tautulli recent error: {e}")

    return plex, streams, recent

class QbtClient:
    """Минимальный клиент qBittorrent API v2."""
    def __init__(self, url, user, password):
        self.url = url
        self.session = requests.Session()
        self.authenticated = False
        if user and password:
            self.login(user, password)

    def login(self, user, password):
        try:
            r = self.session.post(f"{self.url}/api/v2/auth/login", data={"username": user, "password": password}, timeout=5)
            if r.ok and r.text == "Ok.":
                self.authenticated = True
                log.info("qBittorrent auth OK")
        except Exception as e:
            log.warning(f"qBittorrent auth failed: {e}")

    def get_downloading(self):
        if not self.authenticated: return []
        try:
            r = self.session.get(f"{self.url}/api/v2/torrents/info", params={"filter": "downloading"}, timeout=5)
            if r.ok:
                torrents = r.json()
                result = []
                for t in torrents[:5]: # Максимум 5
                    name = t.get("name", "Unknown")
                    # Скорость
                    speed = t.get("dlspeed", 0)
                    speed_str = format_speed(speed)
                    # ETA
                    eta_sec = t.get("eta", 8640000)
                    if eta_sec > 8640000: eta_str = "∞"
                    elif eta_sec > 3600: eta_str = f"{eta_sec//3600}h{(eta_sec%3600)//60}m"
                    else: eta_str = f"{(eta_sec//60)}m"
                    
                    result.append({"name": name, "speed": speed_str, "eta": eta_str})
                return result
        except Exception as e:
            log.warning(f"qBittorrent fetch error: {e}")
        return []

# ---------------- Главный цикл ----------------

def run_main_loop():
    global current_context
    ser = None
    last_net1 = None
    last_net2 = None
    last_poll = 0
    qbt_client = QbtClient(QBT_URL, QBT_USER, QBT_PASS)

    def connect_serial():
        nonlocal ser
        try:
            ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
            time.sleep(2) # Ждём инициализации Arduino
            protocol_state.reset()
            log.info(f"Serial connected: {SERIAL_PORT}")
            return True
        except Exception as e:
            log.error(f"Serial connect failed: {e}")
            ser = None
            return False

    connect_serial()

    while True:
        now = time.time()
        
        # 1. Сбор метрик
        ctx = {}
        ctx.update(collect_cpu_ram())
        ctx["cpu_temp_c"] = collect_cpu_temp()
        ctx.update(collect_disks())
        
        # Сеть с расчётом дельты скорости
        net1_raw = collect_network(NET_IFACE1)
        net2_raw = collect_network(NET_IFACE2) if NET_IFACE2 else None
        
        ctx["net"] = {"net1": None, "net2": None}
        if net1_raw:
            if last_net1 and net1_raw["status"] == "UP":
                dt = now - last_poll
                rx_speed = (net1_raw["rx_bytes"] - last_net1["rx_bytes"]) / dt if dt > 0 else 0
                tx_speed = (net1_raw["tx_bytes"] - last_net1["tx_bytes"]) / dt if dt > 0 else 0
                net1_raw["rx"] = format_speed(rx_speed)
                net1_raw["tx"] = format_speed(tx_speed)
            else:
                net1_raw["rx"] = "0 B/s"
                net1_raw["tx"] = "0 B/s"
            ctx["net"]["net1"] = net1_raw
            last_net1 = net1_raw
            
        if net2_raw:
            if last_net2 and net2_raw["status"] == "UP":
                dt = now - last_poll
                rx_speed = (net2_raw["rx_bytes"] - last_net2["rx_bytes"]) / dt if dt > 0 else 0
                tx_speed = (net2_raw["tx_bytes"] - last_net2["tx_bytes"]) / dt if dt > 0 else 0
                net2_raw["rx"] = format_speed(rx_speed)
                net2_raw["tx"] = format_speed(tx_speed)
            else:
                net2_raw["rx"] = "0 B/s"
                net2_raw["tx"] = "0 B/s"
            ctx["net"]["net2"] = net2_raw
            last_net2 = net2_raw

        # Внешние API
        ctx["plex"], ctx["streams"], ctx["recent"] = collect_tautulli()
        ctx["qbt"] = qbt_client.get_downloading()

        # 2. Рендер экранов (берём актуальный список из веб-модуля)
        active_screens = screens_webui.get_screens()
        lines = rotation_state.current_lines(active_screens, ctx, now)
        
        # 3. Формирование протокола
        # Считаем проценты для баров (упрощённо, можно брать из ctx)
        bar_config["BAR1"]["pct"] = ctx.get("cpu_pct", 0)
        bar_config["BAR2"]["pct"] = ctx.get("ram_pct", 0)
        
        # Сеть для бара: берём макс из rx/tx относительно NET_MAX_MBPS
        net_pct = 0
        if ctx["net"]["net1"] and ctx["net"]["net1"]["status"] == "UP":
            # Парсим скорость обратно в байты для расчёта % (грубо)
            # Для простоты оставим 0, если нет парсинга, или сделаем заглушку
            pass 
        bar_config["BAR3"]["pct"] = net_pct
        bar_config["BAR4"]["pct"] = ctx.get("array_pct", 0)

        values = {
            "BRI": str(brightness),
            "L1": lines[0],
            "L2": lines[1],
            "L3": lines[2],
        }
        for i in range(1, 5):
            b = bar_config[f"BAR{i}"]
            values[f"BAR{i}"] = protocol.pack_bar(b["pct"], b["c1"], b["c2"], b["c3"], b["solid"])

        serial_str = protocol_state.build(values, now)

        # 4. Отправка на Arduino
        if serial_str:
            if ser and ser.is_open:
                try:
                    ser.write((serial_str + "\n").encode('utf-8'))
                except Exception as e:
                    log.error(f"Serial write error: {e}")
                    ser.close()
                    ser = None
            elif not ser:
                connect_serial()

        # 5. Сохранение контекста для веб-интерфейса
        with context_lock:
            current_context = ctx

        last_poll = now
        time.sleep(POLL_INTERVAL)

# ---------------- Flask App ----------------

def create_app():
    app = Flask(__name__)
    
    # Главная страница (заглушка, можно расширить)
    @app.route("/")
    def index():
        return """<html><body style="background:#17181a;color:#e0e0e0;font-family:sans-serif;text-align:center;padding:50px;">
        <h1 style="color:#ff8c2f;">shkaf-hud</h1>
        <p>Система активна. Перейдите в раздел <a href="/screens" style="color:#ff8c2f;">OLED screens</a> для настройки.</p>
        </body></html>"""

    # API для баров (упрощённое)
    @app.route("/api/bars", methods=["GET", "POST"])
    def api_bars():
        global bar_config, brightness
        if request.method == "POST":
            data = request.get_json(force=True)
            if "brightness" in data:
                brightness = int(data["brightness"])
            for i in range(1, 5):
                key = f"BAR{i}"
                if key in data:
                    bar_config[key] = data[key]
            protocol_state.reset() # Форсируем отправку новых цветов
            return jsonify({"ok": True})
        return jsonify({"bars": bar_config, "brightness": brightness})

    # Регистрируем роуты экранов
    def get_context_safe():
        with context_lock:
            return dict(current_context)
            
    screens_webui.register_screens_routes(app, get_context_safe)
    
    return app

# ---------------- Точка входа ----------------

if __name__ == "__main__":
    log.info("Starting shkaf-hud bridge...")
    
    # Запуск Flask в отдельном потоке
    app = create_app()
    flask_thread = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=WEB_PORT, use_reloader=False), daemon=True)
    flask_thread.start()
    
    # Запуск главного цикла в основном потоке
    try:
        run_main_loop()
    except KeyboardInterrupt:
        log.info("Shutting down...")
