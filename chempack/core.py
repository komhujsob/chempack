"""Read-only source scanning and verified, collision-safe package creation."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import re
import shutil
import stat
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

EXTENSIONS = {
    ".pdf",
    ".mnova",
    ".mnpub",
    ".csv",
    ".tsv",
    ".txt",
    ".md",
    ".rtf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".doc",
    ".xls",
    ".ppt",
    ".ods",
    ".odt",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".svg",
    ".dx",
    ".jdx",
    ".zip",
    ".cdx",
    ".cdxml",
    ".sdf",
    ".mol",
    ".mol2",
    ".cif",
    ".xyz",
    ".log",
    ".out",
}
SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "build", "dist"}
KINDS = (
    "1H",
    "13C",
    "11B",
    "19F",
    "31P",
    "2D",
    "HRMS",
    "UV-Vis",
    "PL",
    "IR",
    "Структура",
    "Другое",
    "Проверить",
)
MAX_FILE = 512 * 1024 * 1024
MAX_TOTAL = 2 * 1024 * 1024 * 1024
MAX_FILES = 5000
CHUNK = 1024 * 1024


class Cancelled(Exception):
    pass


@dataclass
class Item:
    relative: str
    size: int
    sha256: str
    mtime_ns: int
    sample: str = ""
    kind: str = "Другое"
    include: bool = True
    warnings: list[str] = field(default_factory=list)
    stored: str = ""


@dataclass
class Scan:
    root: Path
    items: list[Item]
    skipped: list[str]


def check_cancel(cancel):
    if cancel and cancel.is_set():
        raise Cancelled("Операция отменена.")


def open_regular(path: Path):
    """Open without following a symlink, then verify the descriptor is a regular file."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Это не обычный файл: {path.name}")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def digest_file(path: Path, cancel=None) -> tuple[str, os.stat_result]:
    digest = hashlib.sha256()
    with open_regular(path) as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > MAX_FILE:
            raise ValueError(f"Файл больше 512 МБ: {path.name}")
        read = 0
        while chunk := stream.read(CHUNK):
            check_cancel(cancel)
            read += len(chunk)
            if read > MAX_FILE:
                raise ValueError(f"Файл вырос больше 512 МБ: {path.name}")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (
        after.st_size,
        after.st_mtime_ns,
    ) or read != before.st_size:
        raise ValueError(f"Файл изменился во время чтения: {path.name}. Повтори сканирование.")
    return digest.hexdigest(), before


def identify(relative: str) -> tuple[str, str, list[str]]:
    path = Path(relative)
    name = unicodedata.normalize("NFKC", path.stem)
    # These are filename hints, not interpretation of a spectrum or proof of identity.
    normalized = name.replace("¹", "1").replace("³", "3").replace("⁹", "9")
    kinds = []
    for mass, atom in (("1", "H"), ("13", "C"), ("11", "B"), ("19", "F"), ("31", "P")):
        pattern = (
            rf"(?<![A-Za-z0-9])(?:{mass}\s*[-_]?\s*{atom}|{atom}\s*[-_]?\s*{mass})(?![A-Za-z0-9])"
        )
        if re.search(pattern, normalized, re.I):
            kinds.append(mass + atom)
    for kind, pattern in (
        ("2D", r"\b(?:cosy|hsqc|hmbc|noesy|roesy|tocsy)\b"),
        ("HRMS", r"\b(?:hrms|hr-ms|mass|ms)\b"),
        ("UV-Vis", r"\b(?:uv[- ]?vis|uv|absorption)\b"),
        ("PL", r"\b(?:pl|emission|fluorescence)\b"),
        ("IR", r"\b(?:ftir|ft-ir|ir)\b"),
    ):
        if re.search(pattern, normalized.replace("_", " "), re.I):
            kinds.append(kind)
    if path.suffix.casefold() in {".cdx", ".cdxml", ".sdf", ".mol", ".mol2", ".cif", ".xyz"}:
        kinds.append("Структура")
    kinds = list(dict.fromkeys(kinds))
    kind = kinds[0] if len(kinds) == 1 else "Проверить" if kinds else "Другое"
    warnings = ["Несколько типов в имени: " + ", ".join(kinds)] if len(kinds) > 1 else []
    samples = set()
    for segment in [name, *reversed(path.parts[:-1])]:
        segment = unicodedata.normalize("NFKC", segment)
        for match in re.finditer(
            r"(?i)(?:compound|sample|образец|соединение)[\s_-]*(\d{1,4}[a-z]?)\b", segment
        ):
            samples.add(match[1].upper())
        for match in re.finditer(
            r"(?<![A-Za-z0-9])([A-Za-z]{1,6}[-_]\d{1,4}[A-Za-z]?)(?![A-Za-z0-9])", segment
        ):
            candidate = match[1].replace("_", "-").upper()
            if not candidate.startswith(("SAMPLE-", "COMPOUND-")) and candidate not in {
                "H-1",
                "C-13",
                "B-11",
                "F-19",
                "P-31",
            }:
                samples.add(candidate)
        # A separator such as '_' is a valid boundary for filenames.
        for match in re.finditer(r"(?:^|[_\s(])([A-Za-z]{1,4}\d{2,4}[a-z]?)(?=$|[_\s).])", segment):
            candidate = match[1].upper()
            if candidate not in {"H1", "C13", "B11", "F19", "P31"}:
                samples.add(candidate)
        if samples:
            break
    if len(samples) > 1:
        warnings.append("Несколько кодов образца: " + ", ".join(sorted(samples)))
    sample = next(iter(samples)) if len(samples) == 1 else ""
    if not sample:
        warnings.append("Код образца не определён — назначь вручную")
    if kind == "Другое":
        warnings.append("Тип материала не определён по имени")
    return sample, kind, warnings


def scan_folder(root: Path | str, progress=None, cancel=None) -> Scan:
    root = Path(root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Выбери папку с материалами.")
    items, skipped = [], []
    total = 0
    for folder, directories, filenames in os.walk(root, followlinks=False):
        check_cancel(cancel)
        current = Path(folder)
        if "acqus" in filenames and any(name in filenames for name in ("fid", "ser")):
            skipped.append(
                str(current.relative_to(root)) + "/ — сырые Bruker-данные: добавь их готовым ZIP"
            )
            directories[:] = []
            continue
        if (current / "chempack-manifest.json").is_file():
            skipped.append(str(current.relative_to(root)) + "/ — уже готовый пакет")
            directories[:] = []
            continue
        kept = []
        for name in sorted(directories):
            p = current / name
            if name.startswith(".") or name in SKIP_DIRS or p.is_symlink():
                skipped.append(str(p.relative_to(root)) + "/ — служебная папка или ссылка")
            else:
                kept.append(name)
        directories[:] = kept
        for name in sorted(filenames):
            check_cancel(cancel)
            path = current / name
            relative = str(path.relative_to(root))
            if name.startswith((".", "~$")) or path.is_symlink():
                skipped.append(relative + " — служебный файл или ссылка")
                continue
            if path.suffix.casefold() not in EXTENSIONS:
                skipped.append(relative + " — неподдерживаемое расширение")
                continue
            if len(items) >= MAX_FILES:
                raise ValueError("В папке больше 5000 подходящих файлов. Выбери отдельный проект.")
            if progress:
                progress(f"Читаю: {relative}")
            sha, info = digest_file(path, cancel)
            total += info.st_size
            if total > MAX_TOTAL:
                raise ValueError("Материалы занимают больше 2 ГБ. Выбери папку поменьше.")
            sample, kind, warnings = identify(relative)
            items.append(
                Item(relative, info.st_size, sha, info.st_mtime_ns, sample, kind, warnings=warnings)
            )
    if not items:
        raise ValueError("В этой папке не нашлось подходящих файлов.")
    return Scan(root, items, skipped)


def summary(scan: Scan, required=("1H", "13C")) -> dict:
    selected = [item for item in scan.items if item.include]
    samples = defaultdict(list)
    duplicates = defaultdict(list)
    for item in selected:
        if item.sample:
            samples[item.sample].append(item)
        duplicates[item.sha256].append(item)
    groups = [group for group in duplicates.values() if len(group) > 1]
    missing = {
        sample: [kind for kind in required if not any(item.kind == kind for item in items)]
        for sample, items in sorted(samples.items())
    }
    return {
        "files": len(selected),
        "samples": len(samples),
        "unassigned": sum(not item.sample for item in selected),
        "unknown_type": sum(item.kind in {"Другое", "Проверить"} for item in selected),
        "duplicate_copies": sum(len(group) - 1 for group in groups),
        "duplicate_groups": [[item.relative for item in group] for group in groups],
        "cross_sample_duplicates": [
            [item.relative for item in group]
            for group in groups
            if len({item.sample for item in group}) > 1
        ],
        "missing": missing,
        "required": list(required),
        "bytes": sum(item.size for item in selected),
    }


def portable_name(name: str) -> str:
    name = unicodedata.normalize("NFC", str(name))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if name.upper().split(".")[0] in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *{f"COM{i}" for i in range(10)},
        *{f"LPT{i}" for i in range(10)},
    }:
        name = "_" + name
    return name[:100].rstrip(" .") or "untitled"


def checked_source(scan: Scan, item: Item) -> Path:
    relative = Path(item.relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Недопустимый путь в списке файлов.")
    source = scan.root / relative
    # Check every component: a directory might have been replaced with a symlink since scanning.
    if any(
        parent.is_symlink() for parent in [source, *source.parents] if parent != scan.root.parent
    ):
        raise ValueError(f"Путь стал символической ссылкой: {item.relative}")
    if not source.resolve(strict=True).is_relative_to(scan.root):
        raise ValueError("Файл вышел за пределы исходной папки.")
    return source


def copy_verified(scan: Scan, item: Item, output: Path, cancel=None):
    source = checked_source(scan, item)
    digest = hashlib.sha256()
    with open_regular(source) as incoming, output.open("xb") as outgoing:
        info = os.fstat(incoming.fileno())
        if (info.st_size, info.st_mtime_ns) != (item.size, item.mtime_ns):
            raise ValueError(
                f"После сканирования изменился файл: {item.relative}. Повтори сканирование."
            )
        count = 0
        while chunk := incoming.read(CHUNK):
            check_cancel(cancel)
            count += len(chunk)
            if count > item.size:
                raise ValueError(f"Файл вырос во время упаковки: {item.relative}")
            digest.update(chunk)
            outgoing.write(chunk)
    if count != item.size or digest.hexdigest() != item.sha256:
        raise ValueError(f"Содержимое изменилось: {item.relative}. Повтори сканирование.")


def safe_csv(value) -> str:
    text = str(value)
    return (
        "'" + text
        if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n"))
        else text
    )


def render_catalog(manifest: dict) -> str:
    escape = html.escape
    stats = manifest["summary"]
    groups = defaultdict(list)
    for item in manifest["files"]:
        groups[item["sample"] or "Без образца"].append(item)
    cards = []
    for sample, items in sorted(groups.items()):
        missing = stats["missing"].get(sample, [])
        notice = (
            "Не найдено по пометкам: " + ", ".join(missing)
            if missing
            else "Есть все выбранные типы"
            if sample != "Без образца"
            else "Назначение файлов не подтверждено"
        )
        links = "".join(
            '<tr><td><a href="'
            + quote(item["stored"], safe="/")
            + '">'
            + escape(item["relative"])
            + "</a></td><td>"
            + escape(item["kind"])
            + "</td><td>"
            + f"{item['size'] / 1024:.1f} КБ"
            + "</td><td><code>"
            + item["sha256"][:12]
            + "</code></td></tr>"
            for item in items
        )
        cards.append(
            "<details open><summary>"
            + escape(sample)
            + "<span>"
            + str(len(items))
            + ' файлов</span></summary><p class="notice">'
            + escape(notice)
            + '</p><div class="table"><table><thead><tr><th>Исходный файл → открыть</th><th>Тип</th><th>Размер</th><th>SHA-256</th></tr></thead><tbody>'
            + links
            + "</tbody></table></div></details>"
        )
    duplicates = "".join(
        "<li>" + escape(" = ".join(group)) + "</li>" for group in stats["duplicate_groups"]
    )
    if not duplicates:
        duplicates = "<li>Совпадений содержимого нет.</li>"
    return (
        """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ChemPack — пакет материалов</title>
<style>*{box-sizing:border-box}body{margin:0;background:#f3f5ef;color:#183b36;font:14px/1.7 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}main{max-width:1100px;margin:auto;padding:42px 24px}header{display:flex;justify-content:space-between;border-bottom:1px solid #d5dfd1;padding-bottom:18px}.brand{font-size:23px;font-weight:650}.tag{font:11px monospace;color:#6c8074}h1{font-size:36px;line-height:1.2;letter-spacing:-1px;margin:30px 0 13px}p{color:#6c8074}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:25px 0}.stats div{border:1px solid #d5dfd1;padding:16px;background:white;border-radius:10px}.stats strong{display:block;font-size:31px;font-weight:500}.stats span{font-size:11px;color:#6c8074}details{background:#fff;border:1px solid #d5dfd1;border-radius:10px;padding:18px;margin-bottom:14px}summary{cursor:pointer;font-size:18px;font-weight:550}summary span{float:right;font-size:11px;font-weight:400;color:#6c8074}.notice{font-size:12px}.table{overflow:auto}table{width:100%;border-collapse:collapse;text-align:left}td,th{font-size:12px;padding:10px 8px;border-bottom:1px solid #e8ede4;overflow-wrap:anywhere}th{font-size:10px;color:#6c8074}a{color:#177761}code{font:10px monospace}footer{font-size:11px;margin-top:25px;color:#6c8074}.note{background:#e7efdd;border-radius:10px;padding:17px;font-size:12px;margin:25px 0}li{font-size:12px;overflow-wrap:anywhere}@media(max-width:650px){.stats{grid-template-columns:1fr 1fr}h1{font-size:28px}header{gap:10px;flex-wrap:wrap}}</style><main><header><span class="brand">⬡ ChemPack</span><span class="tag">RESEARCH HANDOFF / LOCAL FIRST</span></header><h1>Материалы собраны.<br>Можно передавать.</h1><p>Каталог файлов, их назначений и контрольных сумм. Ссылки работают после распаковки архива.</p><div class="stats">"""
        + "".join(
            "<div><strong>" + str(value) + "</strong><span>" + title + "</span></div>"
            for value, title in [
                (stats["files"], "исходных файлов"),
                (stats["samples"], "кодов образцов"),
                (stats["duplicate_copies"], "копий с тем же содержимым"),
                (stats["unassigned"], "файлов без кода образца"),
            ]
        )
        + '</div><div class="note">Типы и коды определяются по именам и ручным пометкам, а не из содержимого спектров. Отсутствие пометки не означает отсутствие эксперимента. Одинаковые файлы у разных образцов требуют проверки. Контрольные суммы проверены при сборке.</div>'
        + "".join(cards)
        + "<details><summary>Совпадения содержимого</summary><ul>"
        + duplicates
        + "</ul></details><footer>Создано UTC: "
        + escape(manifest["created_utc"])
        + '<br>Полный реестр: <a href="chempack-manifest.json">JSON</a> · <a href="files.csv">CSV</a><br>В каталоге нет абсолютных путей исходного компьютера.</footer></main></html>'
    )


def build_package(
    scan: Scan, destination: Path | str, required=("1H", "13C"), progress=None, cancel=None
) -> Path:
    destination = Path(destination).expanduser().absolute()
    if destination.resolve().is_relative_to(scan.root):
        raise ValueError(
            "Создай пакет вне исходной папки, чтобы не упаковать старые результаты повторно."
        )
    selected = [item for item in scan.items if item.include]
    if not selected:
        raise ValueError("Не выбрано ни одного файла.")
    if destination.exists():
        raise FileExistsError("Папка назначения уже существует. Выбери новое имя.")
    destination.mkdir(parents=False, exist_ok=False)
    packaged = []
    used = set()
    try:
        for item in selected:
            check_cancel(cancel)
            if progress:
                progress(f"Копирую: {item.relative}")
            folder = (
                Path("samples")
                / portable_name(item.sample or "Без образца")
                / portable_name(item.kind)
            )
            original = Path(item.relative).name
            filename = portable_name(original)
            candidate = folder / filename
            suffix = 1
            # Case-insensitive uniqueness also works on Windows/macOS and after Unicode normalization.
            while unicodedata.normalize("NFC", candidate.as_posix()).casefold() in used:
                suffix += 1
                stem, ext = Path(filename).stem, Path(filename).suffix
                candidate = folder / f"{stem[:70]}__{suffix}{ext}"
            used.add(unicodedata.normalize("NFC", candidate.as_posix()).casefold())
            target = destination / candidate
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_verified(scan, item, target, cancel)
            record = asdict(item)
            record["stored"] = candidate.as_posix()
            # mtime is an implementation detail, not an experimental timestamp.
            record.pop("mtime_ns")
            packaged.append(record)
        manifest = {
            "schema_version": 1,
            "tool": "ChemPack 0.1.0",
            "created_utc": datetime.now(UTC).isoformat(),
            "summary": summary(scan, required),
            "files": packaged,
            "skipped": scan.skipped,
            "policy": "Filename hints reviewed by the user; originals copied byte-for-byte; all selected duplicates preserved.",
        }
        (destination / "chempack-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (destination / "index.html").write_text(render_catalog(manifest), encoding="utf-8")
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            ["sample", "kind", "original_relative", "packaged_relative", "bytes", "sha256"]
        )
        for item in packaged:
            writer.writerow(
                [
                    safe_csv(item[key])
                    for key in ("sample", "kind", "relative", "stored", "size", "sha256")
                ]
            )
        (destination / "files.csv").write_text(output.getvalue(), encoding="utf-8-sig")
        if progress:
            progress("Создаю ZIP-архив…")
        paths = sorted(path for path in destination.rglob("*") if path.is_file())
        with zipfile.ZipFile(
            destination / "ChemPack.zip", "x", zipfile.ZIP_DEFLATED, compresslevel=4
        ) as archive:
            for path in paths:
                check_cancel(cancel)
                archive.write(path, path.relative_to(destination).as_posix())
        return destination
    except BaseException:
        # Only a new directory created by this call can be removed here.
        shutil.rmtree(destination)
        raise


def create_demo(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "Участник-А/BDP-01_1H.txt": "DEMO ONLY — synthetic placeholder, not an experimental spectrum.\n1H test attachment.\n",
        "Участник-Б/BDP-01_1H_copy.txt": "DEMO ONLY — synthetic placeholder, not an experimental spectrum.\n1H test attachment.\n",
        "Участник-А/BDP-01_13C.txt": "DEMO ONLY — synthetic placeholder.\n13C test attachment.\n",
        "Участник-В/BDP-02_1H.txt": "DEMO ONLY — synthetic placeholder for sample 2.\n",
        "Участник-В/BDP-02_19F.txt": "DEMO ONLY — synthetic placeholder for 19F.\n",
        "Участник-В/BDP-02_HRMS.txt": "DEMO ONLY — synthetic mass attachment.\n",
        "обсудить.md": "DEMO: файл без кода образца — проверить вручную.\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
