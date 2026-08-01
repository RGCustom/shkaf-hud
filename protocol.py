"""
protocol.py

Собирает serial-строку для Arduino из МИНИМАЛЬНОГО набора переменных:

    BAR1, BAR2, BAR3, BAR4  - вся информация по одному бару в одной строке:
                               "pct,c1,c2,c3,solid" (проценты + 3 цвета градиента
                               + флаг "цвет на 100%"), через запятую
    BRI                     - яркость 0-100
    L1, L2, L3              - три строки OLED (уже отрендеренные шаблонизатором)

8 переменных, при этом каждая может быть длинной - вместо прежних ~20 мелких
полей (C01..C33, G0-3, BAR0-3 отдельно и т.п.). Arduino ничего не считает и не
форматирует - просто раскладывает то, что пришло, по своим четырём барам и
трём строкам экрана.

ГЛАВНОЕ: строка на Arduino шлётся только если что-то реально изменилось.
Если между тиками ничего не поменялось (обычное дело - OLED сменит экран раз
в несколько секунд, а не каждую секунду) - ничего не шлём вообще, экономим
канал и разгружаем Arduino. Раз в FULL_RESYNC_SECONDS всё равно шлём полное
состояние целиком - на случай, если Arduino перезагрузилась/потеряла кусок
данных, и рискует зависнуть в устаревшем состоянии молча.
"""

import time


def pack_bar(pct, c1, c2, c3, solid):
    """pct: 0-100 int, c1/c2/c3: hex без '#', solid: bool -> 'pct,c1,c2,c3,0|1'"""
    return f"{int(round(pct))},{c1},{c2},{c3},{1 if solid else 0}"


class ProtocolState:
    def __init__(self, full_resync_seconds=30):
        self.last = {}
        self.last_full_sync = 0.0
        self.full_resync_seconds = full_resync_seconds

    def build(self, values: dict, now=None):
        """
        values: {"BAR1": "...", "BAR2": "...", "BAR3": "...", "BAR4": "...",
                 "BRI": "15", "L1": "...", "L2": "...", "L3": "..."}
        Возвращает готовую строку для serial (без \n) или None, если слать нечего.
        """
        now = now if now is not None else time.time()
        force_full = (now - self.last_full_sync) >= self.full_resync_seconds

        changed = {}
        for k, v in values.items():
            if force_full or self.last.get(k) != v:
                changed[k] = v

        self.last.update(values)
        if force_full:
            self.last_full_sync = now

        if not changed:
            return None

        return "|".join(f"{k}:{v}" for k, v in changed.items())

    def reset(self):
        """Форсировать полную пересылку на следующем build() - например, сразу
        после переподключения Arduino, чтобы она точно получила актуальное состояние,
        а не молча ждала следующего реального изменения."""
        self.last = {}
        self.last_full_sync = 0.0
