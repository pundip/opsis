"""Connector discovery. Adding a connector never touches core code.

Three sources, later ones may not replace earlier ids:
1. Built-in packages in opsis/connectors/<id>/ that export CONNECTOR.
2. Drop-in folders in <plugins_dir>/connectors/<id>/__init__.py that export CONNECTOR.
3. Installed packages with an "opsis.connectors" entry point.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import pkgutil
import sys
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import opsis.connectors as builtin_package

log = logging.getLogger("opsis.connectors")

REQUIRED = ("id", "name", "version", "config_schema", "test_connection", "discover", "read")


class ConnectorRegistry:
    def __init__(self, plugins_dir: Path | None = None) -> None:
        self._classes: dict[str, type[Any]] = {}
        self._load_builtin()
        if plugins_dir is not None:
            self._load_plugins(plugins_dir / "connectors")
        self._load_entry_points()

    def _register(self, cls: Any, source: str) -> None:
        missing = [attr for attr in REQUIRED if not hasattr(cls, attr)]
        if missing:
            log.warning("Skipping connector from %s: missing %s", source, ", ".join(missing))
            return
        if cls.id in self._classes:
            log.warning("Skipping connector %r from %s: id already registered", cls.id, source)
            return
        self._classes[cls.id] = cls

    def _load_builtin(self) -> None:
        for module in pkgutil.iter_modules(builtin_package.__path__):
            if not module.ispkg:
                continue
            loaded = importlib.import_module(f"opsis.connectors.{module.name}")
            if hasattr(loaded, "CONNECTOR"):
                self._register(loaded.CONNECTOR, f"opsis.connectors.{module.name}")

    def _load_plugins(self, folder: Path) -> None:
        if not folder.is_dir():
            return
        for path in sorted(folder.iterdir()):
            init = path / "__init__.py"
            if not init.is_file():
                continue
            name = f"opsis_plugins.connectors.{path.name}"
            try:
                spec = importlib.util.spec_from_file_location(
                    name, init, submodule_search_locations=[str(path)]
                )
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
                self._register(module.CONNECTOR, str(path))
            except Exception:  # a broken plugin must not stop the app
                log.exception("Could not load connector plugin %s", path)

    def _load_entry_points(self) -> None:
        for entry in entry_points(group="opsis.connectors"):
            try:
                self._register(entry.load(), f"entry point {entry.name}")
            except Exception:
                log.exception("Could not load connector entry point %s", entry.name)

    def all(self) -> list[type[Any]]:
        return sorted(self._classes.values(), key=lambda cls: str(cls.name))

    def get(self, connector_id: str) -> type[Any] | None:
        return self._classes.get(connector_id)


_registries: dict[Path | None, ConnectorRegistry] = {}


def get_registry(plugins_dir: Path | None = None) -> ConnectorRegistry:
    if plugins_dir not in _registries:
        _registries[plugins_dir] = ConnectorRegistry(plugins_dir)
    return _registries[plugins_dir]
