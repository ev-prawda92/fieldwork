"""ERP connectors for the billing bridge: NetSuite, Certinia PSA (Salesforce), Oracle Fusion Cloud,
SAP S/4HANA Cloud and Workday.

Each one does the same job (see billing.py): list the projects or contracts a SOW can link to, list their
billing milestones, tell the ERP a milestone is done and signed off, and read back invoiced / paid.

What each vendor's API actually lets us do differs, and the code says so rather than pretending:

  NetSuite     OAuth 2.0 (code + PKCE) with an integration record in the customer's account. Completing the
               project task a milestone billing line is tied to is what makes that line billable.
  Certinia     Rides on a Salesforce connection. Sign-off sets the PSA milestone to Approved with an actual
               date (and Include In Financials / Approved for Billing where the org has those fields).
  Oracle       Fusion Project Billing REST with an integration user. Sign-off creates (or releases) a project
               billing event, keyed by our milestone id in SourceReference so it's never created twice.
  SAP          S/4HANA Cloud OData with a communication user. Sign-off sets the milestone element's actual
               finish, which is what lifts the billing block on a milestone billing plan date.
  Workday      Read-only here: a custom report (RaaS) supplies installments and invoice status. Sign-offs reach
               Workday through the signed milestone.accepted webhook, into the customer's own integration.

Where the vendor's documentation left a detail open (field names that vary by org, a status id), it's a
setting with a sensible default, and the connection's health shows the vendor's answer if it's wrong.
"""

import base64
import json
import re
from datetime import date, datetime, timezone
from urllib.parse import quote as urlquote, urlencode, urlparse

from . import billing, core, crm, http
from .billing import BillingProvider, quote, safe_id

HOST = re.compile(r"^[a-z0-9][a-z0-9.\-]{2,200}$")


def _host(v: str, what: str) -> str:
    h = (v or "").strip().lower()
    h = urlparse(h).netloc if "//" in h else h.split("/")[0]
    if not HOST.match(h) or "." not in h:
        raise core.ConnectError(f"that doesn't look like a {what} host name")
    return h


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _day(v) -> str | None:
    if not v:
        return None
    s = str(v)
    m = re.match(r"/Date\((-?\d+)", s)  # OData v2 dates
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, timezone.utc).date().isoformat()
    return s[:10] if re.match(r"\d{4}-\d{2}-\d{2}", s) else None


def _num(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ===================================================================== NetSuite

class NetSuite(BillingProvider):
    key = "netsuite"
    name = "NetSuite"
    tenant_app = True
    pkce = True
    blurb = "Signed-off milestones complete their NetSuite project task, so the milestone billing line bills."
    setup = ("In NetSuite: Setup → Integration → Manage Integrations → New. Tick OAuth 2.0 and Authorization Code "
             "Grant, scope REST Web Services, and set the redirect URI to {public}/oauth/callback. Then connect here "
             "with your account ID and the integration's client ID and secret. The person who approves needs a role "
             "with REST Web Services and access to projects, project tasks and invoices.")
    token_fields = ({"key": "account", "label": "Account ID", "help": "e.g. 1234567 or 1234567_SB1"},
                    {"key": "client_id", "label": "Client ID"},
                    {"key": "client_secret", "label": "Client secret", "secret": True})
    settings_fields = ({"key": "completed_status", "label": "Project task status that means complete",
                        "help": "the status id NetSuite shows on a completed task", "default": "COMPLETE"},)

    @staticmethod
    def dns(account: str) -> str:
        a = (account or "").strip()
        if not re.match(r"^[A-Za-z0-9_\-]{3,40}$", a):
            raise core.ConnectError("That doesn't look like a NetSuite account ID")
        return a.lower().replace("_", "-")

    def rest(self, account: str) -> str:
        return f"https://{self.dns(account)}.suitetalk.api.netsuite.com/services/rest"

    def start_meta(self, fields):
        acct, cid, sec = fields.get("account", ""), fields.get("client_id", ""), fields.get("client_secret", "")
        self.dns(acct)
        if len(cid) < 8 or len(sec) < 8:
            raise core.ConnectError("Paste the integration's client ID and secret from NetSuite")
        return {"account": acct, "client_id": cid, "client_secret": sec}

    def start_url(self, meta, state, challenge):
        q = {"response_type": "code", "client_id": meta["client_id"], "redirect_uri": core.redirect_uri(),
             "scope": "rest_webservices", "state": state, "code_challenge": challenge,
             "code_challenge_method": "S256"}
        return f"https://{self.dns(meta['account'])}.app.netsuite.com/app/login/oauth2/authorize.nl?{urlencode(q)}"

    def _token(self, meta, form):
        r = http.request("POST", f"{self.rest(meta['account'])}/auth/oauth2/v1/token", form=form,
                         headers={"Authorization": _basic(meta["client_id"], meta["client_secret"])})
        if r.status in (400, 401):
            raise core.NeedsReauth(f"NetSuite refused the sign-in ({r.status}): {str(r.json())[:200]}")
        if not r.ok:
            raise http.HTTPError(f"NetSuite sign-in failed ({r.status})", r.status)
        return {**r.json(), **meta}

    def exchange_meta(self, meta, code, verifier):
        return self._token(meta, {"grant_type": "authorization_code", "code": code,
                                  "redirect_uri": core.redirect_uri(), "code_verifier": verifier})

    def refresh(self, tokens):
        new = self._token({k: tokens[k] for k in ("account", "client_id", "client_secret")},
                          {"grant_type": "refresh_token", "refresh_token": tokens.get("refresh_token", "")})
        new.setdefault("refresh_token", tokens.get("refresh_token"))
        return new

    def identify(self, rt, tokens):
        return {"external_account_id": tokens["account"].upper(), "account_name": f"NetSuite {tokens['account'].upper()}",
                "extra": {"account": tokens["account"]}}

    def suiteql(self, rt, cx, q: str, limit: int = 1000) -> list:
        out, offset = [], 0
        for _ in range(20):
            res = core.authed(rt.conn, cx, "POST", f"{self.rest(cx.tokens['account'])}/query/v1/suiteql",
                              "NetSuite query", params={"limit": limit, "offset": offset}, json_body={"q": q},
                              headers={"Prefer": "transient"})
            out += res.get("items", [])
            if not res.get("hasMore"):
                break
            offset += limit
        return out

    def projects(self, rt, cx):
        rows = self.suiteql(rt, cx, "SELECT id, entityid, companyname FROM job WHERE isinactive = 'F' ORDER BY id")
        return [{"id": str(r["id"]), "name": r.get("companyname") or r.get("entityid") or str(r["id"]),
                 "customer": "", "currency": None} for r in rows]

    def lines(self, rt, cx, project_id):
        pid = project_id if project_id.isdigit() else None
        if not pid:
            raise core.ConnectError("NetSuite project ids are numbers")
        rows = self.suiteql(rt, cx, "SELECT id, title, enddate, BUILTIN.DF(status) AS status_name FROM projecttask"
                                    f" WHERE company = {pid} AND ismilestone = 'T' ORDER BY id")
        return [{"id": str(r["id"]), "name": r.get("title") or f"Task {r['id']}", "amount": None,
                 "due_on": _day(r.get("enddate")),
                 "state": "complete" if "complet" in str(r.get("status_name", "")).lower() else "open"} for r in rows]

    def complete(self, rt, cx, ms, sow, packet):
        task = (ms["external_id"] or "").strip()
        if not task.isdigit():
            raise core.ConnectError("Link this milestone to its NetSuite project task first")
        status = (cx.settings.get("completed_status") or "COMPLETE").strip()
        core.authed(rt.conn, cx, "PATCH", f"{self.rest(cx.tokens['account'])}/record/v1/projectTask/{task}",
                    "NetSuite project task update", json_body={"status": {"id": status}})
        return {"external_id": task, "note": f"project task {task} marked complete"}

    def line_state(self, rt, cx, ms, sow):
        pid = (sow["external_id"] or "").strip()
        if not pid.isdigit():
            return None
        if ms["invoice_ref"]:
            ref = quote(ms["invoice_ref"])
            rows = self.suiteql(rt, cx, "SELECT id, tranid, trandate, foreigntotal, foreignamountunpaid FROM transaction"
                                        f" WHERE type = 'CustInvc' AND tranid = '{ref}'")
        else:  # not matched yet: the first invoice on the project for this amount since the sign-off
            since = (ms["decided_at"] or "")[:10] or date.today().isoformat()
            rows = self.suiteql(rt, cx, "SELECT id, tranid, trandate, foreigntotal, foreignamountunpaid FROM transaction"
                                        f" WHERE type = 'CustInvc' AND entity = {pid}"
                                        f" AND trandate >= TO_DATE('{since}', 'YYYY-MM-DD') ORDER BY trandate, id")
            taken = {r["invoice_ref"] for r in rt.conn.execute(
                "SELECT invoice_ref FROM milestones WHERE sow_id=? AND invoice_ref IS NOT NULL", (sow["id"],))}
            rows = [r for r in rows if abs((_num(r.get("foreigntotal")) or 0) - float(ms["amount"] or 0)) < 0.01
                    and r.get("tranid") not in taken][:1]
        if not rows:
            return None
        inv = rows[0]
        paid = (_num(inv.get("foreignamountunpaid")) or 0) <= 0.005
        return {"status": "paid" if paid else "invoiced", "invoice_ref": inv.get("tranid"), "on": _day(inv.get("trandate"))}


# ===================================================================== Certinia PSA

class Certinia(BillingProvider, crm.Salesforce):
    key = "certinia"
    name = "Certinia PSA"
    env = "salesforce"           # the same Salesforce app
    blurb = "Signed-off milestones are approved in Certinia PSA, ready for its billing run."
    setup = ("Uses the Salesforce app (same client ID and secret). Connect with a Salesforce user who can edit "
             "pse__Milestone__c. If your org approves milestones through an approval process, leave the approve "
             "settings off and Fieldwork will set the status and actual date only.")
    settings_fields = ({"key": "approve", "label": "Tick Approved", "type": "bool", "default": True},
                       {"key": "include_in_financials", "label": "Tick Include In Financials", "type": "bool",
                        "default": True},
                       {"key": "approve_for_billing", "label": "Tick Approved for Billing", "type": "bool",
                        "default": False})
    SFID = re.compile(r"^[A-Za-z0-9]{15,18}$")

    def check_settings(self, rt, cx, new):
        pass

    def _inst(self, cx) -> str:
        inst = (cx.tokens.get("instance_url") or cx.extra.get("instance_url") or "").rstrip("/")
        if not inst:
            raise core.ConnectError("Salesforce didn't say which instance to use; reconnect it")
        return inst

    def _q(self, rt, cx, soql: str) -> list:
        inst = self._inst(cx)
        res = core.authed(rt.conn, cx, "GET", f"{inst}/services/data/{self.VERSION}/query", "Certinia query",
                          params={"q": soql})
        rows = list(res.get("records", []))
        for _ in range(20):
            if not res.get("nextRecordsUrl"):
                break
            res = core.authed(rt.conn, cx, "GET", inst + res["nextRecordsUrl"], "Certinia query")
            rows += res.get("records", [])
        return rows

    def _fields(self, rt, cx, obj: str) -> set:
        r = http.request("GET", f"{self._inst(cx)}/services/data/{self.VERSION}/sobjects/{obj}/describe",
                         bearer=core.access_token(rt.conn, cx))
        if r.status == 404:
            raise core.ConnectError("Certinia PSA isn't installed in this Salesforce org (no pse__Milestone__c)")
        if not r.ok:
            raise http.HTTPError(f"Salesforce describe failed ({r.status})", r.status)
        return {f["name"] for f in r.json().get("fields", [])}

    def after_connect(self, rt, cx):
        ms, proj = self._fields(rt, cx, "pse__Milestone__c"), self._fields(rt, cx, "pse__Proj__c")
        with rt.conn.tx():
            core.save(rt.conn, cx, extra={**cx.extra, "ms_fields": sorted(f for f in ms if f.startswith("pse__")),
                                          "proj_fields": sorted(f for f in proj if f.startswith("pse__"))})

    def _has(self, cx, field: str, obj: str = "ms") -> bool:
        return field in set(cx.extra.get(f"{obj}_fields") or [])

    def projects(self, rt, cx):
        acct = ", pse__Account__r.Name" if self._has(cx, "pse__Account__c", "proj") else ""
        active = " WHERE pse__Is_Active__c = true" if self._has(cx, "pse__Is_Active__c", "proj") else ""
        rows = self._q(rt, cx, f"SELECT Id, Name{acct} FROM pse__Proj__c{active} ORDER BY Name LIMIT 2000")
        return [{"id": r["Id"], "name": r.get("Name") or r["Id"],
                 "customer": ((r.get("pse__Account__r") or {}).get("Name") or ""), "currency": None} for r in rows]

    def _ms_select(self, cx) -> str:
        extra = [f for f in ("pse__Invoiced__c", "pse__Billed__c", "pse__Actual_Date__c") if self._has(cx, f)]
        return ", ".join(["Id", "Name", "pse__Milestone_Amount__c", "pse__Target_Date__c", "pse__Status__c", *extra])

    def _state(self, r) -> str:
        if r.get("pse__Invoiced__c"):
            return "invoiced"
        return "complete" if r.get("pse__Status__c") == "Approved" else "open"

    def lines(self, rt, cx, project_id):
        if not self.SFID.match(project_id):
            raise core.ConnectError("that isn't a Salesforce record id")
        rows = self._q(rt, cx, f"SELECT {self._ms_select(cx)} FROM pse__Milestone__c WHERE pse__Project__c ="
                                f" '{project_id}' ORDER BY pse__Target_Date__c")
        return [{"id": r["Id"], "name": r.get("Name") or r["Id"], "amount": _num(r.get("pse__Milestone_Amount__c")),
                 "due_on": _day(r.get("pse__Target_Date__c")), "state": self._state(r)} for r in rows]

    def complete(self, rt, cx, ms, sow, packet):
        mid = (ms["external_id"] or "").strip()
        if not self.SFID.match(mid):
            raise core.ConnectError("Link this milestone to its Certinia milestone first")
        body = {"pse__Status__c": "Approved", "pse__Actual_Date__c": packet["accepted_on"]}
        for setting, field in (("approve", "pse__Approved__c"), ("include_in_financials", "pse__Include_In_Financials__c"),
                               ("approve_for_billing", "pse__Approved_for_Billing__c")):
            if cx.settings.get(setting) and self._has(cx, field):
                body[field] = True
        r = http.request("PATCH", f"{self._inst(cx)}/services/data/{self.VERSION}/sobjects/pse__Milestone__c/{mid}",
                         json_body=body, bearer=core.access_token(rt.conn, cx))
        if r.status == 401:
            r = http.request("PATCH", f"{self._inst(cx)}/services/data/{self.VERSION}/sobjects/pse__Milestone__c/{mid}",
                             json_body=body, bearer=core.access_token(rt.conn, cx, force_refresh=True))
        if not r.ok:
            err = r.json()
            msg = err[0].get("message") if isinstance(err, list) and err else str(err)[:300]
            raise http.HTTPError(f"Certinia refused the approval ({r.status}): {msg}", r.status, err)
        return {"external_id": mid, "note": "approved in Certinia PSA"}

    def line_state(self, rt, cx, ms, sow):
        mid = (ms["external_id"] or "").strip()
        if not self.SFID.match(mid):
            return None
        rows = self._q(rt, cx, f"SELECT {self._ms_select(cx)} FROM pse__Milestone__c WHERE Id = '{mid}'")
        if rows and rows[0].get("pse__Invoiced__c"):
            return {"status": "invoiced", "invoice_ref": None}
        return None

    def sync(self, rt, cx, full=False):
        return billing.pull_statuses(rt, cx)


# ===================================================================== Oracle Fusion Cloud

class OracleFusion(BillingProvider):
    key = "oracle_fusion"
    name = "Oracle Fusion Cloud"
    auth = "token"
    blurb = "Sign-offs become project billing events in Oracle Project Billing; invoiced status flows back."
    setup = ("Create an integration user in Oracle Fusion with a role that can manage project billing events "
             "(and read contracts). Connect with your Fusion host, that user and its password. Set the billing "
             "event type you use for milestone billing in the connection's settings.")
    token_fields = ({"key": "host", "label": "Fusion host", "help": "e.g. abcd.fa.us2.oraclecloud.com"},
                    {"key": "username", "label": "Integration user"},
                    {"key": "password", "label": "Password", "secret": True})
    settings_fields = ({"key": "event_type", "label": "Billing event type name", "help": "as set up in Project Billing",
                        "default": ""},
                       {"key": "contract_line", "label": "Contract line number", "default": "1"})
    API = "/fscmRestApi/resources/11.13.18.05"

    def _h(self, tokens) -> dict:
        return {"Authorization": _basic(tokens["username"], tokens["password"]), "REST-Framework-Version": "4"}

    def _get(self, tokens, path: str, params: dict) -> dict:
        r = http.request("GET", f"https://{tokens['host']}{self.API}/{path}", params=params, headers=self._h(tokens))
        if r.status == 401:
            raise core.NeedsReauth("Oracle Fusion refused the integration user; check it and reconnect")
        if not r.ok:
            raise http.HTTPError(f"Oracle Fusion {path} failed ({r.status}): {str(r.json())[:300]}", r.status)
        return r.json()

    def from_fields(self, rt, fields):
        tokens = {"host": _host(fields["host"], "Oracle Fusion"), "username": fields["username"],
                  "password": fields["password"]}
        self._get(tokens, "projectBillingEvents", {"limit": 1, "onlyData": "true"})
        return tokens

    def identify(self, rt, tokens):
        return {"external_account_id": tokens["host"], "account_name": tokens["host"].split(".")[0],
                "extra": {"host": tokens["host"]}}

    def _events(self, tokens, q: str, limit: int = 500) -> list:
        out, offset = [], 0
        for _ in range(20):
            res = self._get(tokens, "projectBillingEvents", {"q": q, "onlyData": "true", "limit": limit,
                                                              "offset": offset, "orderBy": "EventId"})
            out += res.get("items", [])
            if not res.get("hasMore"):
                break
            offset += limit
        return out

    def projects(self, rt, cx):
        """Contracts that already carry billing events (a contract without any can still be linked by number)."""
        res = self._get(cx.tokens, "projectBillingEvents", {"onlyData": "true", "limit": 500,
                                                             "fields": "ContractNumber,BillTrnsCurrencyCode"})
        seen = {}
        for e in res.get("items", []):
            if e.get("ContractNumber"):
                seen.setdefault(e["ContractNumber"], e.get("BillTrnsCurrencyCode"))
        return [{"id": k, "name": f"Contract {k}", "customer": "", "currency": v} for k, v in sorted(seen.items())]

    @staticmethod
    def _state(e) -> str:
        if e.get("Invoiced") == "F" or ((_num(e.get("InvoicedAmount")) or 0) >= (_num(e.get("BillTrnsAmount")) or 0) > 0):
            return "invoiced"
        return "open"

    def lines(self, rt, cx, project_id):
        evs = self._events(cx.tokens, f"ContractNumber='{quote(project_id)}'")
        return [{"id": str(e.get("EventId")), "name": e.get("EventDescription") or f"Event {e.get('EventNumber')}",
                 "amount": _num(e.get("BillTrnsAmount")), "due_on": _day(e.get("CompletionDate")),
                 "state": self._state(e), "invoice_ref": None} for e in evs]

    def _self_link(self, e) -> str | None:
        for ln in e.get("links", []) or []:
            if ln.get("rel") == "self" and ln.get("href"):
                return ln["href"]
        return None

    def _find(self, tokens, ms) -> dict | None:
        if (ms["external_id"] or "").isdigit():
            got = self._events(tokens, f"EventId={ms['external_id']}", 5)
        else:
            got = self._events(tokens, f"SourceReference='{quote(ms['id'])}'", 5)
        return got[0] if got else None

    def complete(self, rt, cx, ms, sow, packet):
        tok = cx.tokens
        found = self._find(tok, ms)
        if found:  # an event planned in Oracle: date it today and lift any hold, so the next billing run picks it up
            href = self._self_link(found)
            if not href:
                full = self._get(tok, "projectBillingEvents", {"q": f"EventId={found['EventId']}", "limit": 1})
                href = self._self_link((full.get("items") or [{}])[0])
            if not href or urlparse(href).netloc != tok["host"]:
                raise core.ConnectError("Oracle didn't return a link to update that billing event")
            r = http.request("PATCH", href, json_body={"CompletionDate": packet["accepted_on"], "BillHold": "N"},
                             headers={**self._h(tok), "Content-Type": "application/vnd.oracle.adf.resourceitem+json"})
            if not r.ok:
                raise http.HTTPError(f"Oracle refused the billing event update ({r.status}): {str(r.json())[:300]}",
                                     r.status)
            return {"external_id": str(found.get("EventId")), "note": "billing event released"}
        contract = (sow["external_id"] or "").strip()
        etype = (cx.settings.get("event_type") or "").strip()
        if not contract or not etype:
            raise core.ConnectError("Link the SOW to its Oracle contract number and set the billing event type")
        desc = f"{ms['name']} · signed off {packet['accepted_on']}" + (f" by {packet['accepted_by']}" if packet["accepted_by"] else "")
        body = {"ContractNumber": contract, "ContractLineNumber": str(cx.settings.get("contract_line") or "1"),
                "EventTypeName": etype, "EventDescription": desc[:240], "CompletionDate": packet["accepted_on"],
                "BillTrnsAmount": float(ms["amount"] or 0), "BillTrnsCurrencyCode": sow["currency"],
                "SourceReference": ms["id"]}
        r = http.request("POST", f"https://{tok['host']}{self.API}/projectBillingEvents", json_body=body,
                         headers={**self._h(tok), "Content-Type": "application/vnd.oracle.adf.resourceitem+json"})
        if not r.ok:
            raise http.HTTPError(f"Oracle refused the billing event ({r.status}): {str(r.json())[:300]}", r.status)
        return {"external_id": str(r.json().get("EventId") or "") or None, "note": "billing event created"}

    def line_state(self, rt, cx, ms, sow):
        e = self._find(cx.tokens, ms)
        if e and self._state(e) == "invoiced":
            return {"status": "invoiced", "invoice_ref": f"event {e.get('EventNumber') or e.get('EventId')}"}
        return None


# ===================================================================== SAP S/4HANA Cloud

class SAPS4(BillingProvider):
    key = "sap_s4"
    name = "SAP S/4HANA Cloud"
    auth = "token"
    blurb = "Sign-offs confirm the project milestone, which releases its billing plan date; billing and clearing flow back."
    setup = ("In S/4HANA Cloud, create a communication system and user, then communication arrangements for "
             "SAP_COM_0308 (Enterprise Project) and the billing document API. Connect with the API host shown on "
             "the arrangement and the communication user.")
    token_fields = ({"key": "host", "label": "API host", "help": "from the communication arrangement, e.g. my123456-api.s4hana.ondemand.com"},
                    {"key": "username", "label": "Communication user"},
                    {"key": "password", "label": "Password", "secret": True})
    PROJ = "/sap/opu/odata/sap/API_ENTERPRISE_PROJECT_SRV;v=0002"
    BILL = "/sap/opu/odata/sap/API_BILLING_DOCUMENT_SRV"
    UUID = re.compile(r"^[0-9a-fA-F\-]{32,36}$")

    def _h(self, tokens) -> dict:
        return {"Authorization": _basic(tokens["username"], tokens["password"])}

    def _get(self, tokens, path: str, params: dict | None = None) -> list:
        r = http.request("GET", f"https://{tokens['host']}{path}", params={**(params or {}), "$format": "json"},
                         headers=self._h(tokens))
        if r.status == 401:
            raise core.NeedsReauth("S/4HANA refused the communication user; check it and reconnect")
        if not r.ok:
            raise http.HTTPError(f"S/4HANA {path.rsplit('/', 1)[-1]} failed ({r.status}): {str(r.json())[:300]}", r.status)
        d = r.json().get("d", {})
        return d.get("results", [d] if d else [])

    def from_fields(self, rt, fields):
        tokens = {"host": _host(fields["host"], "S/4HANA API"), "username": fields["username"],
                  "password": fields["password"]}
        self._get(tokens, f"{self.PROJ}/A_EnterpriseProject", {"$top": 1})
        return tokens

    def identify(self, rt, tokens):
        return {"external_account_id": tokens["host"], "account_name": tokens["host"].split(".")[0].replace("-api", ""),
                "extra": {"host": tokens["host"]}}

    def projects(self, rt, cx):
        rows = self._get(cx.tokens, f"{self.PROJ}/A_EnterpriseProject", {"$top": 1000})
        return [{"id": r.get("ProjectUUID"), "name": f"{r.get('Project', '')} {r.get('ProjectDescription', '')}".strip(),
                 "customer": "", "currency": r.get("ProjectCurrency")} for r in rows if r.get("ProjectUUID")]

    def lines(self, rt, cx, project_id):
        if not self.UUID.match(project_id):
            raise core.ConnectError("that isn't an S/4HANA project UUID")
        rows = self._get(cx.tokens, f"{self.PROJ}/A_EnterpriseProjectElement",
                         {"$filter": f"ProjectUUID eq guid'{project_id}' and IsProjectMilestone eq true", "$top": 500})
        return [{"id": f"{r['ProjectElementUUID']}|{r.get('ProjectElement', '')}",
                 "name": r.get("ProjectElementDescription") or r.get("ProjectElement") or "Milestone", "amount": None,
                 "due_on": _day(r.get("PlannedEndDate")),
                 "state": "complete" if _day(r.get("ActualEndDate")) else "open"} for r in rows]

    def _ids(self, ms) -> tuple[str, str]:
        uuid, _, elem = (ms["external_id"] or "").partition("|")
        return uuid, elem

    def complete(self, rt, cx, ms, sow, packet):
        uuid, _ = self._ids(ms)
        if not self.UUID.match(uuid):
            raise core.ConnectError("Link this milestone to its S/4HANA project milestone first")
        tok = cx.tokens
        url = f"https://{tok['host']}{self.PROJ}/A_EnterpriseProjectElement(guid'{uuid}')"
        head = http.request("GET", url, params={"$format": "json"},
                            headers={**self._h(tok), "x-csrf-token": "Fetch"})
        csrf = head.headers.get("x-csrf-token", "")
        if not head.ok or not csrf:
            raise http.HTTPError(f"S/4HANA didn't hand out a CSRF token ({head.status})", head.status)
        cookies = "; ".join(c.split(";")[0] for c in re.split(r",\s*(?=[^;,]+=)", head.headers.get("set-cookie", ""))
                            if "=" in c)
        ms_since = int(datetime.fromisoformat(packet["accepted_on"]).replace(tzinfo=timezone.utc).timestamp() * 1000)
        r = http.request("PATCH", url, json_body={"ActualEndDate": f"/Date({ms_since})/"},
                         headers={**self._h(tok), "x-csrf-token": csrf, **({"Cookie": cookies} if cookies else {})})
        if not r.ok:
            raise http.HTTPError(f"S/4HANA refused the milestone update ({r.status}): {str(r.json())[:300]}", r.status)
        return {"note": "milestone actual finish set"}

    def line_state(self, rt, cx, ms, sow):
        _, elem = self._ids(ms)
        if not elem:
            return None
        items = self._get(cx.tokens, f"{self.BILL}/A_BillingDocumentItem",
                          {"$filter": f"WBSElement eq '{quote(elem)}'", "$select": "BillingDocument", "$top": 50})
        docs = sorted({i["BillingDocument"] for i in items if i.get("BillingDocument")})
        states = []
        for doc in docs[:10]:
            got = self._get(cx.tokens, f"{self.BILL}/A_BillingDocument('{quote(doc)}')")
            if got and not got[0].get("BillingDocumentIsCancelled"):
                states.append((doc, got[0].get("InvoiceClearingStatus"), _day(got[0].get("BillingDocumentDate"))))
        if not states:
            return None
        doc, clearing, on = states[-1]
        return {"status": "paid" if all(s[1] == "C" for s in states) else "invoiced", "invoice_ref": doc, "on": on}


# ===================================================================== Workday

class Workday(BillingProvider):
    key = "workday"
    name = "Workday"
    auth = "token"
    pushes = False
    blurb = "Reads billing installments and invoice status from a Workday custom report; sign-offs go to Workday by signed webhook."
    setup = ("In Workday, build an advanced custom report of your billing installments (contract, installment, "
             "amount, status, invoice, payment status), enable it as a web service and share it with an integration "
             "system user. Connect with the report's JSON URL and that user. To have sign-offs complete milestones in "
             "Workday, point a signed webhook for milestone.accepted at your Workday integration.")
    token_fields = ({"key": "report_url", "label": "Report URL (JSON)",
                     "help": "https://<host>/ccx/service/customreport2/<tenant>/<owner>/<report>?format=json"},
                    {"key": "username", "label": "Integration system user"},
                    {"key": "password", "label": "Password", "secret": True})
    settings_fields = ({"key": "col_contract", "label": "Contract column", "default": "Contract"},
                       {"key": "col_id", "label": "Installment id column", "default": "Installment_ID"},
                       {"key": "col_name", "label": "Installment name column", "default": "Installment"},
                       {"key": "col_amount", "label": "Amount column", "default": "Amount"},
                       {"key": "col_date", "label": "Date column", "default": "Installment_Date"},
                       {"key": "col_invoice", "label": "Invoice column", "default": "Invoice"},
                       {"key": "col_paid", "label": "Payment status column", "default": "Payment_Status"})

    def _rows(self, tokens) -> list:
        r = http.request("GET", tokens["report_url"], headers={"Authorization": _basic(tokens["username"],
                                                                                        tokens["password"])})
        if r.status == 401:
            raise core.NeedsReauth("Workday refused the integration user; check it and reconnect")
        if not r.ok:
            raise http.HTTPError(f"Workday report failed ({r.status})", r.status)
        return r.json().get("Report_Entry", [])

    def from_fields(self, rt, fields):
        u = urlparse(fields["report_url"].strip())
        if u.scheme != "https" or "/ccx/service/customreport2/" not in u.path:
            raise core.ConnectError("Use the report's web service URL (…/ccx/service/customreport2/…)")
        q = dict(p.split("=", 1) for p in u.query.split("&") if "=" in p)
        q["format"] = "json"
        url = f"https://{u.netloc}{u.path}?{urlencode(q)}"
        tokens = {"report_url": url, "username": fields["username"], "password": fields["password"]}
        self._rows(tokens)
        return tokens

    def identify(self, rt, tokens):
        parts = urlparse(tokens["report_url"]).path.split("/")
        tenant = parts[4] if len(parts) > 4 else "Workday"
        return {"external_account_id": tokens["report_url"].split("?")[0], "account_name": f"Workday {tenant}",
                "extra": {"tenant": tenant}}

    def _col(self, cx, key: str, row: dict):
        v = row.get(cx.settings.get(key) or "")
        if isinstance(v, dict):  # reference columns come back as {"Descriptor": ..., "ID": ...}
            v = v.get("Descriptor") or v.get("ID")
        return v

    def projects(self, rt, cx):
        seen = {}
        for r in self._rows(cx.tokens):
            c = self._col(cx, "col_contract", r)
            if c:
                seen.setdefault(str(c), None)
        return [{"id": k, "name": k, "customer": "", "currency": None} for k in sorted(seen)]

    def _state(self, cx, r) -> str:
        paid = str(self._col(cx, "col_paid", r) or "").lower()
        if paid in ("paid", "fully paid", "true", "1"):
            return "paid"
        return "invoiced" if self._col(cx, "col_invoice", r) else "open"

    def lines(self, rt, cx, project_id):
        return [{"id": str(self._col(cx, "col_id", r) or ""), "name": str(self._col(cx, "col_name", r) or "Installment"),
                 "amount": _num(self._col(cx, "col_amount", r)), "due_on": _day(self._col(cx, "col_date", r)),
                 "state": self._state(cx, r), "invoice_ref": self._col(cx, "col_invoice", r)}
                for r in self._rows(cx.tokens) if str(self._col(cx, "col_contract", r)) == project_id
                and self._col(cx, "col_id", r)]

    def line_state(self, rt, cx, ms, sow):
        ext = (ms["external_id"] or "").strip()
        if not ext:
            return None
        for r in self._rows(cx.tokens):
            if str(self._col(cx, "col_id", r)) == ext:
                st = self._state(cx, r)
                return {"status": st, "invoice_ref": self._col(cx, "col_invoice", r)} if st != "open" else None
        return None


for _p in (NetSuite(), Certinia(), OracleFusion(), SAPS4(), Workday()):
    core.register(_p)
