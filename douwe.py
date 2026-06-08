import argparse
import importlib.util
import mimetypes
import re
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlsplit


STARTUP_DELAY_SECONDS = 0.8
RUNNER_DIR = Path(__file__).resolve().parent
ROOT = RUNNER_DIR.parent.parent
PROJECTS_DIR = ROOT / "projects"
PROJECT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def parse_args(argv=None):
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


def find_open_port(host, preferred_port):
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


def configure_django():
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


def load_project(project_id):
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


def project_url(project_id, embed=False):
    path = f"/projects/{project_id}"
    if not embed:
        return path
    return f"{path}?{urlencode({'embed': '1'})}"


def not_found_response():
    return 404, b"Not found", {"content-type": "text/plain; charset=utf-8"}


def static_response(path, project_id):
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
                return 200, candidate.read_bytes(), {"content-type": content_type}
    return not_found_response()


def make_request(handler, body=b""):
    from django.test import RequestFactory

    headers = {key.lower(): value for key, value in handler.headers.items()}
    request = RequestFactory().generic(
        handler.command,
        handler.path,
        data=body,
        content_type=headers.get("content-type", "application/octet-stream"),
    )
    request.META["SERVER_NAME"] = handler.server.server_name
    request.META["SERVER_PORT"] = str(handler.server.server_port)
    return request


def django_response_tuple(response):
    headers = {key.lower(): value for key, value in response.headers.items()}
    return response.status_code, bytes(response.content), headers


def project_response(project, handler, path, body):
    from django.http import HttpResponseRedirect

    request = make_request(handler, body)
    prefix = f"/projects/{project.id}"
    handler_name = None
    if path.startswith(prefix + "/"):
        handler_name = path.removeprefix(prefix + "/")

    if handler_name:
        response = project.handle_request(handler_name, request)
        if response:
            return django_response_tuple(response)
        return django_response_tuple(HttpResponseRedirect("/projects/unknown"))
    return django_response_tuple(project.render(request))


def make_handler(project, embed):
    root_target = project_url(project.id, embed)

    class DouweProjectHandler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.handle_request(send_body=False)

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self, send_body=True):
            parsed = urlsplit(self.path)
            path = parsed.path or "/"
            body = self.read_body()

            if path == "/":
                self.send_response(302)
                self.send_header("location", root_target)
                self.end_headers()
                return

            if path.startswith("/static/"):
                status, content, headers = static_response(path, project.id)
            elif path == f"/projects/{project.id}" or path.startswith(
                f"/projects/{project.id}/"
            ):
                status, content, headers = project_response(project, self, path, body)
            else:
                status, content, headers = not_found_response()

            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("content-length", str(len(content)))
            self.end_headers()
            if send_body:
                self.wfile.write(content)

        def read_body(self):
            length = int(self.headers.get("content-length", "0") or 0)
            if not length:
                return b""
            return self.rfile.read(length)

    return DouweProjectHandler


def open_browser_later(url):
    def opener():
        time.sleep(STARTUP_DELAY_SECONDS)
        webbrowser.open(url)

    thread = threading.Thread(target=opener, daemon=True)
    thread.start()


def main(argv=None):
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

    handler = make_handler(project, args.embed)
    server = ThreadingHTTPServer((args.host, port), handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
