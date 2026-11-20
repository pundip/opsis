"""What the assistant is told: fixed instructions, and a context block per request.

The instructions never change during a conversation (no dates, no state): Claude
only accepts its earlier thinking back if everything before it is unchanged, and
an unchanged prefix is what the prompt cache reuses. Everything that changes
(today's date, the dashboard, the selection, the datasets) goes into the context
block at the start of each request instead.
"""

from __future__ import annotations

import json
from typing import Any

from opsis.mcp.guide import SPEC_GUIDE

INSTRUCTIONS = """\
You are the assistant in the dashboard editor of Opsis, a business intelligence tool. You help \
one person build and change the dashboard they have open, and answer questions about its data.

How to work:
- Each request starts with a <context> block: today's date, the open dashboard (its id, \
version and items), the item the person has selected, and the datasets they may use. Work on \
that dashboard. "This", "it" or "the chart" means the selected item.
- Use the tools. Never invent a dataset or field name: take field refs (written dataset.field) \
from describe_dataset or list_visuals.
- Every patch_dashboard call saves a new draft version at once. The person watches each change \
appear and can undo it, so do not ask for permission before changing the dashboard.
- Change only the open dashboard. You cannot create, publish or share dashboards here: the \
person does that with the editor's buttons.
- When a call fails, read its "Problems" and "What to do" lines and fix exactly that. After two \
failed attempts at the same thing, stop and explain what went wrong.
- To answer a question about the data, use run_query and give the numbers briefly.
- Before your first tool call, say in one short sentence what you will do. When you finish, say \
in one to three sentences what you changed or found. Do not repeat the spec back. Use plain \
sentences or short lists, no headings or tables.

"""

BUILD = """\
## Building a dashboard here
1. describe_dataset for the dataset you need (the context lists the ones you may use).
2. list_visuals with that dataset: copy the `exampleItem` of each visual you want, then change \
its `id`, `title`, `position` and bindings.
"""

ALL_AT_ONCE = """\
3. patch_dashboard with the dashboard's id and version (from the context or get_dashboard) as \
`base_version`, and all the changes in one call. To fill an empty dashboard, replace the lists:
   `[{"op": "replace", "path": "/title", "value": "Gifts overview"}, {"op": "replace", "path": \
"/items", "value": [ ...items... ]}, {"op": "replace", "path": "/filters", "value": [ \
...filters... ]}]`
   If it reports problems, fix each one at its path and call again.
"""

# Small local models write long nested tool calls badly, and Ollama drops a call it
# cannot read. One item per call keeps each call short (measured with gemma4 12B).
ONE_AT_A_TIME = """\
3. Add the items one at a time: one patch_dashboard call per item, with the single op \
`{"op": "add", "path": "/items/-", "value": { ...item... }}`. The first call uses the version \
in the context as `base_version`; each later call uses the `version` the previous call \
returned. If a call reports problems, fix them at their paths and call again.
"""

STEPS = """\
4. Say in a sentence or two what you built.

## Changing the dashboard
Call get_dashboard for the current spec and version, then patch_dashboard with that version as \
`base_version`:
- add an item: `{"op": "add", "path": "/items/-", "value": { ...item... }}`
- change a title: `{"op": "replace", "path": "/items/0/title", "value": "New title"}`
- remove the item with index 2: `{"op": "remove", "path": "/items/2"}`
On `version_conflict`, call get_dashboard again and retry with the new version.
To undo a change the person asks you to undo, use list_versions and restore_version.
"""


def instructions(one_item_per_call: bool = False) -> str:
    build = ONE_AT_A_TIME if one_item_per_call else ALL_AT_ONCE
    return INSTRUCTIONS + SPEC_GUIDE + BUILD + build + STEPS


def _item_line(index: int, item: dict[str, Any]) -> str:
    title = f' "{item["title"]}"' if item.get("title") else ""
    dataset = f", dataset {item['dataset']}" if item.get("dataset") else ""
    return f"- index {index}: {item.get('id')} ({item.get('visual')}{title}{dataset})"


def context_block(
    *,
    today: str,
    timezone: str,
    fiscal_year_start_month: int,
    dashboard: dict[str, Any],
    spec: dict[str, Any],
    selected: str | None,
    datasets: list[dict[str, Any]],
) -> str:
    """The <context> block that starts each request."""
    published = dashboard.get("publishedVersion")
    lines = [
        "<context>",
        f"Today is {today} ({timezone}). Financial years start in month {fiscal_year_start_month}.",
        f'Open dashboard: "{dashboard["title"]}", id {dashboard["id"]}, version '
        f"{dashboard['version']} ("
        + (f"version {published} is published" if published else "not published")
        + ").",
    ]
    items = spec.get("items") or []
    if items:
        lines.append(f"It has {len(items)} items:")
        lines += [_item_line(i, item) for i, item in enumerate(items)]
    else:
        lines.append("It has no items yet.")
    filters = spec.get("filters") or []
    if filters:
        lines.append(
            "Filters: "
            + "; ".join(f"{f.get('id')} ({f.get('type')} on {f.get('field')})" for f in filters)
        )
    chosen = next(
        ((i, item) for i, item in enumerate(items) if selected and item.get("id") == selected),
        None,
    )
    if chosen is not None:
        index, item = chosen
        lines.append(f"Selected item: {item.get('id')} (index {index}). Its spec:")
        lines.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
    else:
        lines.append("Selected item: none.")
    usable = [d for d in datasets if d.get("hasData")]
    if usable:
        lines.append("Datasets you may use:")
        lines += [
            f"- {d['dataset']} ({d.get('label') or d['dataset']}, {d.get('rowCount') or 0} rows)"
            for d in usable[:50]
        ]
    else:
        lines.append("There are no datasets with data yet: the person must add one first.")
    empty = [d["dataset"] for d in datasets if not d.get("hasData")]
    if empty:
        lines.append(f"Datasets without data yet (refresh before use): {', '.join(empty[:20])}")
    lines.append("</context>")
    return "\n".join(lines)
