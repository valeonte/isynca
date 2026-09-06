# isynca

[![CI](https://github.com/valeonte/isynca/actions/workflows/ci.yml/badge.svg)](https://github.com/valeonte/isynca/actions/workflows/ci.yml)

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
| `isynca photos archive SRC... --to DEST` | Upload, then move what iCloud holds into DEST |
| `isynca ledger stats` | Summarise what the ledger has recorded |
| `isynca ledger list` | List recorded uploads |
| `isynca ledger forget PATH` | Drop one file's record so it uploads again |
| `isynca ledger prune` | Remove cache rows for files that no longer exist |

## Signing in once

`auth login` records the account it signed in as, so later commands do not need
`--apple-id` repeated:

```bash
isynca auth login --apple-id you@example.com   # once
isynca auth status                             # no --apple-id needed
isynca photos upload ~/Media                   # nor here
```

The account is stored beside the session cookies in `~/.local/share/isynca/`,
not written into your `config.toml` — rewriting that file would discard your
comments and layout. It is the lowest-precedence source, so `--apple-id`,
`ISYNCA_APPLE_ID`, and an `apple_id` in `config.toml` all still override it.
`auth logout` forgets it again.

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

## Requiring a capture date

`--require-date-taken` holds back any file with no "date taken", reports it by
name, and uploads the rest. It works on `upload` and `archive`, and on `scan`
for a network-free audit:

```bash
isynca photos scan ~/Inbox --require-date-taken     # which files lack a date?
isynca photos upload ~/Inbox --require-date-taken   # upload only dated files
```

Photos and video store this in completely different places, so there are two
readers:

- **Images** use EXIF `DateTimeOriginal`, falling back to `DateTimeDigitized`
  then `DateTime`. Editors and export pipelines often drop the original tag
  while keeping one of the others, and reporting an obviously-dated photo as
  undated would be worse than accepting the fallback. HEIC is read via
  `pillow-heif` — without it every iPhone photo would look undated.
- **Video** has no EXIF. MP4/MOV keep the date in container atoms:
  `com.apple.quicktime.creationdate` where Apple wrote one, otherwise the
  `mvhd` creation time. The Apple value is preferred because `mvhd` is written
  by the muxer, so a re-encode overwrites it. An `mvhd` of zero — which plenty
  of muxers emit — counts as no date, not as a video shot in 1904.

A held-back file is never uploaded and, in archive mode, never moved. Files
already in iCloud skip the check entirely: blocking them would achieve nothing
and would strand them in the source folder forever.

## Archive mode

`photos archive` is `photos upload` plus filing: once iCloud holds a file, its
local copy is moved under a target folder, keeping the path it had relative to
the source it was found under.

```bash
isynca photos archive ~/Inbox --to ~/Archive --dry-run
isynca photos archive ~/Inbox --to ~/Archive
```

```
~/Inbox/trip/day1/clip.mov   ->   ~/Archive/trip/day1/clip.mov
```

Three rules make this safe to point at real files:

- **A file moves only when iCloud is known to hold it** — a created asset or a
  reported duplicate. An upload that was accepted but not yet indexed
  (`unverified`) keeps its local copy and is counted as *held*; a later run
  re-checks it.
- **Files already in iCloud are moved too**, not just ones uploaded on this
  run. Without that the source folder would never drain — everything sent by an
  earlier run would be skipped and left sitting there.
- **An existing file at the destination is never overwritten.** The source is
  left alone and the collision is reported, for you to resolve.

A target inside a source (or a source inside the target) is rejected up front:
the first would be re-scanned on the next run, the second would move files onto
themselves.

`--dry-run` previews the whole thing, including real collision checks, without
uploading, moving, or removing anything.

### Empty folders

Draining a tree leaves its folders standing, so an archive run finishes by
removing the ones it emptied — deepest first, so a branch whose leaves all go
takes its parents with it. `--no-prune-empty-dirs` leaves the structure in
place instead, and `prune_empty_dirs = false` in the config file makes that the
default.

Three things are never removed: the source folders you named on the command
line, even once they are empty; anything still holding a file, including one
`isynca` never looks at, such as a stray `.txt` or an audio file; and symlinks,
which count as content rather than as folders to descend into. A folder that
cannot be removed is logged and left behind — a folder outliving its files is
untidy, not a failure, and the run still exits 0.

`upload` never prunes: it empties nothing, so a folder that was already empty
is none of its business.

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
prune_empty_dirs = true
notify = true
notify_level = "WARNING"
```

State lives under the XDG directories: the ledger and session cookies in
`~/.local/share/isynca/`, configuration in `~/.config/isynca/`.

## Desktop notifications

A run that logged anything worth seeing tells the desktop about it when it
ends:

```
isynca: 1 error, 12 warnings
upload refused: HEIC variant not accepted
… and 12 more
```

Nothing is sent while the run is in progress. Warnings here are per-file --
one unreadable folder, one photo with no capture date -- and a large scan logs
hundreds of them, so they are counted and summarised into a single
notification rather than popped one at a time. A run that takes more than
twenty seconds also reports finishing, folded into the same notification:

```
isynca: Upload finished
412 uploaded, 3 skipped · 12 warnings
```

Short runs stay silent unless something went wrong. An error that aborts a run
is always notified, however briefly the run lasted.

This turns itself on when there is a desktop to talk to and stays out of the
way when there is not. The signal is a session bus address in the
environment, so cron jobs, ssh sessions, and CI are silent without needing to
be told. Delivery is the freedesktop `org.freedesktop.Notifications`
interface, which KDE Plasma, GNOME, Cinnamon, and XFCE all implement, over
[jeepney](https://pypi.org/project/jeepney/) -- no notification daemon of
isynca's own, and no `notify-send` subprocess. A desktop that will not take
the message is never a reason to fail a run that has otherwise finished.

To switch it off, any of:

```bash
isynca --no-notify photos upload ~/Media   # this run
export ISYNCA_NOTIFY=0                     # this shell
```

```toml
[isynca]
notify = false                             # always
```

`notify_level` raises the bar instead of removing it: `"ERROR"` reports only
failures, leaving per-file warnings to the terminal.

## Development

```bash
pixi run check      # format check, lint, type check, tests
pixi run test       # pytest with 100% coverage gate
pixi run lint       # ruff check
pixi run typecheck  # ty
pixi run fmt        # ruff format
```

CI runs the same four checks on every push and pull request
(`.github/workflows/ci.yml`). Each runs even if an earlier one fails, so a
single run reports every problem. `pixi.lock` is installed with `--locked`, so
a `pixi.toml` edit without a re-lock fails CI rather than quietly resolving to
untested versions.

Tests run with `pytest-socket` blocking network access at the socket layer.
Everything above `icloud/protocols.py` is tested against an in-memory fake, so
no test can reach Apple.
