"""The sales pipeline, live from Salesforce or HubSpot.

Deals sync into the pipeline every 15 minutes (HubSpot also pushes a nudge
the moment a deal changes), with a full reconcile nightly. The staffing check
then runs on real deals instead of last month's export. When a deal is won,
"Open deployment" turns it into a deployment in one click (or automatically,
if the workspace turns that on), carrying the customer, start date and the
company's email domain across.

Stage mapping: a CRM's stages are its own. By default a closed-won deal is
won, closed-lost is lost, and open deals fall into lead / qualified /
proposal / commit by their probability. Any stage can be mapped by name in
the connection's settings.
"""


import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request

from .. import audit, db
from . import actas, core, http
from .core import Provider

PIPELINE = ("lead", "qualified", "proposal", "commit", "won", "lost")
FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")


def by_probability(p: float | None) -> str:
    p = p or 0.0
    return "commit" if p >= 0.9 else "proposal" if p >= 0.5 else "qualified" if p >= 0.2 else "lead"


def map_stage(cx, crm_stage: str, prob: float | None, closed: bool, won: bool) -> str:
    m = {str(k).lower(): v for k, v in (cx.settings.get("stage_map") or {}).items() if v in PIPELINE}
    if crm_stage and crm_stage.lower() in m:
        return m[crm_stage.lower()]
    if closed:
        return "won" if won else "lost"
    return by_probability(prob)


def _num(v) -> float | None:
    try:
        f = float(v)
        return f if f == f and abs(f) < 1e13 else None
    except (TypeError, ValueError):
        return None


def upsert(rt, cx, ext: str, deal: dict, actor: str) -> str | None:
    """Create or update one opportunity from a CRM deal. Returns 'created', 'updated' or None. In a transaction."""
    conn = rt.conn
    ex = conn.execute("SELECT * FROM opportunities WHERE tenant_id=? AND (connection_id=? OR connection_id IS NULL)"
                      " AND external_id=? ORDER BY connection_id DESC", (rt.tenant_id, cx.id, ext)).fetchone()
    vals = {"name": deal["name"][:160] or "Untitled deal", "customer": (deal.get("customer") or "")[:120],
            "value": max(0.0, deal.get("value") or 0.0), "probability": min(1.0, max(0.0, deal.get("probability") or 0.0)),
            "stage": deal["stage"], "expected_start": deal.get("expected_start")}
    if deal.get("weekly_hours") is not None:
        vals["weekly_hours"] = min(400.0, max(0.0, deal["weekly_hours"]))
    if ex:
        changed = {k: v for k, v in vals.items() if ex[k] != v}
        if not changed and ex["connection_id"] == cx.id:
            return None
        sets = {**changed, "connection_id": cx.id, "source": cx.provider, "updated_at": audit.now()}
        conn.execute(f"UPDATE opportunities SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), ex["id"]))
        if "stage" in changed:
            rt.log(actor, "opportunity.update", ex["id"], {"stage": vals["stage"], "from": ex["stage"], "via": cx.provider})
        oid, what = ex["id"], "updated"
    else:
        oid = "opp_" + secrets.token_hex(6)
        conn.execute("INSERT INTO opportunities (id, tenant_id, name, customer, use_case, value, probability, stage,"
                     " expected_start, weekly_hours, source, external_id, updated_at, connection_id)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (oid, rt.tenant_id, vals["name"], vals["customer"], "", vals["value"], vals["probability"],
                      vals["stage"], vals["expected_start"], vals.get("weekly_hours", 0.0), cx.provider, ext,
                      audit.now(), cx.id))
        rt.log(actor, "opportunity.create", oid, {"name": vals["name"], "stage": vals["stage"], "via": cx.provider})
        what = "created"
    if deal.get("domain"):
        cx.extra.setdefault("_domains", {})[oid] = deal["domain"]
    return what


class CRM(Provider):
    category = "crm"
    poll_minutes = 15
    reconcile_hours = 24
    settings_fields = (
        {"key": "stage_map", "label": "Stage mapping", "type": "map", "default": {},
         "help": "CRM stage name → lead, qualified, proposal, commit, won or lost"},
        {"key": "hours_field", "label": "Field holding weekly delivery hours", "type": "text", "default": "",
         "help": "API name of a number field on the deal, if you track it"},
        {"key": "auto_open", "label": "Open a deployment automatically when a deal is won", "type": "bool",
         "default": False},
    )

    def check_settings(self, rt, cx, new):
        if new.get("hours_field") and not FIELD.match(new["hours_field"]):
            raise HTTPException(422, "field names are letters, digits and underscores")
        bad = [v for v in (new.get("stage_map") or {}).values() if v not in PIPELINE]
        if bad:
            raise HTTPException(422, f"stages map to one of {', '.join(PIPELINE)}")

    def store(self, rt, cx, deals: list[tuple[str, dict]]) -> dict:
        actor = f"sync:{self.key}"
        n = {"created": 0, "updated": 0, "won": []}
        with rt.conn.tx():
            for ext, deal in deals:
                before = rt.conn.execute("SELECT id, stage, deployment_id FROM opportunities WHERE tenant_id=?"
                                         " AND external_id=?", (rt.tenant_id, ext)).fetchone()
                what = upsert(rt, cx, ext, deal, actor)
                if what:
                    n[what] += 1
                if deal["stage"] == "won" and (not before or before["stage"] != "won"):
                    row = rt.conn.execute("SELECT id, deployment_id FROM opportunities WHERE tenant_id=? AND"
                                          " external_id=?", (rt.tenant_id, ext)).fetchone()
                    if row and not row["deployment_id"]:
                        n["won"].append(row["id"])
            if cx.extra.get("_domains"):
                core.save(rt.conn, cx, extra=cx.extra)
        if cx.settings.get("auto_open") and n["won"]:
            owner = rt.conn.execute("SELECT * FROM users WHERE id=?", (cx["created_by"],)).fetchone()
            for oid in n["won"]:
                if owner:
                    open_deployment(rt.conn, owner, oid)
        n["won"] = len(n["won"])
        return n


# ================================================================= Salesforce

class Salesforce(CRM):
    key = "salesforce"
    name = "Salesforce"
    pkce = True
    scopes = ("api", "refresh_token", "offline_access")
    blurb = "Opportunities sync into the pipeline every 15 minutes."
    setup = ("Create an External Client App (or Connected App) with OAuth enabled, the callback URL above, and "
             "the scopes api, refresh_token and offline_access. Sandboxes: set FIELDWORK_SALESFORCE_LOGIN_URL="
             "https://test.salesforce.com.")
    VERSION = "v61.0"

    @property
    def login(self) -> str:
        import os
        return os.environ.get("FIELDWORK_SALESFORCE_LOGIN_URL", "https://login.salesforce.com").rstrip("/")

    @property
    def authorize_url(self):
        return f"{self.login}/services/oauth2/authorize"

    @property
    def token_url(self):
        return f"{self.login}/services/oauth2/token"

    def identify(self, rt, tokens):
        me = http.call("GET", tokens["id"], "Salesforce identity", bearer=tokens["access_token"]) if tokens.get("id") \
            else {}
        return {"external_account_id": me.get("organization_id", tokens.get("instance_url", "")),
                "account_name": (tokens.get("instance_url", "").split("//")[-1].split(".")[0] or "Salesforce"),
                "extra": {"instance_url": tokens.get("instance_url"), "user": me.get("username")}}

    def refresh(self, tokens):
        new = super().refresh(tokens)
        new.setdefault("refresh_token", tokens.get("refresh_token"))
        return new

    def sync(self, rt, cx, full=False):
        inst = (cx.tokens.get("instance_url") or cx.extra.get("instance_url") or "").rstrip("/")
        if not inst:
            raise core.ConnectError("Salesforce didn't say which instance to use; reconnect it")
        since = None if full else cx.cursor.get("modstamp")
        hours = cx.settings.get("hours_field") or ""
        fields = "Id, Name, Account.Name, Account.Website, Amount, Probability, StageName, CloseDate, IsClosed, IsWon," \
                 " SystemModstamp" + (f", {hours}" if hours and FIELD.match(hours) else "")
        where = f"SystemModstamp >= {since}" if since else "SystemModstamp = LAST_N_DAYS:365"
        url = f"{inst}/services/data/{self.VERSION}/query"
        res = core.authed(rt.conn, cx, "GET", url, "Salesforce query",
                          params={"q": f"SELECT {fields} FROM Opportunity WHERE {where} ORDER BY SystemModstamp ASC"})
        records, latest = list(res.get("records", [])), since
        pages = 0
        while res.get("nextRecordsUrl") and pages < 20:
            res = core.authed(rt.conn, cx, "GET", inst + res["nextRecordsUrl"], "Salesforce query")
            records += res.get("records", [])
            pages += 1
        deals = []
        for r in records:
            acct = r.get("Account") or {}
            prob = (_num(r.get("Probability")) or 0.0) / 100
            deals.append((r["Id"], {
                "name": r.get("Name") or "", "customer": acct.get("Name") or "", "value": _num(r.get("Amount")) or 0.0,
                "probability": prob, "expected_start": (r.get("CloseDate") or None),
                "stage": map_stage(cx, r.get("StageName") or "", prob, bool(r.get("IsClosed")), bool(r.get("IsWon"))),
                "weekly_hours": _num(r.get(hours)) if hours else None,
                "domain": _domain(acct.get("Website"))}))
            ms = _soql_time(r.get("SystemModstamp"))
            if ms and (not latest or ms > latest):
                latest = ms
        n = self.store(rt, cx, deals)
        if latest:
            with rt.conn.tx():
                core.save(rt.conn, cx, cursor={**cx.cursor, "modstamp": latest})
        return {"deals": len(deals), **n}


def _soql_time(s: str | None) -> str | None:
    if not s:
        return None
    try:
        dt = datetime.strptime(s.replace("+0000", "+00:00").replace("Z", "+00:00"), "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    host = re.sub(r"^https?://", "", url.strip().lower()).split("/")[0].split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    return host if "." in host and len(host) < 200 else None


# ==================================================================== HubSpot

HUBSPOT_API = "https://api.hubapi.com"


class HubSpot(CRM):
    key = "hubspot"
    name = "HubSpot"
    authorize_url = "https://app.hubspot.com/oauth/authorize"
    token_url = f"{HUBSPOT_API}/oauth/v1/token"
    scopes = ("oauth", "crm.objects.deals.read", "crm.objects.companies.read")
    blurb = "Deals sync into the pipeline, and changes arrive as they happen."
    setup = ("Create a public app in a HubSpot developer account with the callback URL above and the scopes "
             + ", ".join(scopes) + ". Under Webhooks, set the target URL to {public}/hooks/hubspot/app and "
             "subscribe to deal.creation and deal.propertyChange (dealstage, amount, closedate).")

    def identify(self, rt, tokens):
        info = http.call("GET", f"{HUBSPOT_API}/oauth/v1/access-tokens/{tokens['access_token']}", "HubSpot account")
        return {"external_account_id": str(info.get("hub_id", "")), "account_name": info.get("hub_domain", "HubSpot"),
                "extra": {"user": info.get("user")}}

    def stages(self, rt, cx) -> dict:
        cached = cx.extra.get("_stages")
        if cached and cx.extra.get("_stages_at", 0) > time.time() - 6 * 3600:
            return cached
        res = core.authed(rt.conn, cx, "GET", f"{HUBSPOT_API}/crm/v3/pipelines/deals", "HubSpot pipelines")
        out = {}
        for p in res.get("results", []):
            for s in p.get("stages", []):
                md = s.get("metadata") or {}
                out[s["id"]] = {"label": s.get("label", s["id"]), "probability": _num(md.get("probability")),
                                "closed": str(md.get("isClosed", "")).lower() == "true"}
        with rt.conn.tx():
            core.save(rt.conn, cx, extra={**cx.extra, "_stages": out, "_stages_at": time.time()})
        return out

    def companies(self, rt, cx, deal_ids: list) -> dict:
        """{deal id: (company name, domain)}"""
        if not deal_ids:
            return {}
        assoc = core.authed(rt.conn, cx, "POST", f"{HUBSPOT_API}/crm/v4/associations/deals/companies/batch/read",
                            "HubSpot associations", json_body={"inputs": [{"id": i} for i in deal_ids]})
        first = {}
        for r in assoc.get("results", []):
            to = r.get("to") or []
            if to:
                first[str(r["from"]["id"])] = str(to[0]["toObjectId"])
        if not first:
            return {}
        comp = core.authed(rt.conn, cx, "POST", f"{HUBSPOT_API}/crm/v3/objects/companies/batch/read",
                           "HubSpot companies", json_body={"inputs": [{"id": c} for c in set(first.values())],
                                                            "properties": ["name", "domain"]})
        names = {str(c["id"]): ((c.get("properties") or {}).get("name") or "",
                                (c.get("properties") or {}).get("domain")) for c in comp.get("results", [])}
        return {d: names.get(c, ("", None)) for d, c in first.items()}

    def sync(self, rt, cx, full=False):
        stages = self.stages(rt, cx)
        hours = cx.settings.get("hours_field") or ""
        props = ["dealname", "amount", "dealstage", "closedate", "hs_lastmodifieddate", "hs_is_closed_won",
                 "hs_is_closed", "hs_deal_stage_probability"] + ([hours] if hours and FIELD.match(hours) else [])
        since = None if full else cx.cursor.get("modified_ms")
        after, results, latest = None, [], since
        for _ in range(50):
            body = {"properties": props, "limit": 100,
                    "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}]}
            if since:
                body["filterGroups"] = [{"filters": [{"propertyName": "hs_lastmodifieddate", "operator": "GTE",
                                                      "value": str(since)}]}]
            if after:
                body["after"] = after
            res = core.authed(rt.conn, cx, "POST", f"{HUBSPOT_API}/crm/v3/objects/deals/search", "HubSpot deals",
                              json_body=body)
            results += res.get("results", [])
            after = ((res.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
        comp = self.companies(rt, cx, [str(r["id"]) for r in results][:1000])
        deals = []
        for r in results:
            p = r.get("properties") or {}
            st = stages.get(p.get("dealstage") or "", {})
            prob = _num(p.get("hs_deal_stage_probability"))
            prob = prob if prob is not None else st.get("probability")
            won = str(p.get("hs_is_closed_won", "")).lower() == "true"
            closed = str(p.get("hs_is_closed", "")).lower() == "true" or st.get("closed", False)
            name, domain = comp.get(str(r["id"]), ("", None))
            deals.append((str(r["id"]), {
                "name": p.get("dealname") or "", "customer": name, "value": _num(p.get("amount")) or 0.0,
                "probability": 1.0 if won else (prob or 0.0), "expected_start": (p.get("closedate") or "")[:10] or None,
                "stage": map_stage(cx, st.get("label", ""), prob, closed, won),
                "weekly_hours": _num(p.get(hours)) if hours else None, "domain": domain}))
            ms = _ms(p.get("hs_lastmodifieddate"))
            if ms and (not latest or ms > latest):
                latest = ms
        n = self.store(rt, cx, deals)
        if latest:
            with rt.conn.tx():
                core.save(rt.conn, cx, cursor={**cx.cursor, "modified_ms": latest})
        return {"deals": len(deals), **n}

    def routes(self, app, d):
        conn = d.conn
        prov = self

        @app.post("/hooks/hubspot/app", include_in_schema=False)
        async def hubspot_app(request: Request):
            body = await request.body()
            ts = request.headers.get("x-hubspot-request-timestamp", "")
            sig = request.headers.get("x-hubspot-signature-v3", "")
            uri = core.public_url() + request.url.path + (f"?{request.url.query}" if request.url.query else "")
            if not prov.verify_v3(request.method, uri, body, ts, sig):
                raise HTTPException(401, "bad signature")
            items = json.loads(body or b"[]")
            stored = 0
            for ev in items if isinstance(items, list) else []:
                for r in conn.execute("SELECT * FROM connections WHERE provider='hubspot' AND status='active'"
                                      " AND external_account_id=?", (str(ev.get("portalId", "")),)).fetchall():
                    if core.store_event(conn, core.Conn(r), str(ev.get("eventId", "")),
                                        ev.get("subscriptionType", ""), ev):
                        stored += 1
            core.kick()
            return {"stored": stored}

    def verify_v3(self, method: str, uri: str, body: bytes, ts: str, sig: str) -> bool:
        secret = self.client_secret()
        if not secret or not ts.isdigit() or abs(time.time() * 1000 - int(ts)) > 5 * 60 * 1000:
            return False
        mac = base64.b64encode(hmac.new(secret.encode(), method.encode() + uri.encode() + body + ts.encode(),
                                        hashlib.sha256).digest()).decode()
        return hmac.compare_digest(mac, sig)

    def handle(self, rt, cx, kind, payload):
        # A change is a nudge: pull what changed since the cursor (several events, one sync).
        last = core.parse(core.get(rt.conn, cx.id)["last_sync_at"])
        if last and last > core.now() - timedelta(seconds=20):
            return
        core.run_sync(rt.conn, core.get(rt.conn, cx.id))


def _ms(s: str | None) -> int | None:
    if not s:
        return None
    if str(s).isdigit():
        return int(s)
    dt = core.parse(str(s))
    return int(dt.timestamp() * 1000) if dt else None


SALESFORCE = core.register(Salesforce())
HUBSPOT = core.register(HubSpot())


# ============================================================ won → deployment

def open_deployment(conn, user, oid: str) -> tuple[bool, dict | str]:
    """Turn a won deal into a deployment, as `user` (their permissions apply)."""
    o = conn.execute("SELECT * FROM opportunities WHERE id=? AND tenant_id=?", (oid, user["tenant_id"])).fetchone()
    if not o:
        return False, "opportunity not found"
    if o["deployment_id"]:
        return False, "this deal already has a deployment"
    cust_name = o["customer"] or o["name"]
    cust = conn.execute("SELECT id FROM customers WHERE tenant_id=? AND lower(name)=?",
                        (user["tenant_id"], cust_name.lower())).fetchone()
    if cust:
        cid = cust["id"]
    else:
        ok, res = actas.call(conn, user, "POST", "/api/customers", {"name": cust_name[:120]}, via="console")
        if not ok:
            return False, res
        cid = res["id"]
    ok, res = actas.call(conn, user, "POST", "/api/deployments",
                         {"customer_id": cid, "name": (o["use_case"] or o["name"])[:120]}, via="console")
    if not ok:
        return False, res
    did = res["id"]
    domain = None
    if o["connection_id"]:
        cx = core.get(conn, o["connection_id"])
        domain = cx.extra.get("_domains", {}).get(oid) if cx else None
    with db.tx(conn):
        conn.execute("UPDATE opportunities SET deployment_id=?, updated_at=? WHERE id=?", (did, audit.now(), oid))
        start = o["expected_start"]
        if start:
            conn.execute("UPDATE deployments SET start_on=COALESCE(start_on, ?) WHERE id=?", (start[:10], did))
        if domain:
            c = conn.execute("SELECT domains FROM customers WHERE id=?", (cid,)).fetchone()
            have = {x for x in (c["domains"] or "").split(",") if x}
            if domain not in have:
                conn.execute("UPDATE customers SET domains=? WHERE id=?", (",".join(sorted(have | {domain})), cid))
        audit.record(conn, user["tenant_id"], user["id"], "opportunity.open_deployment", oid,
                     {"deployment": did, "customer": cid})
    return True, {"deployment_id": did, "customer_id": cid}


def register_routes(app, d):
    Ctx, ctx = d.Ctx, d.ctx

    @app.post("/api/opportunities/{oid}/open-deployment", status_code=201)
    def open_dep(oid: str, c: Ctx = Depends(ctx)):
        c.require("pipeline.view")
        c.require("deployment.create")
        ok, res = open_deployment(d.conn, c.user, oid)
        if not ok:
            raise HTTPException(409 if "already" in str(res) else 404 if "not found" in str(res) else 422, res)
        return res

