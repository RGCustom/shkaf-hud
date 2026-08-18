"""
qbittorrent.py

Сбор данных об активных торрентах qBittorrent для повторяющейся группы "qbt"
(см. variables.py: qbt_name/qbt_speed/qbt_eta/qbt_pos/qbt_count).

Авторизация - через API-ключ (qBittorrent >= 5.2.0 / WebAPI >= 2.14.1):
stateless, без логина/пароля и без кук - просто заголовок
"Authorization: Bearer <ключ>" на каждый запрос. Ключ генерируется в
qBittorrent: Preferences -> WebUI -> API Key -> Generate.

Настройка через переменные окружения (как и Tautulli):
    QBT_URL      - адрес WebUI, например http://127.0.0.1:8080
    QBT_API_KEY  - сгенерированный API-ключ (пусто = интеграция выключена)
"""

import os

import requests

import variables

QBT_URL = os.environ.get("QBT_URL", "http://127.0.0.1:8080")
QBT_API_KEY = os.environ.get("QBT_API_KEY", "")

# Что считать 100% на LED-барах qbt_dl/qbt_ul (аналогично NET_MAX_MBPS для
# общего сетевого бара) - подобрать под реальную ширину своего канала.
QBT_MAX_MBPS = float(os.environ.get("QBT_MAX_MBPS", "100"))


def qbt_get(path, **params):
    headers = {"Authorization": f"Bearer {QBT_API_KEY}"}
    r = requests.get(f"{QBT_URL}/api/v2/{path}", params=params, headers=headers, timeout=5)
    r.raise_for_status()
    return r.json()


def human_rate(bps):
    """'1234' (B/s) -> '1.2 KB/s' и т.п. - общий форматтер скорости: используется
    и для одного торрента (format_qbt_speed), и для суммарной по всем (get_qbt_totals)."""
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024:
            return f"{bps:.0f} {unit}" if unit == "B/s" else f"{bps:.1f} {unit}"
        bps /= 1024
    return f"{bps:.1f} TB/s"


def format_qbt_speed(dlspeed, upspeed):
    """Скорость с указанием направления - качаем (↓) или раздаём (↑)."""
    if dlspeed > 0:
        return f"\u2193 {human_rate(dlspeed)}"
    if upspeed > 0:
        return f"\u2191 {human_rate(upspeed)}"
    return "0 B/s"


def format_qbt_eta(eta_seconds, dlspeed):
    """qBittorrent отдаёт eta=8640000 (100 дней) как 'бесконечность/неизвестно'."""
    if dlspeed <= 0:
        return "раздача"
    if eta_seconds is None or eta_seconds < 0 or eta_seconds >= 8640000:
        return "?"
    h, rem = divmod(int(eta_seconds), 3600)
    m, _ = divmod(rem, 60)
    return f"{h}ч {m}м" if h > 0 else f"{m}м"


def get_qbt_active(limit=None):
    """
    Активные торренты (качаются или раздаются) в формате, который ждёт
    variables.py: [{"name": str, "speed": str, "eta": str}, ...].
    Пустой QBT_API_KEY = интеграция выключена, тихо возвращаем [].
    """
    if not QBT_API_KEY:
        return []

    limit = limit or variables.REPEATING_GROUP_MAX["qbt"]

    try:
        torrents = qbt_get("torrents/info", filter="active", sort="dlspeed", reverse="true")
    except Exception as e:
        print(f"[qbt] torrents/info failed: {e}", flush=True)
        return []

    result = []
    for t in torrents[:limit]:
        dlspeed = t.get("dlspeed", 0)
        result.append({
            "name": t.get("name", "?"),
            "speed": format_qbt_speed(dlspeed, t.get("upspeed", 0)),
            "eta": format_qbt_eta(t.get("eta"), dlspeed),
        })
    return result


def format_free_space(bytes_val):
    """Свободное место на диске загрузок (server_state.free_space_on_disk)."""
    if bytes_val is None or bytes_val < 0:
        return "?"
    gb = bytes_val / (1024 ** 3)
    if gb >= 1000:
        return f"{gb / 1024:.2f} TB"
    return f"{gb:.1f} GB"


def qbt_speed_pct(bytes_per_sec):
    """0-100% от QBT_MAX_MBPS - для LED-баров qbt_dl/qbt_ul (та же логика,
    что net_pct от NET_MAX_MBPS в главном скрипте)."""
    if QBT_MAX_MBPS <= 0:
        return 0.0
    mbps = bytes_per_sec * 8 / 1_000_000
    return max(0.0, min(100.0, mbps / QBT_MAX_MBPS * 100.0))


def get_qbt_totals():
    """
    Сводная статистика по ВСЕМ торрентам (в отличие от get_qbt_active(), которая
    берёт только активные и с лимитом REPEATING_GROUP_MAX) - для переменных
    qbt_total_dl/qbt_total_ul/qbt_count_all/qbt_ratio/qbt_free_space_gb, плюс
    dl_pct/ul_pct (0-100%) для LED-баров.
    Пустой QBT_API_KEY = интеграция выключена, тихо возвращаем нули.
    """
    empty = {
        "total_dl": "0 B/s", "total_ul": "0 B/s",
        "count_all": 0, "ratio": 0.0, "free_space": "?",
        "dl_pct": 0.0, "ul_pct": 0.0,
    }
    if not QBT_API_KEY:
        return empty

    # sync/maindata.server_state - глобальные текущие скорости и свободное
    # место одним запросом (дешевле, чем суммировать dlspeed/upspeed по
    # списку торрентов вручную).
    dlspeed = upspeed = 0
    free_space = None
    try:
        maindata = qbt_get("sync/maindata")
        server_state = maindata.get("server_state", {})
        dlspeed = server_state.get("dl_info_speed", 0)
        upspeed = server_state.get("up_info_speed", 0)
        free_space = server_state.get("free_space_on_disk")
    except Exception as e:
        print(f"[qbt] sync/maindata failed: {e}", flush=True)

    # torrents/info без фильтра - все торренты, для количества и ratio
    # (сумма uploaded/downloaded за всё время по каждому торренту).
    count_all = 0
    ratio = 0.0
    try:
        all_torrents = qbt_get("torrents/info")
        count_all = len(all_torrents)
        total_downloaded = sum(t.get("downloaded", 0) for t in all_torrents)
        total_uploaded = sum(t.get("uploaded", 0) for t in all_torrents)
        if total_downloaded > 0:
            ratio = round(total_uploaded / total_downloaded, 2)
    except Exception as e:
        print(f"[qbt] torrents/info (all) failed: {e}", flush=True)

    return {
        "total_dl": human_rate(dlspeed),
        "total_ul": human_rate(upspeed),
        "count_all": count_all,
        "ratio": ratio,
        "free_space": format_free_space(free_space),
        "dl_pct": qbt_speed_pct(dlspeed),
        "ul_pct": qbt_speed_pct(upspeed),
    }