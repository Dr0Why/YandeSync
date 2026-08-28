# yande-sync v0.1.0 release evidence

## Release candidate

- Artifact: `dist/YandeSync/yande-sync.exe`
- SHA-256: `0A2C4E422485F9EF2135A4F76A363B3C779C44A540AB2F991FFD9C630450FE89`
- Portable `--help` launch: exit code 0

## Runtime acceptance

- Fresh configuration with spaces and non-ASCII paths: passed.
- Query creation, status, and portable restart with one database: passed.
- Healthy verification: exit 0.
- Repeated missing and corrupt verification: remained problems with exit 1.
- Manual restoration of valid bytes: returned to `downloaded` with exit 0.
- Direct and explicit-proxy diagnostics selected the correct connection mode.
- An unavailable explicit proxy failed without direct fallback.
- A second mutating instance was rejected while read-only status remained available.
- Invalid download-directory change was rejected without changing the configured library.

## Automated evidence

- CLI, verification, network, doctor, migration, packaging, portable-runtime, and artifact tests:
  87 passed, 4 platform/fixture-conditioned skips.
- Interruption, recovery, locking, and concurrency tests: 19 passed.
- Final packaging/artifact/portable-runtime check: 19 passed, 4 platform/fixture-conditioned skips.

## Security and artifact evidence

- Hash-locked runtime dependencies had no known applicable high/critical advisory at final check.
- Final bundle contained 76 files and no real config, database, log, JSONL, `.part`, external
  `.pyc`, pytest/cache/fixture, credential, or user-path material.
- `config.example.toml` contained an empty proxy and no configured download directory.

## Limitations

- Live successful yande.re traffic was unavailable in the audit environment.
- Packaged live Ctrl+C during a transfer was not black-box tested.
- Historical migrations were exercised with repository fixtures.
- Successful traffic through a real proxy was unavailable.
