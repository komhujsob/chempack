import hashlib
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from chempack.core import (
    Cancelled,
    build_package,
    create_demo,
    identify,
    portable_name,
    render_catalog,
    scan_folder,
    summary,
)


class TestChemPack(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()

    def file(self, name, content="DEMO\n"):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_identification(self):
        cases = [
            ("BDP-01_1H.txt", "BDP-01", "1H"),
            ("BDP_012_13C.pdf", "BDP-012", "13C"),
            ("BDP001_19F.txt", "BDP001", "19F"),
            ("sample_12/HRMS.csv", "12", "HRMS"),
            ("BDP-02/cosy.pdf", "BDP-02", "2D"),
            ("BDP-02_¹³C.pdf", "BDP-02", "13C"),
            ("BDP-02_11B.pdf", "BDP-02", "11B"),
            ("BDP-01_C-13.pdf", "BDP-01", "13C"),
            ("BDP-02_13C_1H.pdf", "BDP-02", "Проверить"),
            ("BDP-01+BDP-02_1H.txt", "", "1H"),
        ]
        for name, sample, kind in cases:
            with self.subTest(name=name):
                actual = identify(name)
                self.assertEqual((actual[0], actual[1]), (sample, kind))

    def test_does_not_guess_plain_numbers_as_compounds(self):
        self.assertEqual(identify("2026/400/13C.txt")[0], "")

    def test_ignores_hidden_and_symlink_and_unknown(self):
        self.file("BDP-01_1H.txt")
        self.file(".secret.txt")
        self.file("x.exe")
        self.file(".git/notes.txt")
        self.file("raw/1/acqus", "##$NUC1= <1H>")
        self.file("raw/1/fid", "rawdata")
        outside = self.base / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        (self.source / "link.txt").symlink_to(outside)
        result = scan_folder(self.source)
        self.assertEqual(len(result.items), 1)
        self.assertTrue(any("Bruker" in s for s in result.skipped))
        self.assertGreaterEqual(len(result.skipped), 5)

    def test_summary_missing_and_duplicates(self):
        create_demo(self.source)
        result = summary(scan_folder(self.source))
        self.assertEqual(result["files"], 7)
        self.assertEqual(result["samples"], 2)
        self.assertEqual(result["duplicate_copies"], 1)
        self.assertEqual(result["unassigned"], 1)
        self.assertEqual(result["missing"], {"BDP-01": [], "BDP-02": ["13C"]})

    def test_build_copies_every_selected_original_exactly(self):
        create_demo(self.source)
        scan = scan_folder(self.source)
        original = {i.relative: (self.source / i.relative).read_bytes() for i in scan.items}
        output = build_package(scan, self.base / "output")
        manifest = json.loads((output / "chempack-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["files"]), 7)
        self.assertNotIn(str(self.source), json.dumps(manifest))
        with zipfile.ZipFile(output / "ChemPack.zip") as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("index.html", archive.namelist())
            self.assertNotIn("ChemPack.zip", archive.namelist())
            for item in manifest["files"]:
                expected = original[item["relative"]]
                self.assertEqual((output / item["stored"]).read_bytes(), expected)
                self.assertEqual(archive.read(item["stored"]), expected)
                self.assertEqual(hashlib.sha256(expected).hexdigest(), item["sha256"])
                self.assertEqual((self.source / item["relative"]).read_bytes(), expected)

    def test_duplicate_filenames_and_portable_collisions(self):
        self.file("a/BDP-01_1H.txt", "A")
        self.file("b/bdp-01_1h.txt", "B")
        scan = scan_folder(self.source)
        output = build_package(scan, self.base / "output")
        files = json.loads((output / "chempack-manifest.json").read_text())["files"]
        self.assertEqual(len({i["stored"].casefold() for i in files}), 2)
        self.assertEqual({(output / i["stored"]).read_text() for i in files}, {"A", "B"})

    def test_changed_source_rolls_back_package(self):
        path = self.file("BDP-01_1H.txt", "Before")
        scan = scan_folder(self.source)
        path.write_text("After, changed", encoding="utf-8")
        with self.assertRaises(ValueError):
            build_package(scan, self.base / "output")
        self.assertFalse((self.base / "output").exists())
        self.assertEqual(path.read_text(), "After, changed")

    def test_existing_destination_is_never_removed(self):
        self.file("BDP-01_1H.txt")
        destination = self.base / "exists"
        destination.mkdir()
        (destination / "keep.txt").write_text("KEEP")
        with self.assertRaises(FileExistsError):
            build_package(scan_folder(self.source), destination)
        self.assertEqual((destination / "keep.txt").read_text(), "KEEP")

    def test_destination_inside_source_rejected(self):
        self.file("BDP-01_1H.txt")
        with self.assertRaises(ValueError):
            build_package(scan_folder(self.source), self.source / "new")

    def test_symlink_replacement_after_scan_rejected(self):
        source = self.file("BDP-01_1H.txt", "same")
        scan = scan_folder(self.source)
        outside = self.base / "outside.txt"
        outside.write_text("same")
        source.unlink()
        source.symlink_to(outside)
        with self.assertRaises(ValueError):
            build_package(scan, self.base / "output")
        self.assertFalse((self.base / "output").exists())

    def test_tampered_relative_path_rejected(self):
        self.file("BDP-01_1H.txt")
        scan = scan_folder(self.source)
        scan.items[0].relative = "../outside.txt"
        with self.assertRaises(ValueError):
            build_package(scan, self.base / "output")

    def test_excluded_files_are_not_shared(self):
        self.file("BDP-01_1H.txt", "include")
        self.file("private.txt", "exclude")
        scan = scan_folder(self.source)
        for item in scan.items:
            item.include = item.relative != "private.txt"
        output = build_package(scan, self.base / "output")
        manifest = json.loads((output / "chempack-manifest.json").read_text())
        self.assertEqual(len(manifest["files"]), 1)
        self.assertNotIn("private.txt", json.dumps(manifest))

    def test_cancelled_build_cleans_up_only_its_directory(self):
        self.file("BDP-01_1H.txt")
        scan = scan_folder(self.source)
        event = threading.Event()
        event.set()
        with self.assertRaises(Cancelled):
            build_package(scan, self.base / "output", cancel=event)
        self.assertFalse((self.base / "output").exists())
        self.assertTrue(self.source.exists())

    def test_html_escapes_metadata(self):
        self.file("BDP-01_1H.txt")
        scan = scan_folder(self.source)
        scan.items[0].sample = "<script>alert(1)</script>"
        output = build_package(scan, self.base / "output")
        manifest = json.loads((output / "chempack-manifest.json").read_text())
        html = render_catalog(manifest)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_csv_formula_escaped_original_retained_in_json(self):
        self.file("BDP-01_1H.txt")
        scan = scan_folder(self.source)
        scan.items[0].sample = "=1+1"
        output = build_package(scan, self.base / "output")
        self.assertIn("'=1+1", (output / "files.csv").read_text(encoding="utf-8-sig"))
        self.assertEqual(
            json.loads((output / "chempack-manifest.json").read_text())["files"][0]["sample"],
            "=1+1",
        )

    def test_portable_names(self):
        for name in ("../evil", "CON.txt", "NUL", "F:oo/Bar", "", ".."):
            result = portable_name(name)
            self.assertNotIn("/", result)
            self.assertNotIn(":", result)
            self.assertNotIn(result, {"", ".", "..", "NUL", "CON.txt"})

    def test_empty_folder(self):
        with self.assertRaises(ValueError):
            scan_folder(self.source)

    def test_empty_selection(self):
        self.file("BDP-01_1H.txt")
        scan = scan_folder(self.source)
        scan.items[0].include = False
        with self.assertRaises(ValueError):
            build_package(scan, self.base / "output")
        self.assertFalse((self.base / "output").exists())


if __name__ == "__main__":
    unittest.main()
