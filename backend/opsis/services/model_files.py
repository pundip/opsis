"""Every semantic model at once, as a `models/` folder of YAML files, for version
control (brief section 6: models are importable and exportable as `models/*.yml`).

Export: a zip of `models/<dataset>.yml`, one per dataset with data that the
caller may use, each as the model editor shows it. Import: a zip like that, or
one or more YAML files. Every file is checked first and nothing is saved unless
all of them are valid, so a folder never half-applies; a dry run only reports.
A file whose model is already the same is left alone, so importing an export
straight back changes nothing. Each model that changes gets a new version, with
the file name in its note, so an import can be undone per model.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsis.auth.principal import Principal
from opsis.db.models import Dataset
from opsis.errors import ApiError
from opsis.services import access, models
from opsis.settings import Settings

MAX_FILES = 200
MAX_UPLOAD_BYTES = 20_000_000  # a zip, or all the YAML files together
YAML_SUFFIXES = (".yml", ".yaml")


def _current(db: Session, dataset: Dataset) -> dict[str, Any] | None:
    """The model as stored, or the generated starter; None without data."""
    record = models.get_record(db, dataset)
    if record is not None:
        return dict(record.definition)
    columns = models.current_columns(db, dataset)
    return models.starter(dataset, columns) if columns else None


def export_zip(db: Session, principal: Principal) -> tuple[str, bytes]:
    blocked = access.blocked_datasets(db, principal)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for dataset in db.scalars(select(Dataset).order_by(Dataset.name)):
            if dataset.name in blocked:
                continue
            definition = _current(db, dataset)
            if definition is not None:
                archive.writestr(
                    f"models/{dataset.name}.yml", models.to_yaml(dataset.name, definition)
                )
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


def import_files(
    db: Session,
    settings: Settings,
    actor: Principal,
    files: list[tuple[str, bytes]],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Check every file, then (unless a dry run or any file is wrong) save the
    models that change. Each file is reported: changed, unchanged, invalid or
    unknown_dataset; after saving, changed ones are saved with their version."""
    blocked = access.blocked_datasets(db, actor)
    report: list[dict[str, Any]] = []
    plans: list[tuple[Dataset, dict[str, Any], dict[str, Any]]] = []
    seen: dict[str, str] = {}
    for name, content in files:
        entry: dict[str, Any] = {"file": name, "dataset": None, "status": "invalid"}
        report.append(entry)
        try:
            document = models.from_yaml(content.decode("utf-8"))
        except UnicodeDecodeError:
            entry["problems"] = [{"path": "/", "message": "The file is not UTF-8 text."}]
            continue
        except ApiError as exc:
            entry["problems"] = [{"path": "/", "message": exc.message}]
            continue
        dataset_name = document.get("dataset") or PurePosixPath(name).stem
        entry["dataset"] = dataset_name
        if not isinstance(dataset_name, str):
            entry["problems"] = [{"path": "/dataset", "message": "Name the dataset as text."}]
            continue
        if dataset_name in seen:
            entry["problems"] = [
                {"path": "/dataset", "message": f"{seen[dataset_name]} is also for this dataset."}
            ]
            continue
        seen[dataset_name] = name
        dataset = db.scalar(select(Dataset).where(Dataset.name == dataset_name))
        if dataset is None or dataset.name in blocked:
            entry["status"] = "unknown_dataset"
            entry["problems"] = [
                {
                    "path": "/dataset",
                    "message": f"There is no dataset called {dataset_name!r}. Create it and "
                    "refresh it first, then import its model.",
                }
            ]
            continue
        found = models.problems(db, settings, dataset, document)
        if found:
            entry["problems"] = found
            continue
        stored = {k: v for k, v in document.items() if k != "dataset"}
        if _same(stored, _current(db, dataset)):
            entry["status"] = "unchanged"
        else:
            entry["status"] = "changed"
            plans.append((dataset, document, entry))
    valid = all(e["status"] in ("changed", "unchanged") for e in report)
    db.rollback()  # nothing was written while checking
    if dry_run or not valid:
        return {"applied": False, "valid": valid, "files": report}
    for dataset, document, entry in plans:
        record = models.save(
            db, settings, actor, dataset, document, message=f"Imported from {entry['file']}"
        )
        entry["status"] = "saved"
        entry["version"] = record.version
    return {"applied": True, "valid": True, "files": report}
