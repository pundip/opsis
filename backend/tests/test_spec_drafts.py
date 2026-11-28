"""The v2 draft schemas in spec/drafts/v2 (milestone V0 of BUILD_BRIEF_V2.md).

Nothing in the app reads the drafts. These checks keep them honest until each one
moves into spec/ with its milestone (the workspace model and the semantic query moved
in V2): every draft is a valid JSON Schema whose references resolve, every example
validates, documents made by today's code (the demo data) survive the migrations
planned in spec/drafts/v2/README.md, and the drafts refuse what the designs say they
refuse.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from opsis.dashboards import spec as specs
from opsis.dashboards.manifests import load_manifests
from opsis.semantic import model as semantic
from opsis.services import demo

SPEC_DIR = Path("/app/spec")
DRAFTS = SPEC_DIR / "drafts" / "v2"
EXAMPLES = DRAFTS / "examples"
VISUALS_DIR = Path("/app/visuals")
DRAFT_NAMES = {
    "dashboard.schema.json",
    "transform.schema.json",
    "visual-manifest.schema.json",
    "visual-package.schema.json",
}
# Promoted into spec/ in V2; their examples stay with the drafts' examples.
PROMOTED = {"semantic-query.schema.json", "workspace-model.schema.json"}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _schemas() -> dict[str, dict[str, Any]]:
    """The v1 schemas, with each draft in place of the file it replaces."""
    schemas = {path.name: _load(path) for path in SPEC_DIR.glob("*.schema.json")}
    schemas.update({path.name: _load(path) for path in DRAFTS.glob("*.schema.json")})
    return schemas


SCHEMAS = _schemas()
REGISTRY: Registry[Any] = Registry().with_resources(
    (schema["$id"], Resource.from_contents(schema)) for schema in SCHEMAS.values()
)


def problems(name: str, document: Any) -> list[str]:
    validator = Draft202012Validator(SCHEMAS[name], registry=REGISTRY)
    return sorted(
        "/" + "/".join(str(part) for part in error.absolute_path)
        for error in validator.iter_errors(document)
    )


def _references(node: Any) -> list[str]:
    if isinstance(node, dict):
        found = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        return found + [ref for value in node.values() for ref in _references(value)]
    if isinstance(node, list):
        return [ref for value in node for ref in _references(value)]
    return []


def test_every_draft_is_a_valid_schema_whose_references_resolve() -> None:
    assert {path.name for path in DRAFTS.glob("*.schema.json")} == DRAFT_NAMES
    for name in DRAFT_NAMES:
        schema = _load(DRAFTS / name)
        Draft202012Validator.check_schema(schema)
        assert schema["$id"] == f"https://opsis.invalid/spec/{name}"
        assert schema["description"].startswith("DRAFT for v2")
        resolver = REGISTRY.resolver(base_uri=schema["$id"])
        for reference in _references(schema):
            resolver.lookup(reference)  # raises when it points nowhere


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.json")), ids=lambda p: p.name)
def test_every_example_validates(path: Path) -> None:
    schema = f"{path.name.split('.')[0]}.schema.json"
    assert schema in DRAFT_NAMES | PROMOTED
    assert problems(schema, _load(path)) == []


def test_examples_cover_every_draft() -> None:
    covered = {f"{path.name.split('.')[0]}.schema.json" for path in EXAMPLES.glob("*.json")}
    assert covered == (DRAFT_NAMES | PROMOTED) - {"visual-manifest.schema.json"}  # via package


def test_queries_the_v1_demo_dashboard_makes_are_valid_v2_queries() -> None:
    models = {name: semantic.parse_model(name, d) for name, d in demo.MODELS.items()}
    manifests = load_manifests(VISUALS_DIR, SPEC_DIR)
    spec = demo.dashboard_spec()
    states: list[dict[str, Any]] = [
        {},
        {
            "filters": {"fund": ["Library", "Sport"], "period": {"from": "2025-07-01"}},
            "cross": [
                {"field": "demo_regions.state", "values": ["Victoria"], "source": "by_state"}
            ],
            "grains": {"by_month": "fiscal_quarter"},
        },
    ]
    derived = 0
    for state in states:
        for item in spec["items"]:
            query = specs.item_query(spec, item, manifests[item["visual"]], models, state)
            if query is not None:
                derived += 1
                assert problems("semantic-query.schema.json", query) == [], item["id"]
    assert derived == 2 * len(spec["items"])


def test_the_demo_dashboard_migrates_by_changing_only_its_version() -> None:
    v1 = demo.dashboard_spec()
    assert problems("dashboard.schema.json", v1) == ["/specVersion"]
    assert _load(EXAMPLES / "dashboard.demo.json") == {**v1, "specVersion": 2}


def _planned_model_migration(models: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The join-to-relationship rules of ADR-031, written out. V2 replaces this with
    the real migration, which must give the same answers for these cases."""
    tables = {
        name: {"source": {"dataset": name}, **{k: v for k, v in d.items() if k != "joins"}}
        for name, d in models.items()
    }
    relationships: dict[str, dict[str, Any]] = {}
    for name, definition in models.items():
        for join in definition.get("joins") or []:
            local, remote = list(join["on"]), list(join["on"].values())
            if join.get("relationship", "many_to_one") == "one_to_many":
                many, one, active = (join["to"], remote), (name, local), False
                cardinality = "many_to_one"
            else:
                many, one, active = (name, local), (join["to"], remote), True
                cardinality = join.get("relationship", "many_to_one")
            entry = {
                "from": {"table": many[0], "columns": many[1]},
                "to": {"table": one[0], "columns": one[1]},
                "cardinality": cardinality,
                "crossFilter": "single",
                "securityFilter": "single",
                "active": active,
                "joinType": join.get("type", "left"),
            }
            same = next(
                (
                    key
                    for key, other in relationships.items()
                    if (other["from"], other["to"]) == (entry["from"], entry["to"])
                ),
                None,
            )
            if same is not None:  # declared on both sides: keep one, the active one
                relationships[same]["active"] = relationships[same]["active"] or active
                continue
            base = rid = f"{many[0]}__{one[0]}"
            number = 2
            while rid in relationships:
                rid, number = f"{base}_{number}", number + 1
            relationships[rid] = entry
    return {"tables": tables, "relationships": relationships}


def test_the_demo_models_migrate_to_the_example() -> None:
    example = _load(EXAMPLES / "workspace-model.demo.json")
    migrated = _planned_model_migration(demo.MODELS)
    assert example["tables"] == migrated["tables"]
    assert example["relationships"] == migrated["relationships"]
    assert set(example["layout"]["tables"]) == set(demo.MODELS)


@pytest.mark.parametrize(
    ("models", "expected"),
    [
        (  # only the one_to_many side declared: turned round, inactive (v1 never used it)
            {
                "a": {"joins": [{"to": "b", "on": {"x": "y"}, "relationship": "one_to_many"}]},
                "b": {},
            },
            {"b__a": ("b", ["y"], "a", ["x"], "many_to_one", False, "left")},
        ),
        (  # declared on both sides: one relationship, active
            {
                "a": {"joins": [{"to": "b", "on": {"x": "y"}, "relationship": "one_to_many"}]},
                "b": {"joins": [{"to": "a", "on": {"y": "x"}}]},
            },
            {"b__a": ("b", ["y"], "a", ["x"], "many_to_one", True, "left")},
        ),
        (  # an inner one-to-one join keeps its type
            {
                "a": {
                    "joins": [
                        {"to": "b", "on": {"x": "y"}, "type": "inner", "relationship": "one_to_one"}
                    ]
                },
                "b": {},
            },
            {"a__b": ("a", ["x"], "b", ["y"], "one_to_one", True, "inner")},
        ),
    ],
)
def test_planned_join_rules(models: dict[str, Any], expected: dict[str, tuple[Any, ...]]) -> None:
    migrated = _planned_model_migration(models)
    found = {
        rid: (
            r["from"]["table"],
            r["from"]["columns"],
            r["to"]["table"],
            r["to"]["columns"],
            r["cardinality"],
            r["active"],
            r["joinType"],
        )
        for rid, r in migrated["relationships"].items()
    }
    assert found == expected
    document = {"modelVersion": 2, **migrated}
    assert problems("workspace-model.schema.json", document) == []


def test_built_in_manifests_fit_the_draft_after_their_planned_change() -> None:
    paths = sorted(VISUALS_DIR.glob("*/manifest.json"))
    assert len(paths) >= 9
    for path in paths:
        manifest = _load(path)
        flags = manifest.get("capabilities") or {}
        changed = {
            **manifest,
            "author": "Opsis",
            "capabilities": [
                name
                for name, flag in (("crossFilter", "crossFilter"), ("drill", "drillDown"))
                if flags.get(flag)
            ],
        }
        assert problems("visual-manifest.schema.json", changed) == [], path.parent.name
        assert problems("visual-package.schema.json", changed) != []  # not a package


SLICER = {
    "id": "s",
    "visual": "slicer",
    "position": {"x": 0, "y": 0, "w": 3, "h": 4},
    "bindings": {"field": ["demo_regions.state"]},
    "slicer": {"type": "list"},
}


def _dashboard(*items: dict[str, Any]) -> dict[str, Any]:
    layout = {"columns": 12, "rowHeight": 40}
    return {"specVersion": 2, "title": "t", "layout": layout, "items": list(items)}


def _relationship(**extra: Any) -> dict[str, Any]:
    ends = {"from": {"table": "a", "columns": ["x"]}, "to": {"table": "b", "columns": ["y"]}}
    return {"modelVersion": 2, "relationships": {"a__b": {**ends, **extra}}}


@pytest.mark.parametrize(
    ("schema", "document"),
    [
        ("workspace-model.schema.json", _relationship(securityFilter="both")),
        ("workspace-model.schema.json", _relationship(cardinality="many_to_many")),
        ("workspace-model.schema.json", {"modelVersion": 2, "tables": {"a": {"label": "A"}}}),
        ("workspace-model.schema.json", {"modelVersion": 1}),
        ("dashboard.schema.json", _dashboard({k: v for k, v in SLICER.items() if k != "slicer"})),
        ("dashboard.schema.json", _dashboard({**SLICER, "config": {}})),
        (
            "dashboard.schema.json",
            _dashboard({**SLICER, "slicer": {"type": "list", "default": "VIC"}}),
        ),
        ("dashboard.schema.json", _dashboard({**SLICER, "visual": "bar"})),
        (
            "dashboard.schema.json",
            {**_dashboard(), "interactions": {"matrix": {"s": {"t": "dim"}}}},
        ),
        ("transform.schema.json", {"name": "t", "mode": "steps", "query": "SELECT 1"}),
        ("transform.schema.json", {"name": "t", "mode": "query"}),
        (
            "visual-package.schema.json",
            {**_load(EXAMPLES / "visual-package.candlestick.json"), "entry": "../x.js"},
        ),
        (
            "visual-package.schema.json",
            {
                k: v
                for k, v in _load(EXAMPLES / "visual-package.candlestick.json").items()
                if k != "sdkVersion"
            },
        ),
        ("semantic-query.schema.json", {"measures": ["a.b"], "filters": [{"or": []}]}),
    ],
)
def test_drafts_refuse(schema: str, document: Any) -> None:
    assert problems(schema, document) != []


def test_a_draft_relationship_with_both_directions_is_accepted() -> None:
    both = _relationship(crossFilter="both", securityFilter="both")
    assert problems("workspace-model.schema.json", both) == []
    assert problems("dashboard.schema.json", _dashboard(SLICER)) == []
