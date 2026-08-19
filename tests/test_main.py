from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lol_support_advisor import main as app_main


class MainPathTests(unittest.TestCase):
    def test_source_run_uses_repository_root(self) -> None:
        with patch.object(app_main.sys, "frozen", False, create=True):
            self.assertEqual(
                app_main.project_root(),
                Path(app_main.__file__).resolve().parent.parent,
            )

    def test_frozen_run_keeps_data_beside_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "LOL-Support-Advisor-v0.1.0.exe"
            with (
                patch.object(app_main.sys, "frozen", True, create=True),
                patch.object(app_main.sys, "executable", str(executable)),
            ):
                self.assertEqual(app_main.project_root(), executable.parent)

    def test_frozen_resources_are_loaded_from_meipass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(app_main.sys, "frozen", True, create=True),
                patch.object(app_main.sys, "_MEIPASS", temp_dir, create=True),
            ):
                self.assertEqual(
                    app_main.resource_root(), Path(temp_dir).resolve(),
                )


if __name__ == "__main__":
    unittest.main()
