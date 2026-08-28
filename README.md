# yande-sync

`yande-sync` is a security-focused, incremental yande.re downloader for Windows. It keeps
configuration, SQLite state, and logs together while storing images in an explicitly selected
absolute library directory.

## Portable release

The canonical end-user release is the PyInstaller one-folder bundle:

```text
YandeSync\
  yande-sync.exe
  config.example.toml
  README.md
  _internal\
```

Run the first configuration command from any PowerShell working directory:

```powershell
.\yande-sync.exe config set download-dir "D:\Pictures\Yande"
.\yande-sync.exe query add "torino" "torino_(artist)"
.\yande-sync.exe sync
```

The executable creates `config.toml`, `data\yande-sync.db`, `logs\`, and temporary runtime data
beside itself. Moving the complete `YandeSync` folder moves that runtime state. Images remain in
the separately configured library directory.

An installed wheel uses `%LOCALAPPDATA%\YandeSync` for runtime state and is not advertised as a
cross-computer Portable release. Source execution uses the repository root. No mode uses the
current working directory as its runtime root.

## Commands

```text
yande-sync sync [--query COLLECTION_ID] [--limit N] [--concurrency N] [--full-scan]
yande-sync verify [--query COLLECTION_ID]
yande-sync status [--details] [--history N] [--doctor]
yande-sync config
yande-sync config get download-dir
yande-sync config set download-dir ABSOLUTE_PATH [--accept-missing]
yande-sync query
yande-sync query add TAG [TAG ...]
yande-sync query add --to COLLECTION_ID TAG [TAG ...]
yande-sync query remove --from COLLECTION_ID TAG [TAG ...]
yande-sync query rename COLLECTION_ID "NEW_FOLDER_NAME"
yande-sync query enable|disable COLLECTION_ID
yande-sync query artist-name set ARTIST_TAG "JAPANESE_NAME"
yande-sync query artist-name unset ARTIST_TAG
yande-sync query artist-name list
```

Without `--to`, each `query add` invocation creates one logical collection and one folder. With
`--to`, the queries are attached to that existing collection without renaming its folder or
starting a sync. Every query argument is one
complete yande.re source query; the sources in that invocation are queried independently and
their Posts are unioned by Post ID. Quoting preserves spaces and Yande's AND semantics inside one
source expression. For example:

```text
query add karory
  -> karory
query add karory karomix
  -> karory OR karomix
query add "korie_riko rating:safe"
  -> korie_riko AND rating:safe
query add "karory rating:safe" "karomix rating:safe"
  -> (karory AND rating:safe) OR (karomix AND rating:safe)
```

`sync` plans at most 2000 materializations by default and downloads up to 8 images
concurrently. Use `--concurrency 1` for serial downloads or choose any value from 1 to 32;
for example, `yande-sync sync --limit 100 --concurrency 8`.

Run `query add karory` and `query add karomix` separately to create two independent collections.
`query` lists stable numeric collection IDs for `sync --query`, `verify --query`, and
`query enable|disable`. A unique exact source expression is also accepted as a convenience
selector; ambiguous source expressions are rejected.
Each listed Collection includes its persisted folder, enabled state, and ordered Sources.

`query rename COLLECTION_ID "NEW_FOLDER_NAME"` renames only that Collection's folder under
the current download directory and updates its stored paths. It refuses an existing destination,
never merges directories, and does not modify the Collection's Sources.

`query remove --from COLLECTION_ID TAG [TAG ...]` stops future synchronization from those
Sources for that Collection. It does not delete already materialized posts or files, change the
Collection folder, or rewrite stored paths. The last remaining Source cannot be removed.

Collections work without a Japanese name and use their sources joined by ` + ` for the folder
name. An optional local artist-name mapping applies only to the first tag of the first source.
Set it before the first sync if you want the Japanese prefix; artist-name commands never perform
network metadata lookups. Folder identities are stored once, so existing image directories are
not automatically renamed when a mapping is later changed or removed.

For example, a library can contain:

```text
D:\Images\Yande\
├─ korie_riko\
└─ 梱枝りこ korie_riko seifuku\
```

For example, `query add korie_riko` uses `korie_riko\` by default. Running
`query artist-name set korie_riko "梱枝りこ"` before the first sync makes future eligible
folder identities use names such as `梱枝りこ korie_riko\` and
`梱枝りこ korie_riko seifuku\`.

The same Post returned by multiple sources in one collection is stored once in that collection's
folder. If it belongs to two different collections, it is stored as an independent regular file
in both folders, so separate collections can consume more disk space than a globally deduplicated
library.

`sync` checks every source of enabled collections and automatically retries new, pending, failed,
and abandoned downloads. `verify` checks existence, size, MD5, and database/file consistency but
does not download replacements.

Changing `download-dir` never moves, copies, deletes, or rewrites image files. The command reports
tracked, found, and missing counts first. Missing files require interactive confirmation or the
narrow non-interactive `--accept-missing` override.

Upgrades from the former flat layout leave legacy files untouched. A later sync may verify and
copy those bytes into query directories through the normal private `.part` and atomic completion
flow; it never automatically moves or deletes the legacy source.

## Security boundaries

- API access is restricted to `https://yande.re/post.json`.
- Downloads are restricted to `https://files.yande.re` on the default HTTPS port.
- Redirect targets are revalidated and environment proxy settings are ignored.
- Direct connections are the default. An explicitly configured proxy is used without falling
  back to a direct connection, and environment proxy settings are ignored.
- Declared and streamed sizes, MD5, extensions, deterministic filenames, and response limits are
  validated.
- `.part`, final-file, symlink, Windows reparse-point, and hard-link protections prevent unsafe
  overwrite or cleanup behavior.
- Logs sanitize terminal controls and mask likely secrets.

## Backup and migration

Back up `config.toml` and `data\yande-sync.db` together while yande-sync is not running. Schema
migrations create versioned database backups under `data\backups\` and never move or delete image
files. Unsafe legacy absolute paths stop migration and preserve the original database.

## Related Projects

- [FavoriteHelper](https://github.com/Dr0Why/FavoriteHelper) — a related project by the same
  author for working with favorites.

## Support

If you find this project useful, you can optionally support its development through
[Buy Me a Coffee](https://buymeacoffee.com/dr0why).

## Development

```powershell
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install -e . --no-deps --no-build-isolation
python -m pytest -q --basetemp .pytest-local
python -m ruff check .
python -m compileall -q src tests
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist-wheel
pyinstaller yande-sync.spec --clean --noconfirm
```

The repository currently does not declare a software license.
