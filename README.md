# Douwe Runner

Run one douwe.com project locally from the repository root:

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

Inside this checkout, a bare name like `cambrium` prefers `./projects/cambrium`.
If no local project exists, it resolves to `github.com/DOsinga/<name>` and caches
the clone under `~/.cache/douwe/projects/`.

Useful options:

```bash
python projects/douwe_runner/douwe.py cambrium --embed
python projects/douwe_runner/douwe.py cambrium --no-browser
python projects/douwe_runner/douwe.py cambrium --port 9000
python projects/douwe_runner/douwe.py cambrium --refresh
```

The runner intentionally loads only the requested project. That keeps old or
dependency-heavy projects from breaking otherwise simple projects.

This version uses Tornado for serving and Jinja2 for project template rendering.
It does not call Django's renderer or configure Django settings. `{% load static
%}` is stripped from project templates, `{{ static }}` points at `./static/`,
and `{% static "..." %}` is rewritten to a shared `./_site_static/...` URL.

Projects that require WebSockets still need the full Django/Channels site for
now, but Tornado gives the runner a natural place to add that later.

Independent repos should eventually include their own `<project>.html` file with
the existing info block. During migration, if a GitHub repo has no HTML info
block but this checkout has `projects/<project>/<project>.html`, the runner uses
that local manifest and serves assets from the cloned repo first.
