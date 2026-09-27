"""Outbound HTTP for connectors.

Every call goes through `transport`, which by default applies the same guard
as engine webhooks (no private addresses unless the install allows them, no
redirects, a size cap). Tests replace `transport` with a fake router, so no
connector test touches the network.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .. import plugins

MAX_BODY = 5_000_000


class HTTPError(plugins.PluginError):
    def __init__(self, msg: str, status: int = 0, body=None):
        super().__init__(msg)
        self.status = status
        self.body = body


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)

    def json(self):
        try:
            return json.loads(self.body) if self.body else {}
        except ValueError:
            return {"_raw": self.body.decode(errors="replace")[:500]}

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _allow_private() -> bool:
    return os.environ.get("FIELDWORK_ALLOW_PRIVATE_ENGINES") == "1"


def default_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> Response:
    plugins._guard_host(url, _allow_private())
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with plugins._opener.open(req, timeout=timeout) as r:
            return Response(r.status, r.read(MAX_BODY), {k.lower(): v for k, v in r.headers.items()})
    except urllib.error.HTTPError as e:
        return Response(e.code, e.read(50_000), {k.lower(): v for k, v in (e.headers or {}).items()})
    except (urllib.error.URLError, OSError) as e:
        host = urllib.parse.urlparse(url).hostname
        raise HTTPError(f"couldn't reach {host}: {getattr(e, 'reason', e)}")


transport = default_transport


def request(method: str, url: str, *, params: dict | None = None, json_body=None, form: dict | None = None,
            headers: dict | None = None, bearer: str | None = None, timeout: float = 20) -> Response:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}, doseq=True)
    h = {"User-Agent": "Fieldwork/1", "Accept": "application/json"}
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode()
        h["Content-Type"] = "application/json"
    elif form is not None:
        body = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    if bearer:
        h["Authorization"] = f"Bearer {bearer}"
    h.update(headers or {})
    return transport(method.upper(), url, h, body, timeout)


def call(method: str, url: str, what: str, **kw):
    """request() that raises on a non-2xx answer and returns the parsed JSON."""
    r = request(method, url, **kw)
    if not r.ok:
        raise HTTPError(f"{what} failed ({r.status}): {str(r.json())[:300]}", r.status, r.json())
    return r.json()
