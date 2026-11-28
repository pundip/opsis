"""The workspace model as a `models/` folder of YAML files, for version control
(V2_DESIGN 1.4; brief section 6: models are importable and exportable as
`models/*.yml`).

- **Export:** a zip of `models/<table>.yml`, one per table the caller may use, each
  with a `table` key, and `models/workspace-model.yml` with the relationships
  between them and the layout. `workspace-model` cannot be a table name: table names
  have no hyphen.
- **Import:** a zip like that, or YAML files. v1 files (`dataset`, and `joins`) are
  read too: their joins become the table's relationships, by the migration's rules.
  Every file is checked first and nothing is saved unless all of them are valid, so
  a folder never half-applies; a dry run only reports. A table that is already the
  same is left alone, so importing an export straight back changes nothing. What
  changes is saved as one version of the workspace model, with the files in its
  note.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy.orm import Session

from opsis.auth.principal import Principal
from opsis.errors import ApiError
from opsis.semantic import model as semantic
from opsis.semantic import workspace as ws
from opsis.services import model_yaml, models
from opsis.services import workspace_model as wm
from opsis.settings import Settings

MAX_FILES = 200
MAX_UPLOAD_BYTES = 20_000_000  # a zip, or all the YAML files together
YAML_SUFFIXES = (".yml", ".yaml")
WORKSPACE_FILE = "workspace-model"
WORKSPACE_KEYS = ("modelVersion", "label", "description", "relationships", "layout")


def export_zip(db: Session, principal: Principal) -> tuple[str, bytes]:
    state = wm.current(db)
    allowed = wm.visible_to(db, principal)
    visible = ws.visible_part(state.effective, allowed)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, entry in visible["tables"].items():
            archive.writestr(f"models/{name}.yml", model_yaml.dump({"table": name, **entry}))
        rest = {key: visible[key] for key in WORKSPACE_KEYS if key in visible}
        rest["layout"] = wm.visible_layout(state, allowed)
        archive.writestr(f"models/{WORKSPACE_FILE}.yml", model_yaml.dump(rest))
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    return f"opsis-models-{stamp}.zip", buffer.getvalue()


def read_uploads(uploads: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    """(file name, content) of each YAML file in the uploads: zips are opened."""
    files: list[tuple[str, bytes]] = []
    for name, content in uploads:
        lower = name.lower()
        if lower.endswith(".zip"):
            try:
                archive = zipfile.ZipFile(io.BytesIO(content))
            except zipfile.BadZipFile:
                raise ApiError(422, "invalid_file", f"{name} is not a zip file.") from None
            entries = [
                info
                for info in archive.infolist()
                if not info.is_dir()
                and info.filename.lower().endswith(YAML_SUFFIXES)
                # Not the folders and hidden files that zip tools add.
                and not info.filename.startswith("__MACOSX/")
                and not any(part.startswith(".") for part in PurePosixPath(info.filename).parts)
            ]
            # Sizes are checked before anything is unpacked.
            if sum(info.file_size for info in entries) > MAX_UPLOAD_BYTES:
                raise ApiError(413, "too_large", f"{name} unpacks to more than 20 MB of YAML.")
            files += [(info.filename, archive.read(info)) for info in entries]
        elif lower.endswith(YAML_SUFFIXES):
            files.append((name, content))
        else:
            raise ApiError(
                422,
                "invalid_file",
                f"{name} is neither a zip nor a YAML file. Send a zip of a models/ folder, "
                "or .yml files.",
            )
    if not files:
        raise ApiError(422, "invalid_file", "There are no .yml files in what was sent.")
    if len(files) > MAX_FILES:
        raise ApiError(413, "too_large", f"At most {MAX_FILES} model files at once.")
    return files


def _same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


@dataclass
class _File:
    name: str
    entry: dict[str, Any]  # what the report says of it
    kind: str  # "table", "v1" or "workspace"
    document: dict[str, Any]
    table: str | None = None
    joins: dict[str, str] = field(default_factory=dict)  # v1: relationship id -> join index


def _problem(entry: dict[str, Any], found: list[semantic.Problem]) -> None:
    entry["status"] = "invalid"
    entry.setdefault("problems", []).extend(found)


def _table_entry(document: dict[str, Any], table: str) -> dict[str, Any]:
    body = {k: v for k, v in document.items() if k != "table"}
    body.setdefault("source", {"dataset": table})
    return body


def _read(
    settings: Settings,
    state: wm.State,
    allowed: wm.Visible,
    name: str,
    content: bytes,
    entry: dict[str, Any],
) -> _File | None:
    """One file, checked on its own (its structure, and that its dataset exists)."""
    try:
        document = model_yaml.load(content.decode("utf-8"), "a mapping")
    except UnicodeDecodeError:
        _problem(entry, [{"path": "/", "message": "The file is not UTF-8 text."}])
        return None
    except ApiError as exc:
        _problem(entry, [{"path": "/", "message": exc.message}])
        return None
    stem = PurePosixPath(name).stem
    if stem == WORKSPACE_FILE:
        entry["kind"] = "workspace"
        found = wm.schema_check(settings, {"modelVersion": ws.MODEL_VERSION, **document})
        if "tables" in document:
            found.append(
                {"path": "/tables", "message": "Write each table in its own models/<table>.yml."}
            )
        if found:
            _problem(entry, found)
            return None
        return _File(name, entry, "workspace", document)
    v2 = "table" in document or "source" in document
    key = "table" if v2 else "dataset"
    table = document.get(key) or stem
    entry["dataset"] = table if isinstance(table, str) else None
    entry["kind"] = "table" if v2 else "v1"
    if not isinstance(table, str):
        _problem(entry, [{"path": f"/{key}", "message": "Name the table as text."}])
        return None
    if table not in state.datasets or table not in allowed:
        entry["status"] = "unknown_dataset"
        entry["problems"] = [
            {
                "path": f"/{key}",
                "message": f"There is no dataset called {table!r}. Create it and refresh it "
                "first, then import its model.",
            }
        ]
        return None
    if v2:
        validator, _ = models.validators(settings.spec_dir)
        found = semantic.schema_problems(validator, _table_entry(document, table))
        if not found and document.get("source") not in (None, {"dataset": table}):
            found = [{"path": "/source", "message": f"The table {table!r} reads {table!r}."}]
    else:
        found = models.structure_problems(settings, table, document)
        if not found:
            found = models.join_problems(table, document, state, allowed)
    if found:
        _problem(entry, found)
        return None
    return _File(name, entry, entry["kind"], document, table)


def _attribute(
    found: list[semantic.Problem], parsed: list[_File], report: list[dict[str, Any]]
) -> None:
    """Report each problem of the whole model on the file it comes from."""
    tables = {f.table: f for f in parsed if f.table is not None}
    workspace = next((f for f in parsed if f.kind == "workspace"), None)
    for problem in found:
        parts = problem["path"].split("/")
        target, path = None, problem["path"]
        if len(parts) >= 3 and parts[1] == "tables" and parts[2] in tables:
            target = tables[parts[2]]
            path = "/" + "/".join(parts[3:])
        elif len(parts) >= 3 and parts[1] == "relationships":
            if workspace is not None:
                target = workspace
            else:
                for f in parsed:
                    if parts[2] in f.joins:
                        target, path = f, f"/joins/{f.joins[parts[2]]}"
        if target is None:
            target = workspace or (parsed[0] if parsed else None)
        if target is None:
            _problem(report[0], [problem])
        else:
            _problem(target.entry, [{**problem, "path": path}])


def import_files(
    db: Session,
    settings: Settings,
    actor: Principal,
    files: list[tuple[str, bytes]],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Check every file, then (unless a dry run or any file is wrong) save what
    changes. Each file is reported: changed, unchanged, invalid or unknown_dataset;
    after saving, changed ones are `saved`, with the version."""
    state = wm.current(db)
    allowed = wm.visible_to(db, actor)
    visible = ws.visible_part(state.effective, allowed)
    report: list[dict[str, Any]] = []
    parsed: list[_File] = []
    seen: dict[str, str] = {}
    for name, content in files:
        entry: dict[str, Any] = {"file": name, "dataset": None, "status": "invalid"}
        report.append(entry)
        found = _read(settings, state, allowed, name, content, entry)
        if found is None:
            continue
        key = found.table or f"/{WORKSPACE_FILE}"
        if key in seen:
            what = "the workspace model" if found.table is None else "this table"
            _problem(entry, [{"path": "/", "message": f"{seen[key]} is also for {what}."}])
            continue
        seen[key] = name
        parsed.append(found)
    if any("problems" in e for e in report):
        db.rollback()
        return {"applied": False, "valid": False, "files": report}

    # The model the files make, from the part the caller may use.
    combined = ws.definition_of(visible)
    layout = None
    workspace = next((f for f in parsed if f.kind == "workspace"), None)
    if workspace is not None:
        for key in ("label", "description"):
            combined.pop(key, None)
            if key in workspace.document:
                combined[key] = workspace.document[key]
        combined["relationships"] = dict(workspace.document.get("relationships") or {})
        layout = workspace.document.get("layout")
        different = not (
            _same(combined.get("label"), visible.get("label"))
            and _same(combined.get("description"), visible.get("description"))
            and _same(combined["relationships"], visible["relationships"])
            and (layout is None or _same(layout, wm.visible_layout(state, allowed)))
        )
        workspace.entry["status"] = "changed" if different else "unchanged"
    for f in parsed:
        if f.kind == "table":
            assert f.table is not None
            body = _table_entry(f.document, f.table)
            if not _same(body, visible["tables"].get(f.table)):
                combined["tables"][f.table] = body
                f.entry["status"] = "changed"
            else:
                f.entry["status"] = "unchanged"
        elif f.kind == "v1":
            assert f.table is not None
            view = {k: v for k, v in f.document.items() if k != "dataset"}
            before = ws.table_view(combined, f.table)
            if _same(view, before):
                f.entry["status"] = "unchanged"
                f.joins = ws.view_positions(combined, f.table)
                continue
            combined, by_index = ws.apply_table_view(combined, f.table, view)
            f.joins = {rid: index for index, rid in by_index.items()}
            same = _same(combined["tables"][f.table], visible["tables"].get(f.table)) and _same(
                ws.table_view(combined, f.table), ws.table_view(visible, f.table)
            )
            f.entry["status"] = "unchanged" if same else "changed"

    try:
        full, _ = wm.plan(db, settings, state, allowed, document=combined)
    except ApiError as exc:
        if exc.code != "invalid_model":
            raise
        _attribute(exc.details, parsed, report)
        db.rollback()
        return {"applied": False, "valid": False, "files": report}
    new, _ = wm.check(settings, state, allowed, full)
    if new:
        _attribute(new, parsed, report)
    valid = not any("problems" in e for e in report)
    db.rollback()  # nothing was written while checking
    if dry_run or not valid:
        return {"applied": False, "valid": valid, "files": report}
    changed = [e for e in report if e["status"] == "changed"]
    if changed:
        names = ", ".join(e["file"] for e in changed)
        document = {**combined, "layout": layout} if isinstance(layout, dict) else combined
        saved = wm.save(db, settings, actor, document=document, message=f"Imported from {names}")
        for entry in changed:
            entry["status"] = "saved"
            entry["version"] = saved.version
    return {"applied": True, "valid": True, "files": report}
