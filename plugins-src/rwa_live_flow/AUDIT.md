# Live Flow security review and local-fork decision

Audit date: 2026-08-22; Admin compatibility rechecked: 2026-09-08. Scope: every tracked source, test, packaging,
workflow, dependency and release/history record available from upstream at the
pinned revisions in `UPSTREAM.md`.

## Decision

- **Upstream wheel/tag as published: BLOCK for production.** Its coarse RBAC
  gates do not apply remnawave-admin node policies or visible-user scope, so a
  scoped non-superadmin with `live_flow:view` / `live_flow:view_users` could
  receive global topology and active-user PII. Release provenance is also too
  weak for a privileged in-process plugin: unsigned/moved tag, no artifact
  attestation.
- **This local source fork: ALLOW WITH CONDITIONS.** Use only the pinned source
  baked into the compatibility-tested remnawave-admin 4.7.2 backend image, after
  the focused and upstream tests pass in the final merged tree. Grant
  `view_users` only to roles that are intentionally allowed to see login,
  Telegram ID, IP, ASN and location data.

## Threat-model result

No executable path was found for subprocess/command execution, filesystem
writes, dynamic `eval`/`exec`, external telemetry, self-update, WebSocket
handling, or unauthenticated routes. Request values used by SQL are asyncpg
parameters; SQL fragments are module constants. Dynamic UI fields are HTML
escaped. Routes are GET-only and explicitly authenticated.

Local hardening adds:

- official 4.5.6 `get_scope(..., "node", "view")` and
  `get_visible_user_uuids(...)` resolution on `/data` and both user-detail
  routes;
- fail-closed empty scope for regular admins when visibility resolution fails,
  while superadmin and legacy-admin behavior continues to match the panel;
- policy intersection for live poller data and SQL fallbacks, including
  suppression of global node counters for user-restricted roles;
- unique-user pagination, bounded page/offset, at most six recent distinct IPs
  per returned user, per-principal burst limits, and a 5,000-user default
  background-poll cap (environment-configurable only within 500..20,000;
  request page 250..500);
- `truncated` propagation to aggregate/detail responses and visible UI notices;
- single-flight caches plus non-overlapping, abortable browser polling paused
  while the document is hidden.
- activity/history fallbacks reject rows more than 30 seconds in the future,
  preventing pre-4.7.2 timezone-shifted history from appearing current.

## Remaining conditions and limitations

- Plugins run in the backend process and therefore retain the backend's DB and
  Panel API authority; this is hardening, not a sandbox.
- Poller state and rate limiting are per backend worker. More workers multiply
  Panel API polling and the effective burst budget; keep worker count bounded
  or move these controls to shared storage in a future revision.
- A truncated poll cannot prove that an omitted user is offline. The UI marks
  such snapshots incomplete.
- Schema/API compatibility was rechecked against admin 4.7.2. Re-audit upstream changes
  and re-run all tests before rebasing this directory.
- No production deployment or production probe was performed by this audit.

## Verification entry points

- Local security suite: `pytest -c plugins-src/rwa_live_flow/pytest.ini`
- Upstream regression suite: run upstream `tests/` with this directory first on
  `PYTHONPATH`.
- Static checks: Python compile, Ruff using upstream policy, Bandit using the
  upstream B608 exception (all request data remains parameterized), and JS
  syntax validation with Node.
