"""Two-way task sync with GitHub Issues, Linear and Jira.

A deployment can be linked to one tracker target (a GitHub repo, a Linear team,
a Jira project). From then on:

  Fieldwork -> tracker   task created or changed here: queued in the outbox,
                         created/updated there, link stored in task_links
  tracker -> Fieldwork   the tracker's webhook calls
                         /integrations/<workspace>/<provider>/webhook,
                         signature checked, linked task updated here

Changes that arrive from a tracker are applied with origin="tracker" and are
never pushed back, so the two sides can't echo forever.

Status mapping (Fieldwork: open, in_progress, blocked, done):
  GitHub  open/closed, plus labels "fieldwork:in-progress" and "fieldwork:blocked"
  Linear  workflow state type: unstarted / started / completed (blocked -> started)
  Jira    status category: new / indeterminate / done (blocked -> indeterminate)

Credentials are workspace secrets, encrypted at rest:
  github_token, linear_api_key, jira_api_token, and <provider>_webhook_secret
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone

from . import config, events, plugins

STATUS_LABELS = {"in_progress": "fieldwork:in-progress", "blocked": "fieldwork:blocked"}


class TrackerError(plugins.PluginError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cfg(conn, tenant_id: str) -> dict:
    t = conn.execute("SELECT config_json FROM tenants WHERE id=?", (tenant_id,)).fetchone()
    return config.upgrade(json.loads(t["config_json"]))


def _need(value, what: str):
    if not value:
        raise TrackerError(f"{what} isn't set")
    return value


def task_body(task: dict, dep_id: str) -> str:
    return (f"{task['title']}\n\nFrom Fieldwork · {events.dep_link(dep_id)}\n"
            f"Status: {task['status'].replace('_', ' ')}" + (f" · due {task['due']}" if task.get("due") else ""))


# ------------------------------------------------------------------ GitHub

class GitHub:
    name = "github"

    def __init__(self, conn, tenant_id, cfg):
        self.api = cfg["integrations"]["github"]["api_base"]
        self.token = _need(events.secret(conn, tenant_id, "github_token"), "GitHub token")

    def _h(self):
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def _labels(self, status):
        return [STATUS_LABELS[status]] if status in STATUS_LABELS else []

    def create(self, target: str, task: dict, dep_id: str) -> tuple[str, str]:
        code, r = events.http_json(f"{self.api}/repos/{target}/issues", {
            "title": task["title"], "body": task_body(task, dep_id), "labels": self._labels(task["status"])},
            self._h())
        if code >= 300:
            raise TrackerError(f"GitHub create failed ({code}): {r}")
        if task["status"] == "done":
            self.update(target, str(r["number"]), task)
        return str(r["number"]), r.get("html_url", "")

    def update(self, target: str, ext: str, task: dict) -> None:
        code, r = events.http_json(f"{self.api}/repos/{target}/issues/{ext}", {
            "title": task["title"], "state": "closed" if task["status"] == "done" else "open",
            "labels": self._labels(task["status"])}, self._h(), method="PATCH")
        if code >= 300:
            raise TrackerError(f"GitHub update failed ({code}): {r}")

    @staticmethod
    def verify(secret: str, headers: dict, body: bytes) -> bool:
        mac = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(mac, headers.get("x-hub-signature-256", ""))

    @staticmethod
    def parse(headers: dict, payload: dict) -> list[tuple[str, dict]]:
        if headers.get("x-github-event") != "issues" or "issue" not in payload:
            return []
        iss = payload["issue"]
        labels = {l["name"] for l in iss.get("labels", [])}
        if iss.get("state") == "closed":
            status = "done"
        elif STATUS_LABELS["blocked"] in labels:
            status = "blocked"
        elif STATUS_LABELS["in_progress"] in labels:
            status = "in_progress"
        else:
            status = "open"
        return [(str(iss["number"]), {"status": status, "title": iss.get("title")})]


# ------------------------------------------------------------------ Linear

LINEAR_TYPES = {"open": "unstarted", "in_progress": "started", "blocked": "started", "done": "completed"}


class Linear:
    name = "linear"

    def __init__(self, conn, tenant_id, cfg):
        self.api = cfg["integrations"]["linear"]["api_base"] + "/graphql"
        self.key = _need(events.secret(conn, tenant_id, "linear_api_key"), "Linear API key")

    def _q(self, query: str, variables: dict) -> dict:
        code, r = events.http_json(self.api, {"query": query, "variables": variables}, {"Authorization": self.key})
        if code >= 300 or not isinstance(r, dict) or r.get("errors"):
            raise TrackerError(f"Linear request failed ({code}): {r}")
        return r["data"]

    def _state(self, team: str, status: str) -> str | None:
        d = self._q("query($t:ID!){ team(id:$t){ states{ nodes{ id type position } } } }", {"t": team})
        want = LINEAR_TYPES[status]
        states = sorted((n for n in d["team"]["states"]["nodes"] if n["type"] == want), key=lambda n: n["position"])
        return states[0]["id"] if states else None

    def create(self, target: str, task: dict, dep_id: str) -> tuple[str, str]:
        inp = {"teamId": target, "title": task["title"], "description": task_body(task, dep_id)}
        sid = self._state(target, task["status"])
        if sid:
            inp["stateId"] = sid
        d = self._q("mutation($i:IssueCreateInput!){ issueCreate(input:$i){ success issue{ id url } } }", {"i": inp})
        iss = d["issueCreate"]["issue"]
        return iss["id"], iss.get("url", "")

    def update(self, target: str, ext: str, task: dict) -> None:
        inp = {"title": task["title"]}
        sid = self._state(target, task["status"])
        if sid:
            inp["stateId"] = sid
        self._q("mutation($id:String!,$i:IssueUpdateInput!){ issueUpdate(id:$id,input:$i){ success } }",
                {"id": ext, "i": inp})

    @staticmethod
    def verify(secret: str, headers: dict, body: bytes) -> bool:
        mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(mac, headers.get("linear-signature", ""))

    @staticmethod
    def parse(headers: dict, payload: dict) -> list[tuple[str, dict]]:
        if payload.get("type") != "Issue" or not payload.get("data"):
            return []
        d = payload["data"]
        st = (d.get("state") or {}).get("type")
        status = {"completed": "done", "canceled": "done", "started": "in_progress",
                  "unstarted": "open", "backlog": "open", "triage": "open"}.get(st)
        ch = {"title": d.get("title")}
        if status:
            ch["status"] = status
        return [(d["id"], ch)]


# -------------------------------------------------------------------- Jira

JIRA_CATEGORY = {"open": "new", "in_progress": "indeterminate", "blocked": "indeterminate", "done": "done"}


class Jira:
    name = "jira"

    def __init__(self, conn, tenant_id, cfg):
        j = cfg["integrations"]["jira"]
        self.base = _need(j["base_url"], "Jira site URL")
        email = _need(j["email"], "Jira account email")
        token = _need(events.secret(conn, tenant_id, "jira_api_token"), "Jira API token")
        self.auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()

    def _call(self, path, body=None, method="POST"):
        code, r = events.http_json(self.base + path, body, {"Authorization": self.auth}, method=method)
        if code >= 300:
            raise TrackerError(f"Jira request failed ({code}): {r}")
        return r

    def _transition(self, key: str, status: str) -> None:
        want = JIRA_CATEGORY[status]
        ts = self._call(f"/rest/api/3/issue/{key}/transitions", None, "GET").get("transitions", [])
        t = next((t for t in ts if t.get("to", {}).get("statusCategory", {}).get("key") == want), None)
        if t:
            self._call(f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": t["id"]}})

    def create(self, target: str, task: dict, dep_id: str) -> tuple[str, str]:
        doc = {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": task_body(task, dep_id)}]}]}
        r = self._call("/rest/api/3/issue", {"fields": {
            "project": {"key": target}, "summary": task["title"], "issuetype": {"name": "Task"},
            "description": doc}})
        if task["status"] != "open":
            self._transition(r["key"], task["status"])
        return r["key"], f"{self.base}/browse/{r['key']}"

    def update(self, target: str, ext: str, task: dict) -> None:
        self._call(f"/rest/api/3/issue/{ext}", {"fields": {"summary": task["title"]}}, "PUT")
        self._transition(ext, task["status"])

    @staticmethod
    def verify(secret: str, headers: dict, body: bytes) -> bool:
        mac = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(mac, headers.get("x-hub-signature", ""))

    @staticmethod
    def parse(headers: dict, payload: dict) -> list[tuple[str, dict]]:
        iss = payload.get("issue")
        if not iss or not payload.get("webhookEvent", "").startswith("jira:issue"):
            return []
        f = iss.get("fields", {})
        cat = ((f.get("status") or {}).get("statusCategory") or {}).get("key")
        ch = {"title": f.get("summary")}
        status = {"new": "open", "indeterminate": "in_progress", "done": "done"}.get(cat)
        if status:
            ch["status"] = status
        return [(iss["key"], ch)]


ADAPTERS = {"github": GitHub, "linear": Linear, "jira": Jira}


# ----------------------------------------------------------- orchestration

def queue_push(conn, tenant_id: str, cfg: dict, dep_row, task_id: str, origin: str = "fieldwork") -> None:
    """Called inside the task's transaction. Queues a create/update if the deployment syncs."""
    if origin == "tracker":
        return
    sync = json.loads(dep_row["sync_json"] or "{}")
    prov = sync.get("provider")
    if not prov or not cfg["integrations"].get(prov, {}).get("enabled"):
        return
    events.enqueue(conn, tenant_id, "tracker", {"provider": prov, "target": sync["target"], "task_id": task_id,
                                                "deployment_id": dep_row["id"]})


def deliver(conn, tenant_id: str, p: dict) -> None:
    cfg = _cfg(conn, tenant_id)
    adapter = ADAPTERS[p["provider"]](conn, tenant_id, cfg)
    task = conn.execute("SELECT * FROM tasks WHERE id=? AND tenant_id=?", (p["task_id"], tenant_id)).fetchone()
    if not task:
        return
    link = conn.execute("SELECT * FROM task_links WHERE task_id=? AND provider=?",
                        (p["task_id"], p["provider"])).fetchone()
    if link:
        adapter.update(p["target"], link["external_id"], dict(task))
        with conn.tx():
            conn.execute("UPDATE task_links SET synced_at=? WHERE task_id=? AND provider=?",
                         (_now(), p["task_id"], p["provider"]))
    else:
        ext, url = adapter.create(p["target"], dict(task), p["deployment_id"])
        with conn.tx():
            conn.execute("INSERT INTO task_links (task_id, tenant_id, provider, external_id, url, synced_at)"
                         " VALUES (?,?,?,?,?,?) ON CONFLICT (task_id, provider) DO UPDATE SET"
                         " external_id=excluded.external_id, url=excluded.url, synced_at=excluded.synced_at",
                         (p["task_id"], tenant_id, p["provider"], ext, url, _now()))


def apply_inbound(conn, tenant_id: str, provider: str, changes: list[tuple[str, dict]], cfg: dict) -> list[str]:
    """Apply tracker-side changes to linked tasks. Returns the task ids that changed."""
    from . import audit
    changed = []
    for ext, ch in changes:
        link = conn.execute("SELECT * FROM task_links WHERE tenant_id=? AND provider=? AND external_id=?",
                            (tenant_id, provider, ext)).fetchone()
        if not link:
            continue
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (link["task_id"],)).fetchone()
        upd = {}
        if ch.get("status") and ch["status"] != task["status"]:
            upd["status"] = ch["status"]
        if ch.get("title") and ch["title"] != task["title"]:
            upd["title"] = ch["title"][:200]
        if not upd:
            continue
        upd["updated_at"] = audit.now()
        with conn.tx():
            conn.execute(f"UPDATE tasks SET {', '.join(k + '=?' for k in upd)} WHERE id=?",
                         (*upd.values(), task["id"]))
            conn.execute("UPDATE task_links SET synced_at=? WHERE task_id=? AND provider=?",
                         (_now(), task["id"], provider))
            audit.record(conn, tenant_id, f"tracker:{provider}", "task.update", task["id"],
                         {k: v for k, v in upd.items() if k != "updated_at"} | {"origin": provider})
            dep = conn.execute("SELECT name FROM deployments WHERE id=?", (task["deployment_id"],)).fetchone()
            base = {"deployment_id": task["deployment_id"], "deployment": dep["name"], "title": upd.get("title", task["title"]),
                    "actor": provider.title()}
            if upd.get("status") == "blocked":
                events.emit(conn, cfg, tenant_id, "task.blocked", {**base, "assignee": None})
            elif upd.get("status") == "done":
                events.emit(conn, cfg, tenant_id, "task.done", base)
        changed.append(task["id"])
    return changed
