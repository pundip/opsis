"""The words an agent reads before it starts: server instructions, the guide
resource and the build_dashboard prompt. Kept plain and step by step so that a
small model can follow them."""

from __future__ import annotations

INSTRUCTIONS = """\
Opsis is a business intelligence tool. You can explore datasets, run queries and build dashboards.

How to work:
1. list_datasets, then describe_dataset for the dataset you need. Copy field refs \
(written dataset.field) exactly from its result. Never invent a field name.
2. To answer a question about the data, use run_query.
3. To build a dashboard: list_visuals with the dataset name gives a ready-made exampleItem for \
each visual. Assemble items into a spec and call create_dashboard. Later changes go through \
get_dashboard then patch_dashboard with base_version.
4. When a call fails, read the "Problems" and "What to do" lines of the error and fix exactly \
those. A version_conflict means: get_dashboard again, then retry with the new version.
5. Dashboards you create are drafts. Publish only when the user asks. Always give the user the \
editUrl or viewUrl from the result.
"""

GUIDE = """\
# Building dashboards in Opsis

## Words
- **Dataset**: a table of data, named in lower case (for example `gifts`).
- **Dimension**: a field to group or filter by (`gifts.fund`). A time dimension can take a grain \
(day, week, month, quarter, year, fiscal_month, fiscal_quarter, fiscal_year).
- **Measure**: a ready-made number (`gifts.total_amount`). Do not write aggregations yourself; \
pick a measure. If the one you need is missing, add it with patch_semantic_model.
- **Field ref**: always `dataset.field`. Take refs from describe_dataset.

## A dashboard spec
```json
{
  "specVersion": 1,
  "title": "Gifts overview",
  "layout": {"columns": 12, "rowHeight": 40},
  "filters": [],
  "items": [],
  "interactions": {"crossFilter": true}
}
```

## An item
```json
{
  "id": "bar_fund",
  "visual": "bar",
  "title": "Amount by fund",
  "position": {"x": 0, "y": 3, "w": 6, "h": 7},
  "dataset": "gifts",
  "bindings": {"category": ["gifts.fund"], "value": ["gifts.total_amount"]},
  "sort": [{"field": "gifts.total_amount", "dir": "desc"}],
  "limit": 10
}
```
- `id`: unique in the dashboard; letters, digits, `_` and `-`, starting with a letter.
- `visual` and the role names in `bindings` come from list_visuals.
- `position`: grid cells. `x` 0 to 11, `x + w` at most 12, `y` counts rows downward. Items must \
not overlap. A good layout: KPI cards (w 3, h 3) across the top at y 0, two charts side by side \
(w 6, h 7) at y 3, a wide table (w 12, h 8) below.
- A time field with a grain: `{"field": "gifts.gift_date", "grain": "month"}`.
- `sort` may only name a field that is bound in the item.

## A filter
```json
{"id": "f_date", "type": "dateRange", "field": "gifts.gift_date", "label": "Gift date",
 "default": {"relative": "last_12_months"}}
```
`type` is `select`, `multiSelect`, `dateRange` or `search`.

## Steps
1. describe_dataset: note three to six fields that answer the user's question.
2. list_visuals with the dataset: copy an `exampleItem` for each visual you want; change `id`, \
`title`, `position` and the bindings.
3. create_dashboard with the whole spec. If it reports problems, fix each one at its path and \
call again.
4. Tell the user what you built and give them the `editUrl`.

## Changing a dashboard
get_dashboard gives the spec and its `version`. Call patch_dashboard with that version as \
`base_version`:
- add an item: `{"op": "add", "path": "/items/-", "value": { ...item... }}`
- change a title: `{"op": "replace", "path": "/items/0/title", "value": "New title"}`
- remove the item with index 2: `{"op": "remove", "path": "/items/2"}`
On `version_conflict`, call get_dashboard again and retry with the new version.
"""


def build_dashboard_prompt(dataset: str, goal: str) -> str:
    about = f' that answers: "{goal}"' if goal else ""
    return f"""\
Build a dashboard from the Opsis dataset `{dataset}`{about}.

Work through these steps in order. Use the Opsis tools; do not guess field names.

1. Describe. Call describe_dataset with {{"dataset": "{dataset}"}}. Read its dimensions, \
measures and sample values. If it has no data, call trigger_refresh and wait with get_job_status.
2. Propose. Decide on four to six visuals: one to three KPI cards for the headline numbers, a \
chart over time if there is a time dimension, a comparison across the most useful category, and \
a table for detail. Tell the user the plan in two or three lines.
3. Assemble. Call list_visuals with {{"dataset": "{dataset}"}} and copy the exampleItem of each \
visual you chose. Give every item a unique id, a clear title, and a position on the 12-column \
grid that does not overlap any other item. Add a dateRange filter if there is a time dimension.
4. Create. Call create_dashboard with the title and the complete spec.
5. Fix. If the result is an error, change exactly what each problem's path points to, and call \
create_dashboard again. Repeat until it succeeds.
6. Check. Call run_query for one of the visuals to confirm the numbers look sensible.
7. Summarise. Tell the user what each visual shows and give them the editUrl. Say that the \
dashboard is a draft and offer to publish it.
"""
