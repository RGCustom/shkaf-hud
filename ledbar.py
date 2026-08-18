"""
ledbar.py

Расчёт цвета КАЖДОГО светодиода LED-бара на сервере. Плата ничего не считает -
только раскладывает уже готовый массив из LEDS_PER_BAR цветов по своим
физическим диодам (см. LED_MAP в .ino). Линейка всегда фиксированной длины,
так что дешевле посчитать один раз на сервере и переслать как hex-массив, чем
гонять формулы блендинга на 8-битном контроллере.

ВАЖНО: LEDS_PER_BAR здесь должен совпадать с #define LEDS_PER_BAR в прошивке
(promicro_stats_display_rus.ino). Нигде в этом модуле число диодов не
захардкожено отдельно - все функции принимают leds_per_bar параметром (по
умолчанию берут константу снизу), поэтому смена длины бара - это только
правка одной константы.

Режимы бара:

  - "classic" - обычный градиент снизу вверх, как было всегда:
    compute_bar_pixels(pct, c1, c2, c3, solid, ...)

  - "center" - бар разбит на две независимые половины, растущие НАВСТРЕЧУ
    друг другу от центра к краям (не от края к центру!). У каждой половины
    свой % заполнения, свой градиент, свой solid-флаг:
    compute_bar_pixels_center(pct_bottom, pct_top, ...)
    Разбивка по диодам: bottom_count = leds_per_bar // 2, остаток уходит
    в top_count - тоже не захардкожено, считается от leds_per_bar.

  - Peak hold - независимая "точка недавнего максимума", включается поверх
    ЛЮБОГО режима (передаётся отдельным параметром peak_pct/peak_pct_bottom/
    peak_pct_top). Само отслеживание максимума во времени (когда он был,
    когда гаснуть/затухать) живёт в классе PeakHold - вызывающий код (главный
    цикл) раз в тик скармливает туда текущий pct и получает готовое число
    0-100 для точки (или None, если точку сейчас рисовать не нужно).

Логика градиента внутри одной "полосы" диодов (solid-чекбокс "цвет на 100%"
работает так же, как раньше):

  - solid = False: обычный 3-стопный градиент по всей длине полосы, всегда
    c1 (начало) -> c2 (середина) -> c3 (конец), независимо от текущего pct.
  - solid = True:
      - pct < 100: градиент только между c1 и c2 (2 стопа) - c3 не участвует.
      - pct >= 100: вся заполненная полоса заливается сплошным c3.
  - Уровни за пределами заполненности (level >= lit) - чёрные (погашены),
    кроме случая, когда на этот уровень как раз легла точка peak hold.
"""

LEDS_PER_BAR = 12  # должно совпадать с #define LEDS_PER_BAR в .ino

# Тайминги peak hold - одни на все бары/половины (не выносим в настройки
# на каждый бар, чтобы не плодить слайдеры; при необходимости можно вынести
# в env позже).
PEAK_HOLD_SECONDS = 2.0   # style="hold": сколько точка стоит неподвижно, прежде чем погаснуть
PEAK_FADE_SECONDS = 1.5   # style="fade": за сколько точка плавно съезжает вниз к текущему pct


def _hex_to_rgb(hex_str):
    h = hex_str.strip().lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _rgb_to_hex(rgb):
    r, g, b = (max(0, min(255, round(v))) for v in rgb)
    return f"{r:02X}{g:02X}{b:02X}"


def _blend(rgb_a, rgb_b, amount):
    """amount: 0.0 (чистый rgb_a) .. 1.0 (чистый rgb_b)."""
    amount = max(0.0, min(1.0, amount))
    return tuple(a + (b - a) * amount for a, b in zip(rgb_a, rgb_b))


def _gradient_pixels(pct, c1_hex, c2_hex, c3_hex, solid, count):
    """
    Общее ядро градиента на ПОЛОСЕ произвольной длины count (для classic-режима
    count = весь бар; для center-режима count = длина одной половины).
    Индекс 0 = "начало" полосы (там, где pct=0% ничего не горит, а рост идёт
    от него), индекс count-1 = "конец" (там же, где c3 при pct=100%).

    Возвращает список из count строк 'RRGGBB' (без '#', верхний регистр).
    """
    pct = max(0.0, min(100.0, pct))
    lit = round(pct / 100.0 * count)
    lit = max(0, min(count, lit))

    c1, c2, c3 = _hex_to_rgb(c1_hex), _hex_to_rgb(c2_hex), _hex_to_rgb(c3_hex)

    pixels = []
    for level in range(count):
        if level >= lit:
            pixels.append("000000")
            continue

        if solid and pct >= 100:
            pixels.append(_rgb_to_hex(c3))
            continue

        frac = level / (count - 1) if count > 1 else 0.0

        if solid:
            # галка стоит, но ещё не 100% - только c1 -> c2 по всей длине
            rgb = _blend(c1, c2, frac)
        else:
            # обычный 3-стопный градиент: c1 -> c2 -> c3
            if frac <= 0.5:
                rgb = _blend(c1, c2, frac / 0.5)
            else:
                rgb = _blend(c2, c3, (frac - 0.5) / 0.5)

        pixels.append(_rgb_to_hex(rgb))

    return pixels


def _apply_peak(pixels, peak_pct, color_hex, count):
    """Перекрашивает ОДИН диод (тот, что соответствует уровню peak_pct) в
    color_hex - поверх уже посчитанного градиента. pixels/индексация - в той
    же системе координат, что и _gradient_pixels (индекс 0 = начало полосы).
    Не мутирует исходный список."""
    idx = round(peak_pct / 100.0 * count) - 1
    idx = max(0, min(count - 1, idx))
    out = list(pixels)
    out[idx] = color_hex.strip().lstrip("#").upper()
    return out


def compute_bar_pixels(pct, c1_hex, c2_hex, c3_hex, solid, leds_per_bar=LEDS_PER_BAR, peak_pct=None):
    """
    Classic-режим: обычный градиент снизу вверх на весь бар.
    peak_pct - опционально 0-100 (см. PeakHold.update()) - если задано,
    поверх градиента подсвечивается точка недавнего максимума цветом c3.

    Возвращает список из leds_per_bar строк 'RRGGBB'.
    """
    pixels = _gradient_pixels(pct, c1_hex, c2_hex, c3_hex, solid, leds_per_bar)
    if peak_pct is not None:
        pixels = _apply_peak(pixels, peak_pct, c3_hex, leds_per_bar)
    return pixels


def compute_bar_pixels_center(
    pct_bottom, pct_top,
    b_c1, b_c2, b_c3, b_solid,
    t_c1, t_c2, t_c3, t_solid,
    leds_per_bar=LEDS_PER_BAR,
    peak_pct_bottom=None, peak_pct_top=None,
):
    """
    Center-режим: бар разбит на две половины, каждая растёт от центра к
    своему краю независимо (своя метрика/цвета/solid/peak на каждую).

    bottom_count = leds_per_bar // 2, top_count = остаток - если leds_per_bar
    нечётный, лишний диод достаётся верхней половине. Ничего не захардкожено:
    смени leds_per_bar - разбивка пересчитается сама.

    Возвращает ПОЛНЫЙ список из leds_per_bar строк 'RRGGBB' (низ + верх,
    физический порядок - как ожидает протокол/прошивка).
    """
    bottom_count = leds_per_bar // 2
    top_count = leds_per_bar - bottom_count

    # _gradient_pixels строит "от центра" (индекс 0 = центр, растёт наружу) -
    # для нижней половины это нужно развернуть, т.к. физически индекс 0
    # в итоговом массиве - это край бара, а не центр.
    bottom_half = _gradient_pixels(pct_bottom, b_c1, b_c2, b_c3, b_solid, bottom_count)
    if peak_pct_bottom is not None:
        bottom_half = _apply_peak(bottom_half, peak_pct_bottom, b_c3, bottom_count)
    bottom_half = list(reversed(bottom_half))

    top_half = _gradient_pixels(pct_top, t_c1, t_c2, t_c3, t_solid, top_count)
    if peak_pct_top is not None:
        top_half = _apply_peak(top_half, peak_pct_top, t_c3, top_count)

    return bottom_half + top_half


class PeakHold:
    """
    Отслеживает "недавний максимум" одного канала (весь classic-бар, либо
    одна половина center-бара) во времени - для VU-meter-style точки на ленте.
    Стейт живёт в инстансе, вызывающий код (главный цикл) хранит по одному
    инстансу на канал и раз в тик зовёт update(pct, now).

    style="hold" - точка держится PEAK_HOLD_SECONDS неподвижно, потом гаснет разом.
    style="fade" - точка плавно линейно едет вниз к текущему pct за PEAK_FADE_SECONDS.
    """

    def __init__(self, style="hold"):
        self.set_style(style)
        self.peak_pct = 0.0
        self.peak_time = 0.0

    def set_style(self, style):
        self.style = style if style in ("hold", "fade") else "hold"

    def update(self, pct, now):
        """Возвращает peak_pct (0-100) для отрисовки точки, либо None, если
        отдельную точку сейчас рисовать не нужно (пик = текущему заполнению,
        либо уже погас)."""
        pct = max(0.0, min(100.0, pct))

        if pct >= self.peak_pct:
            # новый максимум (или бар только что заполнился выше старой точки) -
            # точка = сам факт заполнения, отдельно рисовать нечего
            self.peak_pct = pct
            self.peak_time = now
            return None

        elapsed = now - self.peak_time

        if self.style == "hold":
            if elapsed >= PEAK_HOLD_SECONDS:
                self.peak_pct = pct  # погасло - "упало" на текущий уровень, ждём новый пик
                return None
            return self.peak_pct

        # style == "fade"
        if elapsed >= PEAK_FADE_SECONDS:
            self.peak_pct = pct
            return None
        frac = elapsed / PEAK_FADE_SECONDS
        current = self.peak_pct - (self.peak_pct - pct) * frac
        return current if current > pct else None
