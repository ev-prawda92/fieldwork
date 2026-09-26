"""The demo workspace's own "go-live readiness" engine, served by the app itself.

Same scoring as examples/engines/readiness_engine.py. In demo mode the app
hosts it at /demo-engines/readiness so the hosted demo's webhook engine works
without a second service. It verifies Fieldwork's signature like any engine.
"""

import re

ITEM = re.compile(r"^\s*[-*]?\s*\[( |x|X)\]\s*(.+)$")


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
