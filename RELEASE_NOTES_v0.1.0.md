# yande-sync v0.1.0

yande-sync is a security-focused Windows CLI for incrementally downloading and verifying
yande.re image collections in a portable local library.

## Highlights

- Incremental multi-source collection synchronization with persistent SQLite state.
- Bounded concurrent downloads with size and MD5 verification.
- Private temporary files, atomic finalization, safe corrupt-file repair, and restart recovery.
- Direct or explicit-proxy networking with TLS and redirect/host validation.
- Portable configuration, status, doctor, verification, collection, and artist-name commands.
- Transactional database migrations and single-operation locking.

## Final fixes

- Repeated `verify` runs continue to report unresolved missing or corrupt files, and a manually
  restored valid file returns to `downloaded` state.
- Network and doctor diagnostics distinguish direct connections from explicit proxy connections.

## Validation

The packaged Windows executable completed fresh-user, configuration, status, verification,
missing/corrupt recovery, proxy/direct failure, concurrency, portability, migration-fixture,
artifact-cleanliness, dependency, and secret checks. Targeted final regression runs reported
87 passed with 4 platform/fixture-conditioned skips, plus 19 interruption/concurrency tests.

Validation limitations:

- A successful live yande.re transfer was unavailable in the audit environment.
- A packaged live Ctrl+C transfer interruption was therefore not black-box tested.
- Historical migration validation used repository fixtures rather than an archived installation.
- Successful traffic through a real proxy was unavailable.

## Artifact

`dist/YandeSync/yande-sync.exe`

SHA-256: `0A2C4E422485F9EF2135A4F76A363B3C779C44A540AB2F991FFD9C630450FE89`
