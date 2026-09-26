"""Customer-built engines: a team's own scripts and integrations, plugged in.

Two kinds, because teams' tools live in two kinds of places:

webhook  Fieldwork calls the team's service when someone clicks Run.
         Request:  POST <url>, JSON body
                   {"engine", "run_id", "deployment": {...}, "input", "requested_by"}
         Headers:  X-Fieldwork-Timestamp: <unix seconds>
                   X-Fieldwork-Signature: sha256=<hex HMAC-SHA256(secret, f"{ts}.{body}")>
         Response: JSON {"summary": str, "status": "pass|warn|fail|info", "result": {...}}

push     The team's script runs wherever it runs (CI, cron, inside the customer's
         environment) and posts results in with an engine token:
             POST /api/ingest/findings   Authorization: Bearer fwe_...
             {"deployment_id", "title", "summary", "status", "result"}

Either way the result becomes a finding, goes through the same second-person
confirmation, and lands in the same audit trail as the built-in engines.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

STATUSES = ("pass", "warn", "fail", "info")
MAX_RESPONSE = 1_000_000
TIMEOUT_S = 20


class PluginError(ValueError):
    pass


def sign(secret: str, ts: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return "sha256=" + mac


def verify_signature(secret: str, ts: str, body: bytes, header: str, max_skew: int = 300) -> bool:
    """Helper for engine authors (also used by the example engine)."""
    try:
        if abs(time.time() - int(ts)) > max_skew:
            return False
    except ValueError:
        return False
    return hmac.compare_digest(sign(secret, ts, body), header or "")


def _guard_host(url: str, allow_private: bool) -> None:
    """Refuse to call private, loopback or link-local addresses (SSRF guard).

    Self-hosted installs whose engines live on an internal network set
    FIELDWORK_ALLOW_PRIVATE_ENGINES=1.
    """
    host = urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise PluginError(f"can't resolve engine host {host!r}")
    if allow_private:
        return
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise PluginError(f"engine host {host!r} resolves to a private address; "
                              "allowed only on self-hosted installs")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        raise PluginError("engine responded with a redirect; engines must answer directly")


_opener = urllib.request.build_opener(_NoRedirect)


def call_webhook(url: str, secret: str, payload: dict, allow_private: bool = False) -> dict:
    _guard_host(url, allow_private)
    body = json.dumps(payload, sort_keys=True).encode()
    ts = str(int(time.time()))
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": "Fieldwork-Engine/1",
        "X-Fieldwork-Timestamp": ts,
        "X-Fieldwork-Signature": sign(secret, ts, body),
    })
    try:
        with _opener.open(req, timeout=TIMEOUT_S) as r:
            raw = r.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as e:
        raise PluginError(f"engine returned HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PluginError(f"couldn't reach engine: {getattr(e, 'reason', e)}")
    if len(raw) > MAX_RESPONSE:
        raise PluginError("engine response too large (1 MB max)")
    try:
        data = json.loads(raw)
    except ValueError:
        raise PluginError("engine didn't return JSON")
    return normalize_result(data)


def normalize_result(data: dict) -> dict:
    if not isinstance(data, dict):
        raise PluginError("engine result must be a JSON object")
    summary = str(data.get("summary") or "").strip()
    if not summary:
        raise PluginError("engine result needs a 'summary'")
    status = data.get("status", "info")
    if status not in STATUSES:
        raise PluginError(f"engine status must be one of {STATUSES}")
    result = data.get("result", {})
    if not isinstance(result, (dict, list)):
        raise PluginError("engine 'result' must be an object or list")
    return {"summary": summary[:2000], "status": status, "result": result}
