"""Fieldwork as an MCP server: use it from Claude, Cursor, Codex, ChatGPT, Gemini and Grok.

Remote (Streamable HTTP):  POST /mcp   Authorization: Bearer <personal access token>
Local (stdio):             python -m fieldwork mcp --url https://your-fieldwork --token fwp_...

The MCP server is a thin client of Fieldwork's own API. Every tool call is
replayed as an API request carrying the caller's token, so an AI agent gets
exactly that person's permissions and scopes, and every change it makes lands
in the audit trail like anyone else's. Engine results still need a second
person to confirm them: the agent proposes, a human decides.
"""

from __future__ import annotations

import json
import sys

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


def _s(**props):
    req = [k for k, v in props.items() if not v.pop("optional", False)]
    return {"type": "object", "properties": props, "required": req, "additionalProperties": False}


TOOLS = [
    {"name": "today", "description": "What needs me today: my blocked and due tasks, findings waiting for my "
     "confirmation, deployments that moved or need attention.", "inputSchema": _s()},
    {"name": "list_deployments", "description": "Deployments I can see, with customer, stage, health and team.",
     "inputSchema": _s()},
    {"name": "get_deployment", "description": "One deployment in detail: stage, exit criteria, team, history, and "
     "what I'm allowed to do on it.", "inputSchema": _s(deployment_id={"type": "string"})},
    {"name": "list_tasks", "description": "Tasks on a deployment, or all of mine.",
     "inputSchema": _s(deployment_id={"type": "string", "optional": True},
                       mine={"type": "boolean", "optional": True})},
    {"name": "create_task", "description": "Create a task on a deployment. Leave assignee_id empty to assign it to me.",
     "inputSchema": _s(deployment_id={"type": "string"}, title={"type": "string"},
                       assignee_id={"type": "string", "optional": True},
                       due={"type": "string", "description": "YYYY-MM-DD", "optional": True},
                       share_with_customer={"type": "boolean", "optional": True})},
    {"name": "update_task", "description": "Change a task's status (open, in_progress, blocked, done) or due date.",
     "inputSchema": _s(task_id={"type": "string"},
                       status={"type": "string", "enum": ["open", "in_progress", "blocked", "done"], "optional": True},
                       due={"type": "string", "optional": True})},
    {"name": "advance_deployment", "description": "Move a deployment to another stage. Moving backward needs a note.",
     "inputSchema": _s(deployment_id={"type": "string"}, to_stage={"type": "string"},
                       note={"type": "string", "optional": True})},
    {"name": "list_engines", "description": "Engines available in this workspace and what input each expects.",
     "inputSchema": _s()},
    {"name": "run_engine", "description": "Run an engine on a deployment (census, cortex, conformance, golive, "
     "attribution, or one of the workspace's own). The result is saved as a finding that someone else must confirm.",
     "inputSchema": _s(deployment_id={"type": "string"}, engine={"type": "string"},
                       input={"type": "string", "description": "CSV or JSON, per the engine's input hint"},
                       title={"type": "string", "optional": True})},
    {"name": "list_findings", "description": "Engine findings on a deployment, with confirmation state.",
     "inputSchema": _s(deployment_id={"type": "string"})},
    {"name": "draft_status_report", "description": "Draft a status report for a deployment from its tasks, stage "
     "moves and confirmed findings. audience: internal or customer.",
     "inputSchema": _s(deployment_id={"type": "string"},
                       audience={"type": "string", "enum": ["internal", "customer"], "optional": True})},
    {"name": "request_approval", "description": "Ask a person to approve an action before you take it (for example "
     "writing to a customer system). Someone other than you decides; poll check_approval for the answer and don't "
     "act until it says approved.",
     "inputSchema": _s(deployment_id={"type": "string"}, agent={"type": "string", "description": "your name"},
                       request={"type": "string", "description": "the action, in a short phrase"},
                       detail={"type": "string", "optional": True})},
    {"name": "check_approval", "description": "Whether an approval you asked for was approved, rejected or is pending.",
     "inputSchema": _s(approval_id={"type": "string"})},
    {"name": "log_delay", "description": "Record that a deployment is waiting on something (security review, change "
     "board, a customer answer, model access, a vendor). A person confirms who owns it.",
     "inputSchema": _s(deployment_id={"type": "string"},
                       signal={"type": "string", "enum": ["manual", "waiting_on_customer", "security_review",
                                                          "change_board", "model_access", "vendor_error"]},
                       reason={"type": "string", "optional": True},
                       evidence={"type": "string", "optional": True})},
    {"name": "portfolio", "description": "Every deployment I can see on the chain: stage, days in stage against "
     "target, who owns the current delay, burn, flags, and headline numbers.", "inputSchema": _s()},
]


TOOLS += [
    {"name": "ai_eval_queue", "description": "List pending evaluation jobs for current workflow versions accessible to this caller.", "inputSchema": _s()},
    {"name": "get_ai_workflow", "description": "Read versioned workflow graph, Cortex policy, evaluations, readiness and rollout packets.", "inputSchema": _s(deployment_id={"type": "string"})},
    {"name": "save_ai_specification", "description": "Create an immutable workflow version. Read current version first; any change requires fresh observed evaluations.", "inputSchema": _s(deployment_id={"type": "string"}, spec={"type": "object"}, base_version_id={"type": ["string", "null"], "optional": True})},
    {"name": "evaluate_ai_workflow", "description": "Run a synthetic rehearsal or evaluate caller-attested observed runner traces. Rehearsals cannot unlock rollout.", "inputSchema": _s(deployment_id={"type": "string"}, evaluation={"type": "object"})},
    {"name": "plan_ai_deployment", "description": "Propose remediation from versioned controls and evaluation evidence. Does not change customer systems.", "inputSchema": _s(deployment_id={"type": "string"})},
    {"name": "apply_ai_plan", "description": "Create reviewed remediation tasks inside Fieldwork, replay safely. Does not deploy external systems.", "inputSchema": _s(deployment_id={"type": "string"}, version_id={"type": "string"}, reviewed={"type": "boolean"})},
    {"name": "check_cortex_authority", "description": "Evaluate a proposed tool action using Cortex. Supply full scoped action context and evidence; a caller-supplied approval boolean is forbidden. Runner must enforce the decision.", "inputSchema": _s(deployment_id={"type": "string"}, request={"type": "object"})},
    {"name": "request_cortex_approval", "description": "Request a separate human approval bound to exact version and scoped action context, after Cortex returns HUMAN_REVIEW.", "inputSchema": _s(deployment_id={"type": "string"}, request={"type": "object"})},
    {"name": "request_ai_rollout", "description": "Freeze a rollout packet using current observed evidence and request review from another person. Never executes the rollout.", "inputSchema": _s(deployment_id={"type": "string"}, version_id={"type": "string"}, reviewed={"type": "boolean"})},
    {"name": "ai_registry", "description": "List accessible deployments by pinned agent/model version to inspect upgrade impact. No automatic upgrades.", "inputSchema": _s()},
    {"name": "deployment_memory", "description": "Read confirmed deployment lessons within this workspace and your authorized deployment scope.", "inputSchema": _s()},
]


def _route(name: str, a: dict) -> tuple[str, str, dict | None]:
    dep = a.get("deployment_id", "")
    ai_routes = {
        "get_ai_workflow": ("GET", "", None),
        "save_ai_specification": ("PUT", "/spec", {"spec": a.get("spec"), "base_version_id": a.get("base_version_id")}),
        "evaluate_ai_workflow": ("POST", "/evals", a.get("evaluation")),
        "plan_ai_deployment": ("GET", "/plan", None),
        "apply_ai_plan": ("POST", "/plan/apply", {"version_id": a.get("version_id"), "reviewed": a.get("reviewed")}),
        "check_cortex_authority": ("POST", "/cortex/check", a.get("request")),
        "request_cortex_approval": ("POST", "/cortex/approvals", a.get("request")),
        "request_ai_rollout": ("POST", "/releases", {"version_id": a.get("version_id"), "reviewed": a.get("reviewed")}),
    }
    if name in ai_routes:
        method, suffix, body = ai_routes[name]
        return method, f"/api/deployments/{dep}/ai"+suffix, body
    if name == "ai_eval_queue":
        return "GET", "/api/ai/eval-jobs", None
    if name == "ai_registry":
        return "GET", "/api/ai/registry", None
    if name == "deployment_memory":
        return "GET", "/api/ai/memory", None
    if name == "today":
        return "GET", "/api/today", None
    if name == "list_deployments":
        return "GET", "/api/deployments", None
    if name == "get_deployment":
        return "GET", f"/api/deployments/{dep}", None
    if name == "list_tasks":
        q = "mine=true" if a.get("mine") else ""
        if dep:
            q = (q + "&" if q else "") + f"deployment_id={dep}"
        return "GET", "/api/tasks" + (f"?{q}" if q else ""), None
    if name == "create_task":
        return "POST", "/api/tasks", {"deployment_id": dep, "title": a["title"], "assignee_id": a.get("assignee_id"),
                                      "due": a.get("due"),
                                      "visibility": "shared" if a.get("share_with_customer") else "internal"}
    if name == "update_task":
        body = {k: a[k] for k in ("status", "due") if k in a}
        return "PATCH", f"/api/tasks/{a['task_id']}", body
    if name == "advance_deployment":
        return "POST", f"/api/deployments/{dep}/advance", {"to_stage": a["to_stage"], "note": a.get("note", "")}
    if name == "list_engines":
        return "GET", "/api/engines", None
    if name == "list_findings":
        return "GET", f"/api/deployments/{dep}/findings", None
    if name == "draft_status_report":
        return "POST", f"/api/deployments/{dep}/reports", {"audience": a.get("audience", "internal")}
    if name == "request_approval":
        return "POST", f"/api/deployments/{dep}/approvals", {"agent": a["agent"], "request": a["request"],
                                                              "detail": a.get("detail", "")}
    if name == "check_approval":
        return "GET", f"/api/approvals/{a['approval_id']}", None
    if name == "log_delay":
        return "POST", f"/api/deployments/{dep}/delays", {"signal": a["signal"], "reason": a.get("reason", ""),
                                                           "evidence": a.get("evidence", "")}
    if name == "portfolio":
        return "GET", "/api/portfolio", None
    raise KeyError(name)


def _err(id_, code, msg):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": msg}}


def register(app) -> None:
    async def call_api(auth: str, method: str, path: str, body):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://fieldwork.internal") as client:
            return await client.request(method, path, json=body, headers={"Authorization": auth})

    async def run_engine(auth: str, a: dict):
        engines = (await call_api(auth, "GET", "/api/engines", None)).json()
        e = engines.get(a["engine"]) if isinstance(engines, dict) else None
        if not e:
            return None, f"no engine {a['engine']!r}; call list_engines"
        if a["engine"] == "sendero":
            return None, "run Sendero from the console (it needs column choices)"
        path = f"stage/{a['engine']}" if e.get("builtin") else f"custom/{a['engine']}"
        return await call_api(auth, "POST", f"/api/deployments/{a['deployment_id']}/engines/{path}",
                              {"input": a["input"], "title": a.get("title", "")}), None

    @app.get("/mcp", include_in_schema=False)
    def mcp_get():
        return Response(status_code=405, headers={"Allow": "POST"})

    @app.post("/mcp", include_in_schema=False)
    async def mcp(request: Request):
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return JSONResponse({"error": "missing bearer token"}, status_code=401,
                                headers={"WWW-Authenticate": 'Bearer realm="fieldwork"'})
        try:
            msg = await request.json()
        except ValueError:
            return JSONResponse(_err(None, -32700, "parse error"), status_code=400)
        if isinstance(msg, list):
            return JSONResponse(_err(None, -32600, "batching isn't supported"), status_code=400)
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:  # notification, e.g. notifications/initialized
            return Response(status_code=202)
        if method == "initialize":
            me = await call_api(auth, "GET", "/api/me", None)
            if me.status_code != 200:
                return JSONResponse(_err(mid, -32001, "invalid token"), status_code=401)
            asked = params.get("protocolVersion")
            ver = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            who = me.json()
            return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": ver, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "fieldwork", "title": who["branding"]["product_name"], "version": "0.9.0"},
                "instructions": (f"You are working in {who['tenant']['name']}'s deployment workspace as "
                                 f"{who['user']['name']} ({who['user']['role_name']}). Engine results are saved as "
                                 "findings that another person must confirm; say so rather than presenting them as final.")}})
        if method == "ping":
            return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": {}})
        if method == "tools/list":
            return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            tool = next((t for t in TOOLS if t["name"] == name), None)
            if not tool:
                return JSONResponse(_err(mid, -32602, f"unknown tool {name}"))
            missing = [k for k in tool["inputSchema"]["required"] if k not in args]
            if missing:
                return JSONResponse(_err(mid, -32602, f"missing argument(s): {', '.join(missing)}"))
            if name == "run_engine":
                resp, problem = await run_engine(auth, args)
            else:
                m, path, body = _route(name, args)
                resp, problem = await call_api(auth, m, path, body), None
            if problem:
                text, is_error = problem, True
            else:
                try:
                    data = resp.json()
                except ValueError:
                    data = {"detail": resp.text[:500]}
                is_error = resp.status_code >= 400
                if name == "draft_status_report" and not is_error:
                    text = data["body_md"]
                else:
                    text = (data.get("detail") if is_error and isinstance(data, dict) else json.dumps(data, indent=1))
                    if isinstance(text, (dict, list)):
                        text = json.dumps(text)
            return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": str(text)[:100_000]}], "isError": is_error}})
        return JSONResponse(_err(mid, -32601, f"method not found: {method}"))


def stdio_bridge(url: str, token: str) -> None:
    """For MCP clients that launch local servers: relay stdin/stdout JSON-RPC to a Fieldwork /mcp endpoint."""
    endpoint = url.rstrip("/") + "/mcp"
    with httpx.Client(timeout=120, headers={"Authorization": f"Bearer {token}"}) as client:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            try:
                r = client.post(endpoint, json=msg)
                if r.status_code == 202 or msg.get("id") is None:
                    continue
                out = r.json()
            except Exception as e:
                out = _err(msg.get("id"), -32000, f"couldn't reach Fieldwork: {e}")
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
