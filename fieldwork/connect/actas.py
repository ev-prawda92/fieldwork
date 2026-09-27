"""Run a console action as a person, from outside the console.

A button pressed in Slack runs the same route the console calls, as the
person who pressed it, so every permission check, validation and audit entry
is the one that already exists. Nothing is re-implemented for chat.
"""

from __future__ import annotations

import inspect

from fastapi import HTTPException
from pydantic import BaseModel

_routes: dict = {}


def bind(app, d) -> None:
    _routes["app"] = app
    _routes["d"] = d


def _find(method: str, path: str):
    for r in _routes["app"].routes:
        if getattr(r, "path", None) == path and method.upper() in getattr(r, "methods", ()):
            return r.endpoint
    raise LookupError(f"no route {method} {path}")


def call(conn, user, method: str, path: str, body: dict | None = None, via: str = "slack", **params):
    """-> (ok, result or error message)."""
    d = _routes["d"]
    tenant = conn.execute("SELECT * FROM tenants WHERE id=?", (user["tenant_id"],)).fetchone()
    c = d.Ctx(conn, user, tenant, via)
    fn = _find(method, path)
    sig = inspect.signature(fn)
    try:
        kw = {}
        for name, prm in sig.parameters.items():
            if name == "c":
                kw["c"] = c
            elif name == "body":
                ann = prm.annotation
                kw["body"] = ann(**(body or {})) if isinstance(ann, type) and issubclass(ann, BaseModel) else body
            elif name in params:
                kw[name] = params[name]
            elif prm.default is not inspect.Parameter.empty:
                kw[name] = prm.default
        return True, fn(**kw)
    except HTTPException as e:
        return False, e.detail if isinstance(e.detail, str) else str(e.detail)
    except ValueError as e:  # pydantic validation, config errors
        return False, str(e).splitlines()[0][:300]
