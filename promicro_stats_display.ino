/*
  promicro_stats_display.ino  (контейнер: shkaf-hud)

  Минимальная прошивка - вся логика (метрики, форматирование, скроллинг-решения,
  выбор экрана) на сервере. Плата умеет только две вещи:
    1. Рисовать 4 LED-бара по присланным цветам/проценту/флагу "цвет на 100%"
    2. Печатать 3 строки текста на OLED, скроллить через getTextBounds ту,
       которая реально не помещается по ширине - остальное не трогает

  ПРИМЕЧАНИЕ: кириллица пока не поддерживается (шрифт Adafruit_GFX только
  латиница) - сервер шлёт транслит. Переход на u8g2 с кириллическим шрифтом
  отложен на потом, чтобы сейчас не рисковать рабочей сборкой.

  Протокол (pipe-delimited, только ИЗМЕНИВШИЕСЯ поля, не все 8 сразу):

    BAR1:<pct>,<c1>,<c2>,<c3>,<0|1>   - % заполнения, 3 цвета градиента (hex
                                         без #), флаг "цвет c3 сплошным на 100%"
    BAR2, BAR3, BAR4                  - аналогично для остальных баров
    BRI:<0-100>                       - яркость ленты
    L1:<текст>, L2:<текст>, L3:<текст> - три строки OLED, уже полностью
                                         готовые к печати

  Библиотеки (Library Manager): FastLED, Adafruit GFX Library, Adafruit SSD1306

  === КАЛИБРОВКА ЛЕНТЫ ===
  Пришли "CAL" в Serial Monitor - по очереди зажжётся каждый физический
  диод белым с номером в консоли. LED_MAP ниже уже откалиброван под текущую
  сборку (2 матрицы 4x4 = 4 вертикальных бара по 8) - трогать только если
  перепаяешь ленту заново.
*/

#include <FastLED.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// ---------------- КОНФИГ ----------------

#define LED_PIN        6
#define LEDS_PER_BAR   8
#define NUM_BARS       4
#define NUM_LEDS       (LEDS_PER_BAR * NUM_BARS)   // 32
#define DEFAULT_BRIGHTNESS 15   // ~15% - безопасно для питания через USB без отдельного 5V

#define OLED_WIDTH   128
#define OLED_HEIGHT  64
#define OLED_ADDR    0x3C

// Откалиброванный порядок физических диодов в цепочке (см. блок калибровки выше)
int LED_MAP[NUM_LEDS] = {
   0,  1,  2,  3, 16, 17, 18, 19,   // бар 1 (низ -> верх)
   7,  6,  5,  4, 23, 22, 21, 20,   // бар 2
   8,  9, 10, 11, 24, 25, 26, 27,   // бар 3
  15, 14, 13, 12, 31, 30, 29, 28    // бар 4
};

// -------------------------------------------------------------

CRGB leds[NUM_LEDS];
Adafruit_SSD1306 display(OLED_WIDTH, OLED_HEIGHT, &Wire, -1);

// --- состояние баров: pct + 3 цвета градиента (0%/50%/100%) + "залить c3 на 100%" ---
int barPct[NUM_BARS] = { 0, 0, 0, 0 };
bool barSolid[NUM_BARS] = { false, false, false, false };
CRGB barColors[NUM_BARS][3] = {
  { CRGB(0x00, 0xFF, 0x42), CRGB(0xFF, 0xF6, 0x00), CRGB(0xFF, 0x00, 0x00) },
  { CRGB(0x00, 0xFF, 0x42), CRGB(0xFF, 0xF6, 0x00), CRGB(0xFF, 0x00, 0x00) },
  { CRGB(0x00, 0xFF, 0x42), CRGB(0xFF, 0xF6, 0x00), CRGB(0xFF, 0x00, 0x00) },
  { CRGB(0x00, 0xFF, 0x42), CRGB(0xFF, 0xF6, 0x00), CRGB(0xFF, 0x00, 0x00) },
};
int brightness = DEFAULT_BRIGHTNESS;

// --- 3 строки OLED + независимый скролл на каждую ---
String lineText[3] = { "", "", "" };
String lastLineText[3] = { "", "", "" };
int lineScrollOffset[3] = { 0, 0, 0 };
unsigned long lastScrollMs[3] = { 0, 0, 0 };
const unsigned long SCROLL_INTERVAL_MS = 300;
const int SCROLL_VISIBLE_CHARS = 10;  // при setTextSize(2) 128px / 12px-на-символ

String inputBuffer = "";

void setup() {
  Serial.begin(115200);

  FastLED.addLeds<WS2812B, LED_PIN, GRB>(leds, NUM_LEDS);
  FastLED.setBrightness(map(brightness, 0, 100, 0, 255));
  fill_solid(leds, NUM_LEDS, CRGB::Black);
  FastLED.show();

  display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR);
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.display();
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      inputBuffer.trim();
      if (inputBuffer == "CAL") {
        runCalibration();
      } else if (inputBuffer.length() > 0) {
        parseLine(inputBuffer);
      }
      inputBuffer = "";
    } else if (c != '\r') {
      inputBuffer += c;
    }
  }

  drawBars();
  drawOled();
}

// ---------------- парсинг протокола ----------------

CRGB hexToColor(const String &hex) {
  if (hex.length() < 6) return CRGB::Black;
  long val = strtol(hex.c_str(), NULL, 16);
  return CRGB((val >> 16) & 0xFF, (val >> 8) & 0xFF, val & 0xFF);
}

void parseBar(int barIndex, const String &val) {
  // "pct,c1,c2,c3,solid"
  int p1 = val.indexOf(',');
  int p2 = val.indexOf(',', p1 + 1);
  int p3 = val.indexOf(',', p2 + 1);
  int p4 = val.indexOf(',', p3 + 1);
  if (p1 == -1 || p2 == -1 || p3 == -1 || p4 == -1) return;

  barPct[barIndex] = val.substring(0, p1).toInt();
  barColors[barIndex][0] = hexToColor(val.substring(p1 + 1, p2));
  barColors[barIndex][1] = hexToColor(val.substring(p2 + 1, p3));
  barColors[barIndex][2] = hexToColor(val.substring(p3 + 1, p4));
  barSolid[barIndex] = val.substring(p4 + 1).toInt() == 1;
}

void parseLine(const String &line) {
  int start = 0;
  while (start < (int)line.length()) {
    int sep = line.indexOf('|', start);
    if (sep == -1) sep = line.length();
    String token = line.substring(start, sep);

    int colon = token.indexOf(':');
    if (colon != -1) {
      String key = token.substring(0, colon);
      String val = token.substring(colon + 1);

      if (key == "BAR1") parseBar(0, val);
      else if (key == "BAR2") parseBar(1, val);
      else if (key == "BAR3") parseBar(2, val);
      else if (key == "BAR4") parseBar(3, val);
      else if (key == "BRI") {
        brightness = val.toInt();
        FastLED.setBrightness(map(brightness, 0, 100, 0, 255));
      }
      else if (key == "L1") lineText[0] = val;
      else if (key == "L2") lineText[1] = val;
      else if (key == "L3") lineText[2] = val;
    }
    start = sep + 1;
  }
}

// ---------------- LED бары ----------------

// градиент по позиции в баре: 3 стопа c1(0%)->c2(50%)->c3(100%), интерполяция через blend()
CRGB gradientColorForLevel(int barIndex, int level) {
  float frac = (float)level / (LEDS_PER_BAR - 1);
  if (frac <= 0.5) {
    uint8_t amount = round((frac / 0.5) * 255);
    return blend(barColors[barIndex][0], barColors[barIndex][1], amount);
  } else {
    uint8_t amount = round(((frac - 0.5) / 0.5) * 255);
    return blend(barColors[barIndex][1], barColors[barIndex][2], amount);
  }
}

void drawOneBar(int barIndex, int pct) {
  int lit = round(pct / 100.0 * LEDS_PER_BAR);
  lit = constrain(lit, 0, LEDS_PER_BAR);

  for (int level = 0; level < LEDS_PER_BAR; level++) {
    int logicalPos = barIndex * LEDS_PER_BAR + level;
    int rawIndex = LED_MAP[logicalPos];

    if (level >= lit) {
      leds[rawIndex] = CRGB::Black;
    } else if (barSolid[barIndex] && pct >= 100) {
      leds[rawIndex] = barColors[barIndex][2];
    } else {
      leds[rawIndex] = gradientColorForLevel(barIndex, level);
    }
  }
}

void drawBars() {
  drawOneBar(0, barPct[0]);
  drawOneBar(1, barPct[1]);
  drawOneBar(2, barPct[2]);
  drawOneBar(3, barPct[3]);
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

// ---------------- OLED (3 строки, скролл через getTextBounds) ----------------

String scrollableText(int idx, const String &text) {
  if (text != lastLineText[idx]) {
    lineScrollOffset[idx] = 0;
    lastLineText[idx] = text;
    lastScrollMs[idx] = millis();
  }

  int16_t x1, y1;
  uint16_t w, h;
  display.getTextBounds(text, 0, 0, &x1, &y1, &w, &h);

  if (w <= OLED_WIDTH) {
    return text;
  }

  if (millis() - lastScrollMs[idx] > SCROLL_INTERVAL_MS) {
    lineScrollOffset[idx] = (lineScrollOffset[idx] + 1) % (text.length() + 4);
    lastScrollMs[idx] = millis();
  }

  String loopText = text + F("    ") + text;
  return loopText.substring(lineScrollOffset[idx], lineScrollOffset[idx] + SCROLL_VISIBLE_CHARS);
}

void drawOled() {
  display.clearDisplay();
  display.setTextSize(2);

  int yPos[3] = { 2, 24, 46 };
  for (int i = 0; i < 3; i++) {
    display.setCursor(0, yPos[i]);
    display.print(scrollableText(i, lineText[i]));
  }

  display.display();
}
