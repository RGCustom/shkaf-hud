/*
promicro_stats_display.ino  (контейнер: shkaf-hud)

Русифицированный вариант под OLED SSD1306 128x64.
Используется U8g2 в постраничном режиме, чтобы сэкономить RAM.
Adafruit_GFX / Adafruit_SSD1306 больше не нужны.

Размер шрифта задаётся в начале:
OLED_FONT_SIZE = 1..4

1 = u8g2_font_6x13_t_cyrillic  - мелкий, максимум текста
2 = u8g2_font_8x13_t_cyrillic  - средний
3 = u8g2_font_9x15_t_cyrillic  - крупный
4 = u8g2_font_10x20_t_cyrillic - максимально крупный для 3 строк

Протокол прежний, pipe-delimited, только ИЗМЕНИВШИЕСЯ поля:

BAR1:<48 hex-символов>   - 8 пикселей x RRGGBB, ПОДРЯД, без разделителей.
BAR2:<48 hex-символов>     Цвет каждого светодиода уже посчитан на сервере
BAR3:<48 hex-символов>     (градиент/solid-режим/яркость - вся эта логика
BAR4:<48 hex-символов>     ушла с платы в Python). Плата просто раскладывает
                           готовые 8 цветов по своим физическим диодам бара
                           через LED_MAP - никакого blend()/интерполяции тут
                           больше нет.
BRI:<0-100>
L1:<текст UTF-8>
L2:<текст UTF-8>
L3:<текст UTF-8>

Пример (BAR1 - 8 пикселей, первые 4 зелёные, дальше жёлтый/оранжевый, потом
2 потушенных):
BAR1:00FF0000FF0040FF0080FF00FFFF00FF8000000000000000|L1:Шкаф HUD

Библиотеки: FastLED, U8g2.
*/

#include <FastLED.h>
#include <Wire.h>
#include <U8g2lib.h>
#include <avr/pgmspace.h>
#include <string.h>

// ---------------- РАЗМЕР ШРИФТА OLED ----------------
// Меняй значение:
// 1 = 6x13, мелкий
// 2 = 8x13, средний
// 3 = 9x15, крупный
// 4 = 10x20, очень крупный
#define OLED_FONT_SIZE 4

#if OLED_FONT_SIZE == 1
  #define OLED_FONT u8g2_font_6x13_t_cyrillic
#elif OLED_FONT_SIZE == 2
  #define OLED_FONT u8g2_font_8x13_t_cyrillic
#elif OLED_FONT_SIZE == 3
  #define OLED_FONT u8g2_font_9x15_t_cyrillic
#elif OLED_FONT_SIZE == 4
  #define OLED_FONT u8g2_font_10x20_t_cyrillic
#else
  #define OLED_FONT u8g2_font_9x15_t_cyrillic
#endif

// Если компилятор ругается на имя шрифта, попробуй заменить, например:
// u8g2_font_9x15_t_cyrillic
// на:
// u8g2_font_9x15_cyrillic
// или обнови библиотеку U8g2.

// ---------------- КОНФИГ ----------------
#define LED_PIN        6
#define LEDS_PER_BAR   8
#define NUM_BARS       4
#define NUM_LEDS       (LEDS_PER_BAR * NUM_BARS)   // 32
#define DEFAULT_BRIGHTNESS 15

#define OLED_WIDTH   128
#define OLED_HEIGHT  64
#define OLED_ADDR    0x3C

// Буферы OLED.
// OLED_LINE_BUF ограничивает максимальную длину хранимой строки.
// 64 байта = до ~31 русской буквы.
#define OLED_LINE_BUF 64
#define OLED_TEMP_BUF 64

// Входная команда по Serial.
// Новый формат BAR (48 hex-символов на бар вместо "pct,c1,c2,c3,solid")
// заметно длиннее старого, поэтому буфер увеличен со 208 до 320 - чтобы
// полный ресинк (все 8 полей: 4хBAR+BRI+3хL) гарантированно влезал даже
// с не самыми короткими текстами на OLED.
#define SERIAL_BUF_SIZE 320

// Чтобы не долбить I2C каждый цикл loop().
#define OLED_REFRESH_MS 50

const unsigned long SCROLL_INTERVAL_MS = 300;

// Постраничный режим U8g2: экономит RAM.
U8G2_SSD1306_128X64_NONAME_1_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE);

// Если этот вариант не стартует, попробуй:
// U8G2_SSD1306_128X64_ALT0_1_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE);

CRGB leds[NUM_LEDS];

// Откалиброванный порядок физических диодов.
// Храним в PROGMEM, чтобы не занимать RAM.
const uint8_t LED_MAP[NUM_LEDS] PROGMEM = {
  0,  1,  2,  3, 16, 17, 18, 19,   // бар 1 (низ -> верх)
  7,  6,  5,  4, 23, 22, 21, 20,   // бар 2
  8,  9, 10, 11, 24, 25, 26, 27,   // бар 3
 15, 14, 13, 12, 31, 30, 29, 28    // бар 4
};

// Готовый цвет каждого светодиода каждого бара - уже посчитан на сервере
// (см. ledbar.py). Плата больше не знает ни про pct, ни про градиент, ни
// про solid-режим - только про 32 конкретных цвета. Глобальные массивы
// зануляются сами при старте (черный = потушено), этого достаточно как
// безопасного значения по умолчанию до прихода первых данных.
CRGB barPixels[NUM_BARS][LEDS_PER_BAR];

uint8_t brightness = DEFAULT_BRIGHTNESS;

// OLED-строки в фиксированных буферах, без String.
char lineText[3][OLED_LINE_BUF];
uint8_t lineChars[3] = { 0, 0, 0 };
uint16_t linePx[3] = { 0, 0, 0 };
uint8_t lineScrollOffset[3] = { 0, 0, 0 };
unsigned long lastScrollMs[3] = { 0, 0, 0 };
uint8_t scrollVisibleChars = 21;

// Временный буфер для скролла.
char oledTemp[OLED_TEMP_BUF];

// Serial-буфер команды.
char serialBuf[SERIAL_BUF_SIZE];
uint16_t serialLen = 0;
bool serialOverflow = false;

// Позиции строк OLED будут рассчитаны под выбранный шрифт.
uint8_t OLED_Y[3];

// ---------------- UTF-8 helpers ----------------

uint8_t utf8StepAt(const char *s, uint8_t i) {
  uint8_t b = (uint8_t)s[i];
  if (b == 0) return 0;

  uint8_t step = 1;

  if (b < 0x80) {
    step = 1;
  } else if ((b & 0xE0) == 0xC0) {
    step = 2;
  } else if ((b & 0xF0) == 0xE0) {
    step = 3;
  } else if ((b & 0xF8) == 0xF0) {
    step = 4;
  }

  // защита от обрезанного UTF-8
  for (uint8_t k = 1; k < step; k++) {
    if (s[i + k] == 0) {
      step = k;
      break;
    }
  }

  return step;
}

uint8_t utf8Len(const char *s) {
  uint8_t count = 0;
  uint8_t i = 0;

  while (s[i]) {
    uint8_t step = utf8StepAt(s, i);
    if (step == 0) break;
    i += step;
    count++;
  }

  return count;
}

const char* utf8CharPtr(const char *s, uint8_t charIndex) {
  uint8_t i = 0;
  uint8_t ch = 0;

  while (s[i] && ch < charIndex) {
    uint8_t step = utf8StepAt(s, i);
    if (step == 0) break;
    i += step;
    ch++;
  }

  return s + i;
}

// ---------------- парсеры без String ----------------

int parseDec(char **pp) {
  char *p = *pp;
  int v = 0;

  while (*p >= '0' && *p <= '9') {
    v = v * 10 + (*p - '0');
    p++;
  }

  *pp = p;
  return v;
}

bool isHexDigit(char c) {
  return (c >= '0' && c <= '9') ||
         (c >= 'A' && c <= 'F') ||
         (c >= 'a' && c <= 'f');
}

uint8_t hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return 0;
}

CRGB parseHex6(char **pp) {
  char *p = *pp;

  // на случай, если сервер вдруг пришлёт с решёткой
  if (*p == '#') p++;

  uint32_t v = 0;

  for (uint8_t i = 0; i < 6 && isHexDigit(*p); i++) {
    v = (v << 4) | hexNibble(*p);
    p++;
  }

  *pp = p;
  return CRGB((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF);
}

// val - 48 hex-символов подряд (8 пикселей x RRGGBB), без разделителей.
// parseHex6 сам продвигает указатель ровно на 6 hex-цифр за вызов, так что
// достаточно просто дёрнуть его 8 раз. Если данные пришли обрезанными -
// parseHex6 на "пустом хвосте" просто вернёт чёрный (0,0,0), это безопасно.
void parseBar(uint8_t idx, char *val) {
  char *p = val;

  for (uint8_t level = 0; level < LEDS_PER_BAR; level++) {
    barPixels[idx][level] = parseHex6(&p);
  }
}

void setLine(uint8_t idx, const char *val) {
  if (idx > 2) return;

  uint8_t i = 0;
  while (i < OLED_LINE_BUF - 1 && val[i] != 0) {
    lineText[idx][i] = val[i];
    i++;
  }
  lineText[idx][i] = 0;

  lineChars[idx] = utf8Len(lineText[idx]);
  linePx[idx] = u8g2.getUTF8Width(lineText[idx]);
  lineScrollOffset[idx] = 0;
  lastScrollMs[idx] = millis();
}

void parseLine(char *line) {
  char *start = line;

  while (*start) {
    char *sep = strchr(start, '|');
    if (sep) *sep = 0;

    char *colon = strchr(start, ':');
    if (colon) {
      *colon = 0;
      char *key = start;
      char *val = colon + 1;

      if (strcmp(key, "BAR1") == 0) parseBar(0, val);
      else if (strcmp(key, "BAR2") == 0) parseBar(1, val);
      else if (strcmp(key, "BAR3") == 0) parseBar(2, val);
      else if (strcmp(key, "BAR4") == 0) parseBar(3, val);
      else if (strcmp(key, "BRI") == 0) {
        char *p = val;
        int v = parseDec(&p);
        if (v < 0) v = 0;
        if (v > 100) v = 100;
        brightness = (uint8_t)v;
        FastLED.setBrightness(map(brightness, 0, 100, 0, 255));
      }
      else if (strcmp(key, "L1") == 0) setLine(0, val);
      else if (strcmp(key, "L2") == 0) setLine(1, val);
      else if (strcmp(key, "L3") == 0) setLine(2, val);
    }

    if (!sep) break;
    start = sep + 1;
  }
}

// ---------------- LED бары ----------------

void drawOneBar(uint8_t barIndex) {
  for (uint8_t level = 0; level < LEDS_PER_BAR; level++) {
    uint8_t logicalPos = barIndex * LEDS_PER_BAR + level;
    uint8_t rawIndex = pgm_read_byte(&LED_MAP[logicalPos]);
    leds[rawIndex] = barPixels[barIndex][level];
  }
}

void drawBars() {
  drawOneBar(0);
  drawOneBar(1);
  drawOneBar(2);
  drawOneBar(3);
  FastLED.show();
}

// ---------------- калибровка ----------------

void runCalibration() {
  Serial.println(F("=== CALIBRATION MODE ==="));

  fill_solid(leds, NUM_LEDS, CRGB::Black);
  FastLED.show();

  for (int i = 0; i < NUM_LEDS; i++) {
    fill_solid(leds, NUM_LEDS, CRGB::Black);
    leds[i] = CRGB::White;
    FastLED.show();

    Serial.print(F("raw index: "));
    Serial.println(i);

    delay(700);
  }

  fill_solid(leds, NUM_LEDS, CRGB::Black);
  FastLED.show();

  Serial.println(F("=== CALIBRATION DONE ==="));
}

// ---------------- OLED ----------------

void calculateOledLayout() {
  int ascent = u8g2.getFontAscent();
  int descent = u8g2.getFontDescent();
  int absDescent = (descent < 0) ? -descent : descent;

  int fontH = ascent + absDescent;
  if (fontH < 1) fontH = 1;

  int lineSpacing = OLED_HEIGHT / 3;

  if (fontH > lineSpacing) {
    lineSpacing = fontH;
  }

  int top = (lineSpacing - fontH) / 2;
  if (top < 0) top = 0;

  int y0 = top + ascent;
  int y1 = y0 + lineSpacing;
  int y2 = y1 + lineSpacing;

  int bottom = y2 + absDescent;
  if (bottom > OLED_HEIGHT) {
    int shift = bottom - OLED_HEIGHT;
    y0 -= shift;
    y1 -= shift;
    y2 -= shift;
  }

  if (y0 < 0) {
    y1 -= y0;
    y2 -= y0;
    y0 = 0;
  }

  if (y0 > OLED_HEIGHT - 1) y0 = OLED_HEIGHT - 1;
  if (y1 > OLED_HEIGHT - 1) y1 = OLED_HEIGHT - 1;
  if (y2 > OLED_HEIGHT - 1) y2 = OLED_HEIGHT - 1;

  OLED_Y[0] = (uint8_t)y0;
  OLED_Y[1] = (uint8_t)y1;
  OLED_Y[2] = (uint8_t)y2;
}

void updateScroll(uint8_t idx) {
  if (lineChars[idx] == 0 || linePx[idx] <= OLED_WIDTH) return;

  if (millis() - lastScrollMs[idx] >= SCROLL_INTERVAL_MS) {
    uint8_t period = lineChars[idx] + 4;
    lineScrollOffset[idx] = (lineScrollOffset[idx] + 1) % period;
    lastScrollMs[idx] = millis();
  }
}

void copyScrolled(
  const char *text,
  uint8_t chars,
  uint8_t offset,
  uint8_t visibleChars,
  char *out,
  uint8_t outSize
) {
  uint8_t pos = 0;

  if (chars == 0) {
    out[0] = 0;
    return;
  }

  uint8_t period = chars + 4;

  for (uint8_t k = 0; k < visibleChars && pos < outSize - 1; k++) {
    uint8_t p = (offset + k) % period;

    if (p < chars) {
      const char *cp = utf8CharPtr(text, p);
      uint8_t len = utf8StepAt(cp, 0);

      for (uint8_t b = 0; b < len && cp[b] && pos < outSize - 1; b++) {
        out[pos++] = cp[b];
      }
    } else {
      out[pos++] = ' ';
    }
  }

  out[pos] = 0;
}

void drawOled() {
  for (uint8_t i = 0; i < 3; i++) {
    updateScroll(i);
  }

  u8g2.firstPage();
  do {
    for (uint8_t i = 0; i < 3; i++) {
      if (lineChars[i] == 0 || linePx[i] <= OLED_WIDTH) {
        u8g2.drawUTF8(0, OLED_Y[i], lineText[i]);
      } else {
        copyScrolled(
          lineText[i],
          lineChars[i],
          lineScrollOffset[i],
          scrollVisibleChars,
          oledTemp,
          (uint8_t)sizeof(oledTemp)
        );
        u8g2.drawUTF8(0, OLED_Y[i], oledTemp);
      }
    }
  } while (u8g2.nextPage());
}

// ---------------- setup / loop ----------------

void setup() {
  Serial.begin(115200);

  FastLED.addLeds<WS2812B, LED_PIN, GRB>(leds, NUM_LEDS);
  FastLED.setBrightness(map(brightness, 0, 100, 0, 255));
  fill_solid(leds, NUM_LEDS, CRGB::Black);
  FastLED.show();

  u8g2.begin();

  // Если твой OLED имеет нестандартный адрес, можно попробовать:
  // u8g2.setI2CAddress(OLED_ADDR << 1);

  u8g2.setDrawColor(1);
  u8g2.setFontMode(1);
  u8g2.setFont(OLED_FONT);

  calculateOledLayout();

  uint8_t maxW = u8g2.getMaxCharWidth();
  if (maxW > 0) {
    scrollVisibleChars = OLED_WIDTH / maxW;
  }

  if (scrollVisibleChars < 1) {
    scrollVisibleChars = 21;
  }

  drawOled();
}

void loop() {
  while (Serial.available()) {
    int c = Serial.read();

    if (c == '\n') {
      if (!serialOverflow) {
        // обрезаем пробелы/табы в конце команды
        while (serialLen > 0 && (serialBuf[serialLen - 1] == ' ' || serialBuf[serialLen - 1] == '\t')) {
          serialLen--;
        }

        serialBuf[serialLen] = 0;

        if (strcmp(serialBuf, "CAL") == 0) {
          runCalibration();
        } else if (serialLen > 0) {
          parseLine(serialBuf);
        }
      }

      serialLen = 0;
      serialOverflow = false;
    } else if (c != '\r') {
      if (serialLen < SERIAL_BUF_SIZE - 1) {
        serialBuf[serialLen++] = (char)c;
      } else {
        serialOverflow = true;
      }
    }
  }

  drawBars();

  static unsigned long lastOledMs = 0;
  if (millis() - lastOledMs >= OLED_REFRESH_MS) {
    drawOled();
    lastOledMs = millis();
  }
}