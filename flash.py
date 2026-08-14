"""
flash.py

Удалённая прошивка Arduino Pro Micro (Leonardo-совместимая, ATmega32u4) прямо
из контейнера, без физического доступа к плате.

Механика (стандартная для Leonardo-бутлоадера):
    1. Открыть обычный serial-порт платы (тот же SERIAL_PORT, что использует
       shkaf_stats_bridge.py) на скорости 1200 бод и сразу закрыть - это
       сигнал "войти в бутлоадер", а не реальный обмен данными.
    2. Плата на ~8 секунд перезагружается в бутлоадер и поднимает НОВЫЙ узел
       /dev/ttyACMx - часто с другим номером (нумерация не детерминирована,
       см. README/обсуждение в проекте). Поэтому ищем разницу в списке
       /dev/ttyACM* до и после touch, а не полагаемся на фиксированный путь.
    3. На найденный bootloader-порт натравливаем avrdude (протокол avr109),
       построчно отдаём его вывод наружу - для стриминга в браузер.

Требует пакет avrdude в образе (см. Dockerfile) и доступ контейнера к /dev
(bind-mount + device-cgroup-rule для major 166 - класс ttyACM, см. README).

Ничего не знает про Flask/HTTP - чистая логика, вызывается из flash_webui.py.
"""

import glob
import os
import queue
import subprocess
import threading
import time

import serial

BAUD_TOUCH = 1200
AVRDUDE_BAUD = 57600
BOOTLOADER_WAIT_TIMEOUT = 5.0
BOOTLOADER_POLL_INTERVAL = 0.2
BOOTLOADER_SETTLE_DELAY = 0.5  # дать бутлоадеру подняться, прежде чем дёргать avrdude
AVRDUDE_TIMEOUT = 60.0  # секунд - типичная заливка занимает 5-15с, с большим запасом


class FlashError(Exception):
    """Любая ошибка на этапах touch/поиск порта/avrdude - с человекочитаемым текстом."""


def list_acm_ports():
    """Все ttyACM-узлы прямо сейчас - как есть, без фильтрации по by-id
    (у bootloader-порта своего стабильного by-id обычно ещё нет)."""
    return set(glob.glob("/dev/ttyACM*"))


def touch_1200bps_reset(port):
    """Открыть-закрыть порт на 1200 бод - штатный способ попросить
    Leonardo-бутлоадер перезагрузиться в режим прошивки."""
    try:
        s = serial.Serial(port, BAUD_TOUCH)
        s.close()
    except (serial.SerialException, OSError) as e:
        raise FlashError(f"не удалось открыть {port} на 1200 бод (touch): {e}")


def wait_for_bootloader_port(before_ports, timeout=BOOTLOADER_WAIT_TIMEOUT):
    """Ждём, пока в /dev не появится новый ttyACM-узел, которого не было
    в before_ports. Возвращает путь к нему или бросает FlashError по таймауту."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        now_ports = list_acm_ports()
        new_ports = now_ports - before_ports
        if new_ports:
            # обычно новый узел ровно один - если вдруг несколько, берём первый
            return sorted(new_ports)[0]
        time.sleep(BOOTLOADER_POLL_INTERVAL)
    raise FlashError(
        "плата не вошла в режим прошивки за отведённое время "
        "(не нашли новый /dev/ttyACM* после touch на 1200 бод)"
    )


def flash(hex_path, serial_port, mcu="atmega32u4", cancel_event=None):
    """
    Генератор: делает touch + ищет bootloader-порт + запускает avrdude,
    построчно yield-ит текстовые статус-сообщения (включая живой вывод
    avrdude). Последняя строка - "OK" при успехе; при ошибке бросает
    FlashError (вызывающий код должен поймать и отдать как есть).

    cancel_event - опциональный threading.Event: если выставлен пользователем
    (кнопка "Отмена" в /flash) во время работы avrdude, процесс принудительно
    убивается тем же способом, что и при таймауте, только с другим текстом
    ошибки.
    """
    if not os.path.isfile(hex_path):
        raise FlashError(f"файл прошивки не найден: {hex_path}")
    if os.path.getsize(hex_path) == 0:
        raise FlashError("файл прошивки пустой")

    if not os.path.exists(serial_port):
        raise FlashError(
            f"плата не найдена на {serial_port} - проверь, что Pro Micro "
            f"физически подключена и путь в настройках контейнера верный "
            f"(ls -l /dev/serial/by-id/ на хосте)"
        )

    yield f"Ищу плату на {serial_port}..."
    before_ports = list_acm_ports()

    yield "Отправляю сигнал перезагрузки в бутлоадер (1200 бод touch)..."
    touch_1200bps_reset(serial_port)

    yield "Жду появления bootloader-порта..."
    bootloader_port = wait_for_bootloader_port(before_ports)
    yield f"Бутлоадер поднялся на {bootloader_port}, жду {BOOTLOADER_SETTLE_DELAY:.1f}с..."
    time.sleep(BOOTLOADER_SETTLE_DELAY)

    cmd = [
        "avrdude",
        "-c", "avr109",
        "-p", mcu,
        "-P", bootloader_port,
        "-b", str(AVRDUDE_BAUD),
        "-D",
        "-U", f"flash:w:{hex_path}:i",
    ]
    yield f"Запускаю avrdude: {' '.join(cmd)}"

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as e:
        raise FlashError(f"не удалось запустить avrdude: {e}")

    output_q = queue.Queue()

    def _pump_output(pipe, q):
        for line in iter(pipe.readline, ""):
            q.put(line)
        q.put(None)  # сигнал конца потока

    reader_thread = threading.Thread(target=_pump_output, args=(proc.stdout, output_q), daemon=True)
    reader_thread.start()

    start_time = time.time()
    timed_out = False
    cancelled = False

    while True:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        remaining = AVRDUDE_TIMEOUT - (time.time() - start_time)
        if remaining <= 0:
            timed_out = True
            break
        # ждём выхода строки максимум 0.5с зараз, чтобы не пропустить cancel_event
        # надолго даже во время долгого молчания avrdude между строками вывода
        try:
            line = output_q.get(timeout=min(remaining, 0.5))
        except queue.Empty:
            continue  # процесс просто пока молчит - это нормально, проверяем таймаут/отмену дальше
        if line is None:
            break  # avrdude закрыл stdout - процесс завершается
        yield line.rstrip("\n")

    if cancelled:
        proc.kill()
        proc.wait()
        raise FlashError("прошивка отменена пользователем")

    if timed_out:
        proc.kill()
        proc.wait()
        raise FlashError(
            f"avrdude не уложился в {AVRDUDE_TIMEOUT:.0f}с и был принудительно "
            f"остановлен - похоже, связь с платой оборвалась во время заливки"
        )

    proc.wait()
    if proc.returncode != 0:
        raise FlashError(f"avrdude завершился с кодом {proc.returncode} - см. лог выше")

    yield "OK"
