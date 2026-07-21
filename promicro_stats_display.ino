/*
  promicro_stats_display.ino  (под контейнер shkaf-hud)

  Pro Micro читает по USB-serial строки вида:

    CPU:57|RAM:42|NET:78|DISK:61|SCREEN:LIB|MOVIES:1243|SERIES:87|USED:134|FREE:86
  или
    CPU:57|RAM:42|NET:78|DISK:61|SCREEN:STREAM|IDX:1|CNT:2|TITLE:Dune Part Two|USER:konst|PROG:45

  Рисует:
    - WS2812: 4 бара по LEDS_PER_BAR диодов - CPU(красный) RAM(зелёный) NET(синий) DISK(жёлтый, %util)
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

CRGB BAR_COLOR[NUM_BARS] = { CRGB::Red, CRGB::Green, CRGB::Blue, CRGB::Yellow };  // дефолты, тут же обновляются из C0..C3

// LED_MAP[логическая позиция] = сырой индекс в цепочке FastLED.
// ПО УМОЛЧАНИЮ "как есть по порядку" - почти наверняка НЕ совпадает с реальной
// раскладкой двух матриц. Прогони калибровку (см. выше) и поправь.
int LED_MAP[NUM_LEDS] = {
   0,  1,  2,  3,  4,  5,  6,  7,     // CPU  (низ -> верх)
   8,  9, 10, 11, 12, 13, 14, 15,     // RAM
  16, 17, 18, 19, 20, 21, 22, 23,     // NET
  24, 25, 26, 27, 28, 29, 30, 31      // DISK
};

#define OLED_WIDTH   128
#define OLED_HEIGHT  64
#define OLED_ADDR    0x3C

// -------------------------------------------------------------

CRGB leds[NUM_LEDS];
Adafruit_SSD1306 display(OLED_WIDTH, OLED_HEIGHT, &Wire, -1);

int cpuPct = 0, ramPct = 0, netPct = 0, diskPct = 0;

String screenType = "LIB";           // "LIB" или "STREAM"
int movies = 0, series = 0, usedTB = 0, freeTB = 0;
int streamIdx = 0, streamCnt = 0, streamProg = 0;
String streamTitle = "", streamUser = "";

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

      if (key == "CPU") cpuPct = val.toInt();
      else if (key == "RAM") ramPct = val.toInt();
      else if (key == "NET") netPct = val.toInt();
      else if (key == "DISK") diskPct = val.toInt();
      else if (key == "C0") BAR_COLOR[0] = hexToColor(val);
      else if (key == "C1") BAR_COLOR[1] = hexToColor(val);
      else if (key == "C2") BAR_COLOR[2] = hexToColor(val);
      else if (key == "C3") BAR_COLOR[3] = hexToColor(val);
      else if (key == "SCREEN") screenType = val;
      else if (key == "MOVIES") movies = val.toInt();
      else if (key == "SERIES") series = val.toInt();
      else if (key == "USED") usedTB = val.toInt();
      else if (key == "FREE") freeTB = val.toInt();
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

void drawOneBar(int barIndex, int pct) {
  int lit = round(pct / 100.0 * LEDS_PER_BAR);
  lit = constrain(lit, 0, LEDS_PER_BAR);

  for (int level = 0; level < LEDS_PER_BAR; level++) {
    int logicalPos = barIndex * LEDS_PER_BAR + level;
    int rawIndex = LED_MAP[logicalPos];
    leds[rawIndex] = (level < lit) ? BAR_COLOR[barIndex] : CRGB::Black;
  }
}

void drawBars() {
  drawOneBar(0, cpuPct);
  drawOneBar(1, ramPct);
  drawOneBar(2, netPct);
  drawOneBar(3, diskPct);
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

void drawOled() {
  display.clearDisplay();
  display.setTextSize(2);
  display.setCursor(0, 0);

  if (screenType == "STREAM" && streamCnt > 0) {
    display.print(F("Stream "));
    display.print(streamIdx);
    display.print('/');
    display.println(streamCnt);

    String t = streamTitle;
    if (t.length() > 10) t = t.substring(0, 10);
    display.setCursor(0, 22);
    display.println(t);

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
    display.print(usedTB);
    display.print('/');
    display.print(freeTB);
    display.print(F("TB"));
  }

  display.display();
}
