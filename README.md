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

This first version is an HTTP runner using Python's standard library server.
Projects that require WebSockets still need the full Django/Channels site for
now.
