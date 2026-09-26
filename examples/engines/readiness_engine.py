"""Example webhook engine: go-live readiness scorer.

The kind of script every deployment team already has somewhere. Registered in
Fieldwork as a webhook engine, it runs from the deployment page, and its
answer becomes a finding with confirmation and audit like any built-in engine.

Input: a checklist, one item per line, "[x]" done / "[ ]" open. Lines tagged
"(blocker)" fail the check if still open.

    FIELDWORK_ENGINE_SECRET=fws_... python examples/engines/readiness_engine.py --port 8787

Standard library only, so it drops into any environment.
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.environ.get("FIELDWORK_ENGINE_SECRET", "")
ITEM = re.compile(r"^\s*[-*]?\s*\[( |x|X)\]\s*(.+)$")


def verify(ts: str, body: bytes, header: str) -> bool:
    if not SECRET:
        return False
    try:
        if abs(time.time() - int(ts)) > 300:
            return False
    except ValueError:
        return False
    mac = "sha256=" + hmac.new(SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, header or "")


def score(checklist: str) -> dict:
    items = []
    for line in checklist.splitlines():
        m = ITEM.match(line)
        if m:
            text = m.group(2).strip()
            items.append({"item": text, "done": m.group(1).lower() == "x",
                          "blocker": "(blocker)" in text.lower()})
    if not items:
        return {"summary": "No checklist items found. Use '[x] done' / '[ ] open' lines.",
                "status": "info", "result": {}}
    done = sum(i["done"] for i in items)
    open_blockers = [i["item"] for i in items if i["blocker"] and not i["done"]]
    pct = round(100 * done / len(items))
    if open_blockers:
        status, verdict = "fail", f"Not ready: {len(open_blockers)} open blocker(s)"
    elif pct < 90:
        status, verdict = "warn", "Ready with risk"
    else:
        status, verdict = "pass", "Ready for cutover"
    return {"summary": f"{verdict} · {done}/{len(items)} items done ({pct}%)",
            "status": status,
            "result": {"percent_done": pct, "open_blockers": open_blockers,
                       "open_items": [i["item"] for i in items if not i["done"]]}}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if not verify(self.headers.get("X-Fieldwork-Timestamp", ""), body,
                      self.headers.get("X-Fieldwork-Signature", "")):
            self.send_response(401); self.end_headers(); return
        req = json.loads(body)
        out = json.dumps(score(req.get("input", ""))).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    print(f"readiness engine on :{args.port}")
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
