from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from codex_telegram_remote.cli import _normalize_command, main


class CliTests(unittest.TestCase):
    def test_normalizes_user_facing_commands(self) -> None:
        self.assertEqual(
            ["--check", "--env", "custom.env"],
            _normalize_command(["check", "--env", "custom.env"]),
        )
        self.assertEqual(
            ["--discover-user-id"],
            _normalize_command(["discover-user-id"]),
        )
        self.assertEqual(
            ["--env", "custom.env"],
            _normalize_command(["run", "--env", "custom.env"]),
        )

    def test_init_creates_private_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "config.env"
            result = main(["init", "--env", str(target)])
            content = target.read_text(encoding="utf-8")
            mode = stat.S_IMODE(target.stat().st_mode)

        self.assertEqual(0, result)
        self.assertIn("CODEX_REMOTE_BOT_TOKEN=", content)
        self.assertEqual(0o600, mode)

    def test_init_refuses_to_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "config.env"
            target.write_text("existing", encoding="utf-8")
            result = main(["init", "--env", str(target)])
            content = target.read_text(encoding="utf-8")

        self.assertEqual(2, result)
        self.assertEqual("existing", content)


if __name__ == "__main__":
    unittest.main()
