"""CLI entrypoint for the recap GraphQL server.

Usage:
    python -m recap.server --db /path/to/recap.db
    python -m recap.server --config recap-server.yaml
    recap-server --db recap.db --port 8000
"""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="recap-server",
        description="recap GraphQL read API server",
    )
    parser.add_argument("--db", metavar="PATH", help="Path to SQLite database file")
    parser.add_argument("--config", metavar="PATH", help="Path to YAML config file")
    parser.add_argument("--host", default=None, help="Bind host (default: 127.0.0.1)")
    parser.add_argument(
        "--port", type=int, default=None, help="Bind port (default: 8000)"
    )
    parser.add_argument(
        "--log-level", default=None, dest="log_level", help="Log level (default: info)"
    )
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn not installed. Install with: pip install 'pyrecap[server]'",
            file=sys.stderr,
        )
        sys.exit(1)

    from recap.server.config import ServerConfig

    if args.config:
        cfg = ServerConfig.from_yaml(args.config)
    elif args.db:
        cfg = ServerConfig(db_path=args.db)
    else:
        parser.error("Either --db or --config is required")

    # CLI flags override config file values
    if args.host is not None:
        cfg = cfg.model_copy(update={"host": args.host})
    if args.port is not None:
        cfg = cfg.model_copy(update={"port": args.port})
    if args.log_level is not None:
        cfg = cfg.model_copy(update={"log_level": args.log_level})

    from recap.server.app import create_app

    app = create_app(cfg.db_path)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=cfg.log_level)


if __name__ == "__main__":
    main()
