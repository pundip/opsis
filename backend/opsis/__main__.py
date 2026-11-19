"""Container entrypoint: python -m opsis <command>.

Written in Python, not shell, so it behaves the same on every platform.

  api [--reload]   run migrations, then serve the app
  worker           run the job worker
  migrate          run migrations only
  check-config     validate the environment and exit (used by the config-check service)
  healthcheck      call the app's own health endpoint (used by the Docker healthcheck)
  generate-key     print a new ENCRYPTION_KEY
  reset-link EMAIL print a one-time link to set the password of EMAIL (recovery)
"""

from __future__ import annotations

import logging
import sys

from opsis.settings import ConfigError, Settings, load_settings

COMMANDS = ("api", "worker", "migrate", "check-config", "healthcheck", "generate-key", "reset-link")
USAGE = f"usage: python -m opsis <{'|'.join(COMMANDS)}>"


def _settings_or_exit() -> Settings:
    try:
        return load_settings()
    except ConfigError as exc:
        # No traceback: the message is the whole point.
        print(f"\n{exc}\n\nFix the values in .env and start again.\n", file=sys.stderr, flush=True)
        raise SystemExit(2) from None


def _healthcheck(settings: Settings) -> None:
    import urllib.request

    url = f"http://127.0.0.1:8080{settings.base_path}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310
            ok = response.status == 200
    except OSError:
        ok = False
    raise SystemExit(0 if ok else 1)


def _reset_link(settings: Settings, args: list[str]) -> None:
    """Recovery when no admin can sign in: anyone who can run commands on the
    server already controls it, so this needs no further proof."""
    if len(args) != 1:
        raise SystemExit("usage: python -m opsis reset-link <email>")
    if not settings.sign_in:
        raise SystemExit("AUTH_MODE=none has no passwords: nobody needs to sign in.")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from opsis.errors import ApiError
    from opsis.services import accounts

    engine = create_engine(settings.sqlalchemy_url)
    try:
        with sessionmaker(engine)() as db:
            url = accounts.reset_link_for_email(db, settings, args[0])
    except ApiError as exc:
        raise SystemExit(exc.message) from None
    finally:
        engine.dispose()
    print(f"One-time link for {args[0]}, valid for {accounts.RESET_HOURS} hours:")
    print(url)


def main(argv: list[str]) -> None:
    if not argv or argv[0] not in COMMANDS:
        raise SystemExit(USAGE)
    command = argv[0]
    if command == "generate-key":
        from cryptography.fernet import Fernet

        print(Fernet.generate_key().decode())
        return

    settings = _settings_or_exit()
    if command == "healthcheck":
        _healthcheck(settings)
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if command == "check-config":
        print(
            f"Configuration is valid: deploy_mode={settings.deploy_mode} "
            f"auth_mode={settings.auth_mode} base_path={settings.base_path or '/'}"
        )
        return

    if command == "reset-link":
        _reset_link(settings, argv[1:])
        return

    from opsis.db.migrate import upgrade_to_head

    if command in ("api", "migrate"):
        upgrade_to_head(settings.sqlalchemy_url)
    if command == "api":
        import uvicorn

        trusted = settings.trusted_proxy_list
        uvicorn.run(
            "opsis.main:create_app",
            factory=True,
            host="0.0.0.0",  # noqa: S104  inside the container; the host side is BIND_ADDRESS
            port=8080,
            log_level=settings.log_level,
            reload="--reload" in argv,
            reload_dirs=["opsis"] if "--reload" in argv else None,
            # X-Forwarded-* is honoured only from TRUSTED_PROXIES, ignored otherwise.
            proxy_headers=bool(trusted),
            forwarded_allow_ips=",".join(trusted) if trusted else None,
        )
    elif command == "worker":
        from opsis.jobs.worker import run_forever

        run_forever(settings)


if __name__ == "__main__":
    main(sys.argv[1:])
