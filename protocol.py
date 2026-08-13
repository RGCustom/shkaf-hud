"""
protocol.py

Собирает serial-строку для Arduino из МИНИМАЛЬНОГО набора переменных:

    BAR1, BAR2, BAR3, BAR4  - готовый цвет КАЖДОГО светодиода бара, посчитанный
                               на сервере (см. ledbar.py): 12 пикселей x RRGGBB,
                               склеенные подряд без разделителей = 72 hex-символа.
                               Никакого pct/c1/c2/c3/solid в проводе больше нет -
                               вся эта логика на сервере уже "запечена" в цвета.
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


def pack_bar_pixels(pixels):
    """
    pixels: список из LEDS_PER_BAR (сейчас 12) строк 'RRGGBB' (без '#'),
    в порядке от первого светодиода бара до последнего - как их отдал
    ledbar.compute_bar_pixels().

    Просто склеивает их подряд без разделителей: разделители не нужны,
    т.к. на приёме (parseHex6 в .ino) каждый кусок фиксированной длины 6
    символов - парсер сам режет строку на шестёрки.

    -> '00FF0000FF0040FF0080FF00FFFF00FF8000000000000000...' (72 символа при 12 LED)
    """
    for px in pixels:
        if len(px) != 6:
            raise ValueError(f"pack_bar_pixels: пиксель должен быть 6 hex-символов (RRGGBB), получено {px!r}")
    return "".join(pixels)


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
