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


def qbt_get(path, **params):
    headers = {"Authorization": f"Bearer {QBT_API_KEY}"}
    r = requests.get(f"{QBT_URL}/api/v2/{path}", params=params, headers=headers, timeout=5)
    r.raise_for_status()
    return r.json()


def format_qbt_speed(dlspeed, upspeed):
    """Скорость с указанием направления - качаем (↓) или раздаём (↑)."""
    def human(bps):
        for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
            if bps < 1024:
                return f"{bps:.0f} {unit}" if unit == "B/s" else f"{bps:.1f} {unit}"
            bps /= 1024
        return f"{bps:.1f} TB/s"

    if dlspeed > 0:
        return f"\u2193 {human(dlspeed)}"
    if upspeed > 0:
        return f"\u2191 {human(upspeed)}"
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