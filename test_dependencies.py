import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import douwe


class ProjectDependenciesTest(unittest.TestCase):
    def test_runner_requirements_take_precedence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "requirements.txt").write_text("large-training-package\n")
            runner_requirements = root / "requirements-runner.txt"
            runner_requirements.write_text("plotly\n")

            self.assertEqual(douwe.project_requirements(root), runner_requirements)

    def test_empty_runner_requirements_disable_default_requirements(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "requirements.txt").write_text("large-training-package\n")
            (root / "requirements-runner.txt").write_text(
                "# The web project only uses the standard library.\n"
            )

            self.assertIsNone(douwe.project_requirements(root))

    def test_reexec_uses_uv_and_marks_dependencies_ready(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            requirements = root / "requirements.txt"
            requirements.write_text("plotly\n")

            with patch.dict(os.environ, {}, clear=True), patch(
                "douwe.shutil.which", return_value="/usr/bin/uv"
            ), patch("douwe.os.execvpe") as execvpe:
                douwe.reexec_with_project_dependencies(
                    root, [str(root), "--no-browser"]
                )

            command = execvpe.call_args.args[1]
            env = execvpe.call_args.args[2]
            self.assertEqual(command[:5], [
                "/usr/bin/uv",
                "run",
                "--no-project",
                "--with-requirements",
                str(requirements),
            ])
            self.assertEqual(command[-2:], [str(root), "--no-browser"])
            self.assertEqual(env[douwe.DEPENDENCIES_READY_ENV], "1")


if __name__ == "__main__":
    unittest.main()
