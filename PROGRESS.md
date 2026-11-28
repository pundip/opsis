# Progress

Last updated: 2026-11-28 (v2: V2 reviewed; your stack migrated to V2 with no result changed; V3 in progress)

Two briefs: `BUILD_BRIEF.md` (v1, milestones M0 to M8, done) and `BUILD_BRIEF_V2.md` (v2, milestones V0 to V7). A fresh session resumes v2 from this file, `BUILD_BRIEF_V2.md` and `docs/V2_DESIGN.md`.

## v2 milestone status

| Milestone | Status |
|---|---|
| V0 Plan | Done, reviewed |
| V1 BYO agent | Done, reviewed; the review's follow-ups done |
| V2 Workspace model and Model view | Done, reviewed (ADR-039 accepted); your stack migrated, no result changed |
| V3 Data prep | In progress |
| V4 Slicers | Not started |
| V5 Visual SDK | Not started |
| V6 Mobile | Not started |
| V7 Export | Not started |

## v2, V2: workspace model and Model view (reviewed)

One workspace model replaces the models per dataset: tables, and relationships between them, as in Power BI. Row-level security now travels through relationships (rule R2 of ADR-032). The choices made while building it are in ADR-039, accepted at the V2 review. Your stack is migrated at the review's go-ahead (see "Your stack migrated to V2" below).

### Step 1: the upgrade check (ADR-038)

V2's first step, alone, as ADR-031 and the V0 review ask: nothing the app does changes, and the migration waits until you have seen this report on your stack and approved it.

- **`python -m opsis results-digest`** runs every item of every dashboard, draft and published, at its default filters: as an admin, as every active person on the versions they can open, and as every live share link (published only, never exempt). It keeps a hash of each result with its row count and measure totals, then runs each item again as it will be once row-level security travels through relationships (R2). It prints and saves the report: who will see fewer rows, in which items and how (rows and totals, now and after); for each viewer, each table that loses rows and whose rules do it; loops to resolve before the migration; items that cannot be shown now either; and the relationships the migration will make, active or not. The files go to `/data/upgrade/` in the api container (or `--out`). It changes nothing.
- **`--compare <file>`** runs again, counting relative dates from the day of the file, and lists every result that changed since, with who saw it and whether the report predicted it. Run after the migration, it is the check that every dashboard renders unchanged apart from the predicted changes.
- **The R2 simulation** (`semantic/relationships.py`): the relationships the migration will make from the v1 joins (the planned migration's rules, and a test that they agree), and a scan that applies R2 through the active ones: transitive, empty and unmatched keys hidden, no access check on a table read only to filter. The compiler takes it through an optional `scan` hook whose default is v1's behaviour (`compile_query`, `queries.execute`, `dashboards.data_for_spec`), so dashboards, previews and MCP are unchanged.
- **Agreed at the review:** the check covers every active person and live share link, not only people with attributes, as a rule can also use the role or the email; and a loop of joins is refused when a rule is on the loop or reachable from it (V2-Q1).

### How step 1 was checked

- **Backend:** 396 pytest tests pass (380 before). The 16 new ones are in `tests/test_results_digest.py`: the relationships agree with the planned migration's (the demo models and four more cases); loops; the six v1 join patterns on real data (direct, two steps, inner, `one_to_many` declared, a restricted dataset in between, two ways to one dataset), each with the rows a person sees now and after, checked against SQL on the source; admins exempt, share links never; who is checked on drafts and on published versions; the command, `--compare`, and that no table, audit row or link's view count changes; the comparison report. `ruff check`, `ruff format --check` and `mypy --strict` pass. The new tests were run again on the final files after two typing fixes.
- **Frontend:** no frontend file changed.
- **Smoke tests in all three places,** as the step touches the query path every dashboard uses (the default scan is v1's): at the root 12 passed (3 skipped), under `/opsis` 12 passed (3 skipped), in server mode 14 passed (1 skipped), as before.
- **Your stack was not touched:** the same five containers, start times and images before and after, and no test env file left.

### The report on your stack (2026-11-28, after the review)

- **Rebuilt** at your go-ahead: `docker compose up -d --build` in your project `opsis`, on commit 9ed0670. The api and config-check containers were recreated; Postgres and proxytest kept running; sample-mysql, stopped for the 43 hours before, started again. No database migration ran (step 1 adds none).
- **`python -m opsis results-digest`:** "Nobody will see fewer rows. Every result stays the same." It checked 5 dashboards: 23 items that show data, in 4 versions (Test1's draft has none), as an admin. Your stack has no other people and no share links (local mode, the one built-in admin), so the admin is everyone there is to check. No loop to resolve, and no item that cannot be shown now. The migration will make three relationships, all active: gifts (donor_id) to donors (id), demo_donors (state_code) to demo_regions (code), demo_gifts (donor_id) to demo_donors (donor_id).
- **Kept:** `/data/upgrade/results-digest-20261128-040111.json` (the digest, for `--compare` after the migration) and the report beside it (`.md`), in the api container's data volume (`opsis_opsisdata`), which a rebuild keeps.
- **The three old worktrees** under `.claude/worktrees` held nothing uncommitted or unpushed (each branch was pushed and merged into main), so they were removed with `git worktree remove` and their local branches deleted. The branches on origin are kept.

### Steps 2 to 8: what was built

- **The workspace model, stored** (`semantic/workspace.py`, `services/workspace_model.py`, migration 0014): one document (the tables someone wrote, and relationships keyed by id) and an immutable version per save, with its author and note. A dataset with data and no table entry is a starter table, generated from its columns. People read and change only the tables they may use; the rest is kept as it is. A save takes a whole document (`PUT`) or JSON Patch (`PATCH`); a stale `baseVersion` is applied anyway when nobody changed the same places since, and refused with 409 otherwise. A save is refused only for problems it adds. Versions, diff, and restore as a new version. The Model view's layout is saved beside the model, not versioned. `spec/workspace-model.schema.json` and the v2 `spec/semantic-query.schema.json` were promoted from the drafts.
- **The migration** (0014, metadata only): edited v1 models become tables, starter ones stay implicit, and each v1 join becomes a relationship by the planned rules (`many_to_one` and `one_to_one` active, `one_to_many` turned round and inactive, a join declared on both sides kept once). The model starts at version 1, "Migrated from the v1 models". The v1 tables are kept as they were.
- **The compiler over relationships** (`semantic/compiler.py`, `semantic/relationships.py`, `semantic/security.py`): fields of any table that the measures' table reaches along active many-to-one relationships, any number of steps (the shortest path wins, two as short are an error that names both, never through a restricted table, and after a left join the rest stay left); filters through relationships as semi-joins in their filter direction (`crossFilter: both` included); measures of several tables in one query; a measure's `relationship`, which may be inactive (Power BI's USERELATIONSHIP); `{table.measure}` in formulas. Every read applies R1 and R2: queries, dashboards, previews, profiles, filter options, the data quality report and the samples MCP returns. A loop that a rule could travel round is refused.
- **Every caller moved to the workspace model:** dashboards, previews, profiles, filter options, the data quality report, model warnings (one per relationship), model files (`models/<table>.yml` and `models/workspace-model.yml`; v1 files still import, their joins as relationships), the demo data, dataset deletion (its table and relationships leave the model, as a version by the system), backups, and `results-digest` (it now runs queries as the app does, and `--compare` also lists predicted changes that did not happen). The v1 routes and MCP tools for one table are a view of the workspace model.
- **REST:** `/api/model` (GET, PUT, PATCH, `/validate`, `/layout`, `/versions`, `/diff`, `/versions/{n}/restore`, `/export`, `/key-check`, `/suggestions`, `/date-table`).
- **MCP:** `get_workspace_model`, `patch_workspace_model`, `validate_workspace_model` and `suggest_relationships`, and the guide. The in-app assistant reads the model and may suggest relationships, but does not get `patch_workspace_model`.
- **Help with relationships** (`services/model_suggestions.py`): a key check for a dragged pair (which side is the one side, the cardinality, and active unless it would make two equally short paths or close a refused loop), and suggestions from names, types and value overlap, each with a confidence and the evidence, read as the person.
- **Date tables** (`connectors/date`, `services/date_table.py`): a built-in date connector, and "Generate a date table": from the start of the financial year of the earliest date in the model to the end of the next one, with financial-year columns from the workspace calendar, refreshed daily, marked as the model's date table, and relationships from date columns offered.
- **The Model view** (`/model`, "Model" in the navigation): a canvas of table cards (keys and related fields always shown; date table, restricted, hidden and starter badges; collapse to the keys), relationships drawn with 1 and *, arrows in the filter direction, dashed when inactive. Drag a field onto a field of another table, check the keys and confirm in a side panel with the guessed settings; edit and remove; Auto layout (dagre) and saved positions. A table panel (label, description, hidden, date table, the fields' settings, and measures with a form for aggregations and arithmetic over measures, including "Through relationship"); a list of the relationships; a YAML tab (Monaco, Check, Save, a note when the model changed meanwhile, download); History (the changes against the version before, restore); Suggest relationships; Generate a date table; "Add a relationship" for keyboard users. The per-table pages stay at `/models` as "Model tables".

### How V2 was checked

- **Backend:** 425 pytest tests pass (396 after step 1). New: `tests/test_row_security.py` (9, the R2 tests of `V2_DESIGN.md` section 3.5: a direct rule, two steps, `securityFilter: both`, inactive relationships, admins and share links, previews and MCP samples and filter options, a restricted table whose rules still filter what is related to it, loops, and a rule on a date table), `tests/test_workspace_model.py` (8: the migration on the demo models and the v1 join patterns, against the app's own conversion, the planned rules and the schema, and the data step on a database at revision 0013), `tests/test_relationships.py` (5: fields of two tables with left and inner joins, keys of two columns, a measure on an inactive relationship, filters one way and both ways, two steps, each against hand-written SQL), `tests/test_relationship_help.py` (5: the date connector, a dragged pair guessed from its keys, suggestions read as the person, the agent getting suggestions and applying nothing, a date table generated and offered), and changes in the existing tests where v1 behaviour moved (MCP, model files, model warnings, queries, the semantic layer, the drafts). `ruff check`, `ruff format --check` and `mypy --strict` pass.
- **Frontend:** typecheck, lint, 60 unit tests (8 new, for the canvas's cards, layout and edges) and the production build. The canvas is a 304 kB chunk loaded only on the Model page; the main chunk is 962 kB (289 kB gzipped).
- **Smoke tests in all three places** (`npm run smoke`): at the root 13 passed (3 skipped), under `/opsis` 13 passed (3 skipped), in server mode 15 passed (1 skipped), one more in each than after step 1. New: `e2e/tests/model-view.spec.ts` drags gifts.donor_id onto donors.id on the canvas, sees the key check name the many side, adds the relationship, builds a table visual on the gifts dataset with the donors' region and the gifts' total (VIC 700, NSW 300), saves it, and edits a table's label in the YAML tab, which the canvas then shows. `e2e/tests/model-warnings.spec.ts` now expects one warning (the repeated key) instead of two, and checks that the lookup's rule filters the gifts for someone it applies to.
- **Found by the smoke test and fixed:** "Saved as version N." never showed on the YAML tab, because the editor reported the saved text as an edit.
- **Benchmark, 10 million rows** (`node scripts/benchmark.mjs --rows 10000000 --rule`, run twice in its own test stack on the laptop M8 used; `--rule` is new):
  - **Without a rule, as in M8:** a typical dashboard opens in 486 ms cold and 71 ms warm (M8: 492 and 81 ms), 426 ms after a filter change and 87 ms warm again; single queries take 55 to 398 ms cold and 10 to 13 ms warm; ingestion 31.3 s (325,000 rows/s). The first run was slower cold (the dashboard in 609 ms, the join query 463 ms against M8's 296 ms), the second was not, and the SQL without a rule has v1's shape, so that was variation between runs.
  - **With a rule on donors that applies to admins too** (`tier <> 'lapsed'`: 7.5 of the 10 million gifts are visible, each read through its donor): the dashboard opens in 1.11 s cold and 96 ms warm, 1.09 s after a filter change and 82 ms warm again; single queries take 165 to 488 ms cold and 11 to 15 ms warm. R2 about doubles a dashboard's cold time (an `EXISTS` on 200,000 donors for 10 million gifts) and leaves warm times as they were. **The brief's target, under 1.5 s warm on 10 million rows, is met with and without the rule.**
  - **Ten uncached queries at once** finished in 3.0 and 3.7 s while `/healthz` answered in a median of 9 and 19 ms. Peak api memory: 1,529 MB during the queries and the burst without the rule, 714 and 749 MB during the pass with the rule (which has no burst). M8 measured 422 MB and M3.5 1,138 MB (on 5 million rows): the peak depends on how the queries overlap, up to four at once with 1 GB each (`QUERY_MAX_CONCURRENT`, `QUERY_MEMORY_LIMIT_MB`).
- **Your stack was not touched:** no test reached the project `opsis` or port 8080 (ADR-037).

### Your stack migrated to V2 (2026-11-28, after the review)

In the order you set at the review.

1. **Backed up:** `python -m opsis backup` in your api container wrote `/backups/opsis-20261128-232820.tar` (1.7 MB: the database at revision 0013, the uploads, and the current snapshot of each of the 10 datasets) into your `opsis_backups` volume. A copy is in `C:\Users\prakashm\opsis-backups\pre-v2\` (the same SHA-256, `af4a7356...804363e`), out of reach of `BACKUP_RETENTION` and of `docker compose down -v`. The image your stack ran is kept as `opsis/app:pre-v2` (`e06c280134bf`, built from commit 9ed0670).
2. **The backup restores:** restored into a scratch Compose project of its own (`opsis-restore`, port 18084 on 127.0.0.1, its own env file with fresh secrets, set up through `scripts/test-stack.mjs` and its guard) on the pre-V2 image, it started (`/healthz` ok). Its database matched yours on every count: revision 0013; 5 dashboards, 17 dashboard versions, 10 datasets, 13 snapshots, 9 connections, 9 models, 15 model versions, 1 user, 1,029 audit rows; the same dashboard titles and dataset names. The app listed the same 5 dashboards as yours, and `results-digest --compare` against your saved digest said "No result changed", so every item shows the same results from the restored files. Then the project was removed, data and all; your containers kept their images and start times.
3. **The rollback steps,** written down before the migration. They put your stack back as it was at the backup (2026-11-28 23:28 Melbourne time); anything changed in Opsis after that is lost. In PowerShell, in `C:\Users\prakashm\Projects\opsis`:
   1. Stop the api: `docker compose stop api` (and `worker`, if you run it).
   2. Point your stack at the image from before: `docker tag opsis/app:pre-v2 opsis/app:dev`
   3. Copy the backup into the volume, in case it has gone from there (copying it again is harmless): `docker cp C:\Users\prakashm\opsis-backups\pre-v2\opsis-20261128-232820.tar opsis-api-1:/backups/`
   4. Restore it with that image: `docker compose run --rm --no-deps api restore /backups/opsis-20261128-232820.tar --yes`. It replaces the database and the data folder's uploads and datasets; the current ones are moved to `/data/before-restore-<time>` in the data volume, not deleted.
   5. Start the stack without building: `docker compose up -d --no-build`. The api is recreated on the old image, and the database is already at its revision (0013).
   6. Check: `docker compose exec api python -m opsis results-digest --compare /data/upgrade/results-digest-20261128-040111.json` should say "No result changed."

   `ENCRYPTION_KEY` stays as it is in your `.env` (a backup does not hold it, and the migration does not change it). After a rollback, `docker compose up -d --build` would build V2 again from this folder and migrate again: build only from the version you want.

4. **Rebuilt with V2:** `docker compose up -d --build` in your project `opsis`, on commit e8cb243 (V2, 94d5fcb, and the review's notes). The api and config-check containers were recreated on the new image (`48d8336c3d99`); Postgres, sample-mysql and proxytest kept running. As it started, the api ran the migration from 0013 to 0014 and came up healthy. The workspace model is at version 1 with the 10 tables and the three relationships the report predicted, all active: gifts (donor_id) to donors (id), demo_donors (state_code) to demo_regions (code), demo_gifts (donor_id) to demo_donors (donor_id).
5. **`results-digest --compare /data/upgrade/results-digest-20261128-040111.json`:** "No result changed." All 23 results (5 dashboards, in the 4 versions with items that show data, as the admin, the only person on your stack) are the same as before and as the report predicted; none is missing and none fails. The comparison is kept beside the first file: `/data/upgrade/results-compared-20261128-123354.json` and its `.md`.

## v2, V1: bring your own agent

People bring their own Anthropic or OpenAI keys, and the workspace's Claude key is only for people an admin allows. The decisions are in ADR-036, accepted at the review. Built:

- **Account page, "AI providers":** add, test, rename and delete Anthropic and OpenAI keys, several per person, each with an optional default model. Only the provider, a name and the last four characters are ever shown. **Test** asks the provider for the key's models (free) and shows the provider's own message when it refuses the key; a new key is tested as soon as it is added. When you edit a key, its default-model field offers the key's own list of models. Below the keys: your assistant use per key and provider (requests and tokens, in all and over the last 30 days).
- **Users page, "AI assistant" tab (admins, with sign-in):** "May use the workspace key" per person and per group, off for everyone by default (a person's own setting wins, then a "no" on any of their groups); each person's result and which providers they have keys for, never the keys; and "Personal API keys" on or off for the whole workspace.
- **The assistant panel:** a provider picker listing only what you may use (your own keys, the workspace key if allowed, Ollama if on) and, for your own keys, a model field offering the key's own list. The choice is remembered per person. With nothing to use, the panel explains how to get something, with a link to the Account page. When what you chose is taken away, you are asked to choose again rather than moved to something else, and a running request stops at its next step.
- **The OpenAI provider:** the official `openai` SDK (Apache-2.0) and its Responses API, with tool calling and streaming, sharing the tool loop, budgets, undo and history with Claude and Ollama. Claude on a person's own key gets the options its model takes.
- **Usage** per request in a new `assistant_usage` table and in the `assistant.request` audit row, which now says whose key answered (own, with the key's id and name; the workspace's; or Ollama). No key and none of the person's words in any log line or audit row.
- **Security:** keys are encrypted with `ENCRYPTION_KEY` and decrypted only inside the backend, for their owner's requests, Test and model list; a key is never in a response, a log line or an audit row (a test searches for both test keys everywhere); deactivating someone deletes their keys (Opsis deactivates people rather than deleting them). MCP is unchanged.
- **Settings:** `ANTHROPIC_API_KEY` is optional now with `ASSISTANT_PROVIDER=anthropic`; new `OLLAMA_ENABLED` (default: on when `ASSISTANT_PROVIDER=ollama`) and `OPENAI_BASE_URL` (for the tests' fake model). README and both `.env` templates are updated, with an upgrade note.
- **Migration 0013:** `user_api_keys`, `capability_grants`, `workspace_settings`, `assistant_choices`, `assistant_usage`, and the option each conversation was written with.
- **When you upgrade the server:** nobody may use the workspace key until you allow them on the Users page; until then people see the "add a key" message.

### How V1 was checked

- **Backend:** 379 pytest tests pass (367 at V0): the new ones are in `tests/test_byo_agent.py`, and the fakes in `tests/test_assistant.py` now answer over HTTP, so every provider runs through its own SDK. One v1 test changed on purpose: the server template no longer needs `ANTHROPIC_API_KEY` to start. `ruff check`, `ruff format --check` and `mypy --strict` pass. Run on the final files, in a Compose project of their own (`opsis-v1t`), removed afterwards.
- **Frontend:** typecheck, lint and the 52 unit tests pass (the new pages are covered by the browser tests).
- **Smoke tests in all three places,** against the same scripted fake model (`e2e/fake-llm.mjs`), which now speaks Ollama's, Anthropic's and OpenAI's APIs and says whose key answered: at the root 12 passed (3 server-only skipped), under `/opsis` 12 passed (3 skipped), and in server mode 14 passed (1 local-only skipped). New: `assistant.spec.ts` builds with an Anthropic key and an OpenAI key added on the Account page; `assistant-access.spec.ts` (server mode) covers the "add a key" message, two people each building with their own key, and a grant, then a revoke that stops the next request without a reload.
- **Found by the first server-mode run, and fixed:** the admin's toggles snapped back until the save came back, so a click looked ignored. They now show the click at once.
- **The done-when items:** no keys and no grant shows the "add a key" message (server-mode browser test, and pytest); an own Anthropic key and an own OpenAI key each build a dashboard (browser tests in all three places, and pytest); granting works and revoking takes effect at once, a running request included (browser test and pytest); no key in an API response, a log line or an audit row (`test_no_key_ever_appears_in_a_response_a_log_or_an_audit_row`, and the browser tests check the page and the key list).
- **Your stack on port 8080:** the root and sub-path runs were by hand in project `opsis-v1` on port 8094 (`smoke.mjs` hard-codes 8080 and restarts your stack), and the server stage in the usual disposable `opsis-servertest`. One thing reached your stack: in the first root run the test stack had loaded your `.env` (Compose's `env_file: .env`), so the fake Google sent the Google Sheets test's browser back to `http://localhost:8080`: one page load, then a POST to `/api/connections/google/finish`, which your stack refused (404). Nothing was written there. Later runs used an override that replaces `env_file`. The test projects, the test image and the test settings are removed.

## After the V1 review: the test message, keys kept when switched off, and test stacks kept apart from yours

Built:

- **"Send a test message"** beside Test on the Account page: one short request to the key's default model (an Anthropic key without one uses the workspace's `ANTHROPIC_MODEL`; an OpenAI key needs one), with `max_tokens` 1 on Anthropic and 16 on OpenAI (its minimum). It costs a fraction of a cent and shows whether the key's account can pay: an account with no credit shows the provider's own words (Anthropic's "credit balance is too low", OpenAI's "exceeded your current quota"). Its tokens count in the usage table, and it is audited (`assistant.key_test_message`, with no key in it). The free Test stays the default, and a new key is still tested with it.
- **Personal keys switched off:** the keys are kept, and their owners still see, rename and delete them. Test, the test message and the model list are refused (409 `byo_keys_off`), so no key is sent anywhere. The Account page says "You can still delete them", and the Users page's hint says the same.
- **A choice that is taken away** asks the person to choose again: V1 already did, so no change. **Usage** stays in tokens; estimated cost per provider is on the backlog.
- **Test stacks kept apart from yours (ADR-037).** `scripts/test-stack.mjs` gives every test stack its own Compose project (`opsis-test`, `opsis-test-server`, `opsis-test-backend`, `opsis-test-bench`), its own env file written from `.env.local.example` or `.env.server.example` with random test secrets (`.env.opsis-test*`, gitignored, removed afterwards), its own image tag, and its own ports: the usual ones plus 10000 (18080, 18088, 18089 and so on), bound to 127.0.0.1. `.env` is never read: `compose.yml`'s `env_file` is now `${OPSIS_ENV_FILE:-.env}`, the scripts pass `--env-file`, and they drop every shell variable the Compose files name, so nothing set in your shell leaks in. Before anything starts, each script checks its plan and the resolved Compose configuration (`docker compose config`, which needs no Docker daemon). It refuses the project `opsis`, port 8080, a published port it did not choose, a port not bound to 127.0.0.1, an image without the test tag, a service without the test marker, and `.env`, then exits with code 2 and "Refusing to start a test stack ... Nothing was started." Playwright refuses a target on port 8080 as well. `npm run smoke`, `npm run test:backend` (new), `npm run lint:backend` and `scripts/benchmark.mjs` all go through it; `npm run smoke -- --dry-run` shows what would start, with an env file of its own, so it can run beside a real run. In another checkout, `OPSIS_TEST_PROJECT` and `OPSIS_TEST_PORT_OFFSET` move its stacks off this one's (README, Development).
- **The proof:** `npm run test:scripts` (16 tests, part of `npm test`). The guard refuses each unsafe plan and each unsafe configuration; then every script is run with an unsafe setting and must exit with code 2 and the message, leaving no env file behind. Those runs point Docker at an address where nothing listens, so even a broken guard could not start anything. A last test runs the dry runs beside the env files of a run in progress and checks they are left as they were (found before the commit; the test fails without the fix).

### How it was checked

- **Backend:** 380 pytest tests pass (379 at V1; new: `test_a_test_message_shows_whether_the_account_can_pay`, and the grants and leak tests cover keys switched off and test messages). `ruff check`, `ruff format --check` and `mypy --strict` pass. Both through the new scripts (`npm run test:backend`, `npm run lint:backend`), so in the project `opsis-test-backend`, removed afterwards, on the final backend files.
- **Frontend:** typecheck, lint and the 52 unit tests pass.
- **The guard:** `npm run test:scripts`, 16 tests pass (the safe dry runs included, as Docker was running).
- **Smoke tests in all three places,** now through `npm run smoke` and its own stacks: at the root (http://localhost:18080) 12 passed (3 server-only skipped), under `/opsis` (http://localhost:18088/opsis/) 12 passed (3 skipped), and in server mode (http://localhost:18089/opsis/) 14 passed (1 local-only skipped), as at V1. New steps: a test message on an Anthropic key, then a key whose account has no credit, which passes Test and fails the test message with Anthropic's words (all three places); and in server mode, personal keys switched off: Ann's key is kept, she has no Test, test message or Add, and she deletes it.
- **Your stack was not touched:** before and after all the runs, project `opsis` had the same five containers, with the same ids, start times and images, and no test env file is left in the folder.

## v2, V0: the plan

No feature code: the app behaves exactly as before. Built:

- **ADR-030 to ADR-035** in `docs/DECISIONS.md` (proposed until your review): the plan and shared choices; one workspace model and its migration; row-level security through relationships and transforms, and the four gates for SQL people write; pages (yes, in V4), slicers and Edit interactions; visual packages and their sandbox; export as data per tile with a renderer service for PDF and PNG.
- **`docs/V2_DESIGN.md`:** the detail behind each ADR (query plans and their SQL, the exact rules with examples, the tests each milestone must pass, the frame page and its policy, the export file), the MCP tools per milestone, risks and questions. Its first lines say how to review it in 30 minutes.
- **Draft schemas in `spec/drafts/v2`:** `workspace-model` (new), `semantic-query`, `dashboard` (specVersion 2), `visual-manifest`, `visual-package` (new) and `transform` (new), with a README that lists what changes in each, the milestone that promotes it, and the migrations planned, plus six examples. They sit in a subfolder because the app loads `spec/*.schema.json` only, so editing the live schemas in place would have changed the running app.
- **`backend/tests/test_spec_drafts.py`** (30 checks): every draft is a valid JSON Schema whose references resolve; every example validates; the queries the v1 demo dashboard makes are valid v2 queries; the demo dashboard migrates by changing only its version; the demo's three models migrate to the workspace model example under the planned rules (written out in the test, with three more cases); the nine built-in manifests fit the draft after their planned change; and 14 documents the drafts must refuse.
- **A DuckDB check for the transform design** (throwaway, not committed), run offline in the test image against DuckDB 1.5.6: with exact allowed paths, external access off and the configuration locked, only the input files can be read, and other files, globs, URLs, extensions, `ATTACH`, `COPY` and settings changes are refused. It found two things the design now handles: `allowed_directories` would still let `COPY` write into a snapshot folder (so exact paths), and the `query()` table function runs SQL from a string (so two parsers check every query). Results are in ADR-032.
- `BUILD_BRIEF_V2.md` is committed, next to `BUILD_BRIEF.md`.

### How V0 was checked

- **Backend:** 367 pytest tests pass (the 337 from before and the 30 new ones); `ruff check`, `ruff format --check` and `mypy --strict` pass. Run on the final files.
- **Frontend:** typecheck, lint and the 52 unit tests pass (no frontend file changed).
- **Smoke tests:** not run. V0 changes nothing the app runs: no code outside the tests, and the drafts are not loaded (the Docker image copies `spec/drafts` but nothing reads it).
- Your stack on port 8080 was not touched: the backend checks ran in a Compose project of their own (`opsis-v0`, its own Postgres and MySQL), removed afterwards.

### Docs to update

The in-app guide (`docs/guide`) is not edited in v2 runs (brief v2, rule 7). What each milestone will make out of date:

- **V0:** nothing (no behaviour changed).
- **V1:** chapter 8 (the providers table: the workspace's Claude key only for people an admin allows, your own Anthropic and OpenAI keys, Ollama with `OLLAMA_ENABLED`; the picker and its "add a key" message; a revoke stopping a request at its next step); chapter 1 (the Account page's AI providers section and its usage table); chapter 7 (admins: the Users page's AI assistant tab, per person and per group, personal keys on or off, deactivating deletes keys); chapter 9 (settings: `ANTHROPIC_API_KEY` optional, `OLLAMA_ENABLED`, the upgrade note). After the review: chapter 1 (the Account page's "Send a test message", which costs a fraction of a cent and shows whether the key's account can pay, and which needs a default model for an OpenAI key); chapter 7 (personal keys switched off: kept, not used or tested, and their owners can still delete them).
- **V2:** chapter 4 (one workspace model; the Model page: the canvas, dragging a field onto a field and the key check, the table panel with fields and the measures form, "Through relationship", the YAML tab with Check and Save, History and restore; starter tables; "Generate a date table"; the "Model tables" pages at `/models`; model files, `models/workspace-model.yml` beside `models/<table>.yml`); chapter 5 (relationships instead of joins: many to one or one to one, filter direction, security filter, active and inactive, a measure that names a relationship, left or inner, paths of any length and ties, Suggest relationships; the warnings are now per relationship, and the one about rules that do not carry over is gone); chapter 7 (row-level security through relationships: what a rule on a lookup hides in related tables, `securityFilter: both`, inactive relationships carry nothing, loops a rule could travel round are refused, a restricted table's rules still filter related tables, and asking for its fields gives 403); chapter 8 (MCP: `get_workspace_model`, `patch_workspace_model`, `validate_workspace_model`, `suggest_relationships`; the assistant's new tools); chapter 9 (the upgrade: back up, `results-digest` before, the migration, `--compare` after; the date table's daily refresh needs `FEATURE_SCHEDULED_REFRESH`; current limits). New screenshots of the Model page.
- **V3:** chapter 3 (the Table view, transforms), a new chapter on data prep and SQL Lab, chapter 7 (inherited rules and restrictions), chapter 8 (MCP tools).
- **V4:** chapter 6 (pages, slicers, Edit interactions, links with slicer state).
- **V5:** a new chapter on imported visuals and the playground; chapter 6 (the visual picker).
- **V6:** chapter 6 (phone layout), chapter 1 (using Opsis on a phone).
- **V7:** chapters 6 and 7 (export, its permission and audit), chapter 9 (the renderer service).

## v1 milestone status

| Milestone | Status |
|---|---|
| M0 Plan | Done, reviewed |
| M1 Skeleton and modes | Done, reviewed, catch-up to the revised brief done |
| M2 Data ingestion | Done, reviewed |
| M3 Semantic layer and queries | Done, reviewed |
| M3.5 Timezones, conditional measures, model versions, benchmark | Done, reviewed |
| M4 Dashboards and visuals | Done, reviewed |
| M5 MCP | Done, reviewed |
| M6 Auth and sharing | Done, reviewed |
| M7 In-app assistant | Done, reviewed |
| M8 More connectors and hardening | Done, awaiting review. Still to do: your part of the install check (Proxmox, real Google sign-in), and real Salesforce and Google Sheets runs with your credentials |
| After M8: legacy data | Done, awaiting review |
| After M8: leaving the editor | Done, awaiting review |
| After M8: join warnings | Done, awaiting review |
| After M8: user guide in the app | Done, awaiting review |

## After M8: the three changes merged into main

Join warnings, asking before the editor is left, and the guide inside the app are merged into `main`, to be deployed. The only conflicts were in `PROGRESS.md` and `docs/DECISIONS.md`, where both sides are kept. There is no new database migration or setting, so an upgrade is a pull and `docker compose up -d --build`.

Verified on the combined code: 337 backend tests, `ruff check`, `ruff format --check` and `mypy --strict`; frontend typecheck, lint and 52 unit tests; the smoke tests at the root (by hand on port 8093, as your stack holds 8080: 11 passed, 2 server-mode tests skipped), under the sub-path (the same) and in server mode (12 passed, 1 local-mode test skipped).

## After M8: the user guide inside the app

The guide could be read only in the source code. A running Opsis now shows it at `{BASE_PATH}/docs/guide`, under **Help** in the menu (details in ADR-029):

- **One source.** The image carries a copy of `docs/guide`, and the api renders a page when it is asked for (`opsis/services/user_guide.py`, `GET /api/guide`, with markdown-it-py, MIT): tables, code blocks, screenshots, and links between chapters and to headings. The page is `frontend/src/pages/Guide.tsx`.
- **Any base path, offline.** Every link and screenshot is written with the base path, nothing is loaded from elsewhere, and the Content Security Policy is unchanged.
- **Who may read it.** Open in local mode. With sign-in, only signed-in people: others get the sign-in page (and the page they asked for after it), and the files answer 401.
- **Links that leave the guide** (the deployment guides, the schemas, the project README) are shown as their text with the file's path, not as links: the app does not ship those files.
- The guide says where to find itself (contents page, and the menu table in chapter 1). Its screenshots predate the Help link.

### How it was verified

- **Backend:** 329 pytest tests pass (12 new, `tests/test_user_guide.py`); `ruff check`, `ruff format --check` and `mypy --strict` pass. The new tests render the real guide at the root and under /opsis and follow every link, heading anchor and screenshot. They also check how links and images are rewritten, that HTML and `javascript:` links stay text, that nothing outside the guide can be read, that local mode is open, and that with sign-in only signed-in people (an admin and a viewer) get anything.
- **Frontend:** typecheck, lint and 52 unit tests pass (4 new).
- **Browser:** the new `e2e/tests/guide.spec.ts` passes with the others at the root and under the sub-path (9 passed, 2 server-mode tests skipped, each) and in server mode (10 passed, 1 local-mode test skipped). It opens **Help**, follows every page, link and screenshot, moves around inside the app (to a heading in another chapter, within a page, Back to the same place, a deep link and a reload), and checks that nothing is requested outside the base path and the page reports no Content Security Policy violation. In server mode a signed-out visitor gets the sign-in page and 401s, then the page they asked for.
- The root stage was run by hand at http://localhost:8093 in a Compose project of its own, because your stack holds port 8080; `npm run smoke -- --subpath --server` ran unchanged. The pages were also looked at in a browser: light and dark, and 768 px wide.

## After M8: warnings for two join traps

Both were found while writing the user guide: a lookup that lists a key twice silently doubled totals, and a row-level security rule on a joined dataset did not restrict the base dataset's rows. Query results are unchanged; the model now says so (details in ADR-028).

- **Repeated join keys:** for every `many_to_one` and `one_to_one` join a query can use, two joins away included, the key columns of the joined dataset's current data are checked. A warning names the key, how many values repeat and up to 5 examples. It is checked again after a refresh (the result is cached by snapshot; nothing is stored, and the refresh job is unchanged). Examples are hidden from people who may not use that dataset or whom its row-level security applies to.
- **Rules across joins:** a model with no rules of its own that joins to a dataset with rules gets a warning.
- **Never an error:** Check and Save go through. Warnings show on the model page (at the top, and after Check and Save), in the model API (get, save, restore, validate) and in MCP `get_semantic_model`, `validate_model` and `patch_semantic_model`.
- **Guide:** chapters 4, 5 and 9. The guide commit (`d92767f`) was fast-forwarded into this branch first.

### How it was verified

- **Backend:** 325 pytest tests pass (8 new, in `tests/test_model_warnings.py`: the doubled totals and the check after a refresh; composite keys, the 5 examples, empty keys and which relationships are checked; two joins away and restricted datasets; rules across joins; examples hidden from an editor on the page, Check, Save and over MCP; the MCP tools; a check that cannot run). `ruff check`, `ruff format --check` and `mypy --strict` pass.
- **Frontend:** typecheck, lint and the 48 unit tests pass.
- **Browser:** the smoke tests, with the new `e2e/tests/model-warnings.spec.ts`, pass at the root, under the sub-path and in server mode. Your stack was running on port 8080, so nothing of it was touched: the root run used this branch's own stack (project `opsis-joinwarn`, port 8091) with the same commands as `npm run smoke`, and the other two ran as `npm run smoke -- --subpath --server`. The test stacks, their volumes and image, and the test `.env` were removed afterwards.

Not done: dashboards, the models list, the model file import and `describe_dataset` show no warnings.

## After M8: leaving the editor

- Leaving the dashboard editor with unsaved changes now asks, **Stay** or **Leave without saving**, for the menu, the breadcrumb links and the Back button. Reloading and closing the tab still get the browser's own warning. A save that finishes while the question is open lets you go (`components/LeaveGuard.tsx`).
- The question needs `useBlocker`, which needs React Router's data router: `main.tsx` uses `createBrowserRouter` with one catch-all route and the same runtime `basename`, and `App` keeps its own `<Routes>`. The main chunk grew by 56 kB (17 kB gzipped).
- The Home page no longer says that semantic models and dashboards are still to come.
- Not covered: **Sign out**, and edits in the JSON tab that were never applied.
- The user guide (chapters 6 and 9) no longer says that the editor drops changes without asking.
- Verified: `npm run test:frontend`, and the smoke tests at the root, under /opsis and in server mode, with a new spec (`e2e/tests/unsaved-changes.spec.ts`). They ran in a Compose project of their own with the root on port 8090, because the laptop's stack held 8080. The first root run found a race (a way out taken just as the question was answered got no question); it is fixed, and a step in the spec reproduces it.

## After M8: legacy data that does not fit its column

The bug: a dataset from a MyISAM table (utf8mb3, non-strict `sql_mode`) failed with `Reading 'a_codes' failed: object of type <class 'str'> cannot be converted to int`. PyMySQL returns zero dates as text, and building the Arrow timestamp column failed the whole read. Fixed at the root, for every connector (details in ADR-027):

- **One conversion layer** (`opsis/connectors/convert.py`) that every connector builds its batches through: CSV, Excel, PostgreSQL, MySQL, Google Sheets and Salesforce. A value that cannot become its column's type is stored as NULL and counted, per column and reason, with up to 5 raw samples. One bad value never fails a read. Clean columns still go straight through Arrow.
- **Invalid dates** (`0000-00-00`, `2020-00-00`, `2020-05-00`, `2021-02-30`, Postgres `infinity` and dates outside years 1 to 9999) follow a new setting on every connection, **Invalid dates**: store as NULL (the default), read the connection's date columns as text, or stop the refresh.
- **The report** is kept with each snapshot (migration 0012) and is the refresh job's `dataQuality` result: per column the count stored as NULL, the reason and samples. It shows on the dataset page (**Data quality**) and in the refresh history, and MCP's `describe_dataset` and `get_job_status` return it. Samples are left out for people who may not use a restricted dataset and where row-level security hides rows from them.
- **Column types by hand** on the dataset page (**Column types**): text, whole number, decimal number, date (with a format such as `%d/%m/%Y`), date and time, or true or false. Saving rebuilds the dataset; the choices are kept when the source's columns change, and audited.
- **Suggestions:** a text column whose values are consistently dates (ISO or a common format), numbers or true/false gets a suggested type with a **Use this type** button. Nothing is applied by itself; placeholders such as `n/a`, codes with leading zeros, and columns with too few values are handled.
- **Errors** from conversion or a cursor column name the table, the column, its source type and a sample value.
- **Type fixes:** MySQL `BIGINT UNSIGNED` and `BIT(64)` are exact decimals, `BIT(n)` is a number, `DECIMAL` or `numeric` beyond 38 digits is a double, `TIME` reads as `HH:MM:SS` (also past 24 hours), Postgres intervals read as Postgres writes them, binary data shows as `0x...`. An incremental cursor ignores invalid dates.

### How it was verified

- **Backend:** 317 pytest tests pass (22 new); `ruff check`, `ruff format --check` and `mypy --strict` pass. New tests cover the conversion layer (zero, zero-day, zero-month and impossible dates; NULLs; garbage in number columns; no rounding of 1.5 into an integer column; samples capped at 5; the three invalid-date settings; suggestions; overrides with formats), a MyISAM table created in `sql_mode=''` in the sample MySQL with ENUM, SET, YEAR, BIT(1), BIT(8), BIT(64), TINYINT(1), DECIMAL(10,2), DECIMAL(65,30), BIGINT UNSIGNED, JSON, TIME and virtual and stored generated columns (the report through the API, the job and MCP; column types set by hand, refused when wrong, kept after the source gains a column; stop and text; an append cursor over zero dates), Postgres `infinity`, BC dates, year 10000 and intervals, CSV and Excel suggestions and overrides, text in Google Sheets date and boolean columns, unparseable Salesforce values, and samples hidden from someone who may not see a restricted dataset.
- **Frontend:** typecheck, lint and the 48 unit tests pass.
- **Smoke tests:** `npm run smoke` passes at the root, under the sub-path and in server mode, with a new spec (`e2e/tests/data-quality.spec.ts`): suggestions shown but not applied, one taken and a type chosen by hand, the dataset rebuilt, the report's table with reasons and samples, the count in the refresh history, and back to the source's types. The root and the sub-path were run again on the final code.
- **Your `a_codes` table:** the dataset that failed now refreshes (154 rows). Its report shows 96 values in `end_date` and 2 in `date_created` stored as NULL, all zero dates, and MCP's `describe_dataset` returns the same.

## M8 More connectors and hardening

Built (details in ADR-026):

- **Google Sheets connector:** each tab of a spreadsheet is a table, read-only. It signs in with your own Google account through your own OAuth client (`credentials.json`, Desktop or Web), or with a service account. **Connect Google account** runs the consent flow (offline access, prompt=consent, PKCE, `spreadsheets.readonly` and `drive.metadata.readonly`). On a laptop Google comes straight back to Opsis; on a server you paste the address Google sent you to. A `token.json` can be used instead. The refresh token is stored encrypted and shown to nobody, the connected account is shown with a **Reconnect** button, and you pick the spreadsheet from your list. When Google answers `invalid_grant`, the connection says it needs reconnecting, its scheduled refreshes pause, and its owner is emailed. Migration 0011. Column types come from the cells. Full refresh only.
- **Salesforce connector:** each object is a table. Sign-in through an External Client App (client credentials, recommended) or a username, password and security token. Merge refresh follows `SystemModstamp` and merges by `Id`. Test connection names the org's edition, so a Developer Edition can be confirmed before anything is read.
- **Refresh schedules** (`FEATURE_SCHEDULED_REFRESH`): presets or cron in the workspace timezone, the next runs shown before saving, at most every 15 minutes, and a **Schedules** page. Migration 0010.
- **Backups** (`FEATURE_BACKUPS`): a nightly job writes one file to the `backups` volume (database, uploads, each dataset's current data), keeps the newest 7, and **Settings** has **Back up now**. Restore is `python -m opsis restore <file> --yes` with the api and worker stopped; the previous data is moved aside. The image now carries `pg_dump` 16.
- **Clean-up job**, nightly: ended sessions, used password links, long-ended share links, old sign-in counts and old finished jobs. The audit log is kept.
- **Email** (`FEATURE_EMAIL`, SMTP): invitations and password reset links are emailed (the admin gets the link instead if sending fails), **Forgotten your password?** on the sign-in page (the same answer for any address, rate-limited), **Send me a test email** in Settings, and **Email me** on a dashboard: the dashboard's numbers on a schedule, as that person sees them, listed under Account. Messages are never stored.
- **Lenient agent inputs** (from the M7 follow-up): the MCP tools and the assistant read a single value as a list of one where a list is expected, worked out from the schemas, and log each one at debug level (`opsis.agent.lenient`). Stored specs, the REST API and the JSON tab stay strict.
- **Models as a folder:** **Export all** (a zip of `models/<dataset>.yml`) and **Import** on the Semantic models page: every file checked first, nothing saved unless all are valid, unchanged models left alone.
- **Two-hop joins:** a dimension two joins away (gifts to donors to regions), every join many-to-one or one-to-one, never through a restricted dataset; two ways to one dataset need a direct join to say which.
- **Drag and drop** of fields into a visual's roles, and between roles, in the editor. The menus stay for the keyboard.
- **Demo data:** **Settings**, **Load demo data**, or `python -m opsis seed-demo`: three made-up datasets with their models and joins, and a published dashboard that uses a field two joins away.
- **SQL Lab is out of v1** (your M8 instructions): `FEATURE_SQL_LAB`, its permission and its menu entry are gone, and it is on the post-v1 backlog.
- **Docs:** the README (demo data, Google Sheets and Salesforce set-up, schedules, backups and restore, email), the Windows guide, the Proxmox guide (backups on NFS, restore, email, an install checklist), ADR-026, an architecture section, and `BACKUP_*` and `SMTP_*` in both env templates.

### How M8 was verified

- **Backend:** 295 pytest tests pass (42 new); `ruff check`, `ruff format --check` and `mypy --strict` pass. New tests cover: both connectors against realistic fakes of the Google and Salesforce APIs (types, header rows, sign-in failures, merge refresh with the repeated second, append refused); schedules (presets, cron checks, the next runs across the end of daylight saving, the tick, no make-up runs, flags); backups (a real `pg_dump` and `pg_restore` into a separate database, retention, the manifest) and the clean-up job; email against a local SMTP server (invitations and resets by email and when sending fails, the same answer for unknown addresses, the rate limit, the test email, emailed dashboards with row-level security, escaping and the removal of a subscription when access ends); lenient inputs; the models folder (export, unchanged re-import, all-or-nothing, dry run, bad files); two-hop joins in the compiler and through the API, with ambiguous, unsafe, restricted and three-hop paths refused; the demo data locally and in server mode. Google Sheets through your own OAuth client is tested against a fake Google token endpoint on a local port, which checks the PKCE verifier, renews tokens, revokes them and answers tokeninfo. The tests cover: the consent address (offline access, prompt=consent, S256, both scopes); finishing straight back in local mode and by a pasted address on a server; a Web client's redirect URIs; unticked scopes; the wrong kind of file; token.json (with and without its scopes, from another client, revoked); the refresh token never in an answer or in plain text; invalid_grant marking the connection, emailing its owner once, pausing its schedule and resuming it after a reconnect; and a new OAuth client ending the old sign-in.
- **Frontend:** `tsc`, ESLint and 48 Vitest tests pass.
- **Browser (`npm run smoke`), all passed** at http://localhost:8080, at http://localhost:8088/opsis/ and in server mode. New: a dataset's refresh schedule (a preset, its next runs, a too-frequent cron refused), the Schedules page and Settings; dragging a field into a role in the editor; and in server mode, against a stand-in mail server whose messages the test reads (`e2e/fake-smtp.mjs`): an invitation by email whose link sets the password, **Email me** and stopping it from Account, **Forgotten your password?** through to a new password, the test email, a real backup made by the worker, and the demo data with its dashboard. Also, at all three, Google Sheets through your own Google account against a stand-in Google (`e2e/fake-google.mjs`). At the root Google comes straight back to Opsis; under /opsis and in server mode the test pastes the address. Then it picks a spreadsheet from the list and refreshes it, the sign-in is revoked (invalid_grant), the connection asks to be reconnected (in server mode its owner is emailed), and it is reconnected and refreshes again. The first run found two problems, both fixed: the Settings page stopped looking for a finished backup or demo after its first check (so a slow job never showed as done), and a wrong button name in the new test.
- **Fresh install from the README:** see below.

### Performance, 10 million rows

`node scripts/benchmark.mjs --rows 10000000` against your stack on this laptop (Docker Desktop, 16 CPUs, 15.4 GB for containers, default query settings, inline job runner). The new step opens a typical dashboard as a browser does: 8 visuals (4 KPI cards with a comparison to the year before, a line by month and channel, a pie, a bar, a table through a join), so 12 queries, 6 requests at a time, with a date range and a region filter.

| Step | Result |
|---|---|
| Ingestion, 10.2 million rows from Postgres to Parquet | 32.2 s (322,000 rows/s); snapshot 147 MB; api peak 290 MB |

| Dashboard load | Total | Slowest item | Cached |
|---|---|---|---|
| Cold: first open | 492 ms | 380 ms | 0 of 8 |
| **Warm: opened again** | **81 ms** | 65 ms | 8 of 8 |
| After a filter change (two regions) | 516 ms | 381 ms | 0 of 8 |
| That filter, warm | 92 ms | 71 ms | 8 of 8 |

**The brief's target, under 1.5 s warm on 10 million rows, is met: 81 ms warm, and 0.5 s even with nothing cached.**

| Query | Cold | Warm | Rows |
|---|---|---|---|
| KPI: total, count, distinct donors, last 12 months | 198 ms | 15 ms | 1 |
| Bar: total by fund, two regions | 84 ms | 15 ms | 8 |
| Line: total by month and channel, 3 years | 114 ms | 13 ms | 148 |
| Join: by donor tier and financial quarter | 296 ms | 13 ms | 39 |
| Table: top 100 of 200,000 donors | 185 ms | 12 ms | 100 |

- Ten different uncached queries at once finished in 3.4 s and 2.6 s (two rounds), while `/healthz` answered in a median of 16 and 12 ms. Peak api memory during the queries: 422 MB.
- At twice the M3.5 size, single queries are about 1.2 times slower and ingestion runs at the same speed per row.

Not measured: the Proxmox LXC (fewer cores, less memory), slow disks, worker mode, and many people at once.

### Fresh install from the README

On this laptop, from a clean clone from GitHub (the `m8-wip` branch, before the merge) into an empty folder, following the README alone. Two test-only differences, because your own stack holds port 8080 and the project name `opsis`: ports 8090 and 8091, and the Compose project name `opsis-fresh`, so it had volumes of its own. All of it was removed afterwards (containers, volumes, image and folder).

- **Laptop template:** `copy .env.local.example .env`, then `docker compose up -d`. The image built from scratch, with no cache, in 64 s; `config-check` reported a valid configuration and the api became healthy. `docker compose exec api python -m opsis seed-demo` loaded the demo data. The browser tests that fit the default settings passed against it (4 of 4): building a dashboard, local mode, MCP with live updates, and access tokens.
- **Server template:** `cp .env.server.example .env`, the key from `generate-key`, and every `CHANGE` value filled with test values (`FEATURE_ASSISTANT=false`, for want of a Claude key). Postgres, the api and the worker became healthy, serving at `/opsis`. The first sign-in asked for a new password, and the nightly backup and clean-up were scheduled. The worker loaded the demo data (all 8 visuals with rows) and made a backup; `python -m opsis backup` wrote another. After the demo dashboard was deleted, the README's restore (stop, `restore <file> --yes`, start) brought it back with its data and moved the previous data aside, and the admin's password was again the one from the time of the backup.
- Not covered here: TLS, a real reverse proxy, Google sign-in, real email, and Linux itself. The server-mode smoke test covers a proxy, a fake Google and a stand-in mail server; the rest is the Proxmox install check.

### Your stack

- **Test leftovers deleted**, as you approved: the ten check dashboards ("Real model check" four times, "Ollama check 1" to "3" twice each), and "Smoke mwh0pics" with its dataset and connection.
- **The demo data is loaded** (with `python -m opsis seed-demo`): **Giving overview (demo)** and the datasets `demo_gifts`, `demo_donors` and `demo_regions`.
- Left as they were: "Gifts overview", "Giving overview (built over MCP)", and **"Test1"** (made today at 17:04, not one of mine, so not on the list).
- Your `.env` still has `FEATURE_SQL_LAB=true`. It is now ignored, so the line can go.

Not verified:

- Salesforce and Google Sheets against the real services (they need your credentials; see the next step).
- A real mail server (only the stand-in and a local SMTP server in pytest), and how emailed dashboards look in Outlook and Gmail.
- The Proxmox install, backups on NFS, the real reverse proxy and Google sign-in: your part, the checklist in `deploy/proxmox/README.md`, section 11.
- Restoring a backup made by an older version.

## M7 In-app assistant (reviewed)

Built in M7 (ADR-025): the Assistant tab in the editor, on Claude (the `anthropic` SDK, adaptive thinking, prompt caching, refusal fallbacks) or Ollama (its own chat API, with the context size sent on every request), a tool loop over 17 of the MCP tools with budgets and Stop, and conversations kept in Postgres. At the M7 review: admin-set restrictions became admin-only, Ollama moved to its native API, and the real Ollama check passed 3 of 3 builds (2 min 21 s to 3 min 4 s, no dropped tool calls). The real Claude check was skipped, as agreed.

## M6 Auth and sharing (reviewed)

Built in M6 (ADR-023): sign-in with local accounts and Google, sessions and lockouts, people and groups, access lists for dashboards and datasets, share links, View as, view state in the URL, the audit log screen, tokens on the REST API, the MCP sharing tools, and the Proxmox and Windows guides. Verified with 231 backend tests, 42 frontend tests and the smoke tests at the root, the sub-path and in server mode. Real Google sign-in and the real server are still to check: that is now a checklist in the Proxmox guide, part of the M8 install check.

## M5 MCP (reviewed)

Built in M5 (ADR-022): the MCP server at `/mcp` (stateless, JSON), tools written for small models, JSON Patch with `base_version`, personal access tokens, audit of agent writes, live editor updates over server-sent events, the Playwright smoke tests and the combo chart's right-hand axis.

M3.5 benchmark, in one line: 5 million rows on this laptop, typical queries 77 to 250 ms cold and 11 to 14 ms warm; 10 uncached queries at once finished in 3.4 to 3.6 s; peak api memory 1.1 GB (full tables below).

## Review outcomes so far

- **V2 (the workspace model and the Model view):** approved, ADR-039 accepted. V2-Q2: a small read-only "v1 history" link on each table's page, with no restore from it. The assistant may propose model changes (new measures, relationships, table and field properties) as a diff that you review and apply with one click: nothing changes until you click Apply, and the change is saved as a model version authored by the assistant. Both at the start of V3. V2-Q3, the migration of your stack, in this order: back it up; prove the backup restores in a separate scratch Compose project (it starts, and the dashboard count matches), then remove that project; write down the exact rollback steps here; rebuild your stack with V2 and run `results-digest --compare` against the saved file; show you the summary. If anything in the comparison was not predicted, stop and show you; otherwise continue with V3.
- **V2 step 1 (the upgrade check):** approved, with the V1 review's fixes. ADR-038 accepted: the check covers every person and every share link. V2-Q1 agreed: a loop of joins is refused when a rule is on the loop or reachable from it; explicit path selection for that case goes to the backlog. The three old worktrees go if they hold nothing unsaved (they held nothing: removed). Then the report runs on your stack: V2 continues if nobody sees fewer rows and nothing needs fixing, and stops to show you otherwise. (It was clean, so V2 continues.)
- **V1 (bring your own agent):** approved, ADR-036 accepted. The free model-list Test stays the default, plus an optional "Send a test message" (one short billed request) that shows whether the account can pay. A choice that is taken away asks the person to choose again. Switching personal keys off keeps the keys, and people can still delete their own. Usage stays in tokens; estimated cost per provider goes to the backlog. Before V2: test isolation, so that no test can reach your stack (every test stack with its own Compose project, an explicit env file, its own ports, never `.env`; the scripts refuse port 8080 and the project `opsis` with a clear error; a test that proves the guard). All done (ADR-037).
- **V0 (v2 plan):** approved, ADR-030 to ADR-035 accepted. Row-level security through relationships applies to migrated models, after a report of which dashboards and which people will see fewer rows is shown and approved. Measures from several tables and measures that name a relationship are in V2; many-to-many is on the backlog. Replacement rules on a transform: anyone allowed to edit the input's rules, except where an admin set the input's rule or restriction (then admins only); every replacement audited, with a clear warning when it gives wider access. The renderer container for PDF and PNG. The security rules' tests must exist and pass before V2 and V3 count as done. Pages in V4. Export cap 25 MB by default, adjustable by admins.
- **M0:** keep psycopg; `Australia/Melbourne`, Monday weeks, financial years in v1 (ADR-012); Apache 2.0; deviations ADR-005, 006 and 007 approved.
- **M1:** brief replaced with the current version (sub-path hosting, section 3.1); ADR-013 superseded by ADR-016; `FISCAL_YEAR_LABEL` added; one-shot config check; placeholder secrets refused in server mode; `httpx2` kept.
- **M2:** keep the built-in schema form for connections; at the start of M4 prototype rjsf with a CSP-safe validator and decide then (ADR-018). `BIND_ADDRESS` stays `0.0.0.0` in the server template and is not a required value. Editors may see connections and their non-secret settings.
- **M3:** approved with M3.5 (timezones, conditional measures, model versions, value checks, benchmark; all done). Admins stay exempt from RLS by default, with a visible badge (done in M4), share links never exempt and "View as" in M6. Bulk model export and two-hop joins go to M8. Every milestone is verified at the root and under /opsis.
- **M4:** approved. Field selector is fine for v1 (drag-and-drop to the M8 backlog). Playwright smoke test at the start of M5, at both URLs, replacing manual browser checks from then on. Combo chart gets an optional right-hand axis per measure. Filter and drill state in the URL in M6 with share links. MCP tool descriptions and errors must work for a smaller model: anything Sonnet struggles with counts as a tool design issue.
- **M5:** approved. REST previews apply the RLS filter, or refuse where a rule cannot be applied. Access tokens on the REST API in M6 with the MCP scopes. Keep MCP stateless. In M6, verify `MCP_AUTH=token` against the running container in server mode through the proxytest profile (done).
- **M6:** approved. Dataset owners restrict their datasets; admins can override, and only an admin lifts an admin's restriction. Sharing is limited to a dashboard's owner and admins. Restricted data must not reach people outside its list through a dashboard, with warnings in the share dialog, and the same rules in the MCP sharing tools. Real Google sign-in becomes a checklist in the Proxmox guide and part of the M8 install check. The Sonnet test has not been run yet. (All done at the start of M7.)
- **M7:** approved. Admin-set restrictions are admin-only, list included. The real Claude check is skipped for now. Ollama moves to its own chat API with the context size set per request (`OLLAMA_NUM_CTX`, default 32768), keeping one visual per call; then the test alias goes and `OLLAMA_MODEL=gemma4:12b`. Refusal fallbacks stay on by default. (All done.)
- **M7 follow-up and M8 instructions:** agent tool inputs accept a single value where a list is expected (stored specs, REST and the JSON tab stay strict; each normalisation logged at debug level). Salesforce is tested against a Developer Edition org only, never a production org, with credentials you provide when needed; for Google Sheets I say when a service account is needed and what access it needs. The Proxmox install and the real Google sign-in are the final install check, done by you from the checklist. SQL Lab is not built for v1 (post-v1 backlog). The test leftovers in your stack are deleted, keeping the demo data. (Done, apart from the real Salesforce and Google Sheets runs and the Proxmox check: see the next step.)
- **For the LXC work (noted at the M2 review):** the server will use port forwarding to the LXC. Check then what source address the api sees for proxied requests, because that is the address `TRUSTED_PROXIES` must list (it may be the forwarder or the Docker gateway, not the reverse proxy itself).

## What exists

Run: `copy .env.local.example .env`, `docker compose up -d`, open http://localhost:8080, then Settings, Load demo data. Add `COMPOSE_PROFILES=sample` for a sample MySQL source (see README).

**v2 plan (V0)**
- `docs/V2_DESIGN.md`, ADR-030 to ADR-035, and the draft schemas with examples in `spec/drafts/v2` (not read by the app), checked by `backend/tests/test_spec_drafts.py`.

**Workspace model (V2)**
- One workspace model (ADR-031, ADR-039): `semantic/workspace.py` (the document, starter tables, checks), `services/workspace_model.py` (read the part a person may use, save, JSON Patch, versions, diff, restore), migration 0014, `spec/workspace-model.schema.json`, REST `/api/model` (`api/workspace_model.py`). The v1 per-table routes and MCP tools are a view of it (`services/models.py`).
- Relationships in queries: `semantic/relationships.py` (paths, loops), `semantic/compiler.py` (fields and measures of several tables, filters as semi-joins, a measure's `relationship`), `semantic/security.py` (R1 and R2 for every read).
- Help with relationships (`services/model_suggestions.py`), date tables (`connectors/date`, `services/date_table.py`), model warnings per relationship (`services/model_warnings.py`), YAML (`services/model_yaml.py`).
- The Model view: `pages/ModelView.tsx`, `components/model/` (the React Flow canvas, side panels, YAML, history), `lib/workspaceModel.ts` and `lib/modelGraph.ts`.
- The upgrade check: `python -m opsis results-digest` (`services/results_digest.py`, ADR-038), and `--compare` after an upgrade.

**Platform (M1 and catch-up)**
- `compose.yml` profiles: `server` (worker), `caddy`, `tunnel`, `ollama`, `proxytest`, `sample`. A one-shot `config-check` service gates `api` and `worker`.
- `opsis/settings.py`: all flags, fail-fast rules (ADR-014), `base_path` derived from `PUBLIC_BASE_URL`, `TRUSTED_PROXIES`.
- `opsis/main.py`: everything mounted under the base path; `index.html` served with a runtime `<base href>` and JSON config block; security headers; CSRF check (`opsis/auth/cookies.py`).
- `opsis/auth/principal.py`: `Principal`, roles, permissions. `AUTH_MODE=none` uses a built-in admin. Other modes return 401 until M6.
- `opsis/api/sse.py`: SSE helper for use behind proxies (first real stream in M5).
- Tests: `npm test`, `npm run smoke`, `npm run test:backend`, `npm run test:scripts`. Every test stack has its own Compose project, env file and ports, and `scripts/test-stack.mjs` refuses yours (ADR-037).

**Data ingestion (M2)**
- Connectors: `opsis/connectors/file` (CSV, Excel), `postgres`, `mysql`, with `registry.py`, `sql_base.py` and `tabular.py`. Drop-in connectors go in `plugins/connectors/<id>/`.
- `opsis/services/`: `connections.py`, `datasets.py`, `refresh.py` (the refresh job), `crypto.py` (Fernet).
- `opsis/storage/snapshots.py`: Parquet snapshots for full, append and merge refresh; preview; cleanup.
- `opsis/jobs/`: `queue.py` and `worker.py` (`run_one`, the worker loop, and `InlineRunner`).
- `opsis/api/ingestion.py`: connectors, connections, uploads, datasets, jobs.
- Frontend: Connections, Datasets, dataset page, Jobs.

**Semantic layer (M3)**
- `spec/semantic-model.schema.json`: the model format (a fourth spec file).
- `opsis/semantic/`: `model.py` (parse, validate, starter models), `expressions.py` (the allow-list for author-written SQL), `compiler.py` (semantic query to DuckDB SQL through a sqlglot tree, with RLS), `calendar.py` (grains, ranges, fiscal years).
- `opsis/query/duckdb_engine.py`: `DuckDBEngine` (per-query connection, memory limit, timeout) and `ResultCache` (LRU).
- `opsis/services/models.py` (storage, YAML import and export, field lists) and `query.py` (validate, compile, run through the cache).
- `opsis/api/semantic.py`: `GET /api/models`, `GET|PUT /api/models/{dataset}`, `/validate`, `/starter`, `/export`, and `POST /api/query` (with `previewAs` for admins and `includeSql`).
- Migration `0003`: `semantic_models`.
- A starter model is generated at a dataset's first refresh and follows its columns until someone edits it.
- Frontend: Semantic models list; a model page with a query builder (measures, group by, date grain and range, one filter, segments, limit, "Run as" for RLS) and a YAML editor with Check, Save, Discard, Regenerate and Download.

**Dashboards (M4)**
- `opsis/dashboards/`: `spec.py` (validation, query derivation), `manifests.py`. `opsis/services/dashboards.py`, `opsis/api/dashboards.py`. Migration `0005`.
- `frontend/src/visuals/<id>/`: manifest and renderer for each of the nine visuals; `registry.ts`; shared `Cartesian.tsx`.
- `frontend/src/lib/`: `dashboard.ts` (types and API hooks), `chartOptions.ts`, `theme.ts` (palette), `history.ts`, `diff.ts`, `markdown.tsx`, `schemaValidator.ts`.
- `frontend/src/components/dashboard/`: `Canvas`, `ItemFrame`, `FiltersBar`, `EditorPanels`, `MonacoJson`; `components/ConfigForm.tsx` (rjsf), `components/EChart.tsx`.
- Pages: `Dashboards`, `DashboardPage`, `DashboardEditor`.

**MCP and agent writes (M5)**
- `opsis/mcp/`: `tools.py` (the 21 tools and `call`), `server.py` (endpoint, authentication, resources, prompt), `guide.py` (instructions, guide and prompt text).
- `opsis/dashboards/patch.py` (JSON Patch), `opsis/services/tokens.py`, `opsis/services/events.py`, `opsis/api/tokens.py`. Migration `0006`.
- Frontend: `pages/Tokens.tsx`; `useDashboardEvents` in `lib/dashboard.ts`; the remote-change notice in `DashboardEditor`.
- `e2e/` (Playwright tests and helpers), `scripts/smoke.mjs`, `compose.subpath.yml`, `.mcp.json`.

**Sign-in and sharing (M6)**
- `opsis/auth/`: `passwords.py` (argon2id), `google.py` (OpenID Connect), `principal.py` (groups, scopes, session).
- `opsis/services/`: `accounts.py` (first admin, invitations, password sign-in, Google matching, admin changes), `sessions.py`, `lockout.py`, `access.py` (dashboard and dataset access, sharing, groups), `links.py` (share links), `crypto.py` (sealed cookies).
- `opsis/api/`: `auth.py`, `admin.py` (users, groups, audit), `sharing.py` (access lists, links, the public side); `deps.py` (sessions, tokens, scopes). Migration `0007`. CLI `reset-link`.
- Frontend: `pages/Login`, `SetPassword`, `Account`, `Users`, `Audit`, `PublicDashboard`; `components/dashboard/SharePanel`, `components/DatasetAccessPanel`; `lib/access.ts`, `lib/urlState.ts`.
- `e2e/auth.spec.ts`, `e2e/local.spec.ts`, `e2e/global-setup.ts`, `e2e/fake-oidc.mjs`, `compose.servertest.yml`; `deploy/proxmox/README.md`, `deploy/windows/README.md`.

**In-app assistant (M7)**
- `opsis/assistant/`: `providers.py` (Claude and Ollama, streamed), `loop.py` (the tool loop and its budgets), `prompt.py` (instructions and the context block), `runs.py` (requests in progress, Stop).
- `opsis/services/assistant.py` (conversations, history, entries), `opsis/api/assistant.py` (the panel's routes). Migration `0009`. `opsis/mcp/guide.py` now shares its spec reference (`SPEC_GUIDE`) with the assistant.
- Frontend: `components/dashboard/AssistantPanel.tsx`, `lib/assistant.ts` (state, the event stream, the live view), the Assistant tab in `DashboardEditor`.
- `e2e/fake-llm.mjs`, `e2e/tests/assistant.spec.ts`, `compose.fakellm.yml`.

**Connectors, schedules, backups, email (M8)**
- Connectors: `opsis/connectors/gsheets`, `opsis/connectors/salesforce`; `x-file-text` in the schema form reads a key file into a field.
- `opsis/services/`: `schedules.py` (cron, presets, the tick), `backups.py` (backup and restore), `maintenance.py` (the clean-up job), `mail.py` (SMTP and the messages), `subscriptions.py` (emailed dashboards), `model_files.py` (the models folder), `demo.py` (the demo data). Migration `0010` (`schedules`).
- `opsis/api/operations.py`: schedules, backups, the test email, emailed dashboards and the demo data. `POST /api/auth/forgot-password`. `GET|POST /api/model-files`. CLI `backup`, `restore`, `seed-demo`.
- `opsis/mcp/lenient.py`; two-hop joins in `opsis/semantic/compiler.py` with `reachable` in `opsis/semantic/model.py`.
- Frontend: `pages/Schedules`, `pages/Settings`, `components/ScheduleCard`, `components/dashboard/EmailPanel`, drag and drop in `EditorPanels`, import and export on `pages/Models`, `lib/operations.ts`.
- `e2e/fake-smtp.mjs`, `e2e/tests/operations.spec.ts`; `scripts/benchmark.mjs` times a dashboard too.

## M3.5 (from the M3 review)

Built (details in ADR-020):

- **Timezones:** timezone-aware timestamps are stored as UTC instants and converted to `WORKSPACE_TIMEZONE` before grouping, ranges and filters. Naive timestamps are workspace-local unless the dimension sets `timezone`.
- **Conditional measures:** `filter` on an aggregate measure, compiled to `agg(...) FILTER (WHERE ...)`, with segments usable by name.
- **Model version history:** immutable versions, author and message, restore, and `baseVersion` conflict detection. A "Version history" panel on the model page.
- **Values:** every query value is type-checked before rendering; new injection tests with quotes, backslashes, comment markers, unicode and null bytes (21 hostile strings, each matched exactly as data and as an RLS attribute).
- **Concurrency:** queries run in the thread pool, at most `QUERY_MAX_CONCURRENT` (default 4) at a time.
- **RLS exemptions:** reported as `rlsBypassed`, with a badge in the query builder; `may_be_exempt=False` for future share links.

Verified: 204 backend tests pass (12 new), lint and strict types clean; 17 frontend tests pass. Checked in the Browser pane at http://localhost:8080 and at http://localhost:8088/opsis/ (save with a note, version list, restore, stale save refused with 409, conditional measure equal to the filtered total, timezone conversion present in the SQL).

### Benchmark, 5 million rows

`node scripts/benchmark.mjs --rows 5000000`, on this laptop: Docker Desktop with 16 CPUs and 15.4 GB of memory available to containers, default settings (`QUERY_MAX_CONCURRENT=4`, `QUERY_THREADS=4`, `QUERY_MEMORY_LIMIT_MB=1024`), inline job runner. Source: a synthetic `gifts` table (8 columns, 3 years of dates, 200,000 donors) in Postgres, plus a 200,000 row `donors` table.

| Step | Result |
|---|---|
| Ingestion, 5.2 million rows from Postgres to Parquet | 18.5 s (about 300,000 rows/s); snapshot 74 MB |
| Peak api memory during ingestion | 264 MB |

| Query | Cold | Warm (cached) | Rows |
|---|---|---|---|
| KPI: total, count, distinct donors, last 12 months | 138 ms | 11 ms | 1 |
| Bar: total by fund, two regions | 77 ms | 12 ms | 8 |
| Line: total by month and channel, 3 years | 96 ms | 13 ms | 148 |
| Join: total and conditional total by donor tier and financial quarter | 250 ms | 13 ms | 39 |
| Table: top 100 of 200,000 donors by total | 197 ms | 14 ms | 100 |

Times are wall-clock for the HTTP request. All are well inside the brief's target of 1.5 s warm for a typical dashboard, at half the brief's 10 million rows.

| 10 different uncached queries at once | Round 1 | Round 2 |
|---|---|---|
| All ten finished in | 3.6 s | 3.4 s |
| Per request: fastest, median, slowest | 1.4, 2.7, 3.6 s | 1.3, 2.6, 3.4 s |
| Longest single engine time | 1.4 s | 1.4 s |
| `/healthz` during the burst: median, worst | 20 ms, 104 ms | 29 ms, 93 ms |

- Peak api memory during the query phases: 1,138 MB (four queries running at once).
- The health check stayed fast during the bursts, which confirms the queries are not blocking the event loop. Requests beyond the four slots waited their turn, which is why the slowest request took as long as the whole burst.
- Each concurrent query was slower than the same kind of query alone (about 1.4 s against 0.1 to 0.25 s), because four queries each using four threads compete for CPU, and these burst queries are heavier (week grain, distinct donors, a HAVING filter).

Not measured: 10 million rows, the Proxmox LXC (fewer cores and less memory than this laptop), slow disks, and worker mode.

## How M3 was verified

All on the Windows laptop with Docker Desktop.

- **Backend:** 192 pytest tests pass; `ruff check`, `ruff format --check` and `mypy --strict` pass. New in M3 (73 tests):
  - Every aggregation, expression measures and nested formulas, compared with results computed in plain Python over 400 generated rows.
  - All eight grains against the calendar rules, plus a sweep of 900 consecutive dates across both week starts and six fiscal start months, for date and timestamp columns.
  - Relative and absolute ranges, every filter operator, measure filters, segments, sort, limit, offset, truncation and the server row cap.
  - Joins, and refusal of fan-out joins, missing joins and measures from a joined dataset.
  - RLS: per attribute, lists and scalars, several rules, roles, `user.email` and `user.role`, rules on joined datasets, eight ways an attribute can be missing or unusable (all deny), and a broken rule blocking instead of failing open.
  - Injection: 23 rejected expressions (subqueries, `read_csv`, `getenv`, semicolons, window and aggregate functions, parameters, unsafe casts and more), SQL inside filter values and inside user attributes, hostile field references, unsafe YAML tags.
  - Model validation paths, starter models, engine timeout, cache keys and eviction.
- **Acceptance, automated (`tests/test_query_api.py`):** datasets loaded from a real Postgres source through the API; semantic query results equal hand-written SQL run on the source for (1) measures by dimension with a filter and a segment, (2) financial quarters joined to another dataset over a date range, (3) a relative financial-year range. RLS preview returns exactly the rows for each attribute set.
- **Acceptance, in the Browser pane:** on the sample `gifts` dataset I edited the model as YAML (the editor reported three deliberate mistakes with their paths, then saved the corrected model with a join and an RLS rule), ran total amount and gift count by fund, and got the same five totals as `SELECT fund_name, SUM(amount), COUNT(*) ... GROUP BY fund_name` on the MySQL source. A query grouped by donor region and financial quarter for "Last financial year" returned Q1 to Q4 FY2026. "Run as a viewer" with `{"funds": ["Library", "Sport"]}` returned those two funds only, and with `{}` returned no rows.
- **Frontend:** `tsc`, ESLint and 17 Vitest tests pass; the production build succeeds.

Three bugs were found by this testing and fixed: YAML reading `on:` as a boolean; the database column type re-sorting model keys; and a save that only reordered fields being skipped.

Not verified:

- Query speed or memory on large data. The largest query ran over 5,000 rows. The 10M-row target is an M8 check.
- The query builder at the `proxytest` sub-path. The pages use the same API client and router as the M2 screens that were tested there, but I did not repeat the browser run under `/opsis`.
- Queries in worker mode specifically (queries run in the api process in both modes, so nothing differs, but it was only exercised in inline mode).
- Concurrent queries under load.
- Everything already listed as unverified for M1 and M2: the Proxmox LXC and real proxy, https end to end, the `caddy`, `tunnel` and `ollama` profiles, dev mode after recent changes, MariaDB and other server versions, and the Mermaid diagrams in a renderer.

## Deviations from the brief

- ADR-005, 006, 007 (approved).
- ADR-016: CSRF protection brought forward from M6.
- ADR-017: the CSV and Excel connector id is `file`, not `csv`.
- ADR-018: connection forms use a small built-in schema form; visual config panels use rjsf with our own CSP-safe validator. shadcn/ui is still not added.
- ADR-019: a fourth spec file; query values are SQL literals rendered by sqlglot (approved); the model editor is still a plain text area.
- ADR-021: field wells were selectors (drag and drop came in M8, ADR-026); cross-filter and drill state live in the view (now also in the URL), not in the spec; the chart palette and mark rules are fixed in code.
- ADR-022: the MCP endpoint is stateless with JSON responses (kept at the M5 review); JSON Patch is our own code.
- ADR-023 (new, M6): `ADMIN_INITIAL_PASSWORD` is optional (Google or `reset-link` instead); Google's ID token is verified with `joserfc` (Authlib's JOSE library) and the HTTP calls use the standard library; `GOOGLE_DISCOVERY_URL` exists for testing; invitations and resets are links the admin passes on unless email is on (M8); the audit log page is available in local mode too.
- ADR-025 (new, M7): the assistant uses 17 of the 23 MCP tools (no creating, publishing or sharing from the panel); only people in the editor can use it, not access tokens; Ollama through its own chat API (`/api/chat`, with `num_ctx` on every request) rather than the OpenAI-compatible endpoint the brief names (decided at the M7 review); our own tool loop rather than the SDK's beta tool runner, so one loop serves both providers.
- ADR-026 (new, M8): Google Sheets signs in with your own OAuth client (installed-app style, with a pasted address where Google cannot come back) or a service account, through its REST API with `google-auth` for the token, not the Google API client library; emailed dashboards carry numbers and tables, not pictures of charts; SQL Lab is not in v1 (your decision); unknown non-GET `/api` paths answer 404.
- ADR-015: the task runner is npm scripts, not `tasks.py`; `httpx2` for the test client (approved), and now also for Ollama.
- **v2, proposed in V0:** the draft schemas are in `spec/drafts/v2`, not edited in place in `spec/` (the app reads `spec/`, and V0 changes no behaviour); ADR-035: PDF and PNG from an optional renderer container rather than the worker container (local mode has no worker).
- **v2, proposed in V1 (ADR-036):** "deleting a user deletes their keys" happens when someone is deactivated, since Opsis deactivates people rather than deleting them (the foreign key cascades as well); in local mode the one built-in admin may always use the workspace key, as there is nobody to grant it to; the key's Test lists its models rather than sending a message, which is free but cannot tell whether the account has credit (kept as the default at the review, with an optional "Send a test message" beside it).
- **v2, agreed at the V2 step 1 review (ADR-038):** the upgrade check runs as every active person and every live share link, not only the people who have attributes (`V2_DESIGN.md` section 1.4), since a rule can also use the role or the email; a loop of relationships is refused when a rule is on it or reachable from it (V2-Q1).
- **v2, proposed in V2 (ADR-039):** a table's page shows the workspace versions in which that table changed; the v1 per-dataset history stays in the database, unchanged, but is no longer shown (the drafts README said it would stay readable; V2-Q2). The in-app assistant gets `get_workspace_model` and `suggest_relationships` but not `patch_workspace_model`, and keeps v1's `patch_semantic_model` for one table. The per-table pages stay, at `/models`, beside the Model view. The YAML tab highlights YAML but leaves checking to the server's Check (no YAML language worker).

## Open issues and notes

- No TOTP second factor (a later item in the brief).
- "View as" shows a person's data, not whether they may open the dashboard.
- A dashboard's owner cannot be changed in the UI, and a deleted owner leaves admins and existing shares as the only way in.
- Share links show filters but no "reset filters" control; reloading without `?s=` resets them.
- Lockouts can be used to lock someone out for a while by guessing their email; they back off from one minute to an hour. The per-address limit only works if the api sees real client addresses, which depends on `TRUSTED_PROXIES` being right.
- The MCP SDK is on 2.3, a new major version, used through its low-level API.
- The live event hub lives in the single api process; several processes would need Postgres LISTEN/NOTIFY (R5).
- A right-hand axis showing counts can have decimal tick labels.
- Bundle size: main chunk 962 kB (about 289 kB gzipped): 914 kB before V2, whose Model page panels added 48 kB; ECharts (562 kB), Monaco (3.4 MB) and the Model view's canvas (React Flow and dagre, 304 kB) are lazy.
- Queries reach related tables in any number of steps, but a dashboard's filters keep v1's reach (at most two relationships from an item's table) until V4 migrates dashboards. The query builder has one filter row.
- A repeated join key is found by comparing the values as stored: two that differ only after the join casts one side's type are not seen.
- Merge refresh does not remove rows deleted in the source (Salesforce included) until a full rebuild. One job runs at a time, so a long refresh delays a scheduled backup or email. Datasets cannot be edited after creation.
- Google Sheets refresh in full only (a sheet has no change marker). Dates in sheets must be formatted as dates to be read as dates. With an OAuth app in Testing status, Google ends the sign-in after 7 days (reconnect, or publish the app). Picking a spreadsheet from the list needs the Google Drive API enabled in the OAuth client's project.
- Emailed dashboards show numbers and short tables, not chart pictures, and use each filter's default.
- A schedule run missed while the stack was down is not made up.
- Backups on NFS need the export to let uid 110001 write (an unprivileged LXC's view of the containers' uid 10001).
- Assistant conversations are kept until their dashboard or their person is deleted; there is no clean-up or export yet.
- A request to the assistant runs in the api process (R5): a restart stops it (Stop is pressed for it, and after 10 seconds it is cancelled). The next request carries on with the conversation.
- A local model is slower than Claude: `gemma4:12b` builds a three-visual dashboard in 2.5 to 3 minutes on this laptop. After a dropped tool call it can think for several minutes, and one such build ran out of the 10-minute budget (`ASSISTANT_TIMEOUT_SECONDS`).
- Risks still open: R2 (DuckDB memory in the api process), R3 (snapshot rewrite cost), R5 (single api process), R8 (hot reload on the Windows filesystem). R10 (share links on RLS datasets) is closed: links deny by default, never inherit an exemption, and cannot exceed their maker's access.

## Backlog agreed at reviews

- **M8:** bulk export and import of a `models/` folder; two-hop joins; the 10 million row check; drag-and-drop of fields into wells; email for invitations, resets and snapshots; a clean-up job for ended sessions and links. (All done.)
- **Post-v1 (decided at M8):** SQL Lab, the admin-only read-only SQL page (`FEATURE_SQL_LAB` in the brief).
- **Later (decided at V2 step 1):** explicit path selection for a loop of relationships that a rule could travel round: the model's author says which path the security filter takes, instead of the loop being refused.
- **Agreed at the V2 review, for the start of V3:** a read-only "v1 history" link on each table's page (`semantic_model_versions`, no restore); the assistant proposing model changes as a diff that you apply with one click, saved as a version by the assistant.
- **Later (decided at V1):** estimated cost per provider beside the tokens on the Account page (prices differ per model and change, so they need a table to keep up to date).
- **Later (brief):** OAuth for the MCP server so it can be a claude.ai connector; TOTP second factor.

## Questions for review

The V1 questions were answered at its review (see "Review outcomes so far").

V2-Q1 was answered at the step 1 review, V2-Q2 and V2-Q3 at the V2 review (see "Review outcomes so far").

From v1, still open:

- **Q1. Emailed dashboards carry numbers, not pictures of charts** (ADR-026): a KPI's value and its comparison, the first 10 rows of each other visual with a bar beside a chart's first measure, and a link to the dashboard. Pictures would need a headless browser in the image (a few hundred MB more, and a browser to keep patched), while tables show reliably in every mail client. Is that enough for v1? (V7's renderer service, if you approve it, could later add them.)

## Exact next step

1. **V3 (data prep)**, starting with the two follow-ups from the V2 review. Your stack is on V2 (see "Your stack migrated to V2", with the rollback steps).
2. **Still yours from M8**, unchanged: the three steps below.

Your part of M8, then the M8 review:

1. **Salesforce, Developer Edition only.** In your Developer Edition org, create an External Client App as the README's Salesforce section describes (OAuth with the `api` scope, the Client Credentials Flow, running as a user who can read Account, Contact and Opportunity). Then add a **Salesforce** connection yourself in Opsis at http://localhost:8080 with the My Domain URL, the consumer key and the consumer secret, press **Test** (it should name a Developer Edition org), save it, and tell me its name. I will confirm the edition before reading anything, then test discovery, a full refresh and a merge refresh. I will not type the key or the secret myself.
2. **Google Sheets with your own Google account.** No service account is needed. In the Google Cloud project of the OAuth client your Python scripts use, enable the Google Sheets API and the Google Drive API. Then add a **Google Sheets** connection in Opsis at http://localhost:8080: sign in with **My Google account (OAuth)**, load your `credentials.json`, save, and choose **Connect**. Google comes straight back to Opsis on the laptop. Pick a spreadsheet (a few tabs, with numbers, dates and text is best) and tell me the connection's name. I will then test discovery, the column types, a refresh and a reconnect. If your OAuth app is in Testing status, its sign-in will last 7 days.
3. **The install check on Proxmox:** follow `deploy/proxmox/README.md` from section 1, then its section 11 checklist, which includes the Google sign-in checklist in section 3. Tell me anything that fails, and which address the audit log shows for your sign-in.
