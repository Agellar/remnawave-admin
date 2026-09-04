# Live Flow safe fork provenance

- Upstream: `https://github.com/chudo-arky/rwa-plugin-live-flow`
- Audited release: `v0.17.0`
- Audited upstream `main`: `c84cadde9aa2f31e70ebbd32bc1ebb0ba3d18b49`
- Release tag target: `c84cadde9aa2f31e70ebbd32bc1ebb0ba3d18b49`
- Upstream wheel SHA-256: `805aaba053b9b5aa9ad42070ab731b54bf2472d3eab9f8373bf453c4934a6dfb`
- Vendored on: `2026-08-26`
- Compatibility rechecked against Remnawave Admin `4.7.1`: `2026-09-04`

The executable Python sources in the release wheel, tag target, and audited
`main` commit were byte-for-byte equivalent after line-ending normalisation.
The published wheel is not installed here: the release tag was unsigned, had
been force-moved during release preparation, and no GitHub artifact attestation
was published.  This directory is the pinned, reviewable source used by our
deployment.

Local hardening relative to upstream 0.17.0:

- fail-closed node access-policy and visible-user filtering;
- superadmin/legacy-admin compatibility without weakening regular roles;
- paged and capped personal-data responses;
- bounded 5,000-user default Panel poll (hard maximum 20,000) with visible
  truncation state;
- per-admin burst limiting;
- non-overlapping, visibility-aware browser polling with cancellation;
- server-side paging retained for personal-data panels while the upstream
  0.17 large-fleet layout, filters, zoom, and page controls are used;
- SVG DOM replacement is skipped for unchanged data and only visible flow
  lines animate; listeners and animation frames are removed on unmount;
- toolbar and personal-data pagers use separate styles, with explicit keyboard
  focus, labels, and themed scrollbars;
- single-flight profile/classification caches;
- focused security and compatibility tests in `tests/test_security.py`.
