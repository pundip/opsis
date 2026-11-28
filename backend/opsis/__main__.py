"""Container entrypoint: python -m opsis <command>.

Written in Python, not shell, so it behaves the same on every platform.

  api [--reload]   run migrations, then serve the app
  worker           run the job worker
  migrate          run migrations only
  check-config     validate the environment and exit (used by the config-check service)
  healthcheck      call the app's own health endpoint (used by the Docker healthcheck)
  generate-key     print a new ENCRYPTION_KEY
  reset-link EMAIL print a one-time link to set the password of EMAIL (recovery)
  backup           write a backup to BACKUP_DIR now
  restore FILE     replace the database and data with a backup (api and worker stopped)
  seed-demo        load the demo datasets and dashboard (made-up giving data)
  results-digest [--out FILE] [--compare FILE] [--today YYYY-MM-DD]
                   what every dashboard shows each viewer (changes nothing); with
                   --compare, every result that changed since an earlier digest
"""

from __future__ import annotations

import logging
import sys

from opsis.settings import ConfigError, Settings, load_settings

COMMANDS = (
    "api",
    "worker",
    "migrate",
    "check-config",
    "healthcheck",
    "generate-key",
    "reset-link",
    "backup",
    "restore",
    "seed-demo",
    "results-digest",
)
DIGEST_USAGE = (
    "usage: python -m opsis results-digest [--out FILE] [--compare FILE] [--today YYYY-MM-DD]"
)
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


def _backup(settings: Settings) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from opsis.errors import JobError
    from opsis.services import backups

    engine = create_engine(settings.sqlalchemy_url)
    try:
        with sessionmaker(engine)() as db:
            made = backups.create(db, settings)
    except JobError as exc:
        raise SystemExit(str(exc)) from None
    finally:
        engine.dispose()
    print(f"Backup written: {settings.backup_dir / made['file']} ({made['bytes']:,} bytes).")


def _restore(settings: Settings, args: list[str]) -> None:
    from pathlib import Path

    from opsis.errors import JobError
    from opsis.services import backups

    files = [a for a in args if not a.startswith("--")]
    if len(files) != 1:
        raise SystemExit("usage: python -m opsis restore <backup file> --yes")
    path = Path(files[0])
    if not path.is_absolute() and not path.exists():
        path = settings.backup_dir / path
    try:
        manifest = backups.read_manifest(path)
    except JobError as exc:
        raise SystemExit(str(exc)) from None
    print(
        f"Backup {path.name}: made {manifest.get('createdAt')} by Opsis {manifest.get('version')}."
    )
    if "--yes" not in args:
        raise SystemExit(
            "This replaces the whole database and the uploads and datasets in the data "
            "folder with the backup's (the current ones are moved aside, not deleted). Stop "
            "the api and worker first, then run again with --yes."
        )
    try:
        done = backups.restore(settings, path)
    except JobError as exc:
        raise SystemExit(str(exc)) from None
    print("Restored. Start the stack: the api upgrades the database when it starts.")
    if done["previousDataMovedTo"]:
        print(f"The previous uploads and datasets are in {done['previousDataMovedTo']}.")


def _seed_demo(settings: Settings) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from opsis.errors import JobError
    from opsis.services import demo

    engine = create_engine(settings.sqlalchemy_url)
    try:
        with sessionmaker(engine, expire_on_commit=False)() as db:
            made = demo.seed(db, settings)
    except JobError as exc:
        raise SystemExit(str(exc)) from None
    finally:
        engine.dispose()
    rows = ", ".join(f"{name} ({count:,} rows)" for name, count in made["rows"].items())
    print(f"Demo data loaded: {rows}.")
    print(f"Open the dashboard: {made['url']}")


def _results_digest(settings: Settings, args: list[str]) -> None:
    """Write what every dashboard shows each viewer (hashes) and a short report, or
    with --compare, what changed since an earlier digest. Reads only."""
    import json
    from datetime import UTC, date, datetime
    from pathlib import Path

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from opsis.services import results_digest

    options: dict[str, str | None] = {"--out": None, "--compare": None, "--today": None}
    rest = list(args)
    while rest:
        flag = rest.pop(0)
        if flag not in options or not rest:
            raise SystemExit(DIGEST_USAGE)
        options[flag] = rest.pop(0)
    previous = None
    if options["--compare"]:
        try:
            previous = json.loads(Path(options["--compare"]).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Cannot read {options['--compare']}: {exc}") from None
        if not isinstance(previous, dict) or previous.get("format") != results_digest.FORMAT:
            raise SystemExit(f"{options['--compare']} is not a results digest.")
    day = previous["today"] if previous else options["--today"]
    try:
        today = date.fromisoformat(day) if day else None
    except ValueError:
        raise SystemExit("--today must be a date: YYYY-MM-DD.") from None

    engine = create_engine(settings.sqlalchemy_url)
    runtime = results_digest.runtime_for(settings)
    try:
        with sessionmaker(engine, expire_on_commit=False)() as db:
            result = results_digest.digest(
                db,
                settings,
                runtime,
                today=today,
                progress=lambda line: print(line, file=sys.stderr),
            )
            db.rollback()
    finally:
        runtime.engine.close()
        engine.dispose()
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    name = f"results-{'compared' if previous else 'digest'}-{stamp}.json"
    out = Path(options["--out"]) if options["--out"] else settings.data_dir / "upgrade" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    text = (
        results_digest.compare_report(previous, result)
        if previous
        else results_digest.report(result)
    )
    out.with_suffix(".md").write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"Written: {out} (the digest, for --compare) and {out.with_suffix('.md')}.")


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
    if command == "backup":
        _backup(settings)
        return
    if command == "restore":
        _restore(settings, argv[1:])
        return
    if command == "seed-demo":
        _seed_demo(settings)
        return
    if command == "results-digest":
        _results_digest(settings, argv[1:])
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
