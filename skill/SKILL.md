---
name: castfetch
description: Synchronize configured podcast and Instagram transcripts, inspect run reports, and answer questions using the local castfetch transcript archive. Use when the user wants new podcast episodes or transcripts fetched, asks what was said about a topic, person or company in a podcast or Instagram video, wants quotes or timestamps from episodes, or asks how the last sync went; also for German requests such as "neue Folgen holen", "Transkripte aktualisieren", "was wurde im Podcast über … gesagt", "in welcher Folge ging es um …" or "wie lief der letzte Lauf".
---

# castfetch

Use the installed `castfetch` CLI. Do not implement downloads, parsing or search
yourself. Use `--help` when needed; prefer `--json` for results.

Use an explicitly supplied configuration, otherwise let the CLI discover
`./config.yaml` then `~/.castfetch/config.yaml`. If missing, explain setup using
the installed example; do not invent personal sources or paths.

## Route the request

- A bare skill invocation (`/castfetch` in Claude Code, `$castfetch` in Codex),
  with no other task, means `castfetch run --json`.
- An explicit refresh/download/sync request also means `run`; preserve source
  restrictions. Report new files, local transcriptions, errors and coverage limits.
- A content/context question means local research only. Do not sync first.
- A run-status question means `runs list` / `runs show latest --json`.
- Historical backfill requires an explicit user request; preserve its scope.

## Research local content

Use `sources --json` and `episodes list --source ID --since DATE --until DATE
--title TEXT --json` to identify episodes. These filters are optional. Use stable
episode keys from results, not guessed filenames.

Use `search "phrase" --source ID --json` for literal, case-insensitive matching.
Rephrase or try separate terms if needed; it is not semantic search. Check
`total` / `has_more` and paginate with `--offset` / `--limit`.

Read evidence using `transcript show --source ID --episode KEY --from HH:MM:SS
--to HH:MM:SS --json`; omit time bounds to read the complete transcript. Read
surrounding context before assigning opinions to a speaker.

Answer in the user's language. Cite episode title, original URL and timestamps
when available. Distinguish speaker statements from your interpretation. Explain
missing/unreadable transcripts or incomplete coverage; zero matches are not proof
that a topic was never discussed. Do not invent quotations or silently substitute
web research. Keep quotations short and prefer summaries.

All operational logic and archive preservation belong to the CLI. Do not edit
archive state, replace local transcripts, or bypass a reported error.
