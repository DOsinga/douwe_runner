# Douwe Runner

Run one douwe.com project locally from the repository root:

```bash
python projects/douwe_runner/douwe.py cambrium
```

Useful options:

```bash
python projects/douwe_runner/douwe.py cambrium --embed
python projects/douwe_runner/douwe.py cambrium --no-browser
python projects/douwe_runner/douwe.py cambrium --port 9000
```

The runner intentionally loads only the requested project. That keeps old or
dependency-heavy projects from breaking otherwise simple projects.

This version uses Tornado for serving and Jinja2 for project template rendering.
It does not call Django's renderer or configure Django settings. `{% load static
%}` is stripped from project templates, `{{ static }}` points at the project
static directory, and `{% static "..." %}` is rewritten to a shared `/static/...`
URL.

Projects that require WebSockets still need the full Django/Channels site for
now, but Tornado gives the runner a natural place to add that later.
