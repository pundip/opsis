"""The user guide inside the app: docs/guide rendered at {base}/docs/guide, with
every link and screenshot working under any base path, links that leave the guide
shown as the name of the file, and only signed-in people reading it."""

from __future__ import annotations

from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from opsis.services import user_guide
from tests.conftest import SERVER, make_settings
from tests.test_auth import INITIAL, SIGN_IN, admin_client, person

ClientFor = Callable[..., TestClient]

BASES = (("http://localhost:8080", ""), ("http://localhost:8088/opsis", "/opsis"))


class Page(HTMLParser):
    """The links, images, ids and text of a rendered page."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self.images: list[dict[str, str]] = []
        self.ids: list[str] = []
        self.tags: set[str] = set()
        self.text = ""
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        self.tags.add(tag)
        if "id" in values:
            self.ids.append(values["id"])
        if tag == "a":
            self.links.append(values)
        if tag == "img":
            self.images.append(values)

    def handle_data(self, data: str) -> None:
        self.text += data


def guide_of(tmp_path: Path, **files: str) -> Path:
    """A small guide of its own, one file per keyword."""
    folder = tmp_path / "guide"
    (folder / "images").mkdir(parents=True)
    for name, text in files.items():
        (folder / f"{name}.md").write_text(text, encoding="utf-8")
    return folder


def render(folder: Path, page: str = "README", url: str = "http://localhost:8080") -> Page:
    settings = make_settings(guide_dir=folder, public_base_url=url)
    return Page(user_guide.render(settings, page)["html"])


@pytest.mark.parametrize(("url", "base"), BASES)
def test_every_page_of_the_guide_works(client_for: ClientFor, url: str, base: str) -> None:
    """Every link goes to a page and a heading that exist, and every screenshot loads."""
    client = client_for(public_base_url=url)
    # The real guide: copied into the image, and mounted into the test container.
    guide_dir = make_settings().guide_dir
    sources = {path.stem: path.read_text(encoding="utf-8") for path in guide_dir.glob("*.md")}
    names = sorted(sources)
    assert "README" in names and len(names) >= 10, f"the guide is not in {guide_dir}"

    pages: dict[str, Page] = {}
    for name in names:
        response = client.get(f"/api/guide/{name}")
        assert response.status_code == 200, name
        body = response.json()
        assert body["page"] == name and body["title"]
        pages[name] = Page(body["html"])
    contents = client.get("/api/guide").json()
    assert contents["page"] == "README" and contents["title"] == "Opsis user guide"

    prefix = f"{base}/docs/guide"
    shown: list[str] = []
    reached: set[str] = set()
    for name, page in pages.items():
        assert len(page.ids) == len(set(page.ids)), name
        # A link to a chapter that is not there would be shown as a file's name.
        assert "(docs/guide/" not in page.text, name
        for link in page.links:
            href = link["href"]
            if href.startswith(("https://", "http://", "mailto:")):
                assert link["target"] == "_blank" and "noopener" in link["rel"], (name, href)
                continue
            assert href == prefix or href.startswith((f"{prefix}/", f"{prefix}#")), (name, href)
            path, _, anchor = href.removeprefix(prefix).partition("#")
            target = path.removeprefix("/") or "README"
            assert target in pages, (name, href)
            if anchor:
                assert f"guide-{anchor}" in pages[target].ids, (name, href)
            reached.add(target)
        for image in page.images:
            src = image["src"]
            assert src.startswith(f"{base}/api/guide/images/"), (name, src)
            assert image["alt"] and int(image["width"]) > 0 and int(image["height"]) > 0
            shown.append(src.removeprefix(base))
    assert reached == set(names), "a page that nothing links to"
    # Every screenshot the Markdown shows is on a page.
    assert shown and len(shown) == sum(text.count("![") for text in sources.values())
    for src in shown:
        response = client.get(src)
        assert response.status_code == 200 and response.headers["content-type"] == "image/png", src
        assert response.headers["cache-control"].startswith("private")


def test_tables_code_and_headings_are_rendered(client_for: ClientFor) -> None:
    client = client_for()
    page = Page(client.get("/api/guide/05-joins").json()["html"])
    assert {"h1", "h2", "h3", "table", "th", "td", "pre", "code", "blockquote", "img"} <= page.tags
    # The anchors GitHub gives these headings, which the guide's links use.
    for anchor in ("two-joins-away", "walk-through-gifts-donors-and-a-spreadsheet"):
        assert f"guide-{anchor}" in page.ids
    assert 'class="language-yaml"' in client.get("/api/guide/05-joins").json()["html"]
    operations = Page(client.get("/api/guide/09-operations").json()["html"])
    assert "guide-backups-and-restore" in operations.ids


def test_links_that_leave_the_guide_name_the_file(client_for: ClientFor) -> None:
    html = client_for().get("/api/guide/01-getting-started").json()["html"]
    page = Page(html)
    assert "Windows guide (deploy/windows/README.md) covers" in page.text
    assert "full list of settings (README.md)" in page.text
    assert not [link for link in page.links if ".." in link["href"] or ".md" in link["href"]]
    assert '<span class="guide-source" title=' in html
    # A link whose text is already the file's path is not named twice.
    dashboards = Page(client_for().get("/api/guide/06-dashboards").json()["html"])
    assert dashboards.text.count("spec/dashboard.schema.json") == 1


@pytest.mark.parametrize(("url", "base"), BASES)
def test_how_links_and_images_are_rewritten(tmp_path: Path, url: str, base: str) -> None:
    folder = guide_of(
        tmp_path,
        README="\n".join(
            [
                "# Title",
                "[here](#title) [there](02-next.md) [part](02-next.md#a-part)",
                "[dotted](./02-next.md) [round](../guide/02-next.md) [home](README.md#title)",
                "[web](https://example.org/x) <mailto:a@example.org>",
                "[out](../../deploy/proxmox/README.md#12) [up](../ARCHITECTURE.md)",
                "[gone](03-missing.md) [rooted](/etc/passwd) [far](../../../../x)",
                "[schema](../../spec/dashboard.schema.json) [**bold** `file`](../x.md)",
                "![Shot](images/a.png) ![Away](https://example.org/b.png) ![No](images/no.png)",
            ]
        ),
        **{"02-next": "# Next\n\n## A part\n"},
    )
    png = b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + (640).to_bytes(4, "big") + (480).to_bytes(4, "big")
    (folder / "images" / "a.png").write_bytes(png)

    page = render(folder, url=url)
    guide = f"{base}/docs/guide"
    assert [link["href"] for link in page.links] == [
        f"{guide}#title",
        f"{guide}/02-next",
        f"{guide}/02-next#a-part",
        f"{guide}/02-next",
        f"{guide}/02-next",
        f"{guide}#title",
        "https://example.org/x",
        "mailto:a@example.org",
    ]
    for words in (
        "out (deploy/proxmox/README.md)",
        "up (docs/ARCHITECTURE.md)",
        "gone (docs/guide/03-missing.md)",
        "rooted (/etc/passwd)",
        "far (../../x)",
        "schema (spec/dashboard.schema.json)",
        "bold file (docs/x.md)",
    ):
        assert words in page.text, words
    assert page.images == [
        {
            "src": f"{base}/api/guide/images/a.png",
            "alt": "Shot",
            "width": "640",
            "height": "480",
            "loading": "lazy",
        }
    ]
    assert "Away" in page.text and "No" in page.text


def test_headings_get_distinct_anchors_with_a_prefix(tmp_path: Path) -> None:
    folder = guide_of(
        tmp_path,
        README="# Root\n\n## Root\n\n## 12. A `FEATURE_X` flag: yes, or no?\n\n## **Bold** & `x`\n",
    )
    assert render(folder).ids == [
        "guide-root",
        "guide-root-1",
        "guide-12-a-feature_x-flag-yes-or-no",
        "guide-bold--x",
    ]


def test_html_and_script_links_stay_text(tmp_path: Path) -> None:
    folder = guide_of(
        tmp_path,
        README='<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">\n\n'
        "[click](javascript:alert(1))\n",
    )
    page = render(folder)
    assert not page.tags & {"script", "img", "a"}
    assert "<script>alert(1)</script>" in page.text


def test_nothing_but_the_guide_can_be_read(client_for: ClientFor, tmp_path: Path) -> None:
    folder = guide_of(tmp_path, README="# Guide\n")
    (folder / "images" / "notes.txt").write_text("not an image")
    (tmp_path / "secret.md").write_text("# Secret\n")
    (tmp_path / "secret.png").write_bytes(b"secret")
    client = client_for(guide_dir=folder)
    assert client.get("/api/guide").status_code == 200
    for path in (
        "/api/guide/nope",
        "/api/guide/README.md",
        "/api/guide/..%2Fsecret",
        "/api/guide/%2E%2E%2Fsecret",
        "/api/guide/..%5Csecret",
        "/api/guide/images/..%2FREADME.md",
        "/api/guide/images/..%2F..%2Fsecret.png",
        "/api/guide/images/notes.txt",
        "/api/guide/images/missing.png",
        "/api/guide/images",
        "/api/guide/" + "a" * 500,
        "/api/guide/images/" + "a" * 500 + ".png",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "not_found", path


def test_without_the_files_the_guide_answers_404(client_for: ClientFor, tmp_path: Path) -> None:
    response = client_for(guide_dir=tmp_path / "not-there").get("/api/guide")
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "There is no such page in the user guide."


PATHS = ("/api/guide", "/api/guide/05-joins", "/api/guide/images/dashboard-demo.png")


def test_local_mode_is_simply_open(client_for: ClientFor) -> None:
    client = client_for(csrf=False)
    for path in PATHS:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "script-src 'self';" in response.headers["content-security-policy"], path


def test_with_sign_in_only_signed_in_people_read_it(client_for: ClientFor) -> None:
    # Server mode under /opsis first: it creates the first admin.
    settings: dict[str, Any] = {**SERVER, "admin_initial_password": INITIAL}
    stranger = client_for(**settings)
    for path in PATHS:
        response = stranger.get(path)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "not_authenticated", path
    wrong = {"Authorization": "Bearer opsis_pat_made-up"}
    assert stranger.get("/api/guide", headers=wrong).status_code == 401

    admin = admin_client(client_for)
    viewer, _ = person(client_for, admin, "vera@example.test", "viewer")
    for client in (admin, viewer):
        for path in PATHS:
            assert client.get(path).status_code == 200, path
    assert client_for(**SIGN_IN).get("/api/guide").status_code == 401
