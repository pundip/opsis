"""The user guide (docs/guide) as pages the app shows at {base}/docs/guide.

The Markdown files are the only source. A page is rendered when it is asked for,
because only the running app knows the base path every link has to carry
(brief 3.1). Links are rewritten on the way:

- to another chapter or a heading: that page's address in the app;
- to a screenshot: its address under /api/guide/images;
- to a file that is not part of the guide (a deployment guide, a schema): plain
  text naming the file in the source, because the app does not serve it;
- to another site: unchanged, and opened in a new tab.

HTML written in the Markdown is shown as text, never passed through.
"""

from __future__ import annotations

import posixpath
import re
import struct
from collections.abc import Iterable
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

from opsis.errors import ApiError
from opsis.settings import Settings

CONTENTS = "README"  # the contents page, at {base}/docs/guide
SOURCE_DIR = "docs/guide"  # where the files live in the source, for links that leave
# Plain file names only, short enough for any filesystem: nothing outside the guide.
PAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
IMAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\.(?:png|jpe?g|gif|webp)")
# Prefixed, so that no heading can take an id the app itself uses.
ANCHOR_PREFIX = "guide-"
NOT_IN_APP = "A file in the Opsis source code, which the app does not show."

_MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("table")


def render(settings: Settings, page: str) -> dict[str, str]:
    """One page: its title and its HTML. 404 when the guide has no such page."""
    source = _page_file(settings, page)
    if source is None:
        raise ApiError(404, "not_found", "There is no such page in the user guide.")
    tokens = _MARKDOWN.parse(source.read_text(encoding="utf-8"))
    title = ""
    anchors: dict[str, int] = {}
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            heading = _words(tokens[index + 1].children or [])
            title = title or heading
            token.attrSet("id", ANCHOR_PREFIX + _anchor(heading, anchors))
        elif token.type == "inline" and token.children:
            token.children = _rewrite(settings, page, token.children)
    html = _MARKDOWN.renderer.render(tokens, _MARKDOWN.options, {})
    return {"page": page, "title": title or page, "html": html}


def image_file(settings: Settings, name: str) -> Path:
    """A screenshot of the guide. 404 for anything else."""
    path = settings.guide_dir / "images" / name
    if not IMAGE_NAME.fullmatch(name) or not path.is_file():
        raise ApiError(404, "not_found", "There is no such image in the user guide.")
    return path


def _page_file(settings: Settings, page: str) -> Path | None:
    path = settings.guide_dir / f"{page}.md"
    return path if PAGE_NAME.fullmatch(page) and path.is_file() else None


def _words(tokens: Iterable[Token]) -> str:
    """The text of an inline run as it reads, without the markup."""
    return "".join(
        " " if t.type == "softbreak" else t.content
        for t in tokens
        if t.type in ("text", "code_inline", "softbreak")
    )


def _anchor(heading: str, used: dict[str, int]) -> str:
    """The anchor GitHub gives a heading, so one link works there and in the app."""
    name = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
    seen = used.get(name, 0)
    used[name] = seen + 1
    return f"{name}-{seen}" if seen else name


Target = tuple[Literal["app", "site", "source"], str]


def _target(settings: Settings, page: str, href: str) -> Target:
    """What a link written in `page` leads to: an address in the app, another
    site, or a file of the source (its path from the repository root)."""
    parts = urlsplit(href)
    if parts.scheme or parts.netloc:
        return "site", href
    if parts.path:
        path = posixpath.normpath(posixpath.join(SOURCE_DIR, unquote(parts.path)))
        folder, name = posixpath.split(path)
        page = name.removesuffix(".md")
        if folder != SOURCE_DIR or page == name or _page_file(settings, page) is None:
            return "source", path
    # Written out in full: a bare "#heading" would resolve against <base href>.
    address = f"{settings.base_path}/docs/guide"
    if page != CONTENTS:
        address += f"/{page}"
    return "app", f"{address}#{parts.fragment}" if parts.fragment else address


def _rewrite(settings: Settings, page: str, tokens: list[Token]) -> list[Token]:
    """The inline tokens of one block, with links and images made to work in the app."""
    result: list[Token] = []
    # One entry per open link: for a link to a source file, its path and where its text starts.
    links: list[tuple[str, int] | None] = []
    for token in tokens:
        if token.type == "image":
            token = _image(settings, token)
        elif token.type == "link_open":
            source = _link(settings, page, token)
            links.append((source, len(result) + 1) if source else None)
        result.append(token)
        if token.type == "link_close" and (opened := links.pop()):
            path, start = opened
            token.tag = "span"
            if _words(result[start:]) != path:  # name the file, unless the text already does
                result += [_text(" ("), Token("code_inline", "code", 0, content=path), _text(")")]
    return result


def _link(settings: Settings, page: str, token: Token) -> str | None:
    """Point an opening link where it leads in the app. A link to a source file
    becomes a plain span instead, and the file's path is returned."""
    kind, value = _target(settings, page, str(token.attrGet("href") or ""))
    if kind == "source":
        token.tag = "span"
        token.attrs = {"class": "guide-source", "title": NOT_IN_APP}
        return value
    token.attrSet("href", value)
    if kind == "site":
        token.attrSet("target", "_blank")
        token.attrSet("rel", "noopener noreferrer")
    return None


def _text(content: str) -> Token:
    return Token("text", "", 0, content=content)


def _image(settings: Settings, token: Token) -> Token:
    """A screenshot with its address in the app and its size; anything else the
    app cannot show (the CSP allows no other site) becomes its description."""
    src = str(token.attrGet("src") or "")
    parts = urlsplit(src)
    path = posixpath.normpath(posixpath.join(SOURCE_DIR, unquote(parts.path)))
    folder, name = posixpath.split(path)
    file = settings.guide_dir / "images" / name
    local = not parts.scheme and not parts.netloc and folder == f"{SOURCE_DIR}/images"
    if not local or not IMAGE_NAME.fullmatch(name) or not file.is_file():
        return _text(_words(token.children or []))
    token.attrSet("src", f"{settings.base_path}/api/guide/images/{name}")
    size = _png_size(file)
    if size:  # room is kept for it, so a link to a heading below stays on that heading
        token.attrSet("width", str(size[0]))
        token.attrSet("height", str(size[1]))
    token.attrSet("loading", "lazy")
    return token


def _png_size(path: Path) -> tuple[int, int] | None:
    with path.open("rb") as file:
        head = file.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", head[16:24])
    return width, height
