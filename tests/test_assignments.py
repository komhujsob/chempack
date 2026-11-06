import json
import tempfile
import unittest
from pathlib import Path

from chempack.core import load_assignments, save_assignments, scan_folder


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.file = self.source / "A.txt"
        self.file.write_text("sample", encoding="utf-8")
        self.profile = self.root / "assignments.json"

    def test_round_trip_and_changed_input(self):
        scan = scan_folder(self.source)
        scan.items[0].sample = "BDP-01"
        scan.items[0].kind = "1H"
        scan.items[0].include = False
        save_assignments(scan, self.profile)

        fresh = scan_folder(self.source)
        self.assertEqual(load_assignments(fresh, self.profile), (1, 0))
        item = fresh.items[0]
        self.assertEqual((item.sample, item.kind, item.include), ("BDP-01", "1H", False))

        self.file.write_text("new content", encoding="utf-8")
        changed = scan_folder(self.source)
        self.assertEqual(load_assignments(changed, self.profile, allow_missing=True), (0, 1))
        self.assertTrue(changed.items[0].include)

    def test_invalid_later_entry_does_not_apply_earlier_changes(self):
        (self.source / "B.txt").write_text("second sample", encoding="utf-8")
        self.profile.write_text(
            json.dumps(
                [
                    {"relative": "A.txt", "include": False},
                    {"relative": "B.txt", "include": "false"},
                ]
            ),
            encoding="utf-8",
        )
        scan = scan_folder(self.source)
        with self.assertRaises(ValueError):
            load_assignments(scan, self.profile)
        self.assertTrue(all(item.include for item in scan.items))

    def test_legacy_partial_profile(self):
        self.profile.write_text('[{"relative":"A.txt","sample":"A01"}]', encoding="utf-8")
        scan = scan_folder(self.source)
        self.assertEqual(load_assignments(scan, self.profile), (1, 0))
        self.assertEqual(scan.items[0].sample, "A01")
        self.assertTrue(scan.items[0].include)
