# Opsis: project instructions

- Before any work, read BUILD_BRIEF.md (the spec) and PROGRESS.md (current state).
- Work on one milestone at a time and stop for review after each.
- At the end of each milestone, update PROGRESS.md, commit with a clear message and push to origin.
- Never commit .env or any secrets.
- Verify UI changes with the Playwright smoke tests (`npm run smoke`) before saying they work. This replaces manual Browser pane checks (decided at the M4 review). Add to the tests in `e2e/` when a milestone adds UI.
- Verify every milestone twice: at the root and under the sub-path with the proxytest profile. `npm run smoke` does both, in test stacks of its own (http://localhost:18080 and http://localhost:18088/opsis/).
- Tests never touch the user's own stack (Compose project `opsis`, port 8080) or read `.env`: run them through the npm scripts, which refuse otherwise (ADR-037).
- Never use em dashes.
