import json


WEBSITE_ROOT = "."


class RedirectException(BaseException):
    def __init__(self, url):
        self.url = url

    def __str__(self):
        return self.url

    def __repr__(self):
        return self.url


class Project:
    def __init__(
        self,
        id,
        name,
        description,
        shortdescription=None,
        type=None,
        files=None,
        hidden=False,
        template=None,
        github=None,
        url=None,
        pass_on_request=None,
        dontrepeatintro=False,
        nochrome=False,
        **kwargs,
    ):
        self.id = id
        self.name = name
        self.description = description
        self.shortdescription = shortdescription or description
        self.type = type
        self.files = files or []
        self.hidden = hidden
        self.template = template or id
        self.github = github
        self.url = url or f"/projects/{id}"
        self.pass_on_request = pass_on_request
        self.dontrepeatintro = dontrepeatintro
        self.nochrome = nochrome

    def fill_dict(self, request, d):
        pass

    def handle_request(self, handler, request):
        pass

    def receive(self, payload):
        pass


class HttpResponse:
    status_code = 200

    def __init__(self, content=b"", content_type=None, status=200, headers=None):
        self.status_code = status
        self.headers = dict(headers or {})
        if content_type:
            self.headers["content-type"] = content_type
        self.content = b""
        if content:
            self.write(content)

    def write(self, content):
        if isinstance(content, str):
            content = content.encode("utf-8")
        self.content += content

    def __setitem__(self, key, value):
        self.headers[key] = value

    def __getitem__(self, key):
        return self.headers[key]

    def set_cookie(self, key, value="", max_age=None, path="/"):
        parts = [f"{key}={value}"]
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        if path:
            parts.append(f"Path={path}")
        self.headers["set-cookie"] = "; ".join(parts)


class HttpResponseBadRequest(HttpResponse):
    def __init__(self, content=b"", content_type="text/plain; charset=utf-8", **kwargs):
        super().__init__(content, content_type=content_type, status=400, **kwargs)


class HttpResponseRedirect(HttpResponse):
    def __init__(self, redirect_to, status=302, **kwargs):
        super().__init__(b"", status=status, headers={"location": redirect_to}, **kwargs)
        self.url = redirect_to


class JsonResponse(HttpResponse):
    def __init__(
        self,
        data,
        encoder=None,
        safe=True,
        json_dumps_params=None,
        **kwargs,
    ):
        if safe and not isinstance(data, dict):
            raise TypeError("JsonResponse safe mode requires a dict")
        dumps_params = json_dumps_params or {}
        content = json.dumps(data, cls=encoder, **dumps_params)
        super().__init__(content, content_type="application/json", **kwargs)
