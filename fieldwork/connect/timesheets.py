"""Time, from where people already track it: Harvest and Toggl Track.

Entries land in the same time table the console writes to, keyed by the
source's entry id, so an edit there updates the row here and a deletion is
removed at the nightly reconcile (which re-reads the last 60 days). People
are matched by email; projects are matched to deployments by name, or by
the mapping in the connection's settings. Time on a project that isn't a
deployment still counts toward the person's week, as internal time.
"""

from __future__ import annotations

import base64
from datetime import timedelta

from .. import audit
from . import core, http
from .core import Provider

WINDOW_DAYS = 60


class TimeSource(Provider):
    category = "time"
    poll_minutes = 30
    reconcile_hours = 24
    settings_fields = (
        {"key": "project_map", "label": "Projects → deployments", "type": "map", "default": {},
         "help": "Project id → deployment id; unmapped projects match a deployment with the same name"},
    )

    def fetch(self, rt, cx, since_day: str, updated_since: str | None) -> tuple[list, dict]:
        """-> (entries, projects {id: {"name", "client"}}); entry: {ext, email, project_id, day, hours}"""
        raise NotImplementedError

    def deployment_for(self, rt, cx, projects: dict, deps: list, pid) -> str | None:
        m = cx.settings.get("project_map") or {}
        if pid is not None and str(pid) in m:
            return m[str(pid)] if any(d["id"] == m[str(pid)] for d in deps) else None
        pr = projects.get(str(pid)) if pid is not None else None
        if not pr:
            return None
        name = (pr.get("name") or "").strip().lower()
        hit = [d for d in deps if d["name"].strip().lower() == name]
        if len(hit) == 1:
            return hit[0]["id"]
        client = (pr.get("client") or "").strip().lower()
        hit = [d for d in deps if client and d["customer"].strip().lower() == client]
        return hit[0]["id"] if len(hit) == 1 else None

    def options(self, rt, cx) -> dict:
        deps = self._deps(rt)
        projects = cx.extra.get("_projects", {})
        return {"deployments": [{"id": d["id"], "name": d["name"]} for d in deps],
                "projects": [{"id": k, "name": v.get("name"), "client": v.get("client"),
                              "deployment_id": self.deployment_for(rt, cx, projects, deps, k)}
                             for k, v in sorted(projects.items(), key=lambda kv: kv[1].get("name") or "")]}

    def _deps(self, rt) -> list:
        return [dict(r) for r in rt.conn.execute(
            "SELECT d.id, d.name, c.name customer FROM deployments d JOIN customers c ON c.id=d.customer_id"
            " WHERE d.tenant_id=?", (rt.tenant_id,))]

    def sync(self, rt, cx, full=False):
        today = core.now().date()
        since_day = (today - timedelta(days=WINDOW_DAYS if full else 14)).isoformat()
        updated_since = None if full else cx.cursor.get("updated_since")
        entries, projects = self.fetch(rt, cx, since_day, updated_since)
        people = rt.users_by_email()
        deps = self._deps(rt)
        n = {"created": 0, "updated": 0, "removed": 0, "unmatched_people": 0}
        seen = set()
        with rt.conn.tx():
            core.save(rt.conn, cx, extra={**cx.extra, "_projects": projects})
            for e in entries:
                u = people.get((e.get("email") or "").lower())
                if not u:
                    n["unmatched_people"] += 1
                    continue
                if not e.get("day") or e.get("hours") is None:
                    continue
                hours = round(max(0.0, min(24.0, float(e["hours"]))), 2)
                seen.add(str(e["ext"]))
                dep = self.deployment_for(rt, cx, projects, deps, e.get("project_id"))
                ex = rt.conn.execute("SELECT id, hours, day, deployment_id, user_id FROM time_entries"
                                     " WHERE connection_id=? AND external_id=?", (cx.id, str(e["ext"]))).fetchone()
                if ex:
                    if (ex["hours"], ex["day"], ex["deployment_id"], ex["user_id"]) != (hours, e["day"], dep, u["id"]):
                        rt.conn.execute("UPDATE time_entries SET hours=?, day=?, deployment_id=?, user_id=? WHERE id=?",
                                        (hours, e["day"], dep, u["id"], ex["id"]))
                        n["updated"] += 1
                elif hours > 0:
                    rt.conn.execute("INSERT INTO time_entries (tenant_id, user_id, deployment_id, day, hours, source,"
                                    " created_at, external_id, connection_id) VALUES (?,?,?,?,?,?,?,?,?)",
                                    (rt.tenant_id, u["id"], dep, e["day"], hours, self.key, audit.now(),
                                     str(e["ext"]), cx.id))
                    n["created"] += 1
            if full:  # anything in the window the source no longer has was deleted there
                stale = [r["id"] for r in rt.conn.execute(
                    "SELECT id, external_id FROM time_entries WHERE connection_id=? AND day>=?", (cx.id, since_day))
                    if r["external_id"] not in seen]
                for i in range(0, len(stale), 200):
                    chunk = stale[i:i + 200]
                    rt.conn.execute(f"DELETE FROM time_entries WHERE id IN ({','.join('?' * len(chunk))})", chunk)
                n["removed"] = len(stale)
            if n["created"] or n["updated"] or n["removed"]:
                rt.log(f"sync:{self.key}", "time.sync", cx.id, {k: v for k, v in n.items() if v})
            core.save(rt.conn, cx, cursor={**cx.cursor, "updated_since": core.iso(core.now() - timedelta(minutes=5))})
        return {"entries": len(entries), **n}


# ==================================================================== Harvest

HARVEST_ID = "https://id.getharvest.com"
HARVEST_API = "https://api.harvestapp.com/v2"


class Harvest(TimeSource):
    key = "harvest"
    name = "Harvest"
    authorize_url = f"{HARVEST_ID}/oauth2/authorize"
    token_url = f"{HARVEST_ID}/api/v2/oauth2/token"
    blurb = "Time entries sync every 30 minutes; projects map to deployments."
    setup = "Create an OAuth2 application at id.getharvest.com/developers with the callback URL above."

    def authorize_params(self, state, challenge):
        return {"client_id": self.client_id(), "response_type": "code", "state": state,
                "redirect_uri": core.redirect_uri()}

    def identify(self, rt, tokens):
        r = http.call("GET", f"{HARVEST_ID}/api/v2/accounts", "Harvest accounts", bearer=tokens["access_token"])
        acct = next((a for a in r.get("accounts", []) if a.get("product") == "harvest"), None)
        if not acct:
            raise core.ConnectError("That Harvest login has no Harvest account")
        return {"external_account_id": str(acct["id"]), "account_name": acct.get("name", "Harvest"),
                "extra": {"account_id": acct["id"]}}

    def _get(self, rt, cx, path, params) -> list:
        """Every page of a Harvest list endpoint."""
        key = path.strip("/").split("/")[0]
        out, page = [], 1
        while page and page <= 50:
            r = core.authed(rt.conn, cx, "GET", f"{HARVEST_API}{path}", f"Harvest {key}",
                            params={**params, "page": page, "per_page": 2000},
                            headers={"Harvest-Account-Id": str(cx.extra["account_id"])})
            out += r.get(key, [])
            page = r.get("next_page")
        return out

    def fetch(self, rt, cx, since_day, updated_since):
        users = {u["id"]: u.get("email") for u in self._get(rt, cx, "/users", {})}
        projects = {str(p["id"]): {"name": p.get("name"), "client": (p.get("client") or {}).get("name")}
                    for p in self._get(rt, cx, "/projects", {})}
        params = {"from": since_day}
        if updated_since:
            params["updated_since"] = updated_since
        entries = [{"ext": e["id"], "email": users.get((e.get("user") or {}).get("id")),
                    "project_id": (e.get("project") or {}).get("id"), "day": e.get("spent_date"),
                    "hours": e.get("hours")} for e in self._get(rt, cx, "/time_entries", params)]
        return entries, projects


# ====================================================================== Toggl

TOGGL = "https://api.track.toggl.com"


class Toggl(TimeSource):
    key = "toggl"
    name = "Toggl Track"
    auth = "token"
    blurb = "Paste a workspace admin's API token; time syncs every 30 minutes."
    setup = ("A workspace admin's API token (Toggl → Profile settings → API Token). Admin rights let Fieldwork read "
             "everyone's time through the Reports API.")

    def _h(self, token: str) -> dict:
        return {"Authorization": "Basic " + base64.b64encode(f"{token}:api_token".encode()).decode()}

    def from_token(self, rt, token, account):
        me = http.request("GET", f"{TOGGL}/api/v9/me", headers=self._h(token))
        if me.status in (401, 403):
            raise core.ConnectError("Toggl didn't accept that API token")
        if not me.ok:
            raise http.HTTPError(f"Toggl sign-in failed ({me.status})", me.status)
        wid = account or str(me.json().get("default_workspace_id") or "")
        if not wid.isdigit():
            raise core.ConnectError("Say which Toggl workspace (its numeric id)")
        return {"api_token": token, "workspace_id": wid}

    def identify(self, rt, tokens):
        w = http.call("GET", f"{TOGGL}/api/v9/workspaces/{tokens['workspace_id']}", "Toggl workspace",
                      headers=self._h(tokens["api_token"]))
        return {"external_account_id": str(tokens["workspace_id"]), "account_name": w.get("name", "Toggl"),
                "extra": {"workspace_id": tokens["workspace_id"]}}

    def fetch(self, rt, cx, since_day, updated_since):
        tok = cx.tokens
        h, wid = self._h(tok["api_token"]), tok["workspace_id"]
        users = {u["id"]: u.get("email") for u in
                 http.call("GET", f"{TOGGL}/api/v9/workspaces/{wid}/users", "Toggl people", headers=h)}
        projects = {str(p["id"]): {"name": p.get("name"), "client": p.get("client_name")} for p in
                    http.call("GET", f"{TOGGL}/api/v9/workspaces/{wid}/projects", "Toggl projects", headers=h,
                              params={"active": "both", "per_page": 200}) or []}
        body = {"start_date": since_day, "end_date": core.now().date().isoformat(), "page_size": 1000}
        entries = []
        for _ in range(50):
            r = http.request("POST", f"{TOGGL}/reports/api/v3/workspace/{wid}/search/time_entries", headers=h,
                             json_body=body)
            if not r.ok:
                raise http.HTTPError(f"Toggl report failed ({r.status})", r.status)
            for row in r.json() or []:
                for te in row.get("time_entries", []):
                    if te.get("seconds", 0) < 0:  # still running
                        continue
                    entries.append({"ext": te["id"], "email": users.get(row.get("user_id")),
                                    "project_id": row.get("project_id"), "day": (te.get("start") or "")[:10],
                                    "hours": round(te.get("seconds", 0) / 3600, 2)})
            nxt_id, nxt_row = r.headers.get("x-next-id"), r.headers.get("x-next-row-number")
            if not nxt_id:
                break
            body = {**body, "first_id": int(nxt_id), "first_row_number": int(nxt_row or 0)}
        return entries, projects


HARVEST = core.register(Harvest())
TOGGL_TRACK = core.register(Toggl())
