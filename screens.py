"""
screens.py
Хранилище OLED-экранов + логика ротации и фильтрации.

Экран - это {id, name, l1, l2, l3, duration, enabled}. 
name - чисто служебное, на дисплей не попадает. l1/l2/l3 - шаблоны (см. templates.py).

КЛЮЧЕВЫЕ ПРАВИЛА ФИЛЬТРАЦИИ:
1. Если шаблоны экрана используют переменные из повторяющейся группы (stream/
   recent/qbt) - такой экран разворачивается в N копий (по числу активных элементов).
   Если сейчас 0 элементов - экран целиком пропускается (не показывается вообще).
2. Если экран использует обычные (scalar) переменные, но хоть одна не резолвится
   (например net2 не настроен или отвалился) - экран тоже пропускается.
3. Смешивать переменные из РАЗНЫХ повторяющихся групп в одном экране нельзя 
   (например stream и qbt одновременно) - такой экран считается невалидным и игнорируется.

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

# Дефолтные экраны. Они создадутся при первом запуске, если файла конфигурации нет.
DEFAULT_SCREENS = [
    {
        "id": "default-storage",
        "name": "Storage",
        "l1": "Cache {cache_pct}%",
        "l2": "Array {array_pct}%",
        "l3": "Free {free_tb:.2f}TB",
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
        "l2": "↓ {net1_rx}",
        "l3": "↑ {net1_tx}",
        "duration": 4.0,
        "enabled": True,
    },
    {
        "id": "default-net2",
        "name": "Network 2",
        "l1": "{net2_name} {net2_speed} {net2_ip}",
        "l2": "↓ {net2_rx}",
        "l3": "↑ {net2_tx}",
        "duration": 4.0,
        "enabled": True,
    },
    {
        "id": "default-plex",
        "name": "Plex Library",
        "l1": "Movies {plex_movies}",
        "l2": "Series {plex_series}",
        "l3": "Songs {plex_songs}",
        "duration": 4.0,
        "enabled": True,
    },
    {
        "id": "default-streams",
        "name": "Active Streams",
        "l1": "Playing {stream_pos}/{stream_count}",
        "l2": "{stream_user:4} {stream_progress}% {stream_mode}",
        "l3": "{stream_title}",
        "duration": 5.0,
        "enabled": True,
    },
    {
        "id": "default-recent",
        "name": "Recent Additions",
        "l1": "Added: {recent_ago}",
        "l2": "{recent_code}",
        "l3": "{recent_title}",
        "duration": 5.0,
        "enabled": True,
    },
]

# ---------------- Загрузка / Сохранение ----------------

def load_screens():
    """Загружает экраны из JSON. Если файла нет или он битый - возвращает дефолт."""
    try:
        with open(SCREENS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return data
    except Exception:
        pass
    return [dict(s) for s in DEFAULT_SCREENS]

def save_screens(screens):
    """Сохраняет текущий список экранов в JSON."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SCREENS_FILE, "w", encoding="utf-8") as f:
        json.dump(screens, f, ensure_ascii=False, indent=2)

# ---------------- CRUD операции ----------------

def new_screen(name="New screen", l1="", l2="", l3="", duration=4.0):
    """Фабрика нового экрана с валидацией полей."""
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

def get_screen_by_id(screens, screen_id):
    for s in screens:
        if s["id"] == screen_id:
            return s
    return None

def reorder_screens(screens, id_order):
    """Пересобирает список экранов в порядке id_order. Остальные добавляет в конец."""
    by_id = {s["id"]: s for s in screens}
    reordered = [by_id[i] for i in id_order if i in by_id]
    missing = [s for s in screens if s["id"] not in id_order]
    return reordered + missing

# ---------------- Рендер активного списка (для текущего тика) ----------------

def build_active_screens(screens, context):
    """
    Возвращает список готовых к показу экранов:
    [{"screen_id": ..., "lines": [l1,l2,l3], "duration": float}, ...]
    
    Применяет строгую фильтрацию:
    - Скрыты экраны с 0 активных элементов в повторяющейся группе.
    - Скрыты экраны, где не резолвятся scalar-переменные.
    - Скрыты невалидные экраны (смешаны разные повторяющиеся группы).
    """
    active = []
    for screen in screens:
        if not screen.get("enabled", True):
            continue
            
        l1, l2, l3 = screen.get("l1", ""), screen.get("l2", ""), screen.get("l3", "")
        
        # Определяем, к каким повторяющимся группам относится экран
        groups = set()
        for tpl in (l1, l2, l3):
            groups |= templates.template_group(tpl)
            
        if len(groups) > 1:
            # Смешаны переменные из разных повторяющихся групп (например stream и qbt)
            # Это невалидный конфиг, пропускаем экран, чтобы не рендерить чепуху
            continue
            
        if not groups:
            # Обычный (нерепитящийся) экран. Рендерим один раз.
            r1, ok1 = templates.render(l1, context)
            r2, ok2 = templates.render(l2, context)
            r3, ok3 = templates.render(l3, context)
            if ok1 and ok2 and ok3:
                active.append({
                    "screen_id": screen["id"], 
                    "lines": [r1, r2, r3], 
                    "duration": screen["duration"]
                })
            continue
            
        # Повторяющийся экран - разворачиваем по числу активных элементов
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

# ---------------- Ротация ----------------

class RotationState:
    """
    Живёт в памяти главного цикла. Продвигает текущий экран по его 
    собственному duration. Устойчива к изменению длины активного списка 
    между тиками (стрим кончился/начался, торрент докачался и т.п.).
    """
    def __init__(self):
        self.index = 0
        self.switched_at = time.time()
        self.last_active_count = 0

    def current_lines(self, screens, context, now=None):
        now = now if now is not None else time.time()
        active = build_active_screens(screens, context)
        
        if not active:
            # Нет ни одного экрана для показа. Возвращаем пустые строки.
            # Сбрасываем индекс и таймер, чтобы при появлении экранов начать сначала.
            self.index = 0
            self.switched_at = now
            self.last_active_count = 0
            return ["", "", ""]

        # Если список активных экранов изменился (кто-то отвалился или появился),
        # корректируем индекс через modulo, чтобы не выйти за границы списка.
        # Таймер при этом НЕ сбрасываем - пусть текущий экран доиграет своё время.
        if len(active) != self.last_active_count:
            self.index = self.index % len(active)
            self.last_active_count = len(active)

        current = active[self.index]
        
        if now - self.switched_at >= current["duration"]:
            self.index = (self.index + 1) % len(active)
            self.switched_at = now
            current = active[self.index]
            
        return current["lines"]