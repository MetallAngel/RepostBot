import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from repostbot import update


def package(version, extra=None):
    files = {name: b"new code" for name in sorted(update.REQUIRED)}
    files["version.json"] = json.dumps({"version": version}).encode()
    files.update(extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in files.items():
            archive.writestr("RepostBot/" + name, raw)
    return buffer.getvalue()


class UpdateTests(unittest.TestCase):
    def test_rejects_unsafe_and_old_archives(self):
        with self.assertRaises(ValueError):
            update.inspect_package(package(update.CURRENT_VERSION))
        with self.assertRaises(ValueError):
            update.inspect_package(package("9.0.0", {"../data/bot.sqlite3": b"bad"}))
        with self.assertRaises(ValueError):
            update.inspect_package(package("9.0.0", {"data/bot.sqlite3": b"bad"}))

    def test_apply_preserves_runtime_and_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            (data / "updates").mkdir(parents=True)
            (data / "bot.sqlite3").write_bytes(b"settings and tokens")
            (root / ".runtime").mkdir()
            (root / ".runtime" / "python.exe").write_bytes(b"python runtime")
            (root / "repostbot").mkdir()
            (root / "repostbot" / "app.py").write_bytes(b"old code")
            with patch.object(update, "ROOT", root):
                version = update.stage_package(package("9.0.0", {"start_linux.sh": b"#!/usr/bin/env bash\n"}), data)
                self.assertEqual(version, "9.0.0")
                (data / "updates" / "apply-requested.txt").write_text(version)
                self.assertEqual(update.apply_staged(data), "9.0.0")
            self.assertEqual((root / "repostbot" / "app.py").read_bytes(), b"new code")
            self.assertEqual((root / "start_linux.sh").read_bytes(), b"#!/usr/bin/env bash\n")
            self.assertEqual((data / "bot.sqlite3").read_bytes(), b"settings and tokens")
            self.assertEqual((root / ".runtime" / "python.exe").read_bytes(), b"python runtime")
            self.assertFalse((data / "updates" / "pending.zip").exists())
            self.assertFalse((data / "updates" / "apply-requested.txt").exists())

    def test_failed_install_rolls_back_code_and_retains_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            (root / "repostbot").mkdir()
            (root / "repostbot" / "app.py").write_bytes(b"old code")
            update.stage_package(package("9.0.0"), data)
            real_replace = update.os.replace
            calls = 0
            def fail_second(source, target):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("test disk error")
                return real_replace(source, target)
            with patch.object(update, "ROOT", root), patch("repostbot.update.os.replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    update.apply_staged(data)
            self.assertTrue((data / "updates" / "pending.zip").exists())
            # Whichever existing file was touched first is restored; no version bump.
            self.assertEqual((root / "repostbot" / "app.py").read_bytes(), b"old code")


if __name__ == "__main__":
    unittest.main()
