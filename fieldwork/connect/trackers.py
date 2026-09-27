"""One-click installs for GitHub, Linear and Jira.

The two-way task sync itself lives in fieldwork/trackers.py and is unchanged:
these providers replace pasted tokens with a sign-in, subscribe to changes
automatically, map assignees to people here, and reconcile every linked task
nightly so a missed webhook can't leave a task stale.

  GitHub  OAuth App. Linking a deployment to a repo creates a signed repo
          webhook for issue events; disconnecting removes it.
  Linear  OAuth app. The app's own webhook (configured once in Linear's app
          settings, signed with FIELDWORK_LINEAR_WEBHOOK_SECRET) delivers
          every workspace's issue changes to /hooks/linear/app.
  Jira    Atlassian OAuth 2.0 (3LO). Linking a project registers a dynamic
          webhook filtered to that project; Jira expires those after 30 days,
          so they're refreshed every 25.
"""


import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import timedelta

from fastapi import HTTPException, Request

from .. import trackers as sync
from . import core, http
from .core import Provider

JIRA_API = "https://api.atlassian.com"


def _repos_for(conn, tenant_id: str, provider: str) -> dict:
    """{target: [deployment ids]} for deployments linked to this tracker."""
    out: dict = {}
    for d in conn.execute("SELECT id, sync_json FROM deployments WHERE tenant_id=?", (tenant_id,)):
        s = json.loads(d["sync_json"] or "{}")
        if s.get("provider") == provider and s.get("target"):
            out.setdefault(s["target"], []).append(d["id"])
    return out


def _links(conn, tenant_id: str, provider: str) -> list:
    return conn.execute("SELECT l.external_id, l.task_id, t.deployment_id, d.sync_json FROM task_links l"
                        " JOIN tasks t ON t.id=l.task_id JOIN deployments d ON d.id=t.deployment_id"
                        " WHERE l.tenant_id=? AND l.provider=?", (tenant_id, provider)).fetchall()


class TrackerApp(Provider):
    category = "tracker"
    poll_minutes = 0
    reconcile_hours = 24
    settings_fields = (
        {"key": "people", "label": "People in the tracker who use a different email", "type": "map",
         "help": "Tracker login, id or email → the person's email here", "default": {}},
    )

    # -- people
    def lookup_email(self, rt, cx, info: dict) -> str | None:
        return None

    def resolver(self, rt, cx):
        by_email = rt.users_by_email()
        people = {str(k).lower(): str(v).lower() for k, v in (cx.settings.get("people") or {}).items() if v}
        cache = dict(cx.extra.get("_emails", {}))
        dirty = []

        def resolve(info: dict) -> str | None:
            keys = [str(info.get(k) or "").lower() for k in ("email", "login", "id")]
            for k in keys:
                if k and k in by_email:
                    return by_email[k]["id"]
                if k and k in people and people[k] in by_email:
                    return by_email[people[k]]["id"]
            ident = str(info.get("id") or info.get("login") or "")
            if not ident:
                return None
            if ident not in cache:
                try:
                    cache[ident] = (self.lookup_email(rt, cx, info) or "").lower()
                except Exception:
                    cache[ident] = ""
                dirty.append(ident)
            e = cache[ident]
            return by_email[e]["id"] if e and e in by_email else None

        def flush():
            if dirty:
                with rt.conn.tx():
                    core.save(rt.conn, cx, extra={**cx.extra, "_emails": cache})
        resolve.flush = flush
        return resolve

    def apply(self, rt, cx, changes: list) -> list:
        resolve = self.resolver(rt, cx)
        changed = sync.apply_inbound(rt.conn, rt.tenant_id, self.key, changes, rt.cfg, resolve)
        resolve.flush()
        return changed

    def after_connect(self, rt, cx):
        linked = sorted(_repos_for(rt.conn, rt.tenant_id, self.key))
        with rt.conn.tx():  # targets already linked by the workspace are the install's to serve
            core.save(rt.conn, cx, extra={**cx.extra, "targets": sorted({*(cx.extra.get("targets") or []), *linked})})
        for target in linked:
            ensure_webhook(rt.conn, rt.tenant_id, self.key, target)


# ===================================================================== GitHub

class GitHubApp(TrackerApp):
    key = "github"
    name = "GitHub"
    authorize_url = "https://github.com/login/oauth/authorize"
    token_url = "https://github.com/login/oauth/access_token"
    scopes = ("repo", "admin:repo_hook", "read:user", "user:email")
    blurb = "Issues sync both ways; repos you link get a webhook automatically."
    setup = ("Register an OAuth App (GitHub → Settings → Developer settings → OAuth Apps) with the callback URL "
             "above. Repos linked to a deployment get their webhook created for you.")

    def api(self, rt, cx=None) -> str:
        """The API host is fixed when the app is installed, so the token can't later be pointed elsewhere."""
        if cx is not None:
            return (cx.extra.get("api_base") or "https://api.github.com").rstrip("/")
        return rt.cfg["integrations"]["github"]["api_base"].rstrip("/")

    def authorize_params(self, state, challenge):
        return {"client_id": self.client_id(), "redirect_uri": core.redirect_uri(), "state": state,
                "scope": " ".join(self.scopes), "allow_signup": "false"}

    def exchange(self, code, verifier):
        r = http.call("POST", self.token_url, "GitHub sign-in", form={
            "client_id": self.client_id(), "client_secret": self.client_secret(), "code": code,
            "redirect_uri": core.redirect_uri()})
        if r.get("error") or not r.get("access_token"):
            raise core.ConnectError(f"GitHub sign-in failed: {r.get('error_description') or r.get('error')}")
        return r

    def identify(self, rt, tokens):
        me = http.call("GET", f"{self.api(rt)}/user", "GitHub profile", bearer=tokens["access_token"])
        return {"external_account_id": str(me.get("id", "")), "account_name": me.get("login", "GitHub"),
                "extra": {"login": me.get("login"), "api_base": self.api(rt)}}

    def lookup_email(self, rt, cx, info):
        if not info.get("login"):
            return None
        u = http.call("GET", f"{self.api(rt, cx)}/users/{info['login']}", "GitHub user",
                      bearer=core.access_token(rt.conn, cx))
        return u.get("email")

    def ensure(self, rt, cx, target: str) -> dict:
        hooks = cx.webhook.get("repos", {})
        if target in hooks:
            return {"status": "exists"}
        secret = cx.webhook.get("secret") or secrets.token_urlsafe(32)
        r = http.call("POST", f"{self.api(rt, cx)}/repos/{target}/hooks", "GitHub webhook",
                      bearer=core.access_token(rt.conn, cx),
                      json_body={"name": "web", "active": True, "events": ["issues"],
                                 "config": {"url": core.hook_url("github", cx.id), "content_type": "json",
                                            "secret": secret, "insecure_ssl": "0"}})
        with rt.conn.tx():
            core.save(rt.conn, cx, webhook={**cx.webhook, "secret": secret, "repos": {**hooks, target: r["id"]}})
        return {"status": "created"}

    def before_disconnect(self, rt, cx):
        tok = core.access_token(rt.conn, cx)
        for target, hid in cx.webhook.get("repos", {}).items():
            http.request("DELETE", f"{self.api(rt, cx)}/repos/{target}/hooks/{hid}", bearer=tok)

    def verify(self, rt, cx, headers, body, query):
        secret = cx.webhook.get("secret")
        if not secret:
            return False
        mac = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(mac, headers.get("x-hub-signature-256", ""))

    def split(self, headers, payload):
        ev = headers.get("x-github-event", "")
        if ev not in ("issues", "ping"):
            return []
        return [(headers.get("x-github-delivery", ""), ev, payload)]

    def handle(self, rt, cx, kind, payload):
        if kind == "issues":
            self.apply(rt, cx, sync.GitHub.parse({"x-github-event": "issues"}, payload))

    def sync(self, rt, cx, full=False):
        tok = core.access_token(rt.conn, cx)
        changes = []
        for link in _links(rt.conn, rt.tenant_id, "github")[:300]:
            target = json.loads(link["sync_json"] or "{}").get("target")
            if not target:
                continue
            num = link["external_id"].rsplit("#", 1)[-1]
            r = http.request("GET", f"{self.api(rt, cx)}/repos/{target}/issues/{num}", bearer=tok)
            if r.ok:
                changes += sync.GitHub.parse({"x-github-event": "issues"},
                                             {"issue": r.json(), "repository": {"full_name": target}})
        return {"checked": len(changes), "changed": len(self.apply(rt, cx, changes))}


# ===================================================================== Linear

class LinearApp(TrackerApp):
    key = "linear"
    name = "Linear"
    authorize_url = "https://linear.app/oauth/authorize"
    token_url = "https://api.linear.app/oauth/token"
    scopes = ("read", "write")
    scope_sep = ","
    blurb = "Issues sync both ways through Linear's app webhook."
    setup = ("Create an OAuth application (Linear → Settings → API → OAuth applications) with the callback URL "
             "above. Turn on its webhook for Issues pointing at {public}/hooks/linear/app and set "
             "FIELDWORK_LINEAR_WEBHOOK_SECRET to its signing secret.")

    def env_needed(self):
        return super().env_needed() + ["FIELDWORK_LINEAR_WEBHOOK_SECRET"]

    def gql(self, rt, cx, query: str, variables: dict | None = None, token: str | None = None) -> dict:
        base = (cx.extra.get("api_base") if cx is not None else None) or rt.cfg["integrations"]["linear"]["api_base"]
        api = base.rstrip("/") + "/graphql"
        r = http.call("POST", api, "Linear request", json_body={"query": query, "variables": variables or {}},
                      bearer=token or core.access_token(rt.conn, cx))
        if r.get("errors"):
            raise http.HTTPError(f"Linear request failed: {str(r['errors'])[:300]}")
        return r["data"]

    def identify(self, rt, tokens):
        d = self.gql(rt, None, "{ viewer { id name email } organization { id name urlKey } }",
                     token=tokens["access_token"])
        return {"external_account_id": d["organization"]["id"], "account_name": d["organization"]["name"],
                "extra": {"url_key": d["organization"].get("urlKey"), "installed_by": d["viewer"].get("email"),
                          "api_base": rt.cfg["integrations"]["linear"]["api_base"].rstrip("/")}}

    def lookup_email(self, rt, cx, info):
        if not info.get("id"):
            return None
        return (self.gql(rt, cx, "query($id:String!){ user(id:$id){ email } }", {"id": info["id"]})
                .get("user") or {}).get("email")

    def ensure(self, rt, cx, target):
        return {"status": "app" if os.environ.get("FIELDWORK_LINEAR_WEBHOOK_SECRET") else "needs_app_webhook"}

    def routes(self, app, d):
        conn = d.conn
        prov = self

        @app.post("/hooks/linear/app", include_in_schema=False)
        async def linear_app(request: Request):
            body = await request.body()
            secret = os.environ.get("FIELDWORK_LINEAR_WEBHOOK_SECRET", "")
            mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            if not secret or not hmac.compare_digest(mac, request.headers.get("linear-signature", "")):
                raise HTTPException(401, "bad signature")
            p = json.loads(body or b"{}")
            ts = p.get("webhookTimestamp")
            if not isinstance(ts, (int, float)) or abs(time.time() * 1000 - ts) > 60 * 1000:  # Linear's advice
                raise HTTPException(401, "stale delivery")
            import asyncio
            return {"results": await asyncio.to_thread(prov.receive, conn, p)}

    def receive(self, conn, p: dict) -> list:
        data = p.get("data") or {}
        ext = f"{p.get('webhookId', '')}:{data.get('id', '')}:{data.get('updatedAt', p.get('createdAt', ''))}"
        results = []
        for r in conn.execute("SELECT * FROM connections WHERE provider='linear' AND status='active'"
                              " AND external_account_id=?", (p.get("organizationId", ""),)).fetchall():
            eid = core.store_event(conn, core.Conn(r), ext, p.get("type", ""), p)
            if eid:
                results.append(core.apply_event(conn, eid))
        return results

    def handle(self, rt, cx, kind, payload):
        if kind == "Issue":
            self.apply(rt, cx, sync.Linear.parse({}, payload))

    def sync(self, rt, cx, full=False):
        ids = [l["external_id"] for l in _links(rt.conn, rt.tenant_id, "linear")][:500]
        changes = []
        for i in range(0, len(ids), 100):
            d = self.gql(rt, cx, "query($ids:[ID!]){ issues(first:100, filter:{id:{in:$ids}}){ nodes{"
                                 " id title assignee{ id email name } state{ type } } } }", {"ids": ids[i:i + 100]})
            for n in d["issues"]["nodes"]:
                changes += sync.Linear.parse({}, {"type": "Issue", "data": n})
        return {"checked": len(changes), "changed": len(self.apply(rt, cx, changes))}


# ======================================================================= Jira

def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_jwt_hs256(token: str, secret: str) -> dict | None:
    try:
        h, p, sig = token.split(".")
        if json.loads(_b64d(h)).get("alg") != "HS256":
            return None
        want = hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(want, _b64d(sig)):
            return None
        claims = json.loads(_b64d(p))
    except (ValueError, TypeError):
        return None
    if claims.get("exp") and claims["exp"] < time.time() - 60:
        return None
    return claims


class JiraApp(TrackerApp):
    key = "jira"
    name = "Jira"
    authorize_url = "https://auth.atlassian.com/authorize"
    token_url = "https://auth.atlassian.com/oauth/token"
    scopes = ("read:jira-work", "write:jira-work", "read:jira-user", "manage:jira-webhook", "offline_access")
    blurb = "Issues sync both ways; linked projects get a webhook that's renewed for you."
    setup = ("Create an OAuth 2.0 (3LO) app in the Atlassian developer console, add the callback URL above, and "
             "grant Jira API scopes: " + ", ".join(scopes) + ".")

    def authorize_params(self, state, challenge):
        return {"audience": "api.atlassian.com", "client_id": self.client_id(), "scope": " ".join(self.scopes),
                "redirect_uri": core.redirect_uri(), "state": state, "response_type": "code", "prompt": "consent"}

    def exchange(self, code, verifier):
        return http.call("POST", self.token_url, "Atlassian sign-in", json_body={
            "grant_type": "authorization_code", "client_id": self.client_id(), "client_secret": self.client_secret(),
            "code": code, "redirect_uri": core.redirect_uri()})

    def refresh(self, tokens):
        r = http.request("POST", self.token_url, json_body={
            "grant_type": "refresh_token", "client_id": self.client_id(), "client_secret": self.client_secret(),
            "refresh_token": tokens.get("refresh_token", "")})
        if r.status in (400, 401, 403):
            raise core.NeedsReauth("Atlassian refused the refresh token; reconnect Jira")
        if not r.ok:
            raise http.HTTPError(f"Atlassian token refresh failed ({r.status})", r.status)
        return {**tokens, **r.json()}

    def identify(self, rt, tokens):
        sites = http.call("GET", f"{JIRA_API}/oauth/token/accessible-resources", "Atlassian sites",
                          bearer=tokens["access_token"])
        site = next((s for s in sites if any("jira" in sc for sc in s.get("scopes", []))), sites[0] if sites else None)
        if not site:
            raise core.ConnectError("That Atlassian account can't reach any Jira site")
        return {"external_account_id": site["id"], "account_name": site.get("name") or site.get("url", "Jira"),
                "extra": {"cloud_id": site["id"], "site_url": site.get("url", "")}}

    def base(self, cx) -> str:
        return f"{JIRA_API}/ex/jira/{cx.extra['cloud_id']}"

    def lookup_email(self, rt, cx, info):
        if not info.get("id"):
            return None
        u = http.call("GET", f"{self.base(cx)}/rest/api/3/user", "Jira user", params={"accountId": info["id"]},
                      bearer=core.access_token(rt.conn, cx))
        return u.get("emailAddress")

    def _register(self, rt, cx, projects: list) -> None:
        """One webhook covering every linked project: Jira allows an OAuth app 5 per user per site."""
        base, tok = self.base(cx), core.access_token(rt.conn, cx)
        old = cx.webhook.get("webhook_id")
        if old:
            http.request("DELETE", f"{base}/rest/api/3/webhook", bearer=tok, json_body={"webhookIds": [old]})
        jql = "project in (" + ", ".join('"' + p.replace('"', '') + '"' for p in projects) + ")"
        r = http.call("POST", f"{base}/rest/api/3/webhook", "Jira webhook", bearer=tok, json_body={
            "url": core.hook_url("jira", cx.id, cx.webhook.get("key", "")),
            "webhooks": [{"events": ["jira:issue_created", "jira:issue_updated"], "jqlFilter": jql}]})
        res = (r.get("webhookRegistrationResult") or [{}])[0]
        if res.get("errors"):
            raise http.HTTPError(f"Jira webhook: {'; '.join(res['errors'])}")
        with rt.conn.tx():
            core.save(rt.conn, cx, webhook={**cx.webhook, "webhook_id": res.get("createdWebhookId"),
                                            "projects": projects,
                                            "renew_at": core.iso(core.now() + timedelta(days=25))})

    def ensure(self, rt, cx, target):
        have = list(cx.webhook.get("projects") or [])
        if target in have and cx.webhook.get("webhook_id"):
            return {"status": "exists"}
        self._register(rt, cx, sorted(set(have) | {target}))
        return {"status": "created"}

    def renew(self, rt, cx):
        wid = cx.webhook.get("webhook_id")
        if wid:
            r = http.request("PUT", f"{self.base(cx)}/rest/api/3/webhook/refresh",
                             bearer=core.access_token(rt.conn, cx), json_body={"webhookIds": [wid]})
            if r.status in (400, 404):  # it lapsed while we were away: register it again
                self._register(rt, cx, list(cx.webhook.get("projects") or []))
                return
            if not r.ok:
                raise http.HTTPError(f"Jira webhook refresh failed ({r.status})", r.status)
        with rt.conn.tx():
            core.save(rt.conn, cx, webhook={**cx.webhook, "renew_at": core.iso(core.now() + timedelta(days=25))})

    def before_disconnect(self, rt, cx):
        wid = cx.webhook.get("webhook_id")
        if wid:
            http.request("DELETE", f"{self.base(cx)}/rest/api/3/webhook", bearer=core.access_token(rt.conn, cx),
                         json_body={"webhookIds": [wid]})

    def verify(self, rt, cx, headers, body, query):
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer ") and self.client_secret():
            return verify_jwt_hs256(auth[7:].strip(), self.client_secret()) is not None
        key = cx.webhook.get("key", "")
        return bool(key) and hmac.compare_digest(query.get("k", ""), key)

    def split(self, headers, payload):
        ev = payload.get("webhookEvent", "")
        if not ev.startswith("jira:issue"):
            return []
        ext = headers.get("x-atlassian-webhook-identifier") or \
            f"{payload.get('timestamp', '')}:{(payload.get('issue') or {}).get('key', '')}"
        return [(ext, ev, payload)]

    def handle(self, rt, cx, kind, payload):
        self.apply(rt, cx, sync.Jira.parse({}, payload))

    def sync(self, rt, cx, full=False):
        keys = [l["external_id"] for l in _links(rt.conn, rt.tenant_id, "jira")][:500]
        tok = core.access_token(rt.conn, cx)
        changes = []
        for i in range(0, len(keys), 100):
            chunk = keys[i:i + 100]
            r = http.request("POST", f"{self.base(cx)}/rest/api/3/search/jql", bearer=tok, json_body={
                "jql": f"key in ({','.join(chunk)})", "fields": ["summary", "status", "assignee"], "maxResults": 100})
            if r.status == 400:  # a linked issue was deleted or moved: look them up one by one
                issues = []
                for k in chunk:
                    one = http.request("GET", f"{self.base(cx)}/rest/api/3/issue/{k}", bearer=tok,
                                       params={"fields": "summary,status,assignee"})
                    if one.ok:
                        issues.append(one.json())
            elif not r.ok:
                raise http.HTTPError(f"Jira search failed ({r.status})", r.status)
            else:
                issues = r.json().get("issues", [])
            for iss in issues:
                changes += sync.Jira.parse({}, {"webhookEvent": "jira:issue_updated", "issue": iss})
        return {"checked": len(changes), "changed": len(self.apply(rt, cx, changes))}


GITHUB = core.register(GitHubApp())
LINEAR = core.register(LinearApp())
JIRA = core.register(JiraApp())


def ensure_webhook(conn, tenant_id: str, provider: str, target: str) -> dict | None:
    """Subscribe to a newly linked repo/team/project. Best effort: the nightly reconcile covers failures."""
    cx = core.active(conn, tenant_id, provider)
    if not cx:
        return None
    try:
        return core.REGISTRY[provider].ensure(core.Runtime(conn, tenant_id), cx, target)
    except Exception as e:
        with conn.tx():
            core.save(conn, cx, last_error=f"webhook for {target}: {str(e)[:300]}")
        return {"status": "error", "error": str(e)[:300]}
