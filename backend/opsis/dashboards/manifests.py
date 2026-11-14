"""Visual manifests: one folder per visual, each with a manifest.json.

The frontend owns the folders (frontend/src/visuals/<id>/). The Docker build
copies the manifests to /app/visuals so the API and the agent can list visuals
and validate dashboard items without any frontend code.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from opsis.semantic.model import schema_problems, schema_validator

log = logging.getLogger("opsis.visuals")

Manifest = dict[str, Any]


@lru_cache(maxsize=4)
def load_manifests(visuals_dir: Path, spec_dir: Path) -> dict[str, Manifest]:
    """Every valid manifest, by visual id. An invalid one is logged and skipped."""
    validator = schema_validator(spec_dir, "visual-manifest.schema.json")
    manifests: dict[str, Manifest] = {}
    if not visuals_dir.is_dir():
        log.warning("No visuals directory at %s", visuals_dir)
        return manifests
    for path in sorted(visuals_dir.glob("*/manifest.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Skipping %s: %s", path, exc)
            continue
        problems = schema_problems(validator, manifest)
        if problems or manifest.get("id") != path.parent.name:
            reason = problems[0]["message"] if problems else "id does not match the folder name"
            log.warning("Skipping visual %s: %s", path.parent.name, reason)
            continue
        manifests[manifest["id"]] = manifest
    return manifests
