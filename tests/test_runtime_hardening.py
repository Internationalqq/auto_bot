from __future__ import annotations

import os
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autobot import scheduled_pipeline


ROOT = Path(__file__).parents[1]


class RuntimeHardeningTests(unittest.TestCase):
    def test_container_uses_pinned_gunicorn_with_health_compatible_bind(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        self.assertIn("gunicorn==23.0.0", requirements)
        dockerfile_path = ROOT / "Dockerfile"
        if dockerfile_path.is_file():
            self.assertIn(
                'CMD ["gunicorn", "--config", "tools/gunicorn_conf.py", "autobot.web_ui:app"]',
                dockerfile_path.read_text(encoding="utf-8"),
            )

        with mock.patch.dict(
            os.environ,
            {"WEB_UI_HOST": "127.0.0.1", "WEB_UI_PORT": "8765"},
            clear=True,
        ):
            config = runpy.run_path(str(ROOT / "tools" / "gunicorn_conf.py"))
        self.assertEqual(config["bind"], "127.0.0.1:8765")
        self.assertEqual(config["worker_class"], "gthread")
        self.assertEqual(config["workers"], 1)
        self.assertEqual(config["threads"], 8)
        self.assertEqual(config["timeout"], 600)

    def test_gunicorn_numeric_environment_is_bounded(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "WEB_UI_PORT": "99999",
                "WEB_UI_WORKERS": "100",
                "WEB_UI_THREADS": "1",
                "WEB_UI_WORKER_TIMEOUT_SEC": "invalid",
            },
            clear=True,
        ):
            config = runpy.run_path(str(ROOT / "tools" / "gunicorn_conf.py"))

        self.assertTrue(config["bind"].endswith(":65535"))
        self.assertEqual(config["workers"], 4)
        self.assertEqual(config["threads"], 2)
        self.assertEqual(config["timeout"], 600)

    def test_scheduled_subprocesses_receive_stage_specific_timeouts(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_run(cmd, **kwargs):
            calls.append(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            os.environ, {}, clear=True
        ), mock.patch.object(scheduled_pipeline.subprocess, "run", side_effect=fake_run):
            scheduled_pipeline._run_main(Path(tmp_dir) / "ids.txt")
            scheduled_pipeline._run_main_from_downloaded("1")
            scheduled_pipeline._run_main_from_tender_url("1", "https://example.test/tender")
            scheduled_pipeline._run_market("1")

        self.assertEqual(
            [call["timeout"] for call in calls],
            [7200, 3600, 3600, 21600],
        )
        self.assertNotIn("capture_output", calls[0])
        self.assertIs(calls[1]["capture_output"], True)

    def test_scheduled_timeout_returns_conventional_exit_code_and_partial_output(self) -> None:
        def timeout_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(
                cmd,
                kwargs["timeout"],
                output=b"partial",
                stderr=b"late",
            )

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            scheduled_pipeline.subprocess, "run", side_effect=timeout_run
        ):
            code, output = scheduled_pipeline._run_main_from_downloaded("1")

        self.assertEqual(code, 124)
        self.assertEqual(output, "partial\nlate")

    def test_cron_installer_uses_non_blocking_single_instance_lock(self) -> None:
        installer = (ROOT / "tools" / "install_cron_tasks.sh").read_text(encoding="utf-8")

        self.assertIn('LOCK_FILE="$REPO_ROOT/data/scheduled_pipeline.lock"', installer)
        self.assertIn("command -v flock", installer)
        self.assertIn('-n -E 0 \\"$LOCK_FILE\\"', installer)

    def test_windows_scheduler_ignores_overlapping_runs(self) -> None:
        installer = (ROOT / "tools" / "install_scheduled_tasks.ps1").read_text(encoding="utf-8")

        self.assertIn("-MultipleInstances IgnoreNew", installer)


if __name__ == "__main__":
    unittest.main()
