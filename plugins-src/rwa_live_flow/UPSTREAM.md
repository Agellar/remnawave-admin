# Live Flow safe fork provenance

- Upstream: `https://github.com/chudo-arky/rwa-plugin-live-flow`
- Audited release: `v0.16.0`
- Audited upstream `main`: `a0962f623afcacd4c7cd96b472ec8eae76885e76`
- Release tag target: `b84c68e7553db965668deb21386457cf3b88ae5a`
- Upstream wheel SHA-256: `4196ba13f2fac4bf76411d6357c4b35c56898630b2004b1fc862985b11111dc3`
- Vendored on: `2026-08-22`

The executable Python sources in the release wheel, tag target, and audited
`main` commit were byte-for-byte equivalent after line-ending normalisation.
The published wheel is not installed here: the release tag was unsigned, had
been force-moved during release preparation, and no GitHub artifact attestation
was published.  This directory is the pinned, reviewable source used by our
deployment.

Local hardening relative to upstream 0.16.0:

- fail-closed node access-policy and visible-user filtering;
- superadmin/legacy-admin compatibility without weakening regular roles;
- paged and capped personal-data responses;
- bounded 5,000-user default Panel poll (hard maximum 20,000) with visible
  truncation state;
- per-admin burst limiting;
- non-overlapping, visibility-aware browser polling with cancellation;
- single-flight profile/classification caches;
- focused security and compatibility tests in `tests/test_security.py`.
