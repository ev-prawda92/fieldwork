"""python -m fieldwork seed | serve [--port 8000] | verify"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(prog="fieldwork")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="build the demo workspace (overwrites the demo db)")
    s.add_argument("--db", default=None)
    v = sub.add_parser("serve", help="run the API and console")
    v.add_argument("--port", type=int, default=8000)
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--db", default=None)
    args = ap.parse_args()

    from . import db
    path = args.db or db.db_path()

    if args.cmd == "seed":
        from .seed import seed
        if Path(path).exists():
            Path(path).unlink()
        tokens = seed(path)
        print(f"seeded {path}")
        for k, t in tokens.items():
            print(f"  {k:<13} {t}")
    elif args.cmd == "serve":
        import uvicorn
        from .app import create_app
        os.environ.setdefault("FIELDWORK_DB", path)
        uvicorn.run(create_app(path), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
