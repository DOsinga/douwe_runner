import argparse
import importlib.util
import mimetypes
import re
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import jinja2
import tornado.ioloop
import tornado.escape
import tornado.web
import yaml


STARTUP_DELAY_SECONDS = 0.8
RUNNER_DIR = Path(__file__).resolve().parent
ROOT = RUNNER_DIR.parent.parent
PROJECTS_DIR = ROOT / "projects"
PROJECT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
INFO_RE = re.compile(
    r'<script\s+type="text/markdown"\s+id="info"\s*>(.*?)</script>',
    re.DOTALL,
)
LOAD_STATIC_RE = re.compile(r"{%\s*load\s+static\s*%}\s*")
DJANGO_STATIC_RE = re.compile(r"{%\s*static\s+(['\"])(.*?)\1\s*%}")


class RunnerProject:
    def __init__(self, project_id, info, html_path):
        self.id = project_id
        self.name = info.get("name", project_id)
        self.description = info.get("description", "")
        self.shortdescription = info.get("shortdescription") or self.description
        self.type = info.get("type")
        self.github = github_name(info.get("github"))
        self.files = info.get("files") or []
        self.pass_on_request = info.get("pass_on_request") or []
        self.nochrome = info.get("nochrome", False)
        self.dontrepeatintro = info.get("dontrepeatintro", False)
        self.html_path = html_path
        self.template_source = preprocess_template(strip_info_block(html_path.read_text()))
        self.impl = None

    def fill_dict(self, request, context):
        if self.impl:
            self.impl.fill_dict(request, context)

    def handle_request(self, handler_name, request):
        if self.impl:
            return self.impl.handle_request(handler_name, request)
        return None

    def receive(self, payload):
        if self.impl:
            return self.impl.receive(payload)
        return None

    def thumbnail(self):
        for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"):
            image = PROJECTS_DIR / self.id / f"{self.id}{ext}"
            if image.is_file():
                return f"/static/projects/{self.id}/{self.id}{ext}"
        return "/static/projects/visited/visited.jpg"


class RunnerRequest:
    def __init__(self, handler):
        parsed = urlsplit(handler.request.uri)
        self.method = handler.request.method
        self.path = parsed.path
        self.body = handler.request.body or b""
        self.GET = {
            key: values[-1] if values else ""
            for key, values in parse_qs(parsed.query).items()
        }
        self.POST = {}
        content_type = handler.request.headers.get("content-type", "")
        if self.body and content_type.startswith(
            "application/x-www-form-urlencoded"
        ):
            self.POST = {
                key: values[-1] if values else ""
                for key, values in parse_qs(self.body.decode("utf-8")).items()
            }
        self.headers = handler.request.headers


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


def github_name(github):
    if not github:
        return None
    if github.startswith("/"):
        return github[1:]
    return f"DOsinga/{github}"


def parse_info_block(html):
    match = INFO_RE.search(html)
    if not match:
        return None
    return yaml.safe_load(match.group(1)) or {}


def strip_info_block(html):
    return INFO_RE.sub("", html, count=1).strip()


def preprocess_template(template_source):
    template_source = LOAD_STATIC_RE.sub("", template_source)
    return DJANGO_STATIC_RE.sub(r"{{ static_file('\2') }}", template_source)


def load_project(project_id):
    if not PROJECT_ID_RE.match(project_id):
        return None

    project_dir = PROJECTS_DIR / project_id
    html_path = project_dir / f"{project_id}.html"
    if not html_path.is_file():
        return None

    info = parse_info_block(html_path.read_text())
    if info is None:
        return None

    project = RunnerProject(project_id, info, html_path)
    project.impl = load_project_impl(project)
    return project


def load_project_impl(project):
    py_file = PROJECTS_DIR / project.id / f"{project.id}.py"
    if not py_file.is_file():
        return None

    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)

    module_name = f"douwe_runner_loaded.{project.id}"
    spec = importlib.util.spec_from_file_location(module_name, py_file)
    if not spec or not spec.loader:
        return None

    try:
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception as exc:
        print(f"Warning: could not load {py_file}: {exc}", file=sys.stderr)
        return None

    impl_class = find_project_class(module)
    if not impl_class:
        return None

    try:
        return impl_class(
            project.id,
            project.name,
            project.description,
            shortdescription=project.shortdescription,
            type=project.type,
            files=project.files,
            github=project.github,
            pass_on_request=project.pass_on_request,
            nochrome=project.nochrome,
            dontrepeatintro=project.dontrepeatintro,
        )
    except Exception as exc:
        print(f"Warning: could not initialize {py_file}: {exc}", file=sys.stderr)
        return None


def find_project_class(module):
    try:
        from projects.common import Project
    except Exception:
        Project = None

    for name in dir(module):
        obj = getattr(module, name)
        if not isinstance(obj, type):
            continue
        if Project and issubclass(obj, Project) and obj is not Project:
            return obj
        if name != "Project" and hasattr(obj, "fill_dict"):
            return obj
    return None


def project_url(project_id, embed=False):
    path = f"/projects/{project_id}"
    if not embed:
        return path
    return f"{path}?{urlencode({'embed': '1'})}"


def jinja_env(project):
    static_prefix = f"/static/projects/{project.id}/"
    env = jinja2.Environment(autoescape=False)
    env.globals["static_file"] = lambda path: f"/static/{path}"
    env.globals["project_static"] = lambda path: f"{static_prefix}{path}"
    return env


def render_project_body(project, request):
    context = {"static": f"/static/projects/{project.id}/", "fs": request.GET.get("fs")}
    for key in project.pass_on_request:
        if key in request.GET:
            context[key] = request.GET[key]
    project.fill_dict(request, context)
    template = jinja_env(project).from_string(project.template_source)
    return template.render(**context)


def render_project_page(project, request, embed=False):
    body = render_project_body(project, request)
    if project.nochrome or embed:
        return body

    downloads = ""
    if project.files and not request.GET.get("fs"):
        items = []
        for entry in project.files:
            filename, description = entry[0], entry[1]
            url = f"/static/projects/{project.id}/{filename}"
            items.append(
                f'<dt><a href="{tornado.escape.xhtml_escape(url)}">'
                f"{tornado.escape.xhtml_escape(filename)}</a></dt>"
                f"<dd>{tornado.escape.xhtml_escape(description)}</dd>"
            )
        downloads = f"""
        <div class="downloads">
          <h3>Downloads</h3>
          <dl>{''.join(items)}</dl>
        </div>
        """

    intro = "" if project.dontrepeatintro else project.description
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{tornado.escape.xhtml_escape(project.name)}</title>
  <meta name="description" content="{tornado.escape.xhtml_escape(project.shortdescription)}">
  <meta property="og:title" content="{tornado.escape.xhtml_escape(project.name)}">
  <meta property="og:image" content="{tornado.escape.xhtml_escape(project.thumbnail())}">
  <script src="/static/jquery-3.1.1.min.js"></script>
  <style>
    body {{ font-family: Georgia, serif; margin: 2rem auto; max-width: 900px; line-height: 1.5; padding: 0 1rem; }}
    h1 {{ font-family: system-ui, sans-serif; }}
    canvas {{ max-width: 100%; }}
    .downloads {{ margin-top: 2em; padding-top: 1.5em; border-top: 1px solid #ddd; }}
    .downloads dl {{ display: grid; grid-template-columns: minmax(140px, 220px) 1fr; gap: .5em 1.5em; }}
    .downloads dt, .downloads dd {{ margin: 0; }}
  </style>
</head>
<body>
  <h1>{tornado.escape.xhtml_escape(project.name)}</h1>
  <div class="intro">{intro}</div>
  <article>{body}</article>
  {downloads}
</body>
</html>"""


def response_tuple(status, body, content_type="text/html; charset=utf-8", headers=None):
    headers = dict(headers or {})
    headers.setdefault("content-type", content_type)
    return status, body.encode("utf-8"), headers


def not_found_response():
    return response_tuple(404, "Not found", "text/plain; charset=utf-8")


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


def handler_response(project, handler_name, request):
    response = project.handle_request(handler_name, request)
    if response is None:
        return response_tuple(
            501,
            f"{project.id}/{handler_name} is not handled by the lightweight runner.",
            "text/plain; charset=utf-8",
        )
    if isinstance(response, str):
        return response_tuple(200, response)
    if isinstance(response, bytes):
        return 200, response, {"content-type": "application/octet-stream"}
    if hasattr(response, "content") and hasattr(response, "status_code"):
        headers = {
            key.lower(): value for key, value in getattr(response, "headers", {}).items()
        }
        return response.status_code, bytes(response.content), headers
    return response_tuple(200, str(response), "text/plain; charset=utf-8")


class RootHandler(tornado.web.RequestHandler):
    def initialize(self, project, embed):
        self.project = project
        self.embed = embed

    def get(self):
        self.redirect(project_url(self.project.id, self.embed))


class StaticHandler(tornado.web.RequestHandler):
    def initialize(self, project):
        self.project = project

    def get(self, path):
        self.serve(path)

    def head(self, path):
        self.serve(path, include_body=False)

    def serve(self, path, include_body=True):
        status, content, headers = static_response(f"/static/{path}", self.project.id)
        self.set_status(status)
        for key, value in headers.items():
            self.set_header(key, value)
        self.set_header("content-length", str(len(content)))
        if include_body:
            self.write(content)


class ProjectHandler(tornado.web.RequestHandler):
    def initialize(self, project, embed):
        self.project = project
        self.embed = embed

    def get(self, handler_name=None):
        self.respond(handler_name)

    def post(self, handler_name=None):
        self.respond(handler_name)

    def head(self, handler_name=None):
        self.respond(handler_name, include_body=False)

    def respond(self, handler_name=None, include_body=True):
        request = RunnerRequest(self)
        if handler_name:
            status, content, headers = handler_response(
                self.project, handler_name, request
            )
        else:
            html = render_project_page(
                self.project, request, self.embed or request.GET.get("embed")
            )
            status, content, headers = response_tuple(200, html)

        self.set_status(status)
        for key, value in headers.items():
            self.set_header(key, value)
        self.set_header("content-length", str(len(content)))
        if include_body:
            self.write(content)


def make_app(project, embed):
    return tornado.web.Application(
        [
            (r"/", RootHandler, {"project": project, "embed": embed}),
            (
                rf"/projects/{re.escape(project.id)}",
                ProjectHandler,
                {"project": project, "embed": embed},
            ),
            (
                rf"/projects/{re.escape(project.id)}/(.*)",
                ProjectHandler,
                {"project": project, "embed": embed},
            ),
            (r"/static/(.*)", StaticHandler, {"project": project}),
        ],
        debug=True,
    )


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

    app = make_app(project, args.embed)
    app.listen(port, address=args.host)
    try:
        tornado.ioloop.IOLoop.current().start()
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
