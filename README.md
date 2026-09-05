# isynca

A modular toolkit for iCloud operations, built on [pyicloud](https://github.com/timlaing/pyicloud).

The first capability is bulk upload of photos and video from a local folder tree
into iCloud Photos, with a local ledger so re-runs skip what is already there.

## Quick start

```bash
pixi install
pixi run isynca auth login --apple-id you@example.com
pixi run isynca photos upload ~/Media --dry-run
pixi run isynca photos upload ~/Media
```

## Commands

| Command | Purpose |
| --- | --- |
| `isynca auth login` | Authenticate, complete 2FA, and persist a trusted session |
| `isynca auth status` | Show whether the stored session is still usable |
| `isynca auth logout` | Drop the stored session and keyring password |
| `isynca photos scan SRC...` | Inventory matching media without touching the network |
| `isynca photos upload SRC...` | Upload discovered media to iCloud Photos |
| `isynca ledger stats` | Summarise what the ledger has recorded |
| `isynca ledger list` | List recorded uploads |
| `isynca ledger forget PATH` | Drop one file's record so it uploads again |
| `isynca ledger prune` | Remove cache rows for files that no longer exist |

## What gets uploaded

Images and video are both included by default. Either kind can be switched off:

```bash
isynca photos upload ~/Media --no-images   # video only
isynca photos upload ~/Media --no-videos   # images only
```

Switching off both is rejected rather than silently matching nothing.

Audio is never uploaded. iCloud Photos ingests images and video and has no
concept of a standalone audio asset, so audio files are not uploadable and
`isynca` does not scan for them.

## How re-runs stay cheap

Uploading re-reads and re-sends every byte, which is expensive for video, so
`isynca` keeps a SQLite ledger under `~/.local/share/isynca/ledger.db`:

- `files` caches each path's size, mtime and content hash. If a file's stat data
  is unchanged, its hash is reused rather than re-read from disk.
- `uploads` records results keyed by **content hash**, so a file that has been
  renamed or moved is still recognised as already uploaded.

Uploads run one file at a time. Each result is committed as soon as its upload
returns, so an interrupted run resumes without re-sending what already made it
across.

Records carry one of three statuses. `confirmed` means iCloud returned the
created asset. `duplicate` means iCloud reported it already held that content.
`unverified` means the bytes were accepted but CloudKit had not finished
indexing before the hydration timeout — that is a success, not a failure, and it
still suppresses a retry.

## Configuration

Settings resolve lowest-to-highest from: built-in defaults, `config.toml`,
`ISYNCA_*` environment variables, then command-line options.

```toml
# ~/.config/isynca/config.toml
[isynca]
apple_id = "you@example.com"
album = "Imported Media"
images = true
videos = true
min_size = 1024
exclude = ["*/.Trash/*", "*.partial"]
```

State lives under the XDG directories: the ledger and session cookies in
`~/.local/share/isynca/`, configuration in `~/.config/isynca/`.

## Development

```bash
pixi run check      # format check, lint, type check, tests
pixi run test       # pytest with 100% coverage gate
pixi run lint       # ruff check
pixi run typecheck  # ty
pixi run fmt        # ruff format
```

Tests run with `pytest-socket` blocking network access at the socket layer.
Everything above `icloud/protocols.py` is tested against an in-memory fake, so
no test can reach Apple.
