"""python -m fieldwork seed | serve | migrate | rotate-keys

Database: --db <sqlite path or postgresql:// URL>, or FIELDWORK_DATABASE_URL /
FIELDWORK_DB in the environment.
"""

from __future__ import annotations

import argparse
import os


def main() -> None:
    ap = argparse.ArgumentParser(prog="fieldwork")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="wipe the database and build the demo workspace")
    s.add_argument("--db", default=None)
    v = sub.add_parser("serve", help="run the API and console")
    v.add_argument("--port", type=int, default=8000)
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--db", default=None)
    m = sub.add_parser("migrate", help="apply pending database migrations")
    m.add_argument("--db", default=None)
    r = sub.add_parser("rotate-keys", help="re-encrypt stored secrets under the newest key")
    r.add_argument("--db", default=None)
    args = ap.parse_args()

    from . import db
    url = args.db or db.database_url()

    if args.cmd == "seed":
        from .seed import seed
        tokens = seed(url)
        print(f"seeded {url}")
        for k, t in tokens.items():
            print(f"  {k:<13} {t}")
    elif args.cmd == "migrate":
        ran = db.migrate(db.connect(url))
        print(f"applied: {ran or 'nothing pending'}")
    elif args.cmd == "rotate-keys":
        from . import crypto
        print(f"re-encrypted {crypto.rotate_all(db.connect(url))} secret(s)")
    elif args.cmd == "serve":
        import uvicorn
        from .app import create_app
        os.environ.setdefault("FIELDWORK_DATABASE_URL" if url.startswith("postgres") else "FIELDWORK_DB", url)
        uvicorn.run(create_app(url), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
