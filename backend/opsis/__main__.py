"""Container entrypoint: python -m opsis <api|worker|migrate|check-config>.

Written in Python, not shell, so it behaves the same on every platform.
"""

from __future__ import annotations

import logging
import sys

from opsis.settings import ConfigError, Settings, load_settings

USAGE = "usage: python -m opsis <api|worker|migrate|check-config> [--reload]"


def _settings_or_exit() -> Settings:
    try:
        return load_settings()
    except ConfigError as exc:
        # No traceback: the message is the whole point.
        print(f"\n{exc}\n\nFix the values in .env and start again.\n", file=sys.stderr, flush=True)
        raise SystemExit(2) from None


def main(argv: list[str]) -> None:
    if not argv or argv[0] not in ("api", "worker", "migrate", "check-config"):
        raise SystemExit(USAGE)
    command = argv[0]
    settings = _settings_or_exit()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if command == "check-config":
        print("Configuration is valid.")
        return

    from opsis.db.migrate import upgrade_to_head

    if command in ("api", "migrate"):
        upgrade_to_head(settings.sqlalchemy_url)
    if command == "api":
        import uvicorn

        uvicorn.run(
            "opsis.main:create_app",
            factory=True,
            host="0.0.0.0",  # inside the container; the host side is BIND_ADDRESS
            port=8080,
            log_level=settings.log_level,
            reload="--reload" in argv,
            reload_dirs=["opsis"] if "--reload" in argv else None,
            proxy_headers=settings.deploy_mode == "server",
            forwarded_allow_ips="*" if settings.deploy_mode == "server" else None,
        )
    elif command == "worker":
        from opsis.jobs.worker import run_forever

        run_forever(settings)


if __name__ == "__main__":
    main(sys.argv[1:])
