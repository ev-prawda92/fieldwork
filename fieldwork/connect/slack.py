"""Slack as an app: install once, then act from Slack.

Installing adds Fieldwork to one channel. Notifications arrive there with
buttons for the decision each one asks for: confirm or reassign who owns a
delay, take or resolve a flag, approve or reject an agent's request, say what
a blocked task is waiting on. People get a direct message when work is
assigned to them, with Start and Done. /fieldwork answers "what needs me" and
"how is <deployment>" without leaving Slack.

A button runs the console's own route as the person who pressed it (matched
by their Slack email), so permissions, validation and the audit trail are
exactly the console's.

App setup (once per install of Fieldwork):
  FIELDWORK_SLACK_CLIENT_ID, FIELDWORK_SLACK_CLIENT_SECRET, FIELDWORK_SLACK_SIGNING_SECRET
  Redirect URL           {public}/oauth/callback
  Interactivity URL      {public}/hooks/slack/interact
  Slash command          /fieldwork -> {public}/hooks/slack/command
  Event subscriptions    {public}/hooks/slack/events  (app_uninstalled, tokens_revoked)
"""


import asyncio
import hashlib
import hmac
import json
import os
import threading
import time
from urllib.parse import parse_qs

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .. import events
from . import actas, core, http
from .core import Provider

API = "https://slack.com/api"
OWNER_KEYS = ("customer", "team", "model_vendor", "software_vendor")


def run_later(fn, *args) -> None:
    """Work Slack shouldn't wait for. Tests replace this to run inline."""
    threading.Thread(target=fn, args=args, daemon=True).start()


def _ok(r: dict, what: str) -> dict:
    if not r.get("ok"):
        raise http.HTTPError(f"Slack {what}: {r.get('error', 'failed')}", 200, r)
    return r


class Slack(Provider):
    key = "slack"
    name = "Slack"
    category = "chat"
    authorize_url = "https://slack.com/oauth/v2/authorize"
    token_url = f"{API}/oauth.v2.access"
    scopes = ("chat:write", "chat:write.public", "commands", "incoming-webhook", "users:read",
              "users:read.email", "im:write")
    scope_sep = ","
    reconcile_hours = 0
    blurb = "Notifications with buttons, direct messages on assignment, and /fieldwork."
    setup = ("Create a Slack app (api.slack.com/apps). Add the redirect URL, turn on Interactivity with "
             "{public}/hooks/slack/interact, add the /fieldwork slash command at {public}/hooks/slack/command, "
             "and subscribe to app_uninstalled and tokens_revoked at {public}/hooks/slack/events. "
             "Bot scopes: " + ", ".join(scopes) + ".")
    settings_fields = (
        {"key": "channel_id", "label": "Channel ID for notifications", "type": "text",
         "help": "Defaults to the channel picked at install"},
        {"key": "dm_assignments", "label": "Message people when work is assigned to them", "type": "bool",
         "default": True},
    )

    def signing_secret(self) -> str:
        return os.environ.get("FIELDWORK_SLACK_SIGNING_SECRET", "")

    def env_needed(self):
        return super().env_needed() + ["FIELDWORK_SLACK_SIGNING_SECRET"]

    def configured(self) -> bool:
        return super().configured() and bool(self.signing_secret())

    def authorize_params(self, state, challenge):
        return {"client_id": self.client_id(), "scope": ",".join(self.scopes), "redirect_uri": core.redirect_uri(),
                "state": state}

    def exchange(self, code, verifier):
        r = http.call("POST", self.token_url, "Slack sign-in", form={
            "client_id": self.client_id(), "client_secret": self.client_secret(), "code": code,
            "redirect_uri": core.redirect_uri()})
        _ok(r, "sign-in")
        return {"access_token": r["access_token"], "refresh_token": r.get("refresh_token"),
                "expires_in": r.get("expires_in"), "bot_user_id": r.get("bot_user_id"),
                "team": r.get("team") or {}, "webhook": r.get("incoming_webhook") or {}, "app_id": r.get("app_id")}

    def refresh(self, tokens):
        r = http.call("POST", self.token_url, "Slack token refresh", form={
            "client_id": self.client_id(), "client_secret": self.client_secret(),
            "grant_type": "refresh_token", "refresh_token": tokens.get("refresh_token", "")})
        if not r.get("ok"):
            raise core.NeedsReauth(f"Slack refused the refresh token ({r.get('error')}); reconnect it")
        return {**tokens, "access_token": r["access_token"], "refresh_token": r.get("refresh_token"),
                "expires_in": r.get("expires_in")}

    def identify(self, rt, tokens):
        team, hook = tokens.get("team") or {}, tokens.get("webhook") or {}
        other = rt.conn.execute("SELECT 1 FROM connections WHERE provider='slack' AND status='active'"
                                " AND external_account_id=? AND tenant_id!=?", (team.get("id", ""), rt.tenant_id)).fetchone()
        if other:  # buttons and commands are routed by Slack workspace, so it belongs to one of ours
            raise core.ConnectError("That Slack workspace is already connected to another Fieldwork workspace")
        return {"external_account_id": team.get("id", ""), "account_name": team.get("name", "Slack"),
                "extra": {"channel": hook.get("channel", ""), "channel_id": hook.get("channel_id", ""),
                          "bot_user_id": tokens.get("bot_user_id"), "app_id": tokens.get("app_id")}}

    def after_connect(self, rt, cx):
        with rt.conn.tx():
            if not cx.settings.get("channel_id") and cx.extra.get("channel_id"):
                core.save(rt.conn, cx, settings={**cx.settings, "channel_id": cx.extra["channel_id"]})

            def on(cfg):  # the events that come with a decision button are the point of the app
                sl = cfg["integrations"]["slack"]
                sl["enabled"] = True
                sl["events"] = list(dict.fromkeys(sl.get("events", []) + ["delay.opened", "flag.raised",
                                                                           "approval.requested", "task.blocked"]))
            core.update_config(rt.conn, rt.tenant_id, cx["created_by"], on)

    def before_disconnect(self, rt, cx):
        try:
            http.request("POST", f"{API}/auth.revoke", bearer=core.access_token(rt.conn, cx))
        finally:
            if not events.secret(rt.conn, rt.tenant_id, "slack_webhook_url"):
                with rt.conn.tx():
                    def off(cfg):
                        cfg["integrations"]["slack"]["enabled"] = False
                    core.update_config(rt.conn, rt.tenant_id, "system", off)

    # ----------------------------------------------------------- API calls

    def api(self, conn, cx, method: str, **body) -> dict:
        r = http.call("POST", f"{API}/{method}", f"Slack {method}", json_body=body,
                      bearer=core.access_token(conn, cx))
        return _ok(r, method)

    def get(self, conn, cx, method: str, **params) -> dict:
        r = http.call("GET", f"{API}/{method}", f"Slack {method}", params=params,
                      bearer=core.access_token(conn, cx))
        return _ok(r, method)

    def post(self, conn, cx, text: str, blocks: list, channel: str | None = None) -> dict:
        ch = channel or cx.settings.get("channel_id") or cx.extra.get("channel_id")
        try:
            if not ch:
                raise http.HTTPError("Slack: no channel", 200, {"error": "channel_not_found"})
            return self.api(conn, cx, "chat.postMessage", channel=ch, text=text, blocks=blocks,
                            unfurl_links=False, unfurl_media=False)
        except http.HTTPError as e:
            hook = cx.tokens.get("webhook", {}).get("url")
            err = (e.body or {}).get("error") if isinstance(e.body, dict) else None
            if channel is None and hook and err in ("not_in_channel", "channel_not_found", "is_archived"):
                r = http.request("POST", hook, json_body={"text": text, "blocks": blocks})
                if r.ok:
                    return {"ok": True, "via": "incoming_webhook"}
            raise

    MAP_TTL = 3600  # re-check who a Slack user is at least hourly

    def slack_user_for(self, conn, cx, email: str) -> str | None:
        """Slack user id for an email. Only hits are cached (for an hour); a miss is asked again next time."""
        cache = cx.extra.get("_by_email", {})
        hit = cache.get(email.lower())
        if isinstance(hit, list) and hit[1] > time.time() - self.MAP_TTL:
            return hit[0]
        try:
            u = self.get(conn, cx, "users.lookupByEmail", email=email)["user"]["id"]
        except http.HTTPError:
            return None
        with conn.tx():
            core.save(conn, cx, extra={**cx.extra, "_by_email": {**cache, email.lower(): [u, time.time()]}})
        return u

    def person_for(self, conn, cx, slack_user: str):
        """The Fieldwork person behind a Slack user, matched by their confirmed Slack email.
        A match is trusted for an hour and only while the person's email here is still that email."""
        known = cx.extra.get("_people", {})
        hit = known.get(slack_user)
        if isinstance(hit, list) and hit[2] > time.time() - self.MAP_TTL:
            u = conn.execute("SELECT * FROM users WHERE id=? AND tenant_id=?", (hit[0], cx.tenant_id)).fetchone()
            if u and u["email"].lower() == hit[1]:
                return u
        try:
            user = self.get(conn, cx, "users.info", user=slack_user)["user"]
        except http.HTTPError:
            return None
        if user.get("deleted") or user.get("is_email_confirmed") is False:
            return None
        email = ((user.get("profile") or {}).get("email") or "").lower()
        if not email:
            return None
        u = conn.execute("SELECT * FROM users WHERE tenant_id=? AND lower(email)=?", (cx.tenant_id, email)).fetchone()
        if u:
            with conn.tx():
                core.save(conn, cx, extra={**cx.extra, "_people": {**known, slack_user: [u["id"], email, time.time()]}})
        return u

    # ----------------------------------------------------- inbound (Slack)

    def verify_request(self, headers: dict, body: bytes) -> bool:
        secret = self.signing_secret()
        ts = headers.get("x-slack-request-timestamp", "")
        if not secret or not ts.isdigit() or abs(time.time() - int(ts)) > 300:
            return False
        mac = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(mac, headers.get("x-slack-signature", ""))

    def connections_for_team(self, conn, team_id: str) -> list:
        return [core.Conn(r) for r in conn.execute(
            "SELECT * FROM connections WHERE provider='slack' AND status='active' AND external_account_id=?",
            (team_id,)).fetchall()]

    def routes(self, app, d):
        conn = d.conn
        prov = self

        async def read(request: Request) -> tuple[dict, bytes]:
            body = await request.body()
            headers = {k.lower(): v for k, v in request.headers.items()}
            if not prov.verify_request(headers, body):
                raise HTTPException(401, "bad signature")
            return headers, body

        @app.post("/hooks/slack/events", include_in_schema=False)
        async def slack_events(request: Request):
            raw = await request.body()
            try:
                first = json.loads(raw or b"{}")
            except ValueError:
                raise HTTPException(400, "not JSON")
            if isinstance(first, dict) and first.get("type") == "url_verification":
                # Echoing the challenge reveals nothing, and Slack sends it while the app is being created,
                # before its signing secret can be on this server. Everything else must be signed.
                return {"challenge": str(first.get("challenge", ""))[:200]}
            _, body = await read(request)
            p = json.loads(body or b"{}")
            return await asyncio.to_thread(prov.on_event, conn, p)

        @app.post("/hooks/slack/interact", include_in_schema=False)
        async def slack_interact(request: Request):
            _, body = await read(request)
            payload = json.loads(parse_qs(body.decode()).get("payload", ["{}"])[0])
            if payload.get("type") == "block_actions" and payload.get("actions"):
                run_later(prov.on_action, conn, payload)  # Slack wants an answer within 3 seconds
            return JSONResponse({}, status_code=200)

        @app.post("/hooks/slack/command", include_in_schema=False)
        async def slack_command(request: Request):
            _, body = await read(request)
            f = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            run_later(prov.answer_command, conn, f)  # an empty 200 now, the answer via response_url
            return Response(status_code=200)

    def on_event(self, conn, p: dict) -> dict:
        ev = p.get("event") or {}
        for cx in self.connections_for_team(conn, p.get("team_id", "")):
            eid = core.store_event(conn, cx, p.get("event_id", ""), ev.get("type", ""), p)
            if eid:
                core.apply_event(conn, eid)
        return {"ok": True}

    def answer_command(self, conn, f: dict) -> None:
        reply = self.on_command(conn, f)
        if f.get("response_url"):
            try:
                http.request("POST", f["response_url"], json_body=reply, timeout=5)
            except Exception:
                pass

    def handle(self, rt, cx, kind, payload):
        if kind == "tokens_revoked" and not ((payload.get("event") or {}).get("tokens") or {}).get("bot"):
            return  # a person revoked their own user token; the install still works
        if kind in ("app_uninstalled", "tokens_revoked"):
            with rt.conn.tx():
                core.save(rt.conn, cx, status="disconnected", tokens=None,
                          last_error="removed from Slack")
                rt.log("system", "connection.remove", cx.id, {"provider": "slack", "reason": kind})
        elif kind == "action":
            pass  # button presses are applied as they arrive; stored for the trail

    # --------------------------------------------------------------- buttons

    ACTIONS = {
        # action_id: (method, path template, body builder)
        "delay.confirm": ("POST", "/api/delays/{sid}/decide", lambda v: ({"sid": v}, {})),
        "delay.reassign": ("POST", "/api/delays/{sid}/decide",
                           lambda v: ({"sid": v.split("|")[0]}, {"owner": v.split("|")[1]})),
        "flag.take": ("POST", "/api/flags/{fid}/take", lambda v: ({"fid": v}, {"note": "Taken in Slack"})),
        "flag.resolve": ("POST", "/api/flags/{fid}/resolve", lambda v: ({"fid": v}, {"note": "Resolved in Slack"})),
        "approval.approve": ("POST", "/api/approvals/{aid}/decide",
                             lambda v: ({"aid": v}, {"approve": True, "note": "Approved in Slack"})),
        "approval.reject": ("POST", "/api/approvals/{aid}/decide",
                            lambda v: ({"aid": v}, {"approve": False, "note": "Rejected in Slack"})),
        "task.waiting_on": ("PATCH", "/api/tasks/{task_id}",
                            lambda v: ({"task_id": v.split("|")[0]}, {"waiting_on": v.split("|")[1]})),
        "task.start": ("PATCH", "/api/tasks/{task_id}", lambda v: ({"task_id": v}, {"status": "in_progress"})),
        "task.done": ("PATCH", "/api/tasks/{task_id}", lambda v: ({"task_id": v}, {"status": "done"})),
    }
    DONE_TEXT = {"delay.confirm": "confirmed the owner", "delay.reassign": "reassigned the delay",
                 "flag.take": "took this", "flag.resolve": "resolved this", "approval.approve": "approved",
                 "approval.reject": "rejected", "task.waiting_on": "said who it's waiting on",
                 "task.start": "started this", "task.done": "finished this"}

    def on_action(self, conn, payload: dict) -> None:
        act = payload["actions"][0]
        action_id = act.get("action_id", "").removeprefix("fw:")
        value = act.get("value") or (act.get("selected_option") or {}).get("value") or ""
        if action_id not in self.ACTIONS:
            return  # link buttons etc.
        team = (payload.get("team") or {}).get("id") or (payload.get("user") or {}).get("team_id", "")
        slack_user = (payload.get("user") or {}).get("id", "")
        method, path, build = self.ACTIONS[action_id]
        params, body = build(value)
        reply = {"response_type": "ephemeral", "replace_original": False,
                 "text": "Fieldwork couldn't find that in a workspace you're in."}
        for cx in self.connections_for_team(conn, team):
            user = self.person_for(conn, cx, slack_user)
            core.store_event(conn, cx, f"{act.get('action_ts', '')}:{slack_user}", "action",
                             {"action": action_id, "value": value, "user": slack_user})
            if not user:
                reply["text"] = ("Your Slack email doesn't match anyone in the Fieldwork workspace, "
                                 "so nothing was changed. Ask an admin to add you with that email.")
                continue
            ok, res = actas.call(conn, user, method, path, body, **params)
            if not ok and "not found" in str(res):
                continue  # belongs to another workspace on the same Slack team
            if ok:
                reply = self.resolved_message(payload, f"{user['name']} {self.DONE_TEXT[action_id]}"
                                                       + self._detail(action_id, value))
            else:
                reply["text"] = f"Couldn't do that: {res}"
            break
        url = payload.get("response_url")
        if url:
            try:
                http.request("POST", url, json_body=reply, timeout=5)
            except Exception:
                pass

    @staticmethod
    def _detail(action_id: str, value: str) -> str:
        if "|" in value:
            owner = value.split("|")[1]
            return f" ({owner.replace('_', ' ')})"
        return ""

    @staticmethod
    def resolved_message(payload: dict, line: str) -> dict:
        blocks = [b for b in ((payload.get("message") or {}).get("blocks") or []) if b.get("type") != "actions"]
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f":white_check_mark: {line}"}]})
        return {"replace_original": True, "text": line, "blocks": blocks}

    # ------------------------------------------------------- slash command

    def on_command(self, conn, f: dict) -> dict:
        text = (f.get("text") or "").strip()
        word, _, rest = text.partition(" ")
        cxs = self.connections_for_team(conn, f.get("team_id", ""))
        if not cxs:
            return {"response_type": "ephemeral", "text": "Fieldwork isn't installed for this Slack workspace."}
        user = None
        for c in cxs:
            user = self.person_for(conn, c, f.get("user_id", ""))
            if user:
                break
        if not user:
            return {"response_type": "ephemeral",
                    "text": "Your Slack email doesn't match anyone in Fieldwork. Ask an admin to add you."}
        word = word.lower()
        if word in ("", "help"):
            return {"response_type": "ephemeral", "text": (
                "*/fieldwork today*: what needs you now\n*/fieldwork status <deployment>*: where a deployment "
                "stands\n*/fieldwork help*: this")}
        if word in ("today", "me", "mine"):
            ok, t = actas.call(conn, user, "GET", "/api/today")
            if not ok:
                return {"response_type": "ephemeral", "text": t}
            lines = [f"*What needs you, {user['name'].split()[0]}*"]
            for label, key in (("Blocked", "blocked"), ("Due soon", "due"), ("New for you", "new")):
                for x in t.get(key, [])[:8]:
                    lines.append(f"• {label}: {x['title']} · <{events.dep_link(x['deployment_id'])}|open>")
            if t.get("confirm"):
                lines.append(f"• {len(t['confirm'])} finding(s) waiting for your confirmation")
            if len(lines) == 1:
                lines.append("Nothing blocked, due or new. :sunny:")
            return {"response_type": "ephemeral", "text": "\n".join(lines)}
        if word == "status":
            ok, rows = actas.call(conn, user, "GET", "/api/portfolio")
            if not ok:
                return {"response_type": "ephemeral", "text": rows}
            q = rest.strip().lower()
            deps = rows["deployments"]
            hits = [r for r in deps if q and (q in r.get("name", "").lower() or q in r.get("customer", "").lower())]
            if not hits:
                return {"response_type": "ephemeral", "text": f"No deployment you can see matches “{rest.strip()}”."}
            out = []
            for r in hits[:5]:
                out.append(f"*<{events.dep_link(r['id'])}|{r['name']}>* · {r.get('stage_name', r.get('stage'))} · "
                           f"{str(r.get('status', '')).replace('_', ' ')}"
                           + (f" · {r['flags']} open flag(s)" if r.get("flags") else "")
                           + (f" · on hold: {r['on_hold']['reason']}" if r.get("on_hold") else ""))
            return {"response_type": "ephemeral", "text": "\n".join(out)}
        return {"response_type": "ephemeral", "text": f"I don't know “{word}”. Try /fieldwork help."}


SLACK = core.register(Slack())


# -------------------------------------------------------- outbound messages

def blocks_for(event: str, data: dict, owners: dict) -> list:
    text = events.slack_text(event, data)
    for tail in ("; confirm or reassign it in the console.", "; approve or reject in the console."):
        text = text.replace(tail, ".")
    blocks: list = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    acts: list = []

    def btn(label, action, value, style=None):
        b = {"type": "button", "text": {"type": "plain_text", "text": label[:75]}, "action_id": "fw:" + action,
             "value": value}
        if style:
            b["style"] = style
        return b

    def select(placeholder, action, options):
        return {"type": "static_select", "action_id": "fw:" + action,
                "placeholder": {"type": "plain_text", "text": placeholder},
                "options": [{"text": {"type": "plain_text", "text": lab[:75]}, "value": val} for lab, val in options]}

    if event == "delay.opened" and data.get("delay_id"):
        sid = data["delay_id"]
        acts.append(btn(f"Confirm: {data.get('owner', 'owner')}", "delay.confirm", sid, "primary"))
        acts.append(select("Someone else…", "delay.reassign", [(owners[o], f"{sid}|{o}") for o in OWNER_KEYS]))
    elif event == "flag.raised" and data.get("flag_id"):
        acts += [btn("Take it", "flag.take", data["flag_id"]), btn("Resolve", "flag.resolve", data["flag_id"])]
    elif event == "approval.requested" and data.get("approval_id"):
        acts += [btn("Approve", "approval.approve", data["approval_id"], "primary"),
                 btn("Reject", "approval.reject", data["approval_id"], "danger")]
    elif event == "task.blocked" and data.get("task_id"):
        acts.append(select("Waiting on…", "task.waiting_on",
                           [(owners[o], f"{data['task_id']}|{o}") for o in OWNER_KEYS]))
    if data.get("deployment_id") and event != "digest":
        acts.append({"type": "button", "text": {"type": "plain_text", "text": "Open"}, "action_id": "fw:open",
                     "url": events.dep_link(data["deployment_id"])})
    if acts:
        blocks.append({"type": "actions", "elements": acts})
    return blocks


def deliver(conn, tenant_id: str, kind: str, p: dict) -> bool:
    """Send an outbox row through the installed app. False if Slack isn't installed (use the webhook URL)."""
    cx = core.active(conn, tenant_id, "slack")
    if not cx:
        return False
    from ..ops import owner_labels
    t = conn.execute("SELECT name FROM tenants WHERE id=?", (tenant_id,)).fetchone()
    owners = owner_labels(t["name"])
    if kind == "slack_dm":
        if not cx.settings.get("dm_assignments", True):
            return True
        u = conn.execute("SELECT email FROM users WHERE id=? AND tenant_id=?",
                         (p["data"].get("assignee_id"), tenant_id)).fetchone()
        sid = SLACK.slack_user_for(conn, cx, u["email"]) if u else None
        if not sid:
            return True  # nobody to message; not an error
        d = p["data"]
        text = (f":inbox_tray: {d.get('actor', 'Someone')} assigned you “{d.get('title')}” on "
                f"<{events.dep_link(d['deployment_id'])}|{d.get('deployment', 'a deployment')}>")
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
        if d.get("task_id"):
            blocks.append({"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Start"}, "action_id": "fw:task.start",
                 "value": d["task_id"], "style": "primary"},
                {"type": "button", "text": {"type": "plain_text", "text": "Done"}, "action_id": "fw:task.done",
                 "value": d["task_id"]},
                {"type": "button", "text": {"type": "plain_text", "text": "Open"}, "action_id": "fw:open",
                 "url": events.dep_link(d["deployment_id"])}]})
        SLACK.post(conn, cx, text, blocks, channel=sid)
        return True
    SLACK.post(conn, cx, events.slack_text(p["event"], p["data"]), blocks_for(p["event"], p["data"], owners))
    return True
