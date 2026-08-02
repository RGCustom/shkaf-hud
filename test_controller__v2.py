import tkinter as tk
from tkinter import ttk, messagebox, colorchooser
import serial
import serial.tools.list_ports
import time
import threading

class ProMicroTester:
    def __init__(self, root):
        self.root = root
        self.root.title("ProMicro Tester (Real-time Sliders)")
        self.root.geometry("900x680")

        self.ser = None
        self.reader_thread = None
        self.stop_reader = False

        # Для throttling: запоминаем время последней отправки для каждого бара
        self.last_bar_send_time = [0, 0, 0, 0]
        self.BAR_THROTTLE_MS = 50  # 50мс = 20 обновлений/сек

        # --- Верхняя панель ---
        top_frame = ttk.Frame(root, padding=10)
        top_frame.pack(fill=tk.X)

        ttk.Label(top_frame, text="COM-порт:").pack(side=tk.LEFT, padx=5)
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(top_frame, textvariable=self.port_var, width=15, state="readonly")
        self.port_combo.pack(side=tk.LEFT, padx=5)
        self.refresh_ports()

        ttk.Button(top_frame, text="🔄 Обновить", command=self.refresh_ports).pack(side=tk.LEFT, padx=5)
        self.btn_connect = ttk.Button(top_frame, text="Подключиться", command=self.toggle_connection)
        self.btn_connect.pack(side=tk.LEFT, padx=5)

        # --- Основная область ---
        main_frame = ttk.Frame(root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Левая колонка
        left_frame = ttk.LabelFrame(main_frame, text="OLED и Яркость", padding=10)
        left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))

        ttk.Label(left_frame, text="Строка L1:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.l1_var = tk.StringVar(value="CPU: 45%")
        ttk.Entry(left_frame, textvariable=self.l1_var, width=20).grid(row=0, column=1, pady=2)

        ttk.Label(left_frame, text="Строка L2:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.l2_var = tk.StringVar(value="GPU: 60C")
        ttk.Entry(left_frame, textvariable=self.l2_var, width=20).grid(row=1, column=1, pady=2)

        ttk.Label(left_frame, text="Строка L3:").grid(row=2, column=0, sticky=tk.W, pady=2)
        self.l3_var = tk.StringVar(value="RAM: 8GB")
        ttk.Entry(left_frame, textvariable=self.l3_var, width=20).grid(row=2, column=1, pady=2)

        ttk.Button(left_frame, text="Отправить OLED", command=self.send_oled).grid(row=3, column=0, columnspan=2, pady=10)

        ttk.Separator(left_frame, orient='horizontal').grid(row=4, column=0, columnspan=2, sticky='ew', pady=10)

        ttk.Label(left_frame, text="Яркость (BRI):").grid(row=5, column=0, sticky=tk.W, pady=5)
        self.bri_var = tk.IntVar(value=5)
        self.bri_label = ttk.Label(left_frame, text="5%")
        self.bri_label.grid(row=5, column=1, sticky=tk.W)
        ttk.Scale(left_frame, from_=0, to=100, variable=self.bri_var, orient=tk.HORIZONTAL,
                  command=self.on_brightness_change).grid(row=6, column=0, columnspan=2, sticky='ew', pady=5)

        ttk.Button(left_frame, text="Применить яркость", command=self.send_bri).grid(row=7, column=0, columnspan=2, pady=5)

        ttk.Separator(left_frame, orient='horizontal').grid(row=8, column=0, columnspan=2, sticky='ew', pady=10)
        ttk.Button(left_frame, text="Запустить CAL", command=self.send_cal).grid(row=9, column=0, columnspan=2, pady=5)

        # Правая колонка
        right_frame = ttk.LabelFrame(main_frame, text="LED Бары (ползунки работают в реальном времени)", padding=10)
        right_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.bars = []
        default_colors = ["00FF42", "FFF600", "FF0000"]
        stop_labels = ["C1 (0%):", "C2 (50%):", "C3 (100%):"]

        for i in range(4):
            bar_frame = ttk.LabelFrame(right_frame, text=f"BAR {i+1}", padding=5)
            bar_frame.grid(row=0, column=i, padx=5, sticky='nsew')
            right_frame.columnconfigure(i, weight=1)

            # Процент - с live-обновлением
            pct_var = tk.IntVar(value=0)
            pct_label = ttk.Label(bar_frame, text="0%")
            pct_label.pack(anchor=tk.W)

            # ВАЖНО: command вызывается при ЛЮБОМ движении ползунка
            scale = ttk.Scale(bar_frame, from_=0, to=100, variable=pct_var, orient=tk.HORIZONTAL,
                              command=lambda val, idx=i: self.on_bar_slider_change(idx))
            scale.pack(fill=tk.X, pady=(0, 8))

            # --- Цвета через палитру: кнопка сама залита текущим цветом, клик открывает askcolor ---
            color_vars = []
            for stop_idx, label_text in enumerate(stop_labels):
                ttk.Label(bar_frame, text=label_text).pack(anchor=tk.W)
                hex_var = tk.StringVar(value=default_colors[stop_idx])
                swatch = tk.Button(
                    bar_frame, text="#" + default_colors[stop_idx],
                    bg="#" + default_colors[stop_idx],
                    activebackground="#" + default_colors[stop_idx],
                    width=10, relief=tk.RAISED,
                )
                swatch.configure(command=lambda hv=hex_var, sw=swatch, idx=i: self.pick_color(hv, sw, idx))
                swatch.pack(anchor=tk.W, fill=tk.X, pady=(0, 4))
                color_vars.append(hex_var)

            solid_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(bar_frame, text="Solid на 100%", variable=solid_var).pack(anchor=tk.W, pady=5)

            btn_frame = ttk.Frame(bar_frame)
            btn_frame.pack(fill=tk.X, pady=5)
            ttk.Button(btn_frame, text="0%", command=lambda v=pct_var, idx=i: self.set_bar_and_send(v, idx, 0)).pack(side=tk.LEFT, expand=True, fill=tk.X)
            ttk.Button(btn_frame, text="100%", command=lambda v=pct_var, idx=i: self.set_bar_and_send(v, idx, 100)).pack(side=tk.LEFT, expand=True, fill=tk.X)

            ttk.Button(bar_frame, text="Применить все", command=self.send_bars).pack(fill=tk.X, pady=5)

            self.bars.append({
                'pct': pct_var,
                'c1': color_vars[0],
                'c2': color_vars[1],
                'c3': color_vars[2],
                'solid': solid_var,
                'pct_label': pct_label,
            })

        # Лог
        log_frame = ttk.LabelFrame(root, text="Лог", padding=5)
        log_frame.pack(fill=tk.X, padx=10, pady=10)
        self.log_text = tk.Text(log_frame, height=8, state=tk.DISABLED, font=("Consolas", 9))
        self.log_text.pack(fill=tk.X)

    def refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo['values'] = ports
        if ports:
            self.port_combo.current(0)

    def toggle_connection(self):
        if self.ser and self.ser.is_open:
            self.stop_reader = True
            if self.reader_thread: self.reader_thread.join(timeout=1)
            self.ser.close()
            self.btn_connect.config(text="Подключиться")
            self.log("Отключено.")
        else:
            port = self.port_var.get()
            if not port:
                messagebox.showerror("Ошибка", "Выберите COM-порт")
                return
            try:
                self.ser = serial.Serial(port, 115200, timeout=1, rtscts=False, dsrdtr=False)
                self.stop_reader = False
                self.reader_thread = threading.Thread(target=self.read_serial, daemon=True)
                self.reader_thread.start()

                self.log(f"Порт {port} открыт. Ждем 2 секунды...")
                time.sleep(2)

                self.btn_connect.config(text="Отключиться")
                self.log(f"Подключено. Инициализация яркости 5%...")

                self.bri_var.set(5)
                self.send_bri()
            except serial.SerialException as e:
                messagebox.showerror("Ошибка", f"Не удалось открыть порт: {e}")

    def read_serial(self):
        while not self.stop_reader:
            if self.ser and self.ser.is_open and self.ser.in_waiting:
                try:
                    line = self.ser.readline().decode('utf-8', errors='ignore').strip()
                    if line:
                        self.log(f"[Плата]: {line}")
                except:
                    pass
            time.sleep(0.1)

    def send_cmd(self, cmd):
        if self.ser and self.ser.is_open:
            full_cmd = cmd + "\n"
            self.ser.write(full_cmd.encode('utf-8'))
            self.ser.flush()
            self.log(f"-> {cmd}")
        else:
            self.log("Ошибка: Порт закрыт!")

    def log(self, msg):
        def _update():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        self.root.after(0, _update)

    def clean_hex(self, hex_str):
        return hex_str.replace('#', '').strip().upper()

    # --- палитра: askcolor() гарантирует валидный hex, без опечаток руками ---
    def pick_color(self, hex_var, swatch_btn, bar_index):
        current = "#" + self.clean_hex(hex_var.get())
        try:
            rgb, hex_code = colorchooser.askcolor(color=current, title="Выбери цвет")
        except tk.TclError:
            # если текущее значение было невалидным hex - открыть без начального цвета
            rgb, hex_code = colorchooser.askcolor(title="Выбери цвет")

        if hex_code is None:
            return  # пользователь нажал "Отмена"

        clean = self.clean_hex(hex_code)
        hex_var.set(clean)
        swatch_btn.config(text="#" + clean, bg="#" + clean, activebackground="#" + clean)

        # сразу отправляем на плату - чтобы результат подбора цвета было видно на ленте сразу
        self.send_single_bar(bar_index)

    def send_single_bar(self, bar_index):
        b = self.bars[bar_index]
        pct = b['pct'].get()
        c1 = self.clean_hex(b['c1'].get())
        c2 = self.clean_hex(b['c2'].get())
        c3 = self.clean_hex(b['c3'].get())
        solid = 1 if b['solid'].get() else 0
        cmd = f"BAR{bar_index+1}:{pct},{c1},{c2},{c3},{solid}"
        self.send_cmd(cmd)

    # --- REAL-TIME: вызывается при движении ползунка ---
    def on_bar_slider_change(self, bar_index):
        """Обновляет label с процентами и отправляет команду с throttling"""
        b = self.bars[bar_index]
        pct = b['pct'].get()

        # Обновляем label с процентами
        b['pct_label'].config(text=f"{pct}%")

        # Throttling: не отправляем чаще чем раз в 50мс
        current_time = int(time.time() * 1000)
        if current_time - self.last_bar_send_time[bar_index] < self.BAR_THROTTLE_MS:
            return

        self.last_bar_send_time[bar_index] = current_time
        self.send_single_bar(bar_index)

    def send_bars(self):
        """Отправляет ВСЕ 4 бара одним пакетом (для кнопки 'Применить все')"""
        parts = []
        for i in range(4):
            b = self.bars[i]
            pct = b['pct'].get()
            c1 = self.clean_hex(b['c1'].get())
            c2 = self.clean_hex(b['c2'].get())
            c3 = self.clean_hex(b['c3'].get())
            solid = 1 if b['solid'].get() else 0
            parts.append(f"BAR{i+1}:{pct},{c1},{c2},{c3},{solid}")

        cmd = "|".join(parts)
        self.send_cmd(cmd)

    def send_bri(self):
        """Отправляет только яркость"""
        self.send_cmd(f"BRI:{self.bri_var.get()}")

    def send_oled(self):
        """Отправляет ТОЛЬКО OLED строки"""
        parts = []
        l1 = self.l1_var.get().strip()
        l2 = self.l2_var.get().strip()
        l3 = self.l3_var.get().strip()
        if l1: parts.append(f"L1:{l1}")
        if l2: parts.append(f"L2:{l2}")
        if l3: parts.append(f"L3:{l3}")

        if parts:
            cmd = "|".join(parts)
            self.send_cmd(cmd)

    def on_brightness_change(self, val=None):
        self.bri_label.config(text=f"{self.bri_var.get()}%")

    def send_cal(self):
        self.send_cmd("CAL")

    def set_bar_and_send(self, pct_var, bar_index, value):
        """Устанавливает значение ползунка и сразу отправляет"""
        pct_var.set(value)
        self.bars[bar_index]['pct_label'].config(text=f"{value}%")
        self.send_single_bar(bar_index)

if __name__ == "__main__":
    root = tk.Tk()
    app = ProMicroTester(root)
    root.mainloop()
