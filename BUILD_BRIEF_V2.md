# Build brief v2: Opsis, the next set of features

> How to use: put this file in the repo root next to `BUILD_BRIEF.md`, then start with:
> "Read BUILD_BRIEF_V2.md and complete Milestone V0 only."

---

## 0. Context and working rules

Opsis v1 is built and documented, as described in `BUILD_BRIEF.md`, `PROGRESS.md` and `docs/DECISIONS.md`. This brief adds the next set of features. Everything in v1 stays in force unless this brief changes it: sub-path hosting, local and server modes, feature flags, RLS, restricted datasets, sharing rules, versioning, the audit log and the smoke tests.

Working rules (same as v1, plus three new ones):

1. **One milestone at a time.** Stop after each one and report what you built, how to run it, how you verified it, and any gaps. Wait for my go-ahead.
2. **Keep `PROGRESS.md` current**, so a fresh session can resume from it plus this brief.
3. **Log decisions** as ADRs in `docs/DECISIONS.md`.
4. **Verify everything:** backend tests, frontend tests, and `npm run smoke` at the root, under `/opsis` and in server mode. Never say something works unless you ran it.
5. **Git:** at the end of each milestone, commit with a clear message and push to `origin`. Never commit `.env` or secrets.
6. **Style:** never use em dashes in anything you write.
7. **New: don't touch the in-platform documentation.** Opsis now has documentation viewable inside the platform. Do not create, edit or delete any of it in this run; I'll ask for an update separately. Record any documentation that will need updating in a "Docs to update" list in `PROGRESS.md`. New developer-facing material (the visual SDK spec and authoring kit) goes in `sdk/`, not in the in-platform docs.
8. **New: security first for user-supplied code and SQL.** This brief introduces imported visual code, user-written SQL and exported files. Each one is a way to bypass RLS, restrictions or the server itself. The rules in each section are requirements, not suggestions.
9. **New: the agent helps everywhere.** Where this brief says "the agent can help", that means new MCP tools and assistant abilities, permission-checked and flag-gated like the existing ones, with compact results and an example input in every description.

---

## 1. Summary of what v2 adds

| Area | What it is |
|---|---|
| BYO agent | Users bring their own Anthropic or OpenAI keys; admins can grant the workspace key per user |
| Relational model | One workspace model with many tables, and a Model view where relationships are drawn as lines, like Power BI's Model view |
| Data prep | A Table view for exploring data and SQL-based transforms (in place of Power Query), plus SQL Lab, all with agent help |
| Slicers | Power BI-style slicers as canvas tiles, where one slicer controls many visuals and "Edit interactions" controls which |
| Visual SDK | A standard package format, so an AI can generate a new visual (such as a candlestick chart) and I can import it without changing core code |
| Mobile | A proper phone layout for viewing dashboards |
| Export | A self-contained, interactive offline copy of a dashboard, plus PDF and PNG |

---

## 2. BYO agent: per-user API keys

**Default rule: users bring their own key.** The workspace's `ANTHROPIC_API_KEY` from `.env` is no longer available to everyone.

**User settings, on a new "AI providers" section of the Account page:**
- Add, test, rename and delete API keys for **Anthropic** and **OpenAI**. A user can hold several keys.
- Keys are encrypted at rest with the existing encryption key. They are never sent back to the browser after saving; only the provider, a label and the last 4 characters are shown.
- A **Test** button makes a minimal API call and reports success or the provider's error.
- Each key can set a default model. The model list comes from the provider's API where possible, and is typed in manually otherwise.

**Admin controls, on the Users page:**
- A per-user and per-group toggle: **"May use the workspace key"**, off by default.
- A workspace setting to switch BYO keys off entirely, so only the workspace key or Ollama can be used.
- Admins can see that a user has keys and their providers, never the keys themselves.

**In the assistant window:**
- A provider and model picker listing only what this user may use: their own Anthropic and OpenAI keys, the workspace key if granted, and Ollama if enabled.
- The choice is remembered per user.
- If the user has nothing available, the panel explains how to add a key, with a link to the Account page.

**Providers:**
- Add an **OpenAI provider** with tool calling, next to the existing Anthropic and Ollama providers. All three share the same tool loop, budgets and undo behaviour.
- Run the same scripted fake-model browser test against each provider, as M7 did.

**Usage:**
- Log token usage per user, per key and per provider, in the audit log and on the user's Account page.
- Never log keys or full prompts.

**Security:**
- Keys are only decrypted inside the backend at call time and never reach the frontend.
- A user's key is only ever used for that user's requests.
- Deleting a user deletes their keys.

**MCP is unchanged:** external agents still connect with access tokens and use their own model.

---

## 3. Relational model and Model view

### 3.1 From per-dataset models to one workspace model

v1 has one semantic model per dataset, with joins inside it. v2 needs a single **workspace model** like a Power BI semantic model:
- It contains many **tables**, each backed by a dataset or a transform (section 4).
- **Relationships** connect the tables.
- **Measures** live on tables but can use fields from related tables.

In V0, propose how to get there from the current design, with a migration that converts existing models and keeps every existing dashboard working. Record it as an ADR. Multiple workspace models (for example one per subject area) are fine if that fits the current code better; say which you recommend.

**Relationship properties (Power BI semantics):**
- From column, to column.
- Cardinality: one-to-many, many-to-one, one-to-one. Many-to-many only if it's cheap and safe; otherwise backlog it.
- Cross-filter direction: single or both.
- Active or inactive. Inactive relationships can be used by a measure explicitly, like Power BI's USERELATIONSHIP.
- Validation: warn when the "one" side has duplicate keys, and show how many.

**The compiler** must follow relationships to resolve fields across tables, as the v1 two-hop joins do today but generalised. It must also detect ambiguous paths and ask the author to resolve them. RLS on a table filters through relationships in the filter direction, and that must be tested.

### 3.2 The Model view (GUI)

A canvas like Power BI's Model view:
- **Tables as cards**, showing name, fields with type icons, keys, and a row count.
- **Drag a field onto a field** in another table to create a relationship, then confirm cardinality and direction in a side panel with sensible defaults guessed from the data.
- Relationship lines show `1` and `*` at the ends and an arrow for filter direction. Inactive relationships are dashed.
- Click a line to edit or delete it.
- Layout: drag to move, zoom and pan, "auto layout", and positions saved per model.
- Edit table and field properties in a side panel: display names, descriptions, hidden fields, formats, data categories (for example "Date", "Currency", "Postcode").
- **Measures** can be created and edited through a form: name, aggregation, field, optional filter, format, description. An expression mode is available too, through the same allow-list as v1.
- **Date tables:** mark a table as the date table and generate one (with financial-year columns from the existing calendar module) at the click of a button.

**YAML stays available, both ways:**
- Every GUI change produces the same YAML.
- YAML can still be edited directly in a Monaco editor with validation, and the canvas updates to match.
- The v1 export/import of `models/*.yml` keeps working, extended to the workspace model.

**Model history:** every change is a new version (as in M3.5), with diff and restore.

**The agent can help:**
- "Suggest relationships": compares column names, types and value overlap, and proposes relationships with a confidence score and the evidence (for example "98% of gifts.donor_id values exist in donors.id"). Nothing is applied without my confirmation.
- Create measures from plain-English descriptions.
- Explain the model in plain English.
- MCP tools for reading and patching the workspace model, including relationships.

---

## 4. Data prep: Table view, SQL transforms and SQL Lab

### 4.1 Table view

A data grid for any dataset or transform, like Power BI's Table view:
- Scroll through rows (virtualised, server-paged), sort, filter per column, and search.
- **Column profile** in a side panel: type, null count, distinct count, min, max, top values, and a small distribution chart. Include the v1 data quality report (values converted to NULL).
- Quick actions that create transform steps (4.2): rename, change type, hide, remove, split, trim, replace values, fill nulls.
- Respects RLS and restrictions exactly as previews do today.

### 4.2 SQL transforms (in place of Power Query)

A **transform** is a dataset defined in SQL over other datasets and transforms, built as Parquet snapshots like any dataset.

- **Steps:** a transform is an ordered list of steps, each a SQL `SELECT` that can refer to the previous step as `prev`. This mirrors Power Query's "Applied steps". Each step is named, can be previewed on its own (first 100 rows), can be disabled or reordered, and shows its row count.
- **Quick actions** from the Table view add steps with generated SQL that I can read and edit.
- **Single-query mode:** a transform can also be one SQL query.
- **Building:**
  - Transforms build after their inputs refresh, following a dependency graph. Cycles are refused.
  - Refreshing a source triggers rebuilds of everything downstream, and the job view shows the chain.
  - A failed step shows the step name, the SQL and DuckDB's error with its position.
- **Versioning:** every save is a new version with diff and restore. Builds record which version they used.
- **Lineage:** a lineage view showing sources, then transforms, then model tables, then dashboards.

**Security rules for transforms (requirements):**
- Only `SELECT` statements, parsed and checked with sqlglot. They may only refer to Opsis datasets and transforms by name, never to files, URLs, other databases or system tables.
- DuckDB runs with external access disabled and its configuration locked (`enable_external_access=false`, `lock_configuration=true`), plus an allow-list of functions. Test that file-reading, HTTP and extension functions are refused.
- **Restrictions are inherited:** a transform built from any restricted dataset is itself restricted to the intersection of its inputs' access lists. Only an admin can loosen that.
- **RLS:** transforms build over full data (like a Power BI refresh), and RLS rules apply at query time on the model table. A rule on a source table must be re-declared (or inherited by default) on the model tables built from it. Propose the exact rule in V0 and test it, so that a transform can never become a way around RLS.
- Time and memory limits on each build.

### 4.3 SQL Lab

The v1 backlog item, now built:
- A Monaco SQL editor with autocomplete for datasets, transforms and columns.
- Run read-only queries over datasets and transforms, with results in the Table view grid.
- Save a query as a transform.
- Query history per user.
- The same security rules as transforms. Queries run as the user, with RLS and restrictions applied.
- Available to editors and admins. The existing `FEATURE_SQL_LAB` flag controls it.

### 4.4 The agent can help

- In SQL Lab and the transform editor: "Ask the agent" writes or fixes SQL from a plain-English request. It sees the schemas of the datasets involved (and samples only where the user may see them), proposes the SQL with an explanation, and runs a preview. Nothing is saved without my confirmation.
- It explains a step or query in plain English.
- MCP tools: list, read, create and patch transforms; preview a step; run a SQL Lab query, row-capped and with RLS applied.

---

## 5. Slicers

Slicers are a new kind of canvas tile, like Power BI's, alongside the existing filter bar (keep the filter bar for dashboard-wide filters).

**Slicer types:**
- **List:** checkboxes, single or multi-select, with "select all" and search.
- **Dropdown:** single or multi-select.
- **Tiles:** button-style options in a row or grid.
- **Between:** a numeric range slider with min and max inputs.
- **Date:**
  - between (range slider with a calendar picker);
  - relative ("last N days/weeks/months/financial years", "this financial year");
  - before or after a date.
- **Hierarchy:** for example financial year, then quarter, then month, or region, then city.
- Options for every slicer: title, single or multi-select, show or hide "Clear", default selection, and sort order.

**One slicer controls many visuals:**
- A slicer filters every visual on the dashboard whose data is related to its field through the model, in the relationship's filter direction. This includes visuals on other tables.
- **Edit interactions**, like Power BI: in edit mode, select a slicer (or any visual) and each other visual gets a toggle, **Filter** or **None** (plus **Highlight** for charts that support it). Store this in the spec.
- Cross-filtering between visuals follows the same interaction settings.
- **Slicer sync:** if dashboards get pages (see below), a slicer can be synced across pages.
- Slicer state goes in the URL, as filters do today, so a view can be bookmarked and shared.

**Pages:** Power BI-style multi-page dashboards are needed to make slicer sync meaningful. Decide in V0 whether pages are cheap to add now. If so, add them (tabs at the bottom, per-page layout, page-level and dashboard-level slicers). If not, leave slicer sync to the backlog.

**Spec:** extend `dashboard.schema.json` with slicer items and an `interactions` matrix, with a `specVersion` bump and a migration so existing dashboards keep working. The agent and MCP tools understand slicers and interactions.

---

## 6. Visual SDK: importable visuals

**Goal:** I can describe a visual (for example "a candlestick chart with a moving-average line, for stock prices over time"), give the SDK's authoring kit to any AI, get a visual package back, and import it into Opsis through the UI. Nothing in the core changes, and nothing needs to be rebuilt.

### 6.1 The Opsis Visual Package

A zip file (`.opsisviz`) containing:
- **`manifest.json`:** the same manifest format as built-in visuals (id, name, version, author, description, roles, configSchema, aiHints), plus:
  - `sdkVersion`;
  - `entry` (the bundle filename);
  - optional `icon`, `licence` and `sampleData`;
  - a `capabilities` list, such as `"crossFilter"`, `"highlight"`, `"drill"` or `"resize"`.
- **One JavaScript bundle**, plain ES module, self-contained. It may bundle libraries; ECharts and D3 are allowed, and the SDK should offer to provide them so packages stay small.
- **Optional** CSS, and a `README.md`.

### 6.2 The runtime contract

- The visual exports `create(host)`, returning an object with `update(data, config, context)` and `destroy()`. Define it in TypeScript types published in `sdk/visuals/`.
- **`data`** is the query result for its roles, in a documented columnar shape, plus field metadata (names, types, formats).
- **`context`** includes the theme (colours, fonts, light or dark), size, locale, the financial-year settings, and the current selection.
- **`host`** gives the visual: `select(...)` for cross-filtering, `drill(...)`, `tooltip(...)`, `requestResize()`, and `log(...)`. Nothing else.

### 6.3 Security for imported visuals (requirements)

Imported visuals are untrusted code:
- **Sandbox:** each one runs in a sandboxed iframe, with `sandbox="allow-scripts"` and no same-origin access. Its CSP blocks all network access (`connect-src 'none'`, no external scripts, images only as data URIs).
- **Messaging:** the iframe talks to Opsis only through `postMessage` with a validated message schema.
- **Data:** it receives only the rows for its own query, after RLS.
- **Who can install:** only admins can install packages. Installs, upgrades and removals are audited. Packages are versioned, and dashboards record which version they use.
- **Validation on import:** check the manifest against the schema, enforce a size limit, require a single entry file, and scan for obviously forbidden APIs (warn, since the sandbox is the real protection).
- **Failure containment:** a crashing or slow visual shows an error tile and never breaks the dashboard. Rendering has a timeout.
- **Built-in visuals** keep running natively, without the sandbox.

### 6.4 Tooling

- **Visual playground:** an admin page where a package can be uploaded and previewed before installing. It renders with its own `sampleData` or with a chosen dataset and field bindings, shows `host.log` output and errors, and lets you resize it, switch light and dark mode, and test cross-filtering.
- **Authoring kit in `sdk/visuals/`:**
  - the spec;
  - TypeScript types;
  - a minimal template package;
  - a validation script (`npx opsis-viz validate my.opsisviz` or the Python equivalent);
  - **`AUTHORING_PROMPT.md`:** a ready-to-paste prompt I can give any AI together with my visual description. It must produce a complete, valid package, including sample data.
- **Reference package:** build a **candlestick chart** as the reference, entirely through the SDK and not as a built-in visual:
  - roles: date (time), open, high, low, close (measures), optional volume;
  - options: moving-average lines (periods configurable), up and down colours, and a volume sub-chart;
  - included in `sdk/visuals/examples/` and importable through the UI.
- **Agent:** `list_visuals` includes installed packages with their aiHints, so the agent can use them in dashboards.

---

## 7. Mobile support

**Viewing on phones is the priority.** Editing on a phone is not required; tablets can keep the desktop editor.

- **Phone layout per dashboard**, like Power BI:
  - By default, tiles stack automatically in a sensible order, full width.
  - In the editor, a "Phone layout" mode lets me choose which tiles appear, their order and their heights.
- **Touch-friendly slicers and filters:** they open as bottom sheets, with large tap targets, and the range sliders work with touch.
- **Charts:** tooltips on tap, no hover-only features, and pinch or tap to open a visual full screen.
- **Navigation:** a compact header, a dashboard list that works on small screens, and sign-in pages that fit a phone.
- **Share links** work well on phones.
- **Performance:** load visible tiles first, then the rest.
- **Testing:** add Playwright runs at phone and tablet viewport sizes to `npm run smoke`, at the root and under `/opsis`.
- **Optional:** make Opsis installable as a PWA (home-screen icon, no offline data). Only if cheap.

---

## 8. Dashboard export

**Interactive offline export:** a single, self-contained HTML file that works without Opsis or a network connection, with working slicers, filters, cross-filtering, tooltips and the chart/table toggle.
- **Data:** embed the data the dashboard needs. Propose the approach in V0. Options include DuckDB-WASM with embedded Parquet, or pre-aggregated data at the granularity of the slicer fields with a small in-browser filtering engine. Weigh file size against fidelity.
- **Size:** set a size cap with a clear warning before export, and show the estimated size.
- **Visuals:** imported SDK visuals are included in the file with the same sandbox.
- **What's in the file:** the export shows the data **as the exporting user sees it**, with their RLS applied and their restrictions respected. Every page and tile carries a footer: "Exported from Opsis by <name> on <date>. Data as at <refresh time>."

**Static exports:** PDF and PNG of the current view, rendered server-side (for example headless Chromium in the worker container), including the phone layout as an option.

**Governance (requirements):** an exported file escapes every Opsis control, so:
- Export is a permission. It's on for editors and admins by default, and admins can grant or revoke it per user or group.
- Dashboards that use restricted datasets can only be exported by the dataset's owner or an admin, as with sharing.
- Every export is audited: who, what, which format, the row count and the file size.
- Workspace setting: interactive export on or off.

---

## 9. Milestones (stop after each one)

**V0. Plan (no feature code).**
- Read `BUILD_BRIEF.md`, this brief, `PROGRESS.md`, `docs/DECISIONS.md` and the codebase.
- Write ADRs and short designs for:
  - the workspace model and its migration (3.1);
  - RLS through transforms and relationships (4.2);
  - pages: yes or no (5);
  - the visual package format and sandbox (6);
  - the export data approach (8).
- Update the JSON schemas in `spec/` as drafts, with migrations planned.
- List risks and questions.
- *Done when:* I can review the designs and schema changes in under 30 minutes.

**V1. BYO agent.**
- Per-user keys, admin toggles, the OpenAI provider, the picker in the assistant window, and usage logging.
- *Done when:*
  - A user with no keys and no grant sees the "add a key" message.
  - A user with their own Anthropic key and another with their own OpenAI key can each build a dashboard through the assistant, using the scripted fake model in tests.
  - Granting the workspace key works, and revoking it takes effect immediately.
  - No key ever appears in an API response, log or audit entry (add a test that searches for this).

**V2. Workspace model and Model view.**
- The migration, the relationships compiler, the Model view canvas, the measures form, date tables, YAML two-way editing, model versions, "Suggest relationships", and the MCP tools.
- *Done when:*
  - All existing dashboards render unchanged after the migration.
  - I can drag a relationship between two tables and immediately use fields from both in one visual.
  - RLS filters through a relationship (tested).
  - Editing the YAML updates the canvas.

**V3. Data prep.**
- Table view, SQL transforms with steps, the dependency graph and rebuilds, lineage, SQL Lab, and agent help for SQL.
- *Done when:*
  - I can build a transform that joins `a_codes` with another table, cleans a column and adds a calculated column, mostly through quick actions and the agent.
  - The transform rebuilds when its source refreshes.
  - The security tests (file, HTTP and extension functions refused; restriction inheritance; RLS) all pass.

**V4. Slicers.**
- All slicer types, Edit interactions, slicer state in the URL, pages if agreed in V0, the spec migration, and MCP support.
- *Done when:*
  - One date slicer and one list slicer filter several visuals across two related tables.
  - Setting one visual to "None" in Edit interactions excludes it.
  - The state survives a reload through the URL.
  - Existing dashboards migrate cleanly.

**V5. Visual SDK.**
- The package format, sandbox, import UI, playground, authoring kit, validation script, and the candlestick reference package.
- *Done when:*
  - I can give `AUTHORING_PROMPT.md` plus a description to an AI, import what it produces, preview it in the playground and use it in a dashboard.
  - The candlestick package works with a stock-price sample dataset and cross-filters with a slicer.
  - A test package that tries `fetch`, reads `document.cookie` or accesses the parent window is blocked.

**V6. Mobile.**
- Phone layout and its editor, touch slicers, the mobile navigation and phone-size smoke tests.
- *Done when:* the demo dashboard is fully usable on a phone-size viewport at the root and under `/opsis`, slicers included, verified by Playwright and by screenshots I can look at.

**V7. Export.**
- Interactive HTML export, PDF and PNG, the permissions, audit and size warnings.
- *Done when:*
  - An exported HTML file opens offline in a browser with working slicers and cross-filtering.
  - A user with RLS sees only their own data in their export.
  - A restricted dashboard can't be exported by someone who isn't allowed.

---

## 10. Non-goals for v2

- DAX, M (Power Query's formula language) or a formula language of our own beyond SQL and the measure form.
- Editing dashboards on a phone.
- Real-time data.
- Importing or converting Power BI files of any kind (`.pbix` reports or `.pbiviz` custom visuals). New visuals come only through the Opsis visual SDK.
- A public marketplace for visual packages.

---

## 11. Start

Begin with **Milestone V0 only**, then stop and report.
