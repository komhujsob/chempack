"""A small native desktop interface; no server, accounts or external dependencies."""

from __future__ import annotations

import queue
import secrets
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .core import (
    KINDS,
    build_package,
    create_demo,
    load_assignments,
    save_assignments,
    scan_folder,
    summary,
)


class App:
    def __init__(self, root):
        self.root = root
        self.scan = None
        self.package = None
        self.demo = None
        self.queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        root.title("ChemPack · Передать результаты без файлового хаоса")
        root.geometry("1180x770")
        root.minsize(880, 620)
        root.configure(background="#f3f5ef")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f5ef")
        style.configure(
            "TLabel", background="#f3f5ef", foreground="#203f39", font=("Helvetica", 11)
        )
        style.configure("Sub.TLabel", foreground="#758074", font=("Helvetica", 10))
        style.configure("TButton", padding=(12, 9), font=("Helvetica", 10))
        style.configure(
            "Accent.TButton",
            background="#cae294",
            foreground="#153a2d",
            font=("Helvetica", 11, "bold"),
        )
        style.map("Accent.TButton", background=[("active", "#d7eba9")])
        style.configure(
            "Treeview",
            rowheight=32,
            font=("Helvetica", 10),
            background="#ffffff",
            fieldbackground="#ffffff",
            foreground="#203f39",
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            font=("Helvetica", 10, "bold"),
            padding=(8, 9),
            background="#e8eee0",
            foreground="#52644f",
        )
        style.map(
            "Treeview", background=[("selected", "#d4e6b9")], foreground=[("selected", "#153a2d")]
        )
        header = tk.Frame(root, bg="#153a32", padx=26, pady=22)
        header.pack(fill="x")
        tk.Label(
            header, text="⬡  ChemPack", bg="#153a32", fg="#e6efda", font=("Helvetica", 24, "bold")
        ).pack(side="left")
        tk.Label(
            header,
            text="СОБРАТЬ → ПРОВЕРИТЬ → ПЕРЕДАТЬ",
            bg="#153a32",
            fg="#adc78b",
            font=("Helvetica", 10),
        ).pack(side="right")
        body = ttk.Frame(root, padding=(26, 22))
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="Собери файлы по образцам и передай одним архивом",
            font=("Helvetica", 17, "bold"),
        ).pack(anchor="w")
        self.step_label = ttk.Label(
            body,
            text="Шаг 1 из 3 · Выбери папку или открой демо",
            style="Sub.TLabel",
        )
        self.step_label.pack(anchor="w", pady=(8, 18))
        actions = ttk.Frame(body)
        actions.pack(fill="x")
        self.choose = ttk.Button(
            actions, text="① Выбрать папку…", command=self.choose_folder, style="Accent.TButton"
        )
        self.choose.pack(side="left")
        self.try_demo = ttk.Button(actions, text="Попробовать демо", command=self.load_demo)
        self.try_demo.pack(side="left", padx=9)
        self.cancel_button = ttk.Button(
            actions, text="Отмена", command=self.cancel.set, state="disabled"
        )
        self.cancel_button.pack(side="right")
        self.path_label = ttk.Label(
            body, text="Файлы ещё не выбраны.", style="Sub.TLabel", wraplength=1050
        )
        self.path_label.pack(anchor="w", pady=(10, 16))
        stats = ttk.Frame(body)
        stats.pack(fill="x", pady=(0, 17))
        self.stats = []
        for title in ("ФАЙЛОВ В ПАКЕТЕ", "КОДОВ ОБРАЗЦОВ", "КОПИЙ ПО SHA-256", "БЕЗ КОДА ОБРАЗЦА"):
            panel = tk.Frame(
                stats,
                bg="#ffffff",
                padx=18,
                pady=12,
                highlightbackground="#dce4d3",
                highlightthickness=1,
            )
            panel.pack(side="left", fill="x", expand=True, padx=(0, 10))
            value = tk.Label(panel, text="—", bg="#ffffff", fg="#1b4739", font=("Helvetica", 27))
            value.pack(anchor="w")
            tk.Label(panel, text=title, bg="#ffffff", fg="#74806c", font=("Helvetica", 8)).pack(
                anchor="w", pady=(4, 0)
            )
            self.stats.append(value)
        toolbar = ttk.Frame(body)
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Label(toolbar, text="Ожидаемые типы:").pack(side="left")
        self.required = tk.StringVar(value="1H,13C")
        self.require_entry = ttk.Entry(toolbar, textvariable=self.required, width=18)
        self.require_entry.pack(side="left", padx=(7, 18))
        self.require_entry.bind("<KeyRelease>", lambda _: self.update_summary())
        self.filter = tk.StringVar(value="Все файлы")
        self.filter_box = ttk.Combobox(
            toolbar,
            textvariable=self.filter,
            values=(
                "Все файлы",
                "Требуют проверки",
                "Без кода образца",
                "Совпадения содержимого",
                "Исключённые",
            ),
            state="readonly",
            width=27,
        )
        self.filter_box.pack(side="left")
        self.filter_box.bind("<<ComboboxSelected>>", lambda _: self.refresh())
        self.edit_button = ttk.Button(
            toolbar, text="Изменить выбранные…", command=self.edit_selected, state="disabled"
        )
        self.edit_button.pack(side="right")
        review = ttk.Frame(body)
        review.pack(fill="x", pady=(0, 10))
        ttk.Label(review, text="Поиск:").pack(side="left")
        self.search = tk.StringVar()
        ttk.Entry(review, textvariable=self.search, width=24).pack(side="left", padx=(7, 12))
        self.search.trace_add("write", lambda *_: self.refresh())
        self.match_label = ttk.Label(review, text="", style="Sub.TLabel")
        self.match_label.pack(side="left")
        self.load_button = ttk.Button(review, text="Загрузить разметку…", command=self.load_review)
        self.load_button.pack(side="right")
        self.save_button = ttk.Button(review, text="Сохранить разметку…", command=self.save_review)
        self.save_button.pack(side="right", padx=8)
        self.load_button.state(["disabled"])
        self.save_button.state(["disabled"])
        footer = ttk.Frame(body)
        footer.pack(side="bottom", fill="x")
        self.missing_label = ttk.Label(
            footer,
            text="В демо можно проверить весь процесс на семи учебных файлах.",
            style="Sub.TLabel",
            wraplength=1050,
        )
        self.missing_label.pack(anchor="w", pady=(12, 12))
        bottom = ttk.Frame(footer)
        bottom.pack(fill="x")
        self.build_button = ttk.Button(
            bottom,
            text="③ Собрать ZIP…",
            style="Accent.TButton",
            command=self.build,
            state="disabled",
        )
        self.build_button.pack(side="left")
        self.open_button = ttk.Button(
            bottom, text="Открыть каталог", command=self.open_catalog, state="disabled"
        )
        self.open_button.pack(side="left", padx=8)
        self.folder_button = ttk.Button(
            bottom, text="Папка с ZIP", command=self.open_folder, state="disabled"
        )
        self.folder_button.pack(side="left")
        self.status = ttk.Label(
            footer,
            text="Локально · без облака · исходные файлы остаются на месте",
            style="Sub.TLabel",
            wraplength=1050,
        )
        self.status.pack(anchor="w", pady=(13, 0))
        table = ttk.Frame(body)
        table.pack(fill="both", expand=True)
        self.table = ttk.Treeview(
            table,
            columns=("included", "file", "sample", "kind", "size", "status"),
            show="headings",
            selectmode="extended",
        )
        for key, title, width in (
            ("included", "В ZIP", 55),
            ("file", "Исходный файл", 360),
            ("sample", "Образец", 100),
            ("kind", "Тип", 90),
            ("size", "Размер", 80),
            ("status", "Проверка", 190),
        ):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=30, stretch=key in {"file", "status"})
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.bind("<Double-1>", self.edit_row)
        self.table.bind("<Return>", lambda _: self.edit_selected())
        self.table.bind("<space>", self.toggle_selected)
        self.table.bind("<<TreeviewSelect>>", lambda _: self.update_actions())
        self.table.bind("<ButtonRelease-1>", self.toggle_cell)

        root.after(100, self.poll)
        root.protocol("WM_DELETE_WINDOW", self.close)

    def required_types(self):
        return tuple(dict.fromkeys(v.strip() for v in self.required.get().split(",") if v.strip()))

    def choose_folder(self):
        path = filedialog.askdirectory(
            title="Папка с экспортами и материалами проекта", parent=self.root
        )
        if path:
            self.run_job(lambda: scan_folder(path, self.report_progress, self.cancel), "scan")

    def load_demo(self):
        if self.demo is None:
            self.demo = tempfile.TemporaryDirectory(prefix="chempack-demo-")
            create_demo(Path(self.demo.name))
        self.run_job(lambda: scan_folder(self.demo.name, self.report_progress, self.cancel), "scan")

    def report_progress(self, text):
        self.queue.put(("progress", text))

    def set_busy(self, value):
        self.busy = value
        for button in (self.choose, self.try_demo):
            button.configure(state="disabled" if value else "normal")
        self.cancel_button.configure(state="normal" if value else "disabled")
        self.update_actions()
        self.require_entry.configure(state="disabled" if value else "normal")
        self.open_button.configure(state="normal" if self.package and not value else "disabled")
        self.folder_button.configure(state="normal" if self.package and not value else "disabled")

    def run_job(self, work, action):
        if self.busy:
            return
        self.cancel.clear()
        self.set_busy(True)
        self.status.configure(text="Обрабатываю файлы…")

        def worker():
            try:
                result = work()
                self.queue.put((action, result))
            except Exception as exc:
                self.queue.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                action, result = self.queue.get_nowait()
                if action == "progress":
                    self.status.configure(text=result)
                elif action == "error":
                    self.set_busy(False)
                    self.status.configure(text=result)
                    if result != "Операция отменена.":
                        messagebox.showerror("Не удалось завершить", result, parent=self.root)
                elif action == "scan":
                    self.scan = result
                    self.package = None
                    self.set_busy(False)
                    self.table.selection_remove(*self.table.selection())
                    self.filter.set("Все файлы")
                    self.search.set("")
                    self.step_label.configure(text="Шаг 2 из 3 · Проверь образцы и типы файлов")
                    self.path_label.configure(text=f"Источник: {result.root}")
                    self.status.configure(
                        text=f"Найдено {len(result.items)} файлов. Пропущено: {len(result.skipped)}. Проверь коды и типы перед сборкой."
                    )
                    self.refresh()
                elif action == "build":
                    self.package = result
                    self.set_busy(False)
                    self.step_label.configure(text="Шаг 3 из 3 · Передай коллегам ChemPack.zip")
                    self.status.configure(
                        text=f"Готово: {result} · Для передачи команде: ChemPack.zip"
                    )
                    messagebox.showinfo(
                        "Пакет готов",
                        "Созданы ChemPack.zip, HTML-каталог и реестр файлов.\n\nПеред отправкой проверь каталог. Ссылки работают после распаковки ZIP.",
                        parent=self.root,
                    )
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def update_summary(self):
        if not self.scan:
            return
        stats = summary(self.scan, self.required_types())
        for label, key in zip(
            self.stats, ("files", "samples", "duplicate_copies", "unassigned"), strict=True
        ):
            label.configure(text=str(stats[key]))
        missing = [
            f"{sample}: {', '.join(kinds)}" for sample, kinds in stats["missing"].items() if kinds
        ]
        text = (
            "Не найдены по пометкам: " + "; ".join(missing[:5])
            if missing
            else "Для найденных образцов есть все выбранные типы."
        )
        if stats["unassigned"]:
            text += f" Без кода образца: {stats['unassigned']}."
        if not stats["samples"]:
            text = "Назначь коды образцов, чтобы проверить комплектность."
        self.missing_label.configure(
            text=text + " Двойной щелчок — изменить. Щелчок в «В ZIP» — включить или исключить."
        )
        self.update_actions()

    def refresh(self):
        selected = self.table.selection()
        position = self.table.yview()[0]
        self.table.delete(*self.table.get_children())
        if not self.scan:
            return
        hashes = {}
        for item in self.scan.items:
            hashes[item.sha256] = hashes.get(item.sha256, 0) + 1
        query = self.search.get().strip().casefold()
        mode = self.filter.get()
        for index, item in enumerate(self.scan.items):
            if query and not any(
                query in value.casefold() for value in (item.relative, item.sample, item.kind)
            ):
                continue
            duplicate = hashes[item.sha256] > 1
            needs_review = not item.sample or item.kind in ("Проверить", "Другое") or duplicate
            if mode == "Требуют проверки" and (not item.include or not needs_review):
                continue
            if mode == "Без кода образца" and item.sample:
                continue
            if mode == "Совпадения содержимого" and not duplicate:
                continue
            if mode == "Исключённые" and item.include:
                continue
            checks = []
            if not item.sample:
                checks.append("Назначить образец")
            if item.kind in ("Проверить", "Другое"):
                checks.append("Уточнить тип")
            if duplicate:
                checks.append("Копия по SHA-256")
            self.table.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    "✓" if item.include else "—",
                    item.relative,
                    item.sample or "—",
                    item.kind,
                    f"{item.size / 1024:.1f} КБ",
                    " · ".join(checks) or "—",
                ),
            )
        visible = self.table.get_children()
        self.table.selection_set([row for row in selected if self.table.exists(row)])
        self.table.yview_moveto(position if selected else 0)
        self.match_label.configure(text=f"{len(visible)} из {len(self.scan.items)} файлов")
        self.update_summary()

    def update_actions(self):
        ready = self.scan is not None and not self.busy
        self.build_button.configure(
            state="normal"
            if ready and any(item.include for item in self.scan.items)
            else "disabled"
        )
        self.edit_button.configure(
            state="normal" if ready and self.table.selection() else "disabled"
        )
        for button in (self.load_button, self.save_button):
            button.configure(state="normal" if ready else "disabled")

    def edit_row(self, event):
        row = self.table.identify_row(event.y)
        if row and self.table.identify_region(event.x, event.y) == "cell":
            if self.table.identify_column(event.x) != "#1":
                self.edit_selected()

    def toggle_cell(self, event):
        row = self.table.identify_row(event.y)
        if row and self.table.identify_column(event.x) == "#1":
            self.toggle_rows((row,))

    def toggle_selected(self, _event):
        self.toggle_rows(self.table.selection())
        return "break"

    def toggle_rows(self, rows):
        if self.busy or not self.scan or not rows:
            return
        items = [self.scan.items[int(row)] for row in rows]
        include = not all(item.include for item in items)
        for item in items:
            item.include = include
        self.refresh()

    def save_review(self):
        path = filedialog.asksaveasfilename(
            parent=self.root,
            defaultextension=".json",
            initialfile="chempack-assignments.json",
            filetypes=[("Разметка ChemPack", "*.json")],
        )
        if path:
            try:
                save_assignments(self.scan, Path(path))
                self.status.configure(text=f"Разметка сохранена: {path}")
            except OSError as exc:
                messagebox.showerror("Не удалось сохранить разметку", str(exc), parent=self.root)

    def load_review(self):
        path = filedialog.askopenfilename(
            parent=self.root,
            filetypes=[("Разметка ChemPack", "*.json")],
        )
        if path:
            try:
                applied, skipped = load_assignments(self.scan, Path(path), allow_missing=True)
                self.refresh()
                self.status.configure(
                    text=f"Применено: {applied}. Отсутствуют или изменились: {skipped}."
                )
            except (OSError, ValueError) as exc:
                messagebox.showerror("Не удалось загрузить разметку", str(exc), parent=self.root)

    def edit_selected(self):
        if self.busy or not self.scan:
            return
        selected = self.table.selection()
        if not selected:
            messagebox.showinfo(
                "Выбери файлы", "Выдели одну или несколько строк в таблице.", parent=self.root
            )
            return
        items = [self.scan.items[int(i)] for i in selected]
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Назначение · {len(items)} файлов")
        dialog.configure(bg="#f3f5ef")
        dialog.transient(self.root)
        panel = ttk.Frame(dialog, padding=24)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text=f"Выбрано файлов: {len(items)}", font=("Helvetica", 14, "bold")).pack(
            anchor="w", pady=(0, 16)
        )
        ttk.Label(panel, text="Код образца (пусто = без образца)").pack(anchor="w")
        sample = tk.StringVar(value=items[0].sample if len({i.sample for i in items}) == 1 else "")
        change_sample = tk.BooleanVar(value=len({i.sample for i in items}) == 1)
        sample.trace_add("write", lambda *_: change_sample.set(True))
        ttk.Checkbutton(panel, text="Изменить код образца", variable=change_sample).pack(anchor="w")
        sample_entry = ttk.Entry(panel, textvariable=sample, width=38)
        sample_entry.pack(fill="x", pady=(7, 14))
        ttk.Label(panel, text="Тип материала").pack(anchor="w")
        kind = tk.StringVar(
            value=items[0].kind if len({i.kind for i in items}) == 1 else "Не менять"
        )
        kind_box = ttk.Combobox(
            panel, textvariable=kind, values=("Не менять", *KINDS), state="readonly"
        )
        kind_box.pack(fill="x", pady=(7, 14))
        include = tk.StringVar(value="Не менять")
        ttk.Label(panel, text="Добавление в ZIP").pack(anchor="w")
        ttk.Combobox(
            panel,
            textvariable=include,
            values=("Не менять", "Включить", "Исключить"),
            state="readonly",
        ).pack(fill="x", pady=(7, 0))
        ttk.Label(
            panel,
            text="Одинаковое содержимое не означает один образец.\nРазметку можно сохранить в JSON из главного окна.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=15)

        def save():
            value = sample.get().strip()
            if len(value) > 80:
                messagebox.showerror("Слишком длинный код", "Максимум 80 символов.", parent=dialog)
                return
            for item in items:
                if change_sample.get():
                    item.sample = value
                if include.get() != "Не менять":
                    item.include = include.get() == "Включить"
                if kind.get() != "Не менять":
                    item.kind = kind.get()
                item.warnings = ["Назначение проверено пользователем"]
            dialog.destroy()
            self.refresh()

        ttk.Button(panel, text="Применить", command=save, style="Accent.TButton").pack(fill="x")
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        dialog.bind("<Return>", lambda _: save())
        dialog.grab_set()
        sample_entry.focus_set()

    def build(self):
        if not self.scan:
            return
        if not any(item.include for item in self.scan.items):
            messagebox.showinfo("Нечего упаковывать", "Включи хотя бы один файл.", parent=self.root)
            return
        parent = filedialog.askdirectory(
            title="Где создать новую папку пакета? Вне исходной папки.", parent=self.root
        )
        if not parent:
            return
        destination = Path(parent) / (
            "ChemPack_" + datetime.now().strftime("%Y-%m-%d_%H-%M") + "_" + secrets.token_hex(2)
        )
        required = self.required_types()
        self.run_job(
            lambda: build_package(
                self.scan, destination, required, self.report_progress, self.cancel
            ),
            "build",
        )

    def open_catalog(self):
        if self.package:
            webbrowser.open((self.package / "index.html").as_uri())

    def open_folder(self):
        if not self.package:
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(self.package)])
        elif sys.platform == "win32":
            import os

            os.startfile(self.package)
        else:
            subprocess.Popen(["xdg-open", str(self.package)])

    def close(self):
        if self.busy:
            messagebox.showinfo(
                "Сначала останови операцию",
                "Нажми «Отмена» и дождись остановки, затем закрой окно.",
                parent=self.root,
            )
            return
        if self.demo:
            self.demo.cleanup()
        self.root.destroy()


def launch():
    root = tk.Tk()
    App(root)
    root.mainloop()
