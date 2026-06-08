from __future__ import annotations

import argparse
import importlib.util
import json
import mimetypes
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlencode

import uvicorn


STARTUP_DELAY_SECONDS = 0.8
RUNNER_DIR = Path(__file__).resolve().parent
ROOT = RUNNER_DIR.parent.parent
PROJECTS_DIR = ROOT / "projects"
PROJECT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
AsgiSend = Callable[[dict], Awaitable[None]]
AsgiReceive = Callable[[], Awaitable[dict]]
AsgiApp = Callable[[dict, AsgiReceive, AsgiSend], Awaitable[None]]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="douwe",
        description="Run one douwe.com project locally.",
    )
    parser.add_argument("project", help="Project id, e.g. cambrium")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind. Defaults to 127.0.0.1.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port to bind. If busy, the next open port is used.",
    )
    parser.add_argument(
        "--embed",
        action="store_true",
        help="Open the project body without the douwe.com page chrome.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Start the server without opening a browser.",
    )
    return parser.parse_args(argv)


def find_open_port(host: str, preferred_port: int) -> int:
    for port in range(preferred_port, preferred_port + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(
        f"Could not find an open port from {preferred_port} to {preferred_port + 99}"
    )


def configure_django() -> None:
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)

    from django.conf import settings

    if settings.configured:
        return

    settings.configure(
        SECRET_KEY="douwe-local-runner",
        DEBUG=True,
        ALLOWED_HOSTS=["127.0.0.1", "localhost"],
        ROOT_URLCONF=__name__,
        DEFAULT_CHARSET="utf-8",
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        STATIC_URL="/static/",
        INSTALLED_APPS=["projects"],
        TEMPLATES=[
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "DIRS": [
                    str(ROOT / "templates"),
                    str(ROOT / "projects" / "templates"),
                ],
                "APP_DIRS": False,
                "OPTIONS": {"context_processors": []},
            }
        ],
        DATABASES={"default": {"ENGINE": "django.db.backends.dummy"}},
        USE_TZ=True,
    )


def load_project(project_id: str):
    if not PROJECT_ID_RE.match(project_id):
        return None

    configure_django()

    import django

    django.setup()

    from projects.autodiscover import find_project_class, parse_info_block
    from projects.common import Project

    project_dir = PROJECTS_DIR / project_id
    html_file = project_dir / f"{project_id}.html"
    if not html_file.is_file():
        return None

    with html_file.open() as f:
        info, _ = parse_info_block(f.read())
    if info is None:
        return None

    project_class = Project
    py_file = project_dir / f"{project_id}.py"
    if py_file.is_file():
        module_name = f"projects.{project_id}.{project_id}"
        spec = importlib.util.spec_from_file_location(module_name, py_file)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            found = find_project_class(module)
            if found:
                project_class = found

    name = info.pop("name", project_id)
    description = info.pop("description", "")
    info["_directory_based"] = True
    info["_html_path"] = str(html_file)
    project_class(project_id, name, description, **info)
    return Project.get_project(project_id)


def project_url(project_id: str, embed: bool = False) -> str:
    path = f"/projects/{project_id}"
    if not embed:
        return path
    return f"{path}?{urlencode({'embed': '1'})}"


async def send_response(
    send: AsgiSend,
    status: int,
    body: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": headers or [],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def send_redirect(send: AsgiSend, location: str) -> None:
    await send_response(send, 302, headers=[(b"location", location.encode("utf-8"))])


def django_response_to_asgi(response) -> tuple[int, bytes, list[tuple[bytes, bytes]]]:
    headers = [
        (key.lower().encode("utf-8"), value.encode("utf-8"))
        for key, value in response.headers.items()
    ]
    return response.status_code, bytes(response.content), headers


def not_found_response() -> tuple[int, bytes, list[tuple[bytes, bytes]]]:
    return 404, b"Not found", [(b"content-type", b"text/plain; charset=utf-8")]


def static_response(
    path: str, project_id: str
) -> tuple[int, bytes, list[tuple[bytes, bytes]]]:
    prefixes = {
        f"/static/projects/{project_id}/": [
            PROJECTS_DIR / project_id / "static",
            PROJECTS_DIR / project_id,
        ],
        "/static/": [ROOT / "static"],
    }
    for prefix, roots in prefixes.items():
        if not path.startswith(prefix):
            continue
        relative = path.removeprefix(prefix)
        if not relative or relative.startswith("/") or ".." in Path(relative).parts:
            return not_found_response()
        for root in roots:
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                continue
            if candidate.is_file():
                content_type = (
                    mimetypes.guess_type(candidate)[0] or "application/octet-stream"
                )
                return (
                    200,
                    candidate.read_bytes(),
                    [(b"content-type", content_type.encode("utf-8"))],
                )
    return not_found_response()


def make_request(scope: dict, body: bytes = b""):
    from django.test import RequestFactory

    query_string = scope.get("query_string", b"").decode("latin1")
    path = scope.get("path") or "/"
    full_path = path if not query_string else f"{path}?{query_string}"
    headers = {
        key.decode("latin1").replace("_", "-"): value.decode("latin1")
        for key, value in scope.get("headers", [])
    }
    request = RequestFactory().generic(
        scope.get("method", "GET"),
        full_path,
        data=body,
        content_type=headers.get("content-type", "application/octet-stream"),
    )
    request.META["SERVER_NAME"] = "127.0.0.1"
    request.META["SERVER_PORT"] = "8765"
    return request


async def read_body(receive: AsgiReceive) -> bytes:
    chunks = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def project_http_response(project, scope: dict, body: bytes):
    from django.http import HttpResponseRedirect

    path = scope.get("path") or "/"
    request = make_request(scope, body)
    prefix = f"/projects/{project.id}"
    handler = None
    if path.startswith(prefix + "/"):
        handler = path.removeprefix(prefix + "/")

    if handler:
        response = project.handle_request(handler, request)
        if response:
            return django_response_to_asgi(response)
        return django_response_to_asgi(HttpResponseRedirect("/projects/unknown"))
    return django_response_to_asgi(project.render(request))


def websocket_app(project) -> AsgiApp:
    groups: dict[str, set[AsgiSend]] = defaultdict(set)

    async def send_json(send: AsgiSend, payload: dict) -> None:
        await send({"type": "websocket.send", "text": json.dumps(payload)})

    async def application(scope: dict, receive: AsgiReceive, send: AsgiSend) -> None:
        path = scope.get("path", "")
        if path != f"/ws/projects/{project.id}":
            await send({"type": "websocket.close", "code": 1008})
            return

        await send({"type": "websocket.accept"})
        group_name = project.id
        groups[group_name].add(send)
        try:
            while True:
                message = await receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message["type"] != "websocket.receive":
                    continue
                text = message.get("text")
                if text is None:
                    continue
                data = json.loads(text)
                room = data.get("room")
                if room:
                    groups[group_name].discard(send)
                    group_name = f"{project.id}-{room}"
                    groups[group_name].add(send)
                response = project.receive(data)
                if response is None:
                    continue
                do_broadcast = response.pop("broadcast", False)
                if do_broadcast:
                    for group_send in list(groups[group_name]):
                        await send_json(group_send, response)
                else:
                    await send_json(send, response)
        finally:
            groups[group_name].discard(send)

    return application


def runner_app(project_id: str, embed: bool) -> AsgiApp:
    project = load_project(project_id)
    if project is None:
        raise RuntimeError(f"Unknown project: {project_id}")

    root_target = project_url(project.id, embed)
    ws_app = websocket_app(project)

    async def application(scope: dict, receive: AsgiReceive, send: AsgiSend) -> None:
        scope_type = scope["type"]
        path = scope.get("path") or "/"

        if scope_type == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope_type == "websocket":
            await ws_app(scope, receive, send)
            return
        if scope_type != "http":
            return
        if path in {"", "/"}:
            await send_redirect(send, root_target)
            return
        if path.startswith("/static/"):
            status, body, headers = static_response(path, project.id)
            await send_response(send, status, body, headers)
            return
        if path == f"/projects/{project.id}" or path.startswith(
            f"/projects/{project.id}/"
        ):
            body = await read_body(receive)
            status, content, headers = project_http_response(project, scope, body)
            await send_response(send, status, content, headers)
            return
        await send_response(send, *not_found_response())

    return application


def open_browser_later(url: str) -> None:
    def opener() -> None:
        time.sleep(STARTUP_DELAY_SECONDS)
        webbrowser.open(url)

    thread = threading.Thread(target=opener, daemon=True)
    thread.start()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project = load_project(args.project)
    if project is None:
        print(f"Unknown project: {args.project}", file=sys.stderr)
        return 2

    port = find_open_port(args.host, args.port)
    url = f"http://{args.host}:{port}{project_url(project.id, args.embed)}"
    if port != args.port:
        print(f"Port {args.port} is busy; using {port}.")
    print(f"Running {project.name} at {url}")

    if not args.no_browser:
        open_browser_later(url)

    uvicorn.run(runner_app(project.id, args.embed), host=args.host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
