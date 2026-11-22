# castfetch

**Make your favorite content searchable. Keep it local.**

Collect transcripts from podcasts, videos, and social media posts with spoken
content. Download published transcripts or generate them locally, then preserve
them in your own archive—ready to search and explore with your favorite AI tools.

castfetch supports direct RSS/Atom podcast feeds and filtered public Instagram
video posts/Reels. It does not archive text-only posts, images, carousels, Stories,
live streams, or private content. It provides local text retrieval and an optional
AI skill; it does not run an LLM or provide a chat service.

## Install

```sh
brew install ph-wolf/tap/castfetch
castfetch --version
```

The Homebrew package installs Python, FFmpeg and yt-dlp, and includes the example
configuration and optional skill. Publisher downloads and archive search do not
require speech recognition. Local transcription additionally requires an Apple
Silicon Mac and an external MLX Whisper environment (see below).

```sh
mkdir -p ~/.castfetch
cp "$(brew --prefix castfetch)/share/castfetch/config.example.yaml" ~/.castfetch/config.yaml
# Edit the YAML: choose an archive directory, replace examples, enable your sources.
castfetch config check
castfetch run --dry-run
castfetch run
```

The example sources are fictional and disabled. There is no interactive setup
dialogue. Running with no enabled sources reports a configuration error.

To update the installed release:

```sh
brew update
brew upgrade castfetch
```

Configuration, archives, model weights and logs live outside Homebrew's install
directory. Upgrades do not replace them. The source checkout is independent of
the installed release.

## Configuration

See [config.example.yaml](config.example.yaml) for a commented example of each
source type and every setting. Sources and archive contents belong in your
personal configuration, not in the application repository.

Configuration precedence (one file, no merging):

1. Explicit `--config /path/config.yaml` (a missing explicit file is an error).
2. `config.yaml` in the working directory.
3. `~/.castfetch/config.yaml`.

`~` expands to your home directory. Relative paths resolve against the selected
configuration's directory. Unknown or duplicate YAML keys are errors. Optional
means a key may be omitted; blank/null values are not substitutes for defaults.
`--config` and `--json` may appear before or after the command.

| Setting | Required / default | Meaning |
| --- | --- | --- |
| `version` | Required: `1` | Configuration schema version. |
| `archive_root` | Required | Common root; source ID determines its subfolder. |
| `formats` | `[text/vtt, application/json]` | Non-empty list of publisher MIME types. Also accepts `text/plain`, `application/srt`, `application/x-subrip`, `text/srt`. |
| `max_feed_bytes` | `52428800` | Positive maximum bytes per feed/profile discovery response. |
| `max_transcript_bytes` | `52428800` | Positive maximum bytes per published transcript. |
| `logging.directory` | `~/.castfetch/runs` | Productive run reports; never automatically deleted. |
| `transcription.python` | Required only for local transcription | Python executable containing `mlx_whisper`. |
| `transcription.model` | `mlx-community/whisper-large-v3-turbo` | External model identifier. |
| `transcription.cache_dir` | Library default | Model cache (`HF_HOME`), not an audio cache. |
| `transcription.max_audio_bytes` | `1073741824` | Positive per-media byte limit. |
| `sources` | Required, non-empty list | Configured source definitions. |
| `sources[].id` | Required, unique | Lowercase letters/numbers, words separated by hyphens. Keep stable. |
| `sources[].name` | Required | Display name. |
| `sources[].type` | Required | `podcast` or `instagram`. |
| `sources[].enabled` | `true` | Whether sync/backfill may process the source. |
| `sources[].feed_url` | Required for podcasts | Direct public HTTP(S) RSS/Atom feed. |
| `sources[].username` | Required for Instagram | Account name without `@`. |
| `sources[].caption_contains` | Required for Instagram | Series substring filter; ignores case, accents and repeated whitespace. |
| `sources[].transcription` | `publisher` | `publisher` or `local`; see policy below. |
| `sources[].initial_sync` | `{days: 30}` | `all` or `{days: N}` with a positive integer; evaluated once. |
| `sources[].language` | Automatic detection | Optional Whisper language code. |

### Publisher versus local

`publisher` downloads published transcripts and never downloads audio for speech
recognition. `local` permits local transcription only when the source as a whole
has no publisher transcripts, considering both current discovery and archive
history. An unsupported publisher format, missing episode transcript, or failed
publisher download never silently enables audio transcription.

MLX Whisper performs speech recognition. castfetch manages temporary media,
invokes the external worker, validates timestamps, and exports VTT, JSON and
readable Markdown. Text is automatically generated, not manually corrected. An
invalid timestamp result is retried once on the same media with word alignment
and without previous-text conditioning. A second failure remains a failed item;
timestamps are not clamped or silently rewritten.

Example external setup (Apple Silicon; `uv` is needed for these setup commands):

```sh
uv venv --python 3.12 ~/.venvs/castfetch-transcription
uv pip install --python ~/.venvs/castfetch-transcription/bin/python mlx-whisper
# Set transcription.python and, optionally, cache_dir in your YAML.
castfetch doctor
```

The model may be downloaded on first eligible transcription. No paid or remote
transcription service is used. Temporary episode media is removed on success and
failure; model weights remain cached.

### First sync, deltas and backfill

`initial_sync` determines a fixed starting boundary at the first successful
discovery. With `{days: 30}`, the boundary is 30 days before that first run; it does
not slide forward on later runs. `all` removes the historical cutoff. Subsequent
runs skip existing transcripts, process new eligible items and retry failed work.
Items without valid publication timestamps are reported and skipped.

Changing `initial_sync` does not widen an existing boundary; castfetch reports the
difference. Explicitly extend coverage instead:

```sh
castfetch backfill --source my-podcast --days 365
castfetch backfill --source my-podcast --all
```

`--days 365` means 365 days before the backfill invocation. Backfill can only
expand coverage; it never shrinks it. It can be a substantial download or local
transcription job. Use `--dry-run` first to inspect candidates.

Instagram discovery uses the account's public profile embed without executing
page scripts, logging in or reading browser cookies. The public window is limited.
`all` means all discoverable qualifying posts, not complete account history.
Previously discovered failures remain retryable after they leave the window;
nonoverlapping windows produce persistent coverage warnings. A qualifying series
video remains eligible even if its caption discusses a promotion or webinar.

## Commands

```sh
castfetch run                        # Sync all enabled sources.
castfetch run --source my-podcast     # --source may be repeated.
castfetch run --dry-run --json        # Network discovery, no writes/media/models.
castfetch sources --json             # Offline configuration listing.
castfetch config check               # Offline validation.
castfetch doctor                     # Check configuration, tools and optional MLX.
castfetch --version
```

`--json` prints a versioned object to stdout; progress goes to stderr. Exit codes
are `0` for success, `1` for processing errors, `2` for configuration/usage errors,
and `130` for an interrupted productive run. No-result searches succeed with zero
hits. Warnings are included in reports and do not by themselves mean failure.

## Search and read locally

```sh
castfetch search "renewable energy" --json
castfetch search "robotics" --source my-podcast --since 2026-01-01 --until 2026-12-31 --json
castfetch episodes list --source my-podcast --title "interview" --json
castfetch transcript show --source my-podcast --episode EPISODE_KEY --json
castfetch transcript show --source my-podcast --episode EPISODE_KEY --from 00:10:00 --to 00:13:00 --json
```

Search is case-insensitive literal text/phrase matching, with normalized
whitespace. It can match phrases across nearby subtitle cues; it is not semantic
search. Source filters are repeatable and date bounds are inclusive publication
dates. By default all local source indexes are searched, including disabled or
no-longer-configured sources. No network, model or index-building step is involved.

Results include stable episode keys, titles, dates, original URLs, transcript
paths, provenance and timestamped context. VTT/SRT is preferred; recognized JSON
segment lists (or objects with `segments`) are a fallback. Plain text can be
searched but has no timestamps. Multiple representations are not counted twice.
Missing, malformed or unsupported transcripts are reported as warnings.

`search` and `episodes list` default to `--limit 20 --offset 0` and return `total`
and `has_more`. Fetch another page with `--offset 20`. Transcript time bounds
accept seconds or `HH:MM:SS`. All retrieval commands are read-only and produce no
run logs.

## Archive, state and preservation

```text
ARCHIVE_ROOT/
  .castfetch.lock
  source-id/
    feed.xml             # Podcasts
    discovery.json       # Instagram
    state.json           # Fixed boundary and discovery coverage
    index.jsonl          # Episodes, offered URLs, files, hashes and provenance
    transcripts/
      YYYY-MM-DD_episode_KEY.vtt
      YYYY-MM-DD_episode_KEY.json
      YYYY-MM-DD_episode_KEY.local.vtt
      YYYY-MM-DD_episode_KEY.local.json
      YYYY-MM-DD_episode_KEY.local.md
```

Local transcripts win. castfetch never overwrites or deletes existing archived
transcripts, including manually modified, empty or damaged files. Such conflicts
are reported. Removed upstream episodes remain archived. Publisher and locally
generated versions may coexist. Machine-managed indexes, feed snapshots and state
are updated atomically; transcript creation never replaces an existing file.
An archive lock prevents concurrent castfetch writers. Keep the state between
runs; do not edit it as a substitute for changing YAML or running backfill.

## Run reports

Configure `logging.directory` (default `~/.castfetch/runs`). Each productive run or
backfill receives a unique UTC run ID and directory containing `result.json` and
`events.jsonl`; `latest.json` points to the latest started run.

Reports have `schema_version: 1`, start/finish times, command, paths, per-source
counts, coverage warnings and errors. Events are JSON objects, one per line, with
run ID, UTC time, level, event type and relevant source/episode/stage fields.
A start report without completion may indicate an active or interrupted run.
No transcript bodies are logged; URL credentials and query strings are redacted.
Logs are private operational data and are never automatically deleted.

```sh
castfetch runs list --limit 10 --json
castfetch runs show latest --json
castfetch runs show RUN_ID --json
```

These commands work offline. `run --json` returns the same final report. Dry runs
and read-only commands create no reports or state. Errors before reporting can be
initialized appear on stderr (or as a JSON error with `--json`).

## Optional AI skill

The release bundles [skill/SKILL.md](skill/SKILL.md), a small CLI-based skill with
no separate parsing, download logic, model API or personal settings. The agent
must run locally where the archive and CLI are accessible.

For Codex, inspect any existing destination, then create a stable link:

```sh
mkdir -p ~/.agents/skills
ln -s "$(brew --prefix castfetch)/share/castfetch/skill" ~/.agents/skills/castfetch
```

For Claude Code use `~/.claude/skills/castfetch` instead. Restart the agent session
to discover the skill. Homebrew upgrades update CLI and skill together.

- Bare `$castfetch`: synchronize configured sources and summarize the run.
- `$castfetch Fetch new transcripts`: explicitly synchronize.
- `$castfetch What does the guest say about robotics in Saturday's episode?`:
  select the episode, search/read local transcripts, and cite episode/timestamps.
- `$castfetch How did the last run go?`: read saved reports.

Content questions do not trigger automatic downloads. If coverage is incomplete,
the skill explains the limitation. The agent interprets the evidence; castfetch
itself does not host a conversation model.

## Development and releases

```sh
uv sync
uv run python -m unittest discover -s tests -v
uv build
uv run castfetch --help
```

Tests use fictional sources, synthetic transcripts and temporary archives. Keep
personal YAML, transcripts, logs, models and environments out of commits.

The version in `pyproject.toml` is the release version. Tag `vX.Y.Z`, publish the
source archive, update the Homebrew formula's URL/SHA-256, and verify installation
and `brew test`. The bundled skill and example configuration share that release.

Licensed under [MIT](LICENSE).
