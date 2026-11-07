# Douwe Runner

Run one douwe.com project locally:

```bash
uvx douwe cambrium
```

or:

```bash
pipx run douwe cambrium
```

A bare name like `cambrium` resolves to `github.com/DOsinga/cambrium` and caches
the clone under `~/.cache/douwe/projects/`.

From the main douwe.com checkout, you can also run the development copy directly:

```bash
python projects/douwe_runner/douwe.py cambrium
```

Project references can be local or GitHub-backed:

```bash
python projects/douwe_runner/douwe.py cambrium
python projects/douwe_runner/douwe.py ./projects/cambrium
python projects/douwe_runner/douwe.py DOsinga/cambrium
python projects/douwe_runner/douwe.py https://github.com/DOsinga/cambrium
```

Inside the douwe.com checkout, a bare name like `cambrium` prefers
`./projects/cambrium`. Outside that checkout, it resolves to
`github.com/DOsinga/<name>`.

Useful options:

```bash
python projects/douwe_runner/douwe.py --list
python projects/douwe_runner/douwe.py list
python projects/douwe_runner/douwe.py cambrium --embed
python projects/douwe_runner/douwe.py cambrium --no-browser
python projects/douwe_runner/douwe.py cambrium --port 9000
python projects/douwe_runner/douwe.py cambrium --refresh
```

Installed-package equivalents work the same way:

```bash
douwe --list
douwe cambrium --port 9000
douwe cambrium --refresh
```

Douwe Runner is a small local server for standalone douwe.com projects. It
accepts a project directory, a `DOsinga/<repo>` reference, or a GitHub URL; when
needed it caches a shallow clone under `~/.cache/douwe/projects/`.

If a project includes `requirements-runner.txt`, Douwe Runner uses `uv` to
start the project with those dependencies in an isolated environment. If that
file is absent, it uses `requirements.txt`. This happens automatically when
the runner is launched with `uvx`.

Use `requirements-runner.txt` when the web project needs fewer packages than
its data generation, training, or development tools. An empty file explicitly
marks the web project as having no extra dependencies.

Use `douwe --list` or `douwe list` to see standalone GitHub-backed projects from
douwe.com. The list comes from `https://douwe.com/projects/index.json` and skips
projects that do not publish a standalone repo yet.

Project templates are rendered with Jinja2 and served with Tornado. Existing
douwe.com project templates can keep using `{{ static }}` and `{% static "..."
%}` asset references, which the runner maps to local project and shared static
directories.

GitHub-backed projects use the `git` command-line tool for cloning and
refreshing. The GitHub CLI (`gh`) is not required, and the runner does not call
the GitHub API. Local project paths do not require git.

Projects that require WebSockets still need the full Django/Channels site for
now.
