"""
screens.py

Хранилище OLED-экранов + логика ротации.

Экран - это {id, name, l1, l2, l3, duration, enabled}. name - чисто служебное,
на дисплей не попадает. l1/l2/l3 - шаблоны (см. templates.py).

Если шаблоны экрана используют переменные из повторяющейся группы (stream/
recent/qbt) - такой экран на рендере разворачивается в N копий (по числу
активных элементов). Если сейчас 0 элементов - экран целиком пропускается.
Если экран использует обычные (scalar) переменные, но они не резолвятся
(например net2 не настроен) - тоже пропускается.

Ротация учитывает "duration" каждого экрана отдельно (не общий интервал).
"""

import json
import os
import time
import uuid

import templates
import variables

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
SCREENS_FILE = os.path.join(CONFIG_DIR, "screens.json")

DEFAULT_SCREENS = [
    {
        "id": "default-storage",
        "name": "Storage",
        "l1": "Cache {cache_pct}%",
        "l2": "Array {array_pct}%",
        "l3": "Fr {free_tb:.2f}TB",
        "duration": 4.0,
        "enabled": True,
    },
    {
        "id": "default-cpuram",
        "name": "CPU/RAM",
        "l1": "CPU {cpu_pct}%",
        "l2": "TEMP {cpu_temp_c:.0f}°C",
        "l3": "RAM {ram_pct}%",
        "duration": 4.0,
        "enabled": True,
    },
    {
        "id": "default-net1",
        "name": "Network 1",
        "l1": "{net1_name} {net1_speed} {net1_ip}",
        "l2": "\u2193 {net1_rx}",
        "l3": "\u2191 {net1_tx}",
        "duration": 4.0,
        "enabled": True,
    },
    {
        "id": "default-net2",
        "name": "Network 2",
        "l1": "{net2_name} {net2_speed} {net2_ip}",
        "l2": "\u2193 {net2_rx}",
        "l3": "\u2191 {net2_tx}",
        "duration": 4.0,
        "enabled": True,
    },
    {
        "id": "default-plex",
        "name": "Plex",
        "l1": "Movies {plex_movies}",
        "l2": "Series {plex_series}",
        "l3": "Songs {plex_songs}",
        "duration": 4.0,
        "enabled": True,
    },
    {
        "id": "default-streams",
        "name": "Streams",
        "l1": "Playing {stream_pos}/{stream_count}",
        "l2": "{stream_user:4} {stream_progress}% {stream_mode}",
        "l3": "{stream_title}",
        "duration": 5.0,
        "enabled": True,
    },
    {
        "id": "default-recent",
        "name": "RecentAdd",
        "l1": "Added: {recent_ago}",
        "l2": "{recent_code}",
        "l3": "{recent_title}",
        "duration": 5.0,
        "enabled": True,
    },
]


def load_screens():
    try:
        with open(SCREENS_FILE) as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return data
    except Exception:
        pass
    return [dict(s) for s in DEFAULT_SCREENS]


def save_screens(screens):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SCREENS_FILE, "w") as f:
        json.dump(screens, f)


# ---------------- CRUD ----------------

def new_screen(name="New screen", l1="", l2="", l3="", duration=4.0):
    return {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "l1": l1, "l2": l2, "l3": l3,
        "duration": max(1.0, float(duration)),
        "enabled": True,
    }


def create_screen(screens, data):
    screen = new_screen(
        name=data.get("name", "New screen"),
        l1=data.get("l1", ""), l2=data.get("l2", ""), l3=data.get("l3", ""),
        duration=data.get("duration", 4.0),
    )
    screens.append(screen)
    return screens, screen


def update_screen(screens, screen_id, data):
    for s in screens:
        if s["id"] == screen_id:
            for field in ("name", "l1", "l2", "l3"):
                if field in data:
                    s[field] = data[field]
            if "duration" in data:
                s["duration"] = max(1.0, float(data["duration"]))
            if "enabled" in data:
                s["enabled"] = bool(data["enabled"])
            return screens, s
    return screens, None


def delete_screen(screens, screen_id):
    return [s for s in screens if s["id"] != screen_id]


def reorder_screens(screens, id_order):
    by_id = {s["id"]: s for s in screens}
    reordered = [by_id[i] for i in id_order if i in by_id]
    # если в id_order чего-то не хватает (рассинхрон) - остальные добавляем в конец,
    # чтобы случайно не потерять экран
    missing = [s for s in screens if s["id"] not in id_order]
    return reordered + missing


# ---------------- рендер активного списка (для текущего тика) ----------------

def build_active_screens(screens, context):
    """
    Возвращает список готовых к показу экранов:
        [{"screen_id": ..., "lines": [l1,l2,l3], "duration": float}, ...]
    Экраны с 0 активных элементов в повторяющейся группе, или с нерезолвящимися
    scalar-переменными - в список не попадают вообще.
    """
    active = []

    for screen in screens:
        if not screen.get("enabled", True):
            continue

        l1, l2, l3 = screen.get("l1", ""), screen.get("l2", ""), screen.get("l3", "")
        groups = set()
        for tpl in (l1, l2, l3):
            groups |= templates.template_group(tpl)

        if len(groups) > 1:
            # смешаны переменные из разных повторяющихся групп - невалидный конфиг,
            # пропускаем экран целиком, чтобы не рендерить чепуху
            continue

        if not groups:
            # обычный (нерепитящийся) экран
            r1, ok1 = templates.render(l1, context)
            r2, ok2 = templates.render(l2, context)
            r3, ok3 = templates.render(l3, context)
            if ok1 and ok2 and ok3:
                active.append({"screen_id": screen["id"], "lines": [r1, r2, r3], "duration": screen["duration"]})
            continue

        # повторяющийся экран - разворачиваем по числу активных элементов
        group_name = next(iter(groups))
        count = variables.group_count(group_name, context)
        for idx in range(count):
            r1, ok1 = templates.render(l1, context, index=idx)
            r2, ok2 = templates.render(l2, context, index=idx)
            r3, ok3 = templates.render(l3, context, index=idx)
            if ok1 and ok2 and ok3:
                active.append({
                    "screen_id": f"{screen['id']}#{idx}",
                    "lines": [r1, r2, r3],
                    "duration": screen["duration"],
                })

    return active


# ---------------- ротация ----------------

class RotationState:
    """Живёт в памяти главного цикла - продвигает текущий экран по его
    собственному duration, переживает изменение длины активного списка
    между тиками (стрим кончился/начался и т.п.)."""

    def __init__(self):
        self.index = 0
        self.switched_at = time.time()

    def current_lines(self, screens, context, now=None):
        now = now if now is not None else time.time()
        active = build_active_screens(screens, context)

        if not active:
            return ["", "", ""]

        if self.index >= len(active):
            self.index = 0
            self.switched_at = now

        current = active[self.index]

        if now - self.switched_at >= current["duration"]:
            self.index = (self.index + 1) % len(active)
            self.switched_at = now
            current = active[self.index]

        return current["lines"]
