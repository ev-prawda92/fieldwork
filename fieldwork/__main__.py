"""fieldwork: command line.

  fieldwork run <engine> <file>        run an engine on a local file, no server needed
  fieldwork engines                    list the built-in engines and their inputs
  fieldwork serve                      run the API and console (plus the outbox worker)
  fieldwork worker                     run only the outbox worker (Slack, webhooks, tracker sync)
  fieldwork digest                     queue today's Slack digest for every workspace
  fieldwork mcp --url U --token T      MCP stdio bridge for AI tools that launch local servers
  fieldwork seed | migrate | rotate-keys

Database: --db <sqlite path or postgresql:// URL>, or FIELDWORK_DATABASE_URL / FIELDWORK_DB.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def main() -> None:
    ap = argparse.ArgumentParser(prog="fieldwork", description="The deployment engine for deployment teams")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run an engine on a local file")
    r.add_argument("engine")
    r.add_argument("file", help="input file, or - for stdin")
    r.add_argument("--json", action="store_true", help="print the full JSON result")
    sub.add_parser("engines", help="list built-in engines")
    for name, helptext in (("seed", "wipe the database and build the demo workspace"),
                           ("migrate", "apply pending database migrations"),
                           ("rotate-keys", "re-encrypt stored secrets under the newest key"),
                           ("worker", "deliver queued notifications and tracker sync"),
                           ("digest", "queue the daily Slack digest")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--db", default=None)
    sub.choices["seed"].add_argument("--if-empty", action="store_true",
                                     help="only seed a database with no workspaces yet")
    v = sub.add_parser("serve", help="run the API and console")
    v.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--db", default=None)
    m = sub.add_parser("mcp", help="MCP stdio bridge")
    m.add_argument("--url", default=os.environ.get("FIELDWORK_URL", ""))
    m.add_argument("--token", default=os.environ.get("FIELDWORK_TOKEN", ""))
    args = ap.parse_args()

    if args.cmd == "run":
        from .engines import REGISTRY, stages
        fn = stages.STAGE_ENGINES.get(args.engine)
        if not fn:
            sys.exit(f"unknown engine {args.engine!r}; try: {', '.join(stages.STAGE_ENGINES)}")
        text = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
        try:
            res = fn(text)
        except stages.StageEngineError as e:
            sys.exit(f"{REGISTRY[args.engine]['name']}: {e}")
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"[{res['status'].upper()}] {res['summary']}")
            if args.engine == "census":
                print("\n" + res["result"]["memo"])
        sys.exit(1 if res["status"] == "fail" else 0)
    if args.cmd == "engines":
        from .engines import REGISTRY, stages
        for k in stages.STAGE_ENGINES:
            e = REGISTRY[k]
            print(f"{k:<12} {e['name']}\n{'':<12} {e['does']}\n{'':<12} input: {e['input_hint']}\n")
        return
    if args.cmd == "mcp":
        if not args.url or not args.token:
            sys.exit("need --url and --token (or FIELDWORK_URL / FIELDWORK_TOKEN)")
        from .mcp import stdio_bridge
        stdio_bridge(args.url, args.token)
        return

    from . import db
    url = args.db or db.database_url()
    if args.cmd == "seed":
        from .seed import seed
        if args.if_empty:
            conn = db.connect(url)
            db.init(conn)
            if conn.execute("SELECT COUNT(*) n FROM tenants").fetchone()["n"]:
                print("database already has workspaces; not seeding")
                return
        tokens = seed(url)
        print(f"seeded {url}")
        for k, t in tokens.items():
            print(f"  {k:<13} {t}")
    elif args.cmd == "migrate":
        print(f"applied: {db.migrate(db.connect(url)) or 'nothing pending'}")
    elif args.cmd == "rotate-keys":
        from . import crypto
        print(f"re-encrypted {crypto.rotate_all(db.connect(url))} secret(s)")
    elif args.cmd == "digest":
        from . import events
        conn = db.connect(url)
        db.init(conn)
        print(f"queued {events.queue_digests(conn)} digest(s); delivered {events.process(conn)}")
    elif args.cmd == "worker":
        import time
        from . import events
        conn = db.connect(url)
        db.init(conn)
        print("outbox worker running")
        while True:
            events.process(conn)
            time.sleep(5)
    elif args.cmd == "serve":
        import uvicorn
        from .app import create_app
        os.environ.setdefault("FIELDWORK_DATABASE_URL" if url.startswith("postgres") else "FIELDWORK_DB", url)
        uvicorn.run(create_app(url, background=True), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
