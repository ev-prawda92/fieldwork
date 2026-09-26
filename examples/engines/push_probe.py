"""Example push engine: a probe that runs inside the customer's environment.

Some checks can only run where the customer's systems are: behind their
firewall, on a schedule, in CI. Those scripts push their results into
Fieldwork with an engine token instead of waiting to be called.

    FIELDWORK_URL=https://your-workspace FIELDWORK_ENGINE_TOKEN=fwe_... \
        python examples/engines/push_probe.py --deployment dep_castellan

Standard library only.
"""

import argparse
import json
import os
import urllib.request


def post_finding(base: str, token: str, deployment_id: str, title: str,
                 summary: str, status: str, result: dict) -> dict:
    body = json.dumps({"deployment_id": deployment_id, "title": title, "summary": summary,
                       "status": status, "result": result}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/api/ingest/findings", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--deployment", required=True)
    args = ap.parse_args()
    # A real probe would measure something here: p95 latency of the agent's
    # tool calls, error rates from the customer's gateway, queue depth...
    measured = {"p95_ms": 1840, "error_rate": 0.031, "calls": 12480, "slo_p95_ms": 1500}
    breach = measured["p95_ms"] > measured["slo_p95_ms"]
    print(post_finding(
        os.environ["FIELDWORK_URL"], os.environ["FIELDWORK_ENGINE_TOKEN"], args.deployment,
        title="Agent latency vs SLO (last 24h)",
        summary=(f"p95 {measured['p95_ms']} ms against a {measured['slo_p95_ms']} ms SLO"
                 f" · {measured['error_rate']:.1%} errors over {measured['calls']:,} calls"),
        status="fail" if breach else "pass", result=measured))
