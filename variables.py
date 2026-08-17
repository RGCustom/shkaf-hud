"""
variables.py

Реестр переменных для OLED-шаблонов (L1/L2/L3).

Ничего сам не собирает - работает поверх "context": обычного словаря с уже
готовыми данными, который раз в тик формирует главный цикл (main-скрипт) и
передаёт сюда. Здесь только словарь "имя переменной -> как её достать из
context", плюс легенда для веб-интерфейса.

Три вида переменных:
  - "scalar"  - одно значение, всегда одно и то же (cpu_pct, plex_movies и т.п.)
  - "list"    - относится к повторяющимся источникам (stream/recent/qbt) -
                резолвится с указанием index (какой именно поток/новинка/закачка)
  - служебные *_count переменные - сколько элементов сейчас доступно в list-группе
                (используются screens.py, чтобы понять, сколько копий экрана
                разворачивать в ротации, и стоит ли вообще их показывать)
"""

# ---------------- структура context (для справки) ----------------
#
# context = {
#     "cpu_pct": float, "ram_pct": float, "cpu_temp_c": float|None,
#     "disk_pct": float, "array_pct": float, "cache_pct": float, "free_tb": float,
#     "net": {
#         "net1": {"name": str, "speed": str, "ip": str, "rx": str, "tx": str} | None,
#         "net2": {...} | None,
#     },
#     "plex": {"movies": int, "series": int, "songs": int},
#     "streams": [ {"user": str, "progress": int, "mode": "D"|"T", "title": str}, ... ],  # до 5
#     "recent":  [ {"ago": str, "code": str, "title": str}, ... ],
#     "qbt":     [ {"name": str, "speed": str, "eta": str}, ... ],
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
#
# group: "scalar" | "stream" | "recent" | "qbt"  (последние три - "повторяющиеся",
#         используется screens.py для разворачивания экрана в N копий - НЕ трогать
#         при перекатегоризации легенды)
# category: только для группировки легенды на /screens (buildLegend() в
#         screens_webui.py) - на разворачивание экранов не влияет

VARIABLES = {
    # --- система ---
    "cpu_pct":     {"label": "Загрузка CPU, %",         "group": "scalar", "category": "Система", "resolver": _scalar("cpu_pct")},
    "cpu_pct_core_max": {"label": "Загрузка самого нагруженного ядра, %", "group": "scalar", "category": "Система", "resolver": _scalar("cpu_pct_core_max")},
    "cpu_freq_mhz": {"label": "Частота CPU, МГц (среднее по ядрам)", "group": "scalar", "category": "Система", "resolver": _scalar("cpu_freq_mhz")},
    "ram_pct":     {"label": "Загрузка RAM, %",          "group": "scalar", "category": "Система", "resolver": _scalar("ram_pct")},
    "ram_used_gb": {"label": "RAM занято, GB",           "group": "scalar", "category": "Система", "resolver": _scalar("ram_used_gb")},
    "ram_total_gb": {"label": "RAM всего, GB",           "group": "scalar", "category": "Система", "resolver": _scalar("ram_total_gb")},
    "swap_pct":    {"label": "Загрузка SWAP, %",         "group": "scalar", "category": "Система", "resolver": _scalar("swap_pct")},
    "load1":       {"label": "Load average, 1 мин",      "group": "scalar", "category": "Система", "resolver": _scalar("load1")},
    "load5":       {"label": "Load average, 5 мин",      "group": "scalar", "category": "Система", "resolver": _scalar("load5")},
    "load15":      {"label": "Load average, 15 мин",     "group": "scalar", "category": "Система", "resolver": _scalar("load15")},
    "cpu_temp_c":  {"label": "Температура CPU, °C",      "group": "scalar", "category": "Система", "resolver": _scalar("cpu_temp_c")},
    "uptime":      {"label": "Аптайм хоста",             "group": "scalar", "category": "Система", "resolver": _scalar("uptime")},
    "container_uptime": {"label": "Аптайм контейнера shkaf-hud", "group": "scalar", "category": "Система", "resolver": _scalar("container_uptime")},
    "time_now":    {"label": "Текущее время (ЧЧ:ММ)",    "group": "scalar", "category": "Система", "resolver": _scalar("time_now")},

    # --- диски / массив ---
    "disk_pct":    {"label": "%util дисков массива",     "group": "scalar", "category": "Диски / массив", "resolver": _scalar("disk_pct")},
    "array_pct":   {"label": "Занято на массиве, %",     "group": "scalar", "category": "Диски / массив", "resolver": _scalar("array_pct")},
    "array_used_tb": {"label": "Массив занято, TB",      "group": "scalar", "category": "Диски / массив", "resolver": _scalar("array_used_tb")},
    "array_total_tb": {"label": "Массив всего, TB",      "group": "scalar", "category": "Диски / массив", "resolver": _scalar("array_total_tb")},
    "cache_pct":   {"label": "Занято на cache, %",    "group": "scalar", "category": "Диски / массив", "resolver": _scalar("cache_pct")},
    "cache_free_tb": {"label": "Cache свободно, TB",     "group": "scalar", "category": "Диски / массив", "resolver": _scalar("cache_free_tb")},
    "cache_total_tb": {"label": "Cache всего, TB",       "group": "scalar", "category": "Диски / массив", "resolver": _scalar("cache_total_tb")},
    "free_tb":     {"label": "Свободно (array+cache), TB", "group": "scalar", "category": "Диски / массив", "resolver": _scalar("free_tb")},

    # --- сеть, слот 1 ---
    "net1_name":   {"label": "Net1: имя интерфейса",     "group": "scalar", "category": "Сеть", "resolver": _net_field("net1", "name")},
    "net1_speed":  {"label": "Net1: скорость линка",     "group": "scalar", "category": "Сеть", "resolver": _net_field("net1", "speed")},
    "net1_ip":     {"label": "Net1: IP-адрес",           "group": "scalar", "category": "Сеть", "resolver": _net_field("net1", "ip")},
    "net1_rx":     {"label": "Net1: входящая скорость",  "group": "scalar", "category": "Сеть", "resolver": _net_field("net1", "rx")},
    "net1_tx":     {"label": "Net1: исходящая скорость", "group": "scalar", "category": "Сеть", "resolver": _net_field("net1", "tx")},
    "net1_total_rx": {"label": "Net1: накоплено принято (с запуска)", "group": "scalar", "category": "Сеть", "resolver": _net_field("net1", "total_rx")},
    "net1_total_tx": {"label": "Net1: накоплено отдано (с запуска)",  "group": "scalar", "category": "Сеть", "resolver": _net_field("net1", "total_tx")},
    "net1_conn_count": {"label": "Net1: активных TCP-соединений",     "group": "scalar", "category": "Сеть", "resolver": _net_field("net1", "conn_count")},

    # --- сеть, слот 2 ---
    "net2_name":   {"label": "Net2: имя интерфейса",     "group": "scalar", "category": "Сеть", "resolver": _net_field("net2", "name")},
    "net2_speed":  {"label": "Net2: скорость линка",     "group": "scalar", "category": "Сеть", "resolver": _net_field("net2", "speed")},
    "net2_ip":     {"label": "Net2: IP-адрес",           "group": "scalar", "category": "Сеть", "resolver": _net_field("net2", "ip")},
    "net2_rx":     {"label": "Net2: входящая скорость",  "group": "scalar", "category": "Сеть", "resolver": _net_field("net2", "rx")},
    "net2_tx":     {"label": "Net2: исходящая скорость", "group": "scalar", "category": "Сеть", "resolver": _net_field("net2", "tx")},
    "net2_total_rx": {"label": "Net2: накоплено принято (с запуска)", "group": "scalar", "category": "Сеть", "resolver": _net_field("net2", "total_rx")},
    "net2_total_tx": {"label": "Net2: накоплено отдано (с запуска)",  "group": "scalar", "category": "Сеть", "resolver": _net_field("net2", "total_tx")},
    "net2_conn_count": {"label": "Net2: активных TCP-соединений",     "group": "scalar", "category": "Сеть", "resolver": _net_field("net2", "conn_count")},

    # --- Media: Plex/Tautulli - библиотека и статус ---
    "plex_movies": {"label": "Кол-во фильмов",           "group": "scalar", "category": "Media", "resolver": _scalar("plex.movies")},
    "plex_series": {"label": "Кол-во сериалов",          "group": "scalar", "category": "Media", "resolver": _scalar("plex.series")},
    "plex_songs":  {"label": "Кол-во треков",            "group": "scalar", "category": "Media", "resolver": _scalar("plex.songs")},
    "plex_server_status": {"label": "Статус Tautulli (online/offline)", "group": "scalar", "category": "Media", "resolver": _scalar("plex_server_status")},
    "plex_transcode_count": {"label": "Сколько стримов сейчас транскодируется", "group": "scalar", "category": "Media", "resolver": _scalar("plex_transcode_count")},
    "plex_users_count": {"label": "Сколько разных пользователей смотрит", "group": "scalar", "category": "Media", "resolver": _scalar("plex_users_count")},

    # --- Media: активные стримы (повторяющаяся группа, до 5 штук) ---
    "stream_user":     {"label": "Стрим: пользователь",        "group": "stream", "category": "Media", "resolver": _list_field("streams", "user")},
    "stream_progress": {"label": "Стрим: прогресс, %",         "group": "stream", "category": "Media", "resolver": _list_field("streams", "progress")},
    "stream_mode":      {"label": "Стрим: D=direct, T=transcode", "group": "stream", "category": "Media", "resolver": _list_field("streams", "mode")},
    "stream_title":     {"label": "Стрим: название",           "group": "stream", "category": "Media", "resolver": _list_field("streams", "title")},
    "stream_bandwidth": {"label": "Стрим: нагрузка на сеть",   "group": "stream", "category": "Media", "resolver": _list_field("streams", "bandwidth")},
    "stream_pos":       {"label": "Стрим: № по счёту (1-based)", "group": "stream", "category": "Media", "resolver": _position()},
    "stream_count":      {"label": "Стрим: сколько сейчас активно", "group": "scalar", "category": "Media", "resolver": _count("streams")},

    # --- Media: недавно добавленное (повторяющаяся группа) ---
    "recent_ago":   {"label": "Добавлено: сколько времени назад", "group": "recent", "category": "Media", "resolver": _list_field("recent", "ago")},
    "recent_code":  {"label": "Добавлено: s01e01 или год фильма", "group": "recent", "category": "Media", "resolver": _list_field("recent", "code")},
    "recent_title": {"label": "Добавлено: название",              "group": "recent", "category": "Media", "resolver": _list_field("recent", "title")},
    "recent_pos":   {"label": "Добавлено: № по счёту (1-based)",  "group": "recent", "category": "Media", "resolver": _position()},
    "recent_count":  {"label": "Добавлено: сколько сейчас доступно", "group": "scalar", "category": "Media", "resolver": _count("recent")},

    # --- qBittorrent: активные торренты (повторяющаяся группа) ---
    "qbt_name":  {"label": "Торрент: имя файла",   "group": "qbt", "category": "qBittorrent", "resolver": _list_field("qbt", "name")},
    "qbt_speed": {"label": "Торрент: скорость",     "group": "qbt", "category": "qBittorrent", "resolver": _list_field("qbt", "speed")},
    "qbt_eta":   {"label": "Торрент: осталось времени", "group": "qbt", "category": "qBittorrent", "resolver": _list_field("qbt", "eta")},
    "qbt_pos":   {"label": "Торрент: № по счёту (1-based)", "group": "qbt", "category": "qBittorrent", "resolver": _position()},
    "qbt_count":  {"label": "Торрент: сколько активно", "group": "scalar", "category": "qBittorrent", "resolver": _count("qbt")},

    # --- qBittorrent: сводные показатели по ВСЕМ торрентам (не только активным) ---
    "qbt_total_dl": {"label": "qBittorrent: суммарная скорость закачки", "group": "scalar", "category": "qBittorrent", "resolver": _scalar("qbt_total_dl")},
    "qbt_total_ul": {"label": "qBittorrent: суммарная скорость раздачи", "group": "scalar", "category": "qBittorrent", "resolver": _scalar("qbt_total_ul")},
    "qbt_count_all": {"label": "qBittorrent: торрентов всего в клиенте", "group": "scalar", "category": "qBittorrent", "resolver": _scalar("qbt_count_all")},
    "qbt_ratio":    {"label": "qBittorrent: общий ratio",               "group": "scalar", "category": "qBittorrent", "resolver": _scalar("qbt_ratio")},
    "qbt_free_space_gb": {"label": "qBittorrent: свободно места на диске загрузок", "group": "scalar", "category": "qBittorrent", "resolver": _scalar("qbt_free_space_gb")},
}

# Порядок категорий в легенде на /screens (buildLegend() в screens_webui.py
# сортирует по этому списку, а не по алфавиту/порядку появления в словаре).
CATEGORY_ORDER = ["Система", "Диски / массив", "Сеть", "Media", "qBittorrent"]

# Группы, которые "разворачиваются" в несколько экранов (по числу элементов).
REPEATING_GROUPS = ("stream", "recent", "qbt")

# Разумный потолок на всякий случай (стримов "обычно" не больше 4-5, но защитимся от абсурдных значений)
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
    """Для веб-интерфейса: список переменных с категорией (для группировки на
    /screens) и признаком repeating (переменная из группы stream/recent/qbt -
    разворачивает экран в N копий, в легенде помечается бейджем ⟲)."""
    return [
        {
            "name": name,
            "group": spec["group"],
            "category": spec.get("category", "Прочее"),
            "repeating": spec["group"] in REPEATING_GROUPS,
            "label": spec["label"],
        }
        for name, spec in VARIABLES.items()
    ]
