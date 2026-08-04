"""
ledbar.py

Расчёт цвета КАЖДОГО светодиода LED-бара на сервере. Раньше градиент
интерполировался на Arduino (blend() между 2 цветами на лету, каждый
кадр); теперь плата ничего не считает - только раскладывает уже готовый
массив из LEDS_PER_BAR цветов по своим физическим диодам (см. LED_MAP
в .ino). Линейка всегда фиксированной длины, так что дешевле посчитать
один раз на сервере и переслать как бинарный (hex) массив, чем гонять
формулы блендинга на 8-битном контроллере.

ВАЖНО: LEDS_PER_BAR здесь должен совпадать с #define LEDS_PER_BAR в обеих
прошивках (promicro_stats_display.ino и promicro_stats_display_rus.ino).

Логика (см. также solid-чекбокс "цвет на 100%" в веб-интерфейсе):

  - solid = False (галка снята): обычный 3-стопный градиент по всей высоте
    бара, всегда - c1 (низ, 0%) -> c2 (середина, 50%) -> c3 (верх, 100%),
    независимо от текущего pct.
  - solid = True (галка стоит):
      - pct < 100:  градиент только между c1 и c2 (2 стопа) по всей высоте
        бара - c3 в этом случае не используется вообще.
      - pct >= 100: весь заполненный бар (то есть все LEDS_PER_BAR диодов,
        раз pct=100) заливается сплошным c3.
  - Уровни выше "заполненности" (level >= lit) - всегда чёрные (погашены).
"""

LEDS_PER_BAR = 8  # должно совпадать с #define LEDS_PER_BAR в .ino


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


def compute_bar_pixels(pct, c1_hex, c2_hex, c3_hex, solid, leds_per_bar=LEDS_PER_BAR):
    """
    Возвращает список из leds_per_bar строк 'RRGGBB' (без '#', верхний регистр) -
    ровно то, что дальше пойдёт в протокол одним склеенным полем.
    """
    pct = max(0.0, min(100.0, pct))
    lit = round(pct / 100.0 * leds_per_bar)
    lit = max(0, min(leds_per_bar, lit))

    c1, c2, c3 = _hex_to_rgb(c1_hex), _hex_to_rgb(c2_hex), _hex_to_rgb(c3_hex)

    pixels = []
    for level in range(leds_per_bar):
        if level >= lit:
            pixels.append("000000")
            continue

        if solid and pct >= 100:
            pixels.append(_rgb_to_hex(c3))
            continue

        frac = level / (leds_per_bar - 1) if leds_per_bar > 1 else 0.0

        if solid:
            # галка стоит, но ещё не 100% - только c1 -> c2 по всей высоте
            rgb = _blend(c1, c2, frac)
        else:
            # обычный 3-стопный градиент: c1 -> c2 -> c3
            if frac <= 0.5:
                rgb = _blend(c1, c2, frac / 0.5)
            else:
                rgb = _blend(c2, c3, (frac - 0.5) / 0.5)

        pixels.append(_rgb_to_hex(rgb))

    return pixels
