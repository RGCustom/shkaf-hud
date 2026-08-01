"""
variables.py
Реестр переменных для OLED-шаблонов (L1/L2/L3).
Ничего сам не собирает - работает поверх "context": обычного словаря с уже
готовыми данными, который раз в тик формирует главный цикл (main-скрипт) и
передаёт сюда. Здесь только словарь "имя переменной -> как её достать из
context", плюс легенда для веб-интерфейса.

Три вида переменных:
"scalar"  - одно значение, всегда одно и то же (cpu_pct, plex_movies и т.п.)
"list"    - относится к повторяющимся источникам (stream/recent/qbt) -
            резолвится с указанием index (какой именно поток/новинка/закачка)
служебные *_count переменные - сколько элементов сейчас доступно в list-группе
            (используются screens.py, чтобы понять, сколько копий экрана
            развернуть в ротации, и стоит ли вообще их показывать)
"""

# ---------------- структура context (для справки) ----------------
# context = {
#   "cpu_pct": float, "ram_pct": float, "cpu_temp_c": float|None,
#   "disk_pct": float, "array_pct": float, "cache_pct": float, "free_tb": float,
#   "net": {
#       "net1": {"name": str, "status": "UP"|"DOWN", "speed": str, "ip": str, "rx": str, "tx": str} | None,
#       "net2": {"name": str, "status": "UP"|"DOWN", "speed": str, "ip": str, "rx": str, "tx": str} | None,
#   },
#   "plex": {"movies": int, "series": int, "songs": int},
#   "streams": [ {"user": str, "progress": int, "mode": "D"|"T", "title": str}, ... ],  # до 5
#   "recent":  [ {"ago": str, "code": str, "title": str}, ... ],
#   "qbt":     [ {"name": str, "speed": str, "eta": str}, ... ],
# }

def _scalar(path):
    """path вида 'plex.movies' - достаёт значение из context по цепочке ключей."""
    keys = path.split(".")
    def resolver(context, index=None):
        val = context
        for k in keys:
            if val is None:
                return None
            val = val.get(k)
        return val
    return resolver

def _list_field(group, field):
    """group='streams', field='title' - достаёт context[group][index][field]."""
    def resolver(context, index=None):
        items = context.get(group) or []
        if index is None or index >= len(items):
            return None
        return items[index].get(field)
    return resolver

def _net_field(slot, field):
    """slot='net1'|'net2' - достаёт context['net'][slot][field], None если интерфейс не выбран/недоступен."""
    def resolver(context, index=None):
        net = context.get("net") or {}
        entry = net.get(slot)
        if not entry:
            return None
        return entry.get(field)
    return resolver

def _count(group):
    def resolver(context, index=None):
        return len(context.get(group) or [])
    return resolver

def _position():
    """1-based номер текущего экземпляра внутри повторяющейся группы (для 'Playing 1/2')."""
    def resolver(context, index=None):
        return (index + 1) if index is not None else None
    return resolver

# ---------------- реестр ----------------
# group: "scalar" | "stream" | "recent" | "qbt"  (последние три - "повторяющиеся")
VARIABLES = {
    # --- система ---
    "cpu_pct":    {"label": "Загрузка CPU, %",          "group": "scalar", "resolver": _scalar("cpu_pct")},
    "ram_pct":    {"label": "Загрузка RAM, %",          "group": "scalar", "resolver": _scalar("ram_pct")},
    "cpu_temp_c": {"label": "Температура CPU, °C",      "group": "scalar", "resolver": _scalar("cpu_temp_c")},
    "disk_pct":   {"label": "%util дисков массива",     "group": "scalar", "resolver": _scalar("disk_pct")},
    "array_pct":  {"label": "Занято на массиве, %",     "group": "scalar", "resolver": _scalar("array_pct")},
    "cache_pct":  {"label": "Занято на cache, %",    "group": "scalar", "resolver": _scalar("cache_pct")},
    "free_tb":    {"label": "Свободно (array+cache), TB", "group": "scalar", "resolver": _scalar("free_tb")},

    # --- сеть, слот 1 ---
    "net1_name":   {"label": "Net1: имя интерфейса",     "group": "scalar", "resolver": _net_field("net1", "name")},
    "net1_status": {"label": "Net1: статус (UP/DOWN)",   "group": "scalar", "resolver": _net_field("net1", "status")},
    "net1_speed":  {"label": "Net1: скорость линка",     "group": "scalar", "resolver": _net_field("net1", "speed")},
    "net1_ip":     {"label": "Net1: IP-адрес",           "group": "scalar", "resolver": _net_field("net1", "ip")},
    "net1_rx":     {"label": "Net1: входящая скорость",  "group": "scalar", "resolver": _net_field("net1", "rx")},
    "net1_tx":     {"label": "Net1: исходящая скорость", "group": "scalar", "resolver": _net_field("net1", "tx")},

    # --- сеть, слот 2 ---
    "net2_name":   {"label": "Net2: имя интерфейса",     "group": "scalar", "resolver": _net_field("net2", "name")},
    "net2_status": {"label": "Net2: статус (UP/DOWN)",   "group": "scalar", "resolver": _net_field("net2", "status")},
    "net2_speed":  {"label": "Net2: скорость линка",     "group": "scalar", "resolver": _net_field("net2", "speed")},
    "net2_ip":     {"label": "Net2: IP-адрес",           "group": "scalar", "resolver": _net_field("net2", "ip")},
    "net2_rx":     {"label": "Net2: входящая скорость",  "group": "scalar", "resolver": _net_field("net2", "rx")},
    "net2_tx":     {"label": "Net2: исходящая скорость", "group": "scalar", "resolver": _net_field("net2", "tx")},

    # --- Plex/Tautulli: библиотека ---
    "plex_movies": {"label": "Кол-во фильмов",           "group": "scalar", "resolver": _scalar("plex.movies")},
    "plex_series": {"label": "Кол-во сериалов",          "group": "scalar", "resolver": _scalar("plex.series")},
    "plex_songs":  {"label": "Кол-во треков",            "group": "scalar", "resolver": _scalar("plex.songs")},

    # --- Plex/Tautulli: активные стримы (повторяющаяся группа, до 5 штук) ---
    "stream_user":     {"label": "Стрим: пользователь (сокращ.)", "group": "stream", "resolver": _list_field("streams", "user")},
    "stream_progress": {"label": "Стрим: прогресс, %",            "group": "stream", "resolver": _list_field("streams", "progress")},
    "stream_mode":     {"label": "Стрим: D=direct, T=transcode",  "group": "stream", "resolver": _list_field("streams", "mode")},
    "stream_title":    {"label": "Стрим: название",               "group": "stream", "resolver": _list_field("streams", "title")},
    "stream_pos":      {"label": "Стрим: № по счёту (1-based)",   "group": "stream", "resolver": _position()},
    "stream_count":    {"label": "Стрим: сколько сейчас активно", "group": "scalar", "resolver": _count("streams")},

    # --- Tautulli: недавно добавленное (повторяющаяся группа) ---
    "recent_ago":   {"label": "Добавлено: сколько времени назад",   "group": "recent", "resolver": _list_field("recent", "ago")},
    "recent_code":  {"label": "Добавлено: s01e01 или год фильма",   "group": "recent", "resolver": _list_field("recent", "code")},
    "recent_title": {"label": "Добавлено: название",                "group": "recent", "resolver": _list_field("recent", "title")},
    "recent_pos":   {"label": "Добавлено: № по счёту (1-based)",    "group": "recent", "resolver": _position()},
    "recent_count": {"label": "Добавлено: сколько сейчас доступно", "group": "scalar", "resolver": _count("recent")},

    # --- qBittorrent (повторяющаяся группа) ---
    "qbt_name":  {"label": "Торрент: имя файла",             "group": "qbt", "resolver": _list_field("qbt", "name")},
    "qbt_speed": {"label": "Торрент: скорость",              "group": "qbt", "resolver": _list_field("qbt", "speed")},
    "qbt_eta":   {"label": "Торрент: осталось времени",      "group": "qbt", "resolver": _list_field("qbt", "eta")},
    "qbt_pos":   {"label": "Торрент: № по счёту (1-based)",  "group": "qbt", "resolver": _position()},
    "qbt_count": {"label": "Торрент: сколько активно",       "group": "scalar", "resolver": _count("qbt")},
}

# Группы, которые "разворачиваются" в несколько экранов (по числу элементов).
REPEATING_GROUPS = ("stream", "recent", "qbt")

# Разумный потолок на всякий случай
REPEATING_GROUP_MAX = {"stream": 5, "recent": 10, "qbt": 10}

def group_count(group_name, context):
    """Сколько элементов сейчас доступно в повторяющейся группе (stream/recent/qbt)."""
    key_map = {"stream": "streams", "recent": "recent", "qbt": "qbt"}
    items = context.get(key_map.get(group_name, group_name)) or []
    return min(len(items), REPEATING_GROUP_MAX.get(group_name, 10))

def resolve(var_name, context, index=None):
    """Достать значение переменной. Возвращает None, если переменной нет
    в реестре, либо данных сейчас нет (например net2 не выбран, или индекс
    вне диапазона активных стримов)."""
    spec = VARIABLES.get(var_name)
    if spec is None:
        return None
    try:
        return spec["resolver"](context, index)
    except Exception:
        return None

def legend():
    """Для веб-интерфейса: список (имя, группа, подпись) для отображения легенды."""
    return [
        {"name": name, "group": spec["group"], "label": spec["label"]}
        for name, spec in VARIABLES.items()
    ]