from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core import Cancelled, build_package, create_demo, load_assignments, scan_folder, summary


def main():
    parser = argparse.ArgumentParser(
        description="ChemPack — собрать материалы проекта в проверенный пакет"
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("gui", help="Открыть окно приложения (по умолчанию)")
    demo = sub.add_parser("demo", help="Создать синтетические демо-файлы")
    demo.add_argument("folder", type=Path)
    for command in ("scan", "pack"):
        run = sub.add_parser(
            command, help="Проверить папку" if command == "scan" else "Собрать пакет"
        )
        run.add_argument("folder", type=Path)
        run.add_argument(
            "--require",
            default="1H,13C",
            help="Ожидаемые типы через запятую; пустая строка отключает проверку",
        )
        run.add_argument(
            "--assign", type=Path, help="JSON-массив назначений: relative, sample, kind, include"
        )
        if command == "pack":
            run.add_argument("--out", type=Path, required=True, help="Новая папка вне исходной")
    args = parser.parse_args()
    if args.command in (None, "gui"):
        try:
            from .gui import launch

            launch()
        except ImportError as exc:
            parser.exit(1, f"Нет Tkinter: {exc}. Установи Python с Tk или используй scan/pack.\n")
        return
    try:
        if args.command == "demo":
            if args.folder.exists():
                raise ValueError("Папка демо уже существует. Выбери новое имя.")
            create_demo(args.folder)
            print(f"Демо-файлы: {args.folder.resolve()}")
            return
        scan = scan_folder(args.folder)
        if args.assign:
            load_assignments(scan, args.assign)
        required = tuple(dict.fromkeys(v.strip() for v in args.require.split(",") if v.strip()))
        if args.command == "scan":
            result = {
                "summary": summary(scan, required),
                "files": [
                    {
                        "relative": item.relative,
                        "sample": item.sample,
                        "kind": item.kind,
                        "include": item.include,
                        "warnings": item.warnings,
                    }
                    for item in scan.items
                ],
                "skipped": scan.skipped,
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            result = build_package(scan, args.out, required)
            print(
                f"Готово: {result}\nДля команды: {result / 'ChemPack.zip'}\nКаталог: {result / 'index.html'}"
            )
    except (ValueError, OSError, Cancelled) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    main()
