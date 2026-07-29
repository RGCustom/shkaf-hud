/*
  promicro_stats_display.ino  (под контейнер shkaf-hud)

  Pro Micro читает по USB-serial строки вида:

    BAR0:57|BAR1:42|BAR2:78|BAR3:61|C01:..|C02:..|C03:..|C11:..|C12:..|C13:..|C21:..|C22:..|C23:..|C31:..|C32:..|C33:..|BRI:60|G0:0|G1:1|G2:0|G3:0|SCREEN:LIB|MOVIES:1243|SERIES:87|TOTC:941|FREEC:310|ARRPCT:67
  или
    BAR0:57|BAR1:42|BAR2:78|BAR3:61|C01:..|C02:..|C03:..|C11:..|C12:..|C13:..|C21:..|C22:..|C23:..|C31:..|C32:..|C33:..|BRI:60|G0:0|G1:1|G2:0|G3:0|SCREEN:STREAM|IDX:1|CNT:2|TITLE:Dune Part Two|USER:konst|PROG:45

  Рисует:
    - WS2812: 4 бара по LEDS_PER_BAR диодов, каждый - 3-стопный градиент (c1 0% -> c2 50% -> c3 100%),
      настраивается через веб-интерфейс контейнера. G0-G3 - если 1, при pct>=100 вся полоска
      заливается c3 (цветом "на 100%") вместо градиента.
    - OLED (SSD1306, крупный шрифт): либо экран библиотеки (LIB), либо текущий поток (STREAM)

  Вся логика "что сейчас показывать" (LIB или который STREAM) решается на хосте -
  скетч просто рендерит то, что прислали, без своей логики ротации.

  Библиотеки (Library Manager): FastLED, Adafruit GFX Library, Adafruit SSD1306

  === КАЛИБРОВКА ЛЕНТЫ (обязательно перед первым использованием) ===
  У тебя 2 физически отдельные матрицы 4x4 в цепочке - порядок/ориентация
  подключения FastLED не знает сама, это нужно определить руками:

    1. Пришли по Serial монитору строку "CAL" (без кавычек) + Enter.
    2. Скетч по очереди зажжёт белым диоды с сырыми индексами 0..31,
       с паузой ~700мс, и напечатает номер каждого в Serial Monitor.
    3. Запиши, на какой физической позиции (какой бар слева направо,
       какая высота снизу вверх) загорается каждый индекс.
    4. Заполни массив LED_MAP ниже: LED_MAP[логическая_позиция] = сырой_индекс,
       где логическая_позиция = barIndex*LEDS_PER_BAR + levelFromBottom
       (0 = самый нижний диод бара).
*/

#include <FastLED.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// ---------------- КОНФИГ ----------------

#define LED_PIN        6
#define LEDS_PER_BAR   8
#define NUM_BARS       4                              // 0=CPU 1=RAM 2=NET 3=DISK
#define NUM_LEDS       (LEDS_PER_BAR * NUM_BARS)       // 32
#define BRIGHTNESS     60

// barColors[bar][0..2] = c1(0%), c2(50%), c3(100%) - дефолты зелёный->жёлтый->красный,
// тут же обновляются из C{bar}{1..3} по serial
CRGB barColors[NUM_BARS][3] = {
  { CRGB(0x00, 0xFF, 0x42), CRGB(0xFF, 0xF6, 0x00), CRGB(0xFF, 0x00, 0x00) },
  { CRGB(0x00, 0xFF, 0x42), CRGB(0xFF, 0xF6, 0x00), CRGB(0xFF, 0x00, 0x00) },
  { CRGB(0x00, 0xFF, 0x42), CRGB(0xFF, 0xF6, 0x00), CRGB(0xFF, 0x00, 0x00) },
  { CRGB(0x00, 0xFF, 0x42), CRGB(0xFF, 0xF6, 0x00), CRGB(0xFF, 0x00, 0x00) },
};

// LED_MAP[логическая позиция] = сырой индекс в цепочке FastLED.
// ПО УМОЛЧАНИЮ "как есть по порядку" - почти наверняка НЕ совпадает с реальной
// раскладкой двух матриц. Прогони калибровку (см. выше) и поправь.
int LED_MAP[NUM_LEDS] = {
   0,  1,  2,  3, 16, 17, 18, 19,     // бар 0 (низ -> верх)
   7,  6,  5,  4, 23, 22, 21, 20,     // бар 1
   8,  9, 10, 11, 24, 25, 26, 27,     // бар 2
  15, 14, 13, 12, 31, 30, 29, 28      // бар 3
};

#define OLED_WIDTH   128
#define OLED_HEIGHT  64
#define OLED_ADDR    0x3C

// -------------------------------------------------------------

CRGB leds[NUM_LEDS];
Adafruit_SSD1306 display(OLED_WIDTH, OLED_HEIGHT, &Wire, -1);

int barPct[NUM_BARS] = { 0, 0, 0, 0 };
int brightness = BRIGHTNESS;
bool solid100[NUM_BARS] = { false, false, false, false };  // true = вся полоска заливается c3, когда pct>=100

String screenType = "LIB";           // "LIB" или "STREAM"
int movies = 0, series = 0, totC = 0, freeC = 0, arrPct = 0;
int streamIdx = 0, streamCnt = 0, streamProg = 0;
String streamTitle = "", streamUser = "";

unsigned long lastScrollMs = 0;
int scrollOffset = 0;
const unsigned long SCROLL_INTERVAL_MS = 300;
const int TITLE_VISIBLE_CHARS = 20;  // при setTextSize(1) влезает ~21 символ

String inputBuffer = "";

void setup() {
  Serial.begin(115200);

  FastLED.addLeds<WS2812B, LED_PIN, GRB>(leds, NUM_LEDS);
  FastLED.setBrightness(BRIGHTNESS);
  fill_solid(leds, NUM_LEDS, CRGB::Black);
  FastLED.show();

  display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR);  // если не найдётся - просто останется пустым
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
      } else {
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

// --- парсинг "KEY:val|KEY:val|..." ---

CRGB hexToColor(const String &hex) {
  if (hex.length() < 6) return CRGB::Black;
  long val = strtol(hex.c_str(), NULL, 16);
  return CRGB((val >> 16) & 0xFF, (val >> 8) & 0xFF, val & 0xFF);
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

      if (key == "BAR0") barPct[0] = val.toInt();
      else if (key == "BAR1") barPct[1] = val.toInt();
      else if (key == "BAR2") barPct[2] = val.toInt();
      else if (key == "BAR3") barPct[3] = val.toInt();
      else if (key == "BRI") { brightness = map(val.toInt(), 0, 100, 0, 255); FastLED.setBrightness(brightness); }
      else if (key == "G0") solid100[0] = (val.toInt() == 1);
      else if (key == "G1") solid100[1] = (val.toInt() == 1);
      else if (key == "G2") solid100[2] = (val.toInt() == 1);
      else if (key == "G3") solid100[3] = (val.toInt() == 1);
      else if (key == "C01") barColors[0][0] = hexToColor(val);
      else if (key == "C02") barColors[0][1] = hexToColor(val);
      else if (key == "C03") barColors[0][2] = hexToColor(val);
      else if (key == "C11") barColors[1][0] = hexToColor(val);
      else if (key == "C12") barColors[1][1] = hexToColor(val);
      else if (key == "C13") barColors[1][2] = hexToColor(val);
      else if (key == "C21") barColors[2][0] = hexToColor(val);
      else if (key == "C22") barColors[2][1] = hexToColor(val);
      else if (key == "C23") barColors[2][2] = hexToColor(val);
      else if (key == "C31") barColors[3][0] = hexToColor(val);
      else if (key == "C32") barColors[3][1] = hexToColor(val);
      else if (key == "C33") barColors[3][2] = hexToColor(val);
      else if (key == "SCREEN") screenType = val;
      else if (key == "MOVIES") movies = val.toInt();
      else if (key == "SERIES") series = val.toInt();
      else if (key == "TOTC") totC = val.toInt();
      else if (key == "FREEC") freeC = val.toInt();
      else if (key == "ARRPCT") arrPct = val.toInt();
      else if (key == "IDX") streamIdx = val.toInt();
      else if (key == "CNT") streamCnt = val.toInt();
      else if (key == "TITLE") streamTitle = val;
      else if (key == "USER") streamUser = val;
      else if (key == "PROG") streamProg = val.toInt();
    }
    start = sep + 1;
  }
}

// ---------------- LED бары ----------------

// градиент по позиции в баре: 3 пользовательских стопа c1(0%)->c2(50%)->c3(100%),
// интерполяция через FastLED blend() - между соседними стопами (не через HSV, тут яркость
// не проседает сама по себе, если стопы выбраны разумно, зато можно задать любые цвета)
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
    } else if (solid100[barIndex] && pct >= 100) {
      leds[rawIndex] = barColors[barIndex][2];  // c3 - цвет "на 100%"
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

// ---------------- OLED ----------------

// --- бегущая строка: если текст длиннее видимого окна, крутит его по кругу ---
String scrollWindow(const String &text) {
  static String lastText = "";
  if (text != lastText) {
    scrollOffset = 0;
    lastText = text;
  }

  if ((int)text.length() <= TITLE_VISIBLE_CHARS) return text;

  if (millis() - lastScrollMs > SCROLL_INTERVAL_MS) {
    scrollOffset = (scrollOffset + 1) % (text.length() + 4);  // +4 = пауза-разрыв между кругами
    lastScrollMs = millis();
  }

  String loop = text + F("    ") + text;  // пробел-разрыв между повторами
  return loop.substring(scrollOffset, scrollOffset + TITLE_VISIBLE_CHARS);
}

void drawOled() {
  display.clearDisplay();
  display.setTextSize(2);
  display.setCursor(0, 0);

  if (screenType == "STREAM" && streamCnt > 0) {
    display.print(F("Stream "));
    display.print(streamIdx);
    display.print('/');
    display.println(streamCnt);

    String t = scrollWindow(streamTitle);
    display.setTextSize(1);
    display.setCursor(0, 24);
    display.println(t);
    display.setTextSize(2);

    display.setCursor(0, 44);
    String u = streamUser;
    if (u.length() > 6) u = u.substring(0, 6);
    display.print(u);
    display.print(' ');
    display.print(streamProg);
    display.print('%');

    int barW = map(streamProg, 0, 100, 0, OLED_WIDTH - 1);
    display.drawRect(0, 60, OLED_WIDTH - 1, 4, SSD1306_WHITE);
    display.fillRect(1, 61, barW, 2, SSD1306_WHITE);
  } else {
    display.print(F("Movies"));
    display.setCursor(84, 0);
    display.println(movies);

    display.setCursor(0, 22);
    display.print(F("Series"));
    display.setCursor(84, 22);
    display.println(series);

    display.setCursor(0, 44);
    display.setTextSize(1);
    display.print(totC / 100.0, 2);
    display.print(F("TB ("));
    display.print(freeC / 100.0, 2);
    display.print(F(" free)"));

    int barW = map(arrPct, 0, 100, 0, OLED_WIDTH - 1);
    display.drawRect(0, 56, OLED_WIDTH - 1, 6, SSD1306_WHITE);
    display.fillRect(1, 57, barW, 4, SSD1306_WHITE);
  }

  display.display();
}
