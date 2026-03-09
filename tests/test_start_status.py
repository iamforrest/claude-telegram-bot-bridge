import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import ClassVar


class StartStatusTests(unittest.TestCase):
    repo_root: ClassVar[Path]
    start_script: ClassVar[Path]

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.start_script = cls.repo_root / "start.sh"

    def _run_status(self, project_root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.start_script), str(project_root), "--status"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=False,
        )

    def _prepare_project(self, tmpdir: str) -> Path:
        project_root = Path(tmpdir)
        bot_dir = project_root / ".telegram_bot"
        logs_dir = bot_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        return project_root

    def test_status_no_pid_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = self._prepare_project(tmpdir)
            result = self._run_status(project_root)

            self.assertEqual(result.returncode, 0)
            self.assertIn("Bot status: unavailable (no PID file;", result.stdout)
            self.assertIn("common causes:", result.stdout)

    def test_status_stale_pid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = self._prepare_project(tmpdir)
            pid_file = project_root / ".telegram_bot" / "bot.pid"
            pid_file.write_text("999999\n", encoding="utf-8")

            result = self._run_status(project_root)

            self.assertEqual(result.returncode, 0)
            self.assertIn("Bot status: unavailable (stale PID: 999999;", result.stdout)
            self.assertIn("common causes:", result.stdout)
            self.assertFalse(pid_file.exists(), "stale pid file should be cleaned up")

    def test_status_running_but_inactive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = self._prepare_project(tmpdir)
            pid_file = project_root / ".telegram_bot" / "bot.pid"
            pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")

            bot_log = project_root / ".telegram_bot" / "logs" / "bot.log"
            bot_log.write_text("old log\n", encoding="utf-8")
            old = int(time.time()) - 2 * 60 * 60
            os.utime(bot_log, (old, old))

            result = self._run_status(project_root)

            self.assertEqual(result.returncode, 2)
            self.assertIn("Bot status: unavailable", result.stdout)
            self.assertIn("inactive for", result.stdout)
            self.assertIn("common causes:", result.stdout)

    def test_status_running_and_healthy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = self._prepare_project(tmpdir)
            pid_file = project_root / ".telegram_bot" / "bot.pid"
            pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")

            bot_log = project_root / ".telegram_bot" / "logs" / "bot.log"
            bot_log.write_text("fresh log\n", encoding="utf-8")

            result = self._run_status(project_root)

            self.assertEqual(result.returncode, 0)
            self.assertIn("Bot status: running", result.stdout)
            self.assertNotIn("unavailable", result.stdout)


if __name__ == "__main__":
    unittest.main()
