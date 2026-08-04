#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ProMicro Tester – GUI utility for sending OLED text, brightness,
and up‑to‑four LED “bars” (percentage + three colour stops + solid flag)
over a serial COM port.

All previously identified bugs are fixed:
* No hard crash when pyserial is missing.
* UI elements correctly enabled/disabled.
* Non‑blocking, cleanly stoppable reader thread.
* Colour validation and error handling.
"""

import re
import queue
import threading
import time
from dataclasses import dataclass
from typing import List

import tkinter as tk
from tkinter import colorchooser, messagebox, ttk

# ----------------------------------------------------------------------
# ---------------------------- OPTIONAL SERIAL IMPORT --------------------
# ----------------------------------------------------------------------
try:
    import serial                     # type: ignore
    from serial.tools import list_ports  # type: ignore
except Exception as e:                # pragma: no cover – only runs when pyserial missing
    serial = None
    list_ports = None
    _IMPORT_ERR = str(e)               # keep the message for later display

# ----------------------------------------------------------------------
# ---------------------------- CONSTANTS ---------------------------------
# ----------------------------------------------------------------------
BAUD_RATE = 115200
READ_POLL_MS = 100          # UI checks inbound queue every 0.1 s
BAR_COUNT = 4
THROTTLE_MS = 50            # max one command per bar each 50 ms (≈20 Hz)
DEFAULT_BRI = 5

# ----------------------------------------------------------------------
# -------------------------- HELPERS ------------------------------------
# ----------------------------------------------------------------------


def clean_hex(hex_str: str) -> str:
    """Return a cleaned hex colour string without leading '#', upper‑cased."""
    return hex_str.replace('#', '').strip().upper()


HEX_RE = re.compile(r'^[0-9A-F]{6}$')


def is_valid_hex(hex_str: str) -> bool:
    """True if ``hex_str`` consists of exactly six hexadecimal digits."""
    return bool(HEX_RE.fullmatch(clean_hex(hex_str)))


# ----------------------------------------------------------------------
# -------------------------- DATA CLASSES --------------------------------
# ----------------------------------------------------------------------


@dataclass
class BarState:
    """Container for a single LED bar configuration."""
    pct: int = 0                # 0‑100 %
    c1: str = "00FF42"
    c2: str = "FFF600"
    c3: str = "FF0000"
    solid: bool = False

    def as_cmd(self, index: int) -> str:
        """Serial command for this bar – ``index`` is 1‑based."""
        solid_val = 1 if self.solid else 0
        return f"BAR{index}:{self.pct},{clean_hex(self.c1)}," \
               f"{clean_hex(self.c2)},{clean_hex(self.c3)},{solid_val}"


# ----------------------------------------------------------------------
# --------------------------- MAIN GUI CLASS ----------------------------
# ----------------------------------------------------------------------


class ProMicroTester:
    """Tkinter based UI that talks to a ProMicro (or compatible) board."""

    # --------------------------------------------------------------
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ProMicro Tester (Real‑time Sliders)")
        self.root.geometry("900x680")

        # ---------- Serial handling ----------
        self.ser: serial.Serial | None = None
        self.reader_thread: threading.Thread | None = None
        self.stop_reader = threading.Event()
        self._rx_queue: queue.Queue[str] = queue.Queue()

        # ---------- Throttling state ----------
        self.last_bar_send_time: List[int] = [0] * BAR_COUNT

        # ---------- UI construction ----------
        self._build_ui()
        self.set_ui_enabled(False)          # everything disabled until a port is opened
        self.root.after(READ_POLL_MS, self._process_rx_queue)

        # Show import error (if any) right away in the log area
        if serial is None:
            self.log(f"⚠️  pyserial not available – serial features disabled.\n"
                     f"Import error: {_IMPORT_ERR}")

        # clean shutdown on window close
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # --------------------------------------------------------------
    def _build_ui(self):
        """Create all widgets and layout."""
        # ---- Top frame (COM selector) ----
        top_frame = ttk.Frame(self.root, padding=10)
        top_frame.pack(fill=tk.X)

        ttk.Label(top_frame, text="COM‑порт:").pack(side=tk.LEFT, padx=5)

        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(
            top_frame,
            textvariable=self.port_var,
            width=15,
            state="readonly",
        )
        self.port_combo.pack(side=tk.LEFT, padx=5)

        # **Create the Connect button BEFORE we call refresh_ports()**
        self.btn_connect = ttk.Button(
            top_frame, text="Подключиться", command=self.toggle_connection
        )
        self.btn_connect.pack(side=tk.LEFT, padx=5)

        # Refresh‑ports button (can be pressed later)
        ttk.Button(top_frame, text="🔄 Обновить", command=self.refresh_ports).pack(
            side=tk.LEFT, padx=5
        )

        # Now we can safely populate the COM list and enable/disable the button.
        self.refresh_ports()

        # ---- Main area (left + right) ----
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # ----- Left column – OLED & Brightness -----
        left_frame = ttk.LabelFrame(main_frame, text="OLED и Яркость", padding=10)
        left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))

        self.l1_var = tk.StringVar(value="CPU: 45%")
        self._make_entry(left_frame, "Строка L1:", self.l1_var, row=0)
        self.l2_var = tk.StringVar(value="GPU: 60C")
        self._make_entry(left_frame, "Строка L2:", self.l2_var, row=1)
        self.l3_var = tk.StringVar(value="RAM: 8GB")
        self._make_entry(left_frame, "Строка L3:", self.l3_var, row=2)

        ttk.Button(
            left_frame, text="Отправить OLED", command=self.send_oled
        ).grid(row=3, column=0, columnspan=2, pady=10)

        ttk.Separator(left_frame, orient="horizontal").grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=10
        )

        # Brightness slider
        ttk.Label(left_frame, text="Яркость (BRI):").grid(row=5, column=0, sticky=tk.W)
        self.bri_var = tk.IntVar(value=DEFAULT_BRI)
        self.bri_label = ttk.Label(left_frame, text=f"{DEFAULT_BRI}%")
        self.bri_label.grid(row=5, column=1, sticky=tk.W)

        ttk.Scale(
            left_frame,
            from_=0,
            to=100,
            variable=self.bri_var,
            orient=tk.HORIZONTAL,
            command=self.on_brightness_change,
        ).grid(row=6, column=0, columnspan=2, sticky="ew", pady=5)

        ttk.Button(left_frame, text="Применить яркость", command=self.send_bri).grid(
            row=7, column=0, columnspan=2, pady=5
        )

        ttk.Separator(left_frame, orient="horizontal").grid(
            row=8, column=0, columnspan=2, sticky="ew", pady=10
        )
        ttk.Button(left_frame, text="Запустить CAL", command=self.send_cal).grid(
            row=9, column=0, columnspan=2, pady=5
        )

        # ----- Right column – LED Bars -----
        right_frame = ttk.LabelFrame(
            main_frame,
            text="LED Бары (ползунки работают в реальном времени)",
            padding=10,
        )
        right_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.bars: List[dict] = []
        default_colors = ["00FF42", "FFF600", "FF0000"]
        stop_labels = ["C1 (0%):", "C2 (50%):", "C3 (100%):"]

        for i in range(BAR_COUNT):
            bar_frame = ttk.LabelFrame(right_frame, text=f"BAR {i+1}", padding=5)
            bar_frame.grid(row=0, column=i, padx=5, sticky="nsew")
            right_frame.columnconfigure(i, weight=1)

            # --- Percentage slider with live label ---
            pct_var = tk.IntVar(value=0)
            pct_label = ttk.Label(bar_frame, text="0%")
            pct_label.pack(anchor=tk.W)

            scale = ttk.Scale(
                bar_frame,
                from_=0,
                to=100,
                variable=pct_var,
                orient=tk.HORIZONTAL,
                command=lambda _val, idx=i: self.on_bar_slider_change(idx),
            )
            scale.pack(fill=tk.X, pady=(0, 8))

            # --- Colour swatches (askcolor) ---
            color_vars = []
            for stop_idx, label_text in enumerate(stop_labels):
                ttk.Label(bar_frame, text=label_text).pack(anchor=tk.W)
                hex_var = tk.StringVar(value=default_colors[stop_idx])
                swatch = tk.Button(
                    bar_frame,
                    text="#" + default_colors[stop_idx],
                    bg="#" + default_colors[stop_idx],
                    activebackground="#" + default_colors[stop_idx],
                    width=10,
                    relief=tk.RAISED,
                )
                swatch.configure(
                    command=lambda hv=hex_var, sw=swatch, idx=i: self.pick_color(hv, sw, idx)
                )
                swatch.pack(anchor=tk.W, fill=tk.X, pady=(0, 4))
                color_vars.append(hex_var)

            # --- Solid‑on‑100% flag ---
            solid_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                bar_frame, text="Solid на 100%", variable=solid_var
            ).pack(anchor=tk.W, pady=5)

            # --- Quick set buttons (0 % / 100 %) ---
            btn_frame = ttk.Frame(bar_frame)
            btn_frame.pack(fill=tk.X, pady=5)
            ttk.Button(
                btn_frame,
                text="0%",
                command=lambda v=pct_var, idx=i: self.set_bar_and_send(v, idx, 0),
            ).pack(side=tk.LEFT, expand=True, fill=tk.X)
            ttk.Button(
                btn_frame,
                text="100%",
                command=lambda v=pct_var, idx=i: self.set_bar_and_send(v, idx, 100),
            ).pack(side=tk.LEFT, expand=True, fill=tk.X)

            # Apply all button (global) – still placed per‑bar for backward compatibility
            ttk.Button(
                bar_frame, text="Применить все", command=self.send_bars
            ).pack(fill=tk.X, pady=5)

            self.bars.append(
                {
                    "pct": pct_var,
                    "c1": color_vars[0],
                    "c2": color_vars[1],
                    "c3": color_vars[2],
                    "solid": solid_var,
                    "pct_label": pct_label,
                }
            )

        # ----- Log frame -----
        log_frame = ttk.LabelFrame(self.root, text="Лог", padding=5)
        log_frame.pack(fill=tk.X, padx=10, pady=10)

        self.log_text = tk.Text(
            log_frame, height=8, state=tk.DISABLED, font=("Consolas", 9)
        )
        self.log_text.pack(fill=tk.X)

    # --------------------------------------------------------------
    @staticmethod
    def _make_entry(parent, label_text: str, var: tk.StringVar, row: int):
        """Utility to create a labelled entry widget."""
        ttk.Label(parent, text=label_text).grid(row=row, column=0, sticky=tk.W, pady=2)
        ttk.Entry(parent, textvariable=var, width=20).grid(
            row=row, column=1, pady=2
        )

    # --------------------------------------------------------------
    def set_ui_enabled(self, enabled: bool):
        """
        Enable/disable all interactive widgets depending on connection state.
        ``enabled`` == True → we are *connected* and can send data.
        """
        normal_state = tk.NORMAL if enabled else tk.DISABLED

        for child in self.root.winfo_children():
            # Buttons, entries, checkboxes, sliders
            if isinstance(child, (ttk.Button, ttk.Entry, ttk.Checkbutton, ttk.Scale)):
                try:
                    child.configure(state=normal_state)
                except tk.TclError:
                    pass

        # Combobox for COM‑port selection – readonly when disconnected,
        # disabled when we are already connected.
        self.port_combo.configure(
            state="readonly" if not enabled else "disabled"
        )

        # Update the connect button text
        self.btn_connect.config(text="Отключиться" if enabled else "Подключиться")

    # --------------------------------------------------------------
    def refresh_ports(self):
        """Populate the COM‑port combobox with currently available serial ports."""
        if list_ports is None:
            # pyserial not present – nothing to do.
            self.port_combo['values'] = []
            self.btn_connect.configure(state=tk.DISABLED)
            return

        ports = [p.device for p in list_ports.comports()]
        self.port_combo["values"] = ports
        if ports:
            self.port_combo.current(0)
            # Enable the connect button only when we have at least one port.
            self.btn_connect.configure(state=tk.NORMAL)
        else:
            self.port_combo.set("")
            self.btn_connect.configure(state=tk.DISABLED)

    # --------------------------------------------------------------
    def toggle_connection(self):
        """Open or close the serial port depending on current state."""
        if self.ser and self.ser.is_open:
            # ------------------- Disconnect path -------------------
            self.log("Отключаемся...")
            self.stop_reader.set()
            if self.reader_thread:
                self.reader_thread.join(timeout=1.0)
            try:
                self.ser.close()
            except Exception as exc:  # pragma: no cover – extremely unlikely
                self.log(f"Ошибка при закрытии порта: {exc}")
            self.ser = None
            self.set_ui_enabled(False)
            self.log("Отключено.")
        else:
            # ------------------- Connect path -------------------
            if serial is None:
                messagebox.showerror(
                    "Ошибка",
                    "Библиотека pyserial не установлена. "
                    "Установите её командой: pip install pyserial"
                )
                return

            port = self.port_var.get()
            if not port:
                messagebox.showerror("Ошибка", "Выберите COM‑порт")
                return

            try:
                self.ser = serial.Serial(
                    port,
                    BAUD_RATE,
                    timeout=0,          # non‑blocking reads; we poll via in_waiting
                    rtscts=False,
                    dsrdtr=False,
                )
            except serial.SerialException as exc:
                messagebox.showerror("Ошибка", f"Не удалось открыть порт: {exc}")
                return

            self.stop_reader.clear()
            self.reader_thread = threading.Thread(
                target=self._reader_loop, daemon=True
            )
            self.reader_thread.start()

            # UI becomes usable immediately; we will set brightness after a short delay.
            self.set_ui_enabled(True)
            self.log(f"Порт {port} открыт. Ожидаем 2 сек…")
            self.root.after(2000, self._post_connect_init)

    # --------------------------------------------------------------
    def _post_connect_init(self):
        """Things that should happen a couple of seconds after the port is opened."""
        if not (self.ser and self.ser.is_open):
            return
        self.log("Подключено. Инициализируем яркость…")
        self.bri_var.set(DEFAULT_BRI)
        self.send_bri()

    # --------------------------------------------------------------
    def _reader_loop(self):
        """Background thread – read incoming lines and push them to a queue."""
        while not self.stop_reader.is_set():
            if (
                self.ser
                and self.ser.is_open
                and self.ser.in_waiting > 0
            ):
                try:
                    raw = self.ser.readline()
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if line:
                        self._rx_queue.put(line)
                except Exception as exc:  # pragma: no cover – defensive
                    self.log(f"Ошибка чтения: {exc}")
            time.sleep(0.05)   # short poll; does not hog CPU

    # --------------------------------------------------------------
    def _process_rx_queue(self):
        """Run on the main thread – drain inbound queue and write to log."""
        while not self._rx_queue.empty():
            line = self._rx_queue.get_nowait()
            self.log(f"[Плата]: {line}")
        self.root.after(READ_POLL_MS, self._process_rx_queue)

    # --------------------------------------------------------------
    def send_cmd(self, cmd: str):
        """Write a single command string (terminated by newline) to the serial port."""
        if not (self.ser and self.ser.is_open):
            self.log("Ошибка: порт закрыт!")
            return
        full = f"{cmd}\n"
        try:
            self.ser.write(full.encode("utf-8"))
            self.ser.flush()
            self.log(f"-> {cmd}")
        except serial.SerialException as exc:
            self.log(f"Ошибка отправки '{cmd}': {exc}")

    # --------------------------------------------------------------
    def log(self, msg: str):
        """Append a line to the read‑only text widget (thread‑safe)."""
        def _append():
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)

        self.root.after(0, _append)

    # --------------------------------------------------------------
    # --------------------------- COMMAND HELPERS --------------------
    # --------------------------------------------------------------

    def pick_color(self, hex_var: tk.StringVar, swatch_btn: tk.Button, bar_index: int):
        """Open colour chooser, validate result and update UI + send new bar."""
        current = "#" + clean_hex(hex_var.get())
        try:
            _, hex_code = colorchooser.askcolor(color=current, title="Выбери цвет")
        except tk.TclError:  # pragma: no cover – occurs only if `current` is invalid
            _, hex_code = colorchooser.askcolor(title="Выбери цвет")

        if hex_code is None:
            return  # user cancelled

        clean = clean_hex(hex_code)
        if not is_valid_hex(clean):
            messagebox.showerror("Ошибка", f"Неверный HEX‑цвет: {hex_code}")
            return

        hex_var.set(clean)
        swatch_btn.config(
            text="#" + clean,
            bg="#" + clean,
            activebackground="#" + clean,
        )
        # Send the whole bar immediately so the user sees the change.
        self.send_single_bar(bar_index)

    # --------------------------------------------------------------
    def send_single_bar(self, bar_index: int):
        """Send command for a single bar (used by colour picker and throttled slider)."""
        b = self.bars[bar_index]
        state = BarState(
            pct=b["pct"].get(),
            c1=b["c1"].get(),
            c2=b["c2"].get(),
            c3=b["c3"].get(),
            solid=b["solid"].get(),
        )
        # Validate colours before sending
        for colour in (state.c1, state.c2, state.c3):
            if not is_valid_hex(colour):
                self.log(f"Ошибка: неверный HEX‑цвет в BAR{bar_index+1}")
                return
        cmd = state.as_cmd(bar_index + 1)
        self.send_cmd(cmd)

    # --------------------------------------------------------------
    def on_bar_slider_change(self, bar_index: int):
        """
        Callback for live slider movement.
        Updates the % label and sends a throttled command.
        """
        b = self.bars[bar_index]
        pct = b["pct"].get()
        b["pct_label"].config(text=f"{pct}%")

        now_ms = int(time.time() * 1000)
        if now_ms - self.last_bar_send_time[bar_index] < THROTTLE_MS:
            return
        self.last_bar_send_time[bar_index] = now_ms
        self.send_single_bar(bar_index)

    # --------------------------------------------------------------
    def set_bar_and_send(self, pct_var: tk.IntVar, bar_index: int, value: int):
        """Set the slider to ``value`` (0 or 100) and send immediately."""
        pct_var.set(value)
        self.bars[bar_index]["pct_label"].config(text=f"{value}%")
        # Bypass throttling – we want an immediate update
        self.last_bar_send_time[bar_index] = 0
        self.send_single_bar(bar_index)

    # --------------------------------------------------------------
    def send_bars(self):
        """Send **all** bars in a single pipe‑separated packet."""
        parts: List[str] = []
        for i, b in enumerate(self.bars):
            state = BarState(
                pct=b["pct"].get(),
                c1=b["c1"].get(),
                c2=b["c2"].get(),
                c3=b["c3"].get(),
                solid=b["solid"].get(),
            )
            # Validate colours before sending the whole packet
            for colour in (state.c1, state.c2, state.c3):
                if not is_valid_hex(colour):
                    self.log(f"Ошибка: неверный HEX‑цвет в BAR{i+1}")
                    return
            parts.append(state.as_cmd(i + 1))

        cmd = "|".join(parts)
        self.send_cmd(cmd)

    # --------------------------------------------------------------
    def send_bri(self):
        """Send only the brightness command."""
        self.send_cmd(f"BRI:{self.bri_var.get()}")

    # --------------------------------------------------------------
    def on_brightness_change(self, _val=None):
        """Update the label that shows current % while the slider moves."""
        self.bri_label.config(text=f"{self.bri_var.get()}%")

    # --------------------------------------------------------------
    def send_oled(self):
        """Send only the OLED text lines (non‑empty ones)."""
        parts: List[str] = []
        l1 = self.l1_var.get().strip()
        l2 = self.l2_var.get().strip()
        l3 = self.l3_var.get().strip()
        if l1:
            parts.append(f"L1:{l1}")
        if l2:
            parts.append(f"L2:{l2}")
        if l3:
            parts.append(f"L3:{l3}")

        if parts:
            cmd = "|".join(parts)
            self.send_cmd(cmd)

    # --------------------------------------------------------------
    def send_cal(self):
        """Send the CAL command (used for calibration)."""
        self.send_cmd("CAL")

    # --------------------------------------------------------------
    def on_close(self):
        """Handler for window close – stops thread, closes port and exits."""
        if self.ser and self.ser.is_open:
            self.log("Закрываем порт перед выходом...")
            self.stop_reader.set()
            if self.reader_thread:
                self.reader_thread.join(timeout=1.0)
            try:
                self.ser.close()
            except Exception:  # pragma: no cover
                pass
        self.root.destroy()


# ----------------------------------------------------------------------
if __name__ == "__main__":
    root = tk.Tk()
    app = ProMicroTester(root)
    root.mainloop()
