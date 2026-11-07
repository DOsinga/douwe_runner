import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import douwe


class ProjectDependenciesTest(unittest.TestCase):
    def test_project_relative_imports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "relative_imports"
            root.mkdir()
            (root / "relative_imports.html").write_text(
                '<script type="text/markdown" id="info">\n'
                "name: Relative imports\n"
                "description: Test project\n"
                "</script>\n"
            )
            (root / "helper.py").write_text("VALUE = 'loaded'\n")
            (root / "relative_imports.py").write_text(
                "from .helper import VALUE\n"
                "from projects.common import Project\n"
                "class RelativeImports(Project):\n"
                "    value = VALUE\n"
            )

            with patch.object(douwe, "ROOT", None):
                project = douwe.load_project(str(root))

            self.assertEqual(project.impl.value, "loaded")

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


class CompatibilityTest(unittest.TestCase):
    def test_django_template_syntax_renders(self):
        source = douwe.preprocess_template(
            """
            {% load static %}
            {% static "projects/example/app.js" %}
            {{ enabled|yesno:'1,' }}
            {{ kickoff|date:'D H:i' }}
            {{ state|default:"Population"|escapejs }}
            {{ payload|json_script:"payload" }}
            """,
            "example",
            "example",
        )
        project = SimpleNamespace()
        template = douwe.jinja_env(project).from_string(source)

        rendered = template.render(
            enabled=True,
            kickoff=datetime(2026, 11, 7, 15, 30, tzinfo=timezone.utc),
            payload={"value": "<safe>"},
        )

        self.assertIn("./static/app.js", rendered)
        self.assertIn("1", rendered)
        self.assertIn("Sat 15:30", rendered)
        self.assertIn("Population", rendered)
        self.assertIn('id="payload"', rendered)
        self.assertIn(r"\u003Csafe\u003E", rendered)

    def test_legacy_project_static_url_uses_project_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "example"
            static = root / "static"
            static.mkdir(parents=True)
            (static / "app.js").write_text("working")
            project = SimpleNamespace(
                id="example",
                root_dir=root,
                html_path=root / "example.html",
            )

            status, content, _ = douwe.static_response(
                "/static/projects/example/app.js", project
            )

            self.assertEqual(status, 200)
            self.assertEqual(content, b"working")

    def test_github_repo_can_select_a_second_project_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for project_id in ("worldsizer", "gapmover"):
                (root / f"{project_id}.html").write_text(
                    '<script type="text/markdown" id="info">\n'
                    f"name: {project_id}\n"
                    "description: Test project\n"
                    "</script>\n"
                )

            with patch("douwe.ensure_github_repo", return_value=root):
                project_id, html_path, _, _ = douwe.resolve_github_project(
                    "DOsinga", "worldsizer", preferred_id="gapmover"
                )

            self.assertEqual(project_id, "gapmover")
            self.assertEqual(html_path.name, "gapmover.html")


if __name__ == "__main__":
    unittest.main()
