"""The workspace model as a models/ folder: export, and import with a check of every
file first (M8; V2_DESIGN 1.4: one file per table and models/workspace-model.yml,
and v1 files still import)."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from tests.test_ingestion import ok
from tests.test_query_api import MODEL_YAML, load
from tests.test_query_api import source as source  # noqa: F401  (fixture)

ClientFor = Callable[..., TestClient]
YAML = "application/yaml"


def post(client: TestClient, files: list[tuple[str, bytes]], dry_run: bool = False) -> Any:
    response = client.post(
        f"/api/model-files?dryRun={'true' if dry_run else 'false'}",
        files=[("files", (name, content, YAML)) for name, content in files],
    )
    return ok(response)


def test_models_folder_export_and_import(client_for: ClientFor, source: dict[str, Any]) -> None:  # noqa: F811
    client = client_for()
    load(client, source)
    ok(client.put("/api/models/gifts", json={"yaml": MODEL_YAML}))

    exported = client.get("/api/model-files")
    assert exported.status_code == 200 and exported.headers["content-type"] == "application/zip"
    assert 'filename="opsis-models-' in exported.headers["content-disposition"]
    archive = zipfile.ZipFile(io.BytesIO(exported.content))
    assert sorted(archive.namelist()) == [
        "models/donors.yml",
        "models/gifts.yml",
        "models/workspace-model.yml",
    ]
    gifts = archive.read("models/gifts.yml").decode()
    assert gifts.startswith("table: gifts\n") and "label: Gifts\n" in gifts
    assert "joins" not in gifts  # they are relationships, in the workspace file
    workspace = archive.read("models/workspace-model.yml").decode()
    assert "gifts__donors:" in workspace and "layout:" in workspace

    # Straight back in: nothing changes, and no new versions are written.
    before = len(ok(client.get("/api/model/versions")))
    same = post(client, [("opsis-models.zip", exported.content)])
    assert same["applied"] is True and {f["status"] for f in same["files"]} == {"unchanged"}
    assert len(ok(client.get("/api/model/versions"))) == before

    # One good change and one broken file: nothing is saved.
    changed = gifts.replace("label: Gifts\n", "label: Gifts received\n")
    broken = "dataset: donors\nmeasures:\n  bad: { agg: sum, column: nope }\n"
    report = post(client, [("gifts.yml", changed.encode()), ("donors.yml", broken.encode())])
    assert (report["applied"], report["valid"]) == (False, False)
    by_dataset = {f["dataset"]: f for f in report["files"]}
    assert by_dataset["gifts"]["status"] == "changed"
    assert by_dataset["donors"]["status"] == "invalid"
    assert by_dataset["donors"]["problems"][0]["path"].startswith("/measures/bad")
    assert ok(client.get("/api/models/gifts"))["definition"]["label"] == "Gifts"

    # A dry run reports; the import itself saves a version noted with the file name.
    dry = post(client, [("models/gifts.yml", changed.encode())], dry_run=True)
    assert (dry["applied"], dry["valid"], dry["files"][0]["status"]) == (False, True, "changed")
    done = post(client, [("models/gifts.yml", changed.encode())])
    assert done["applied"] is True and done["files"][0]["status"] == "saved"
    assert ok(client.get("/api/models/gifts"))["definition"]["label"] == "Gifts received"
    versions = ok(client.get("/api/model/versions"))
    assert len(versions) == before + 1
    assert versions[0]["message"] == "Imported from models/gifts.yml"
    assert done["files"][0]["version"] == versions[0]["version"]

    # A file without `table:` is named by its file name.
    unnamed = changed.replace("table: gifts\n", "")
    named = post(client, [("models/gifts.yml", unnamed.encode())])
    assert (named["files"][0]["dataset"], named["files"][0]["status"]) == ("gifts", "unchanged")

    # A v1 file still imports: its joins become the table's relationships.
    v1 = MODEL_YAML.replace("label: Gifts\n", "label: Gifts (from v1)\n")
    from_v1 = post(client, [("models/gifts.yml", v1.encode())])
    assert (from_v1["files"][0]["kind"], from_v1["files"][0]["status"]) == ("v1", "saved")
    stored = ok(client.get("/api/model"))["document"]
    assert stored["tables"]["gifts"]["label"] == "Gifts (from v1)"
    assert stored["relationships"]["gifts__donors"]["to"] == {"table": "donors", "columns": ["id"]}

    # The workspace file replaces the relationships, and is checked like the rest.
    nowhere = (
        "modelVersion: 2\nrelationships:\n  gifts__nowhere:\n"
        "    from: {table: gifts, columns: [donor_id]}\n    to: {table: nowhere, columns: [id]}\n"
    )
    wrong = post(client, [("models/workspace-model.yml", nowhere.encode())])
    assert (wrong["applied"], wrong["files"][0]["status"]) == (False, "invalid")
    assert wrong["files"][0]["problems"][0]["path"].startswith("/relationships/gifts__nowhere")
    tables_too = post(client, [("models/workspace-model.yml", b"tables: {}\n")])
    assert tables_too["files"][0]["problems"][0]["path"] == "/tables"
    unlinked = post(
        client, [("models/workspace-model.yml", b"modelVersion: 2\nrelationships: {}\n")]
    )
    assert unlinked["files"][0]["status"] == "saved"
    assert ok(client.get("/api/model"))["document"]["relationships"] == {}

    # What is refused, file by file or as a whole.
    unknown = post(client, [("models/nope.yml", b"dimensions: {}\n")])
    assert unknown["files"][0]["status"] == "unknown_dataset" and unknown["applied"] is False
    twice = post(client, [("a.yml", changed.encode()), ("b.yml", changed.encode())])
    assert twice["files"][1]["problems"][0]["message"] == "a.yml is also for this table."
    bad_yaml = post(client, [("gifts.yml", b"dimensions: [\n")])
    assert "The YAML is not valid" in bad_yaml["files"][0]["problems"][0]["message"]
    not_text = post(client, [("gifts.yml", b"\xff\xfe\x00")])
    assert not_text["files"][0]["problems"][0]["message"] == "The file is not UTF-8 text."
    for name, content in (("notes.txt", b"hi"), ("models.zip", b"not a zip")):
        refused = client.post("/api/model-files", files=[("files", (name, content, YAML))])
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "invalid_file"
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as nothing:
        nothing.writestr("README.md", "no models here")
        nothing.writestr("__MACOSX/models/gifts.yml", "junk")
    refused = client.post("/api/model-files", files=[("files", ("m.zip", empty.getvalue(), YAML))])
    assert refused.status_code == 422 and "no .yml files" in refused.json()["error"]["message"]
