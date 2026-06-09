import argparse
import os
import importlib.util
import mimetypes
import re
import subprocess
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
DEFAULT_GITHUB_OWNER = "DOsinga"
RUNNER_DIR = Path(__file__).resolve().parent
CACHE_DIR = Path(os.environ.get("DOUWE_CACHE_DIR", "~/.cache/douwe")).expanduser()


def find_site_root():
    for candidate in (RUNNER_DIR.parent.parent, Path.cwd()):
        if (candidate / "projects").is_dir():
            return candidate
    return None


ROOT = find_site_root()
PROJECTS_DIR = ROOT / "projects" if ROOT else None
PROJECT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
INFO_RE = re.compile(
    r'<script\s+type="text/markdown"\s+id="info"\s*>(.*?)</script>',
    re.DOTALL,
)
LOAD_STATIC_RE = re.compile(r"{%\s*load\s+static\s*%}\s*")
CSRF_TOKEN_RE = re.compile(r"{%\s*csrf_token\s*%}\s*")
DJANGO_STATIC_RE = re.compile(r"{%\s*static\s+(['\"])(.*?)\1\s*%}")
PROJECT_STATIC = "./static/"
SHARED_STATIC = "./_site_static/"


class RunnerProject:
    def __init__(self, project_id, info, html_path, root_dir, source):
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
        self.root_dir = root_dir
        self.source = source
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
            image = self.root_dir / f"{self.id}{ext}"
            if image.is_file():
                return f"{PROJECT_STATIC}{self.id}{ext}"
        return f"{SHARED_STATIC}projects/visited/visited.jpg"


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
        self.FILES = {}
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
    parser.add_argument(
        "project",
        help=(
            "Project id, local path, GitHub owner/repo, or GitHub URL "
            "(e.g. cambrium)"
        ),
    )
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
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh a cached GitHub checkout before running it.",
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
    template_source = CSRF_TOKEN_RE.sub("", template_source)
    return DJANGO_STATIC_RE.sub(r"{{ site_static('\2') }}", template_source)


def load_project(ref, refresh=False):
    resolved = resolve_project_ref(ref, refresh)
    if resolved is None:
        return None

    project_id, html_path, root_dir, source = resolved

    info = parse_info_block(html_path.read_text())
    if info is None:
        return None

    project = RunnerProject(project_id, info, html_path, root_dir, source)
    project.impl = load_project_impl(project)
    return project


def resolve_project_ref(ref, refresh=False):
    local = resolve_local_ref(ref)
    if local:
        return local

    github_ref = github_repo_ref(ref)
    if github_ref:
        owner, repo = github_ref
        root_dir = ensure_github_repo(owner, repo, refresh)
        if not root_dir:
            return None
        html_path = find_project_html(root_dir, repo)
        source = f"github:{owner}/{repo}"
        if not html_path:
            html_path = legacy_site_manifest(repo)
            if html_path:
                source = f"{source} with local manifest:{html_path}"
        if not html_path:
            print(
                f"No project HTML info block found in {owner}/{repo}. "
                f"Add {repo}.html to that repo.",
                file=sys.stderr,
            )
            return None
        return html_path.stem, html_path, root_dir, source

    return None


def resolve_local_ref(ref):
    path = Path(ref).expanduser()
    if path.exists():
        html_path = find_project_html(path)
        if html_path:
            return html_path.stem, html_path, html_path.parent, f"local:{html_path.parent}"
        return None

    if PROJECTS_DIR and PROJECT_ID_RE.match(ref):
        project_dir = PROJECTS_DIR / ref
        html_path = find_project_html(project_dir, ref)
        if html_path:
            return ref, html_path, project_dir, f"local:{project_dir}"
    return None


def find_project_html(path, preferred_id=None):
    if path.is_file():
        if path.suffix == ".html" and parse_info_block(path.read_text()) is not None:
            return path
        return None
    if not path.is_dir():
        return None

    candidates = []
    if preferred_id:
        candidates.append(path / f"{preferred_id}.html")
    candidates.extend([path / f"{path.name}.html", path / "project.html"])
    candidates.extend(sorted(path.glob("*.html")))
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file() and parse_info_block(candidate.read_text()) is not None:
            return candidate
    return None


def legacy_site_manifest(project_id):
    if not PROJECTS_DIR:
        return None
    html_path = PROJECTS_DIR / project_id / f"{project_id}.html"
    if html_path.is_file() and parse_info_block(html_path.read_text()) is not None:
        return html_path
    return None


def github_repo_ref(ref):
    parsed = urlsplit(ref)
    if parsed.scheme in {"http", "https"} and parsed.netloc == "github.com":
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        if len(parts) >= 2:
            return parts[0], parts[1].removesuffix(".git")
        return None

    if ref.startswith("github:"):
        ref = ref.removeprefix("github:")

    if "/" in ref and not ref.startswith("."):
        parts = [p for p in ref.split("/") if p]
        if len(parts) == 2:
            return parts[0], parts[1].removesuffix(".git")

    if PROJECT_ID_RE.match(ref):
        return DEFAULT_GITHUB_OWNER, ref
    return None


def ensure_github_repo(owner, repo, refresh=False):
    target = CACHE_DIR / "projects" / owner / repo
    if target.exists():
        if refresh:
            print(f"Refreshing {owner}/{repo}...", flush=True)
            subprocess.run(["git", "-C", str(target), "pull", "--ff-only"], check=False)
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{owner}/{repo}.git"
    print(f"Cloning {url}...", flush=True)
    result = subprocess.run(["git", "clone", "--depth", "1", url, str(target)])
    if result.returncode != 0:
        return None
    return target


def load_project_impl(project):
    py_file = project.root_dir / f"{project.id}.py"
    if not py_file.is_file():
        return None

    if ROOT:
        root = str(ROOT)
        if root not in sys.path:
            sys.path.insert(0, root)
    project_root = str(project.root_dir)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

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
    path = "/"
    if not embed:
        return path
    return f"{path}?{urlencode({'embed': '1'})}"


def jinja_env(project):
    env = jinja2.Environment(autoescape=False)
    env.globals["site_static"] = lambda path: f"{SHARED_STATIC}{path}"
    env.globals["project_static"] = lambda path: f"{PROJECT_STATIC}{path}"
    return env


def render_project_body(project, request):
    context = {"static": PROJECT_STATIC, "fs": request.GET.get("fs")}
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
            url = f"{PROJECT_STATIC}{filename}"
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


def static_response(path, project):
    prefixes = {
        "/static/": project_static_roots(project),
        "/_site_static/": site_static_roots(),
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


def project_static_roots(project):
    roots = [project.root_dir / "static", project.root_dir]
    manifest_dir = project.html_path.parent
    if manifest_dir != project.root_dir:
        roots.extend([manifest_dir / "static", manifest_dir])
    return roots


def site_static_roots():
    if not ROOT:
        return []
    return [ROOT / "static"]


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
        request = RunnerRequest(self)
        html = render_project_page(
            self.project, request, self.embed or request.GET.get("embed")
        )
        self.set_header("content-type", "text/html; charset=utf-8")
        self.write(html)


class ProjectRedirectHandler(tornado.web.RequestHandler):
    def initialize(self, project, embed):
        self.project = project
        self.embed = embed

    def get(self, handler_name=None):
        suffix = f"?{self.request.query}" if self.request.query else ""
        if handler_name:
            self.redirect(f"/{handler_name}{suffix}")
        else:
            self.redirect(f"{project_url(self.project.id, self.embed)}{suffix}")


class StaticHandler(tornado.web.RequestHandler):
    def initialize(self, project):
        self.project = project

    def get(self, path):
        self.serve(path)

    def head(self, path):
        self.serve(path, include_body=False)

    def serve(self, path, include_body=True):
        prefix = "/_site_static/" if self.request.path.startswith("/_site_static/") else "/static/"
        status, content, headers = static_response(f"{prefix}{path}", self.project)
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
                ProjectRedirectHandler,
                {"project": project, "embed": embed},
            ),
            (
                rf"/projects/{re.escape(project.id)}/(.*)",
                ProjectRedirectHandler,
                {"project": project, "embed": embed},
            ),
            (r"/static/(.*)", StaticHandler, {"project": project}),
            (r"/_site_static/(.*)", StaticHandler, {"project": project}),
            (r"/(.*)", ProjectHandler, {"project": project, "embed": embed}),
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
    project = load_project(args.project, refresh=args.refresh)
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
