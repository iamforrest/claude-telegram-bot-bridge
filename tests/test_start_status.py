import os
import pty
import select
import shutil
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

    def _prepare_script_workspace(self, tmpdir: str) -> Path:
        script_root = Path(tmpdir) / "bridge"
        script_root.mkdir(parents=True, exist_ok=True)
        for filename in (
            "start.sh",
            "requirements.txt",
            ".env.example",
            "CHANGELOG.md",
        ):
            shutil.copy2(self.repo_root / filename, script_root / filename)
        return script_root / "start.sh"

    def _make_fake_python(self, bin_dir: Path) -> None:
        fake_python = bin_dir / "python3"
        fake_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake_python.chmod(0o755)

    def _run_interactive_start(
        self,
        start_script: Path,
        project_root: Path,
        user_input: str,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            ["bash", str(start_script), str(project_root)],
            cwd=start_script.parent,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            text=False,
            close_fds=True,
        )
        os.close(slave_fd)
        output_chunks: list[bytes] = []
        try:
            os.write(master_fd, user_input.encode("utf-8"))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master_fd], [], [], 0.1)
                if ready:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    output_chunks.append(data)
                    continue
                if process.poll() is not None:
                    break
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            os.close(master_fd)

        return subprocess.CompletedProcess(
            process.args,
            process.wait(),
            b"".join(output_chunks).decode("utf-8", errors="replace"),
            "",
        )

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

    def test_status_prefers_heartbeat_over_stale_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = self._prepare_project(tmpdir)
            pid_file = project_root / ".telegram_bot" / "bot.pid"
            pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")

            bot_log = project_root / ".telegram_bot" / "logs" / "bot.log"
            bot_log.write_text("old log\n", encoding="utf-8")
            old = int(time.time()) - 2 * 60 * 60
            os.utime(bot_log, (old, old))

            heartbeat = project_root / ".telegram_bot" / "bot.heartbeat"
            heartbeat.write_text("", encoding="utf-8")

            result = self._run_status(project_root)

            self.assertEqual(result.returncode, 0)
            self.assertIn("Bot status: running", result.stdout)
            self.assertIn("last heartbeat", result.stdout)

    def test_interactive_token_entry_updates_env_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = self._prepare_project(tmpdir)
            start_script = self._prepare_script_workspace(tmpdir)
            fake_bin = Path(tmpdir) / "fake-bin"
            fake_bin.mkdir(parents=True, exist_ok=True)
            self._make_fake_python(fake_bin)

            fake_home = Path(tmpdir) / "home"
            cache_file = fake_home / ".telegram-bot-cache" / "update_check"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text("", encoding="utf-8")

            env = os.environ.copy()
            env["HOME"] = str(fake_home)
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["CLAUDE_CLI_PATH"] = "/bin/true"

            result = self._run_interactive_start(
                start_script,
                project_root,
                "123456789:ABCdefGHIjklMNOpqrsTUVwxyz\n",
                env,
            )

            env_file = project_root / ".telegram_bot" / ".env"
            env_contents = env_file.read_text(encoding="utf-8")

            self.assertEqual(result.returncode, 1)
            self.assertIn("Enter Bot Token:", result.stdout)
            self.assertIn("Token saved to", result.stdout)
            self.assertIn(
                "TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
                env_contents,
            )
            self.assertEqual(env_contents.count("TELEGRAM_BOT_TOKEN="), 1)
            self.assertNotIn(
                "TELEGRAM_BOT_TOKEN = your_bot_token_here",
                env_contents,
            )


if __name__ == "__main__":
    unittest.main()
