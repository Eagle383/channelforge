# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

channelforge forges channel lineups for Channels DVR from many M3U sources (FAST feeds, OTA tuners). It fetches sources, maps their channels onto one canonical lineup via a rule engine, and emits chunked M3Us plus a combined XMLTV guide that Channels DVR pulls over HTTP. Built from scratch as the owner's replacement for a legacy playlist tool; **never name that legacy tool anywhere in code, UI, comments, or docs** — refer to it only as the "legacy" tool (the CSV importer is called "legacy" migration).

## Commands

```bash
.venv/bin/python main.py        # run locally on http://localhost:5100 (CF_PORT to change)
CF_DATA_DIR=/tmp/cf python main.py   # use a throwaway data dir
```

No test suite or linter is configured. Verify changes by running against real data: `channelforge.importer.import_legacy` accepts the four legacy CSVs, then `refresh.run_refresh(log=print)` exercises the whole pipeline.

## Deployment

Push to `main` → GitHub Actions builds `ghcr.io/eagle383/channelforge:main` (name is hardcoded lowercase; `github.repository_owner` is capitalized and breaks Docker tags). The container runs with a `data/` volume mounted at `/app/data`. Owner-specific deployment details live in the untracked `CLAUDE.local.md`.

## Architecture

FastAPI + Jinja templates + SQLite (WAL, thread-local connections in `db.py` — safe for the threaded job runner). All state is one SQLite DB plus generated files in `data/outputs/`.

Flow: `refresh.run_refresh` → `sync_source` (fetch/parse M3U, upsert `source_channels`, absent entries flagged `present=0` but kept) → `rules.apply_all` (rules first, then optional normalized-name auto-assign/auto-create fallthrough) → `build_outputs` (pick best stream per channel by source priority, then drag-and-drop provider order within a source, with health failover; persist each channel's effective number; write chunked M3Us split by guide-type × stream-format, stream the combined guide) → optional push of outputs to Channels DVR.

- `jobs.py` — one heavy job at a time (thread + DB `jobs` row with live log); scheduler fires daily HH:MM settings on `db.local_now()` (setting `timezone`, IANA name, blank = system; defaults end the night run by 04:00). `run_refresh` first POSTs the optional `prerefresh_url` hook and sleeps `prerefresh_wait_min` so upstream feeds (FastChannels force-refresh) rebuild before fetching. Stale `running` jobs are failed at startup by `db.init`.
- `rules.py` — the rule engine. **Performance contract**: all-source `equals` assign/ignore rules go into a dict index (O(1) per channel, thousands of rules are free); everything else is an ordered scan with precompiled regexes. Don't add per-channel work that scales with rule count. After rules, `apply_all` optionally auto-assigns still-unassigned children by `normalize()`d name match against existing channels (`auto_assign_normalized`, default on) and auto-creates channels when nothing matches (`auto_create_channels`, default off); ignore rules always win. Manual assign/ignore on the Assign page auto-creates a matching equals rule (`auto_rule_on_assign`).
- `m3u.provider_of` — provider prefix of combined-feed channel ids (`samsung.x` → `samsung`; letter-first before a dot, so OTA ids like `3.1` yield ""). The Sources page has a drag-and-drop provider order (setting `provider_order`, JSON list) used as the stream-pick tie-break within a source; the Channels page filters/sorts by provider.
- `xmltv.py` — **streams** the merged guide to disk element-by-element. Never build the combined guide as an in-memory tree: real guides are ~26 sources / 380k+ programmes and blew a 1 GiB container limit doing exactly that.
- `channels_dvr.py` — Channels DVR API client. Supports multiple servers (comma-separated setting); every operation loops over all of them. Output registration PUTs to `/providers/m3u/sources/{slug}` with deterministic names so re-runs update in place, and prunes only `CF`-prefixed stale sources — never other m3u sources.
- `csv_io.py` — CSV export/import for channels, rules, and assignments. This round-trip (export → edit in spreadsheet → import, matched by `id`, blank id = insert) is a core user workflow; keep it working when changing schemas.
- `importer.py` — one-time legacy CSV migration. It inserts `present=0` stub rows into `source_channels` so assignments survive until the first refresh fills in URLs.
- Channel identity within a source is `m3u.external_id` (channel-id → tvg-id → tvg-name → name → url hash); changing it orphans existing assignments.

## Data model notes

`channels` = canonical lineup ("parents"); `source_channels` = per-source entries ("children") pointing at a channel or `ignored`. A channel with `gracenote_id` (or a child carrying `tvc-guide-stationid`) lands in the gracenote M3Us; otherwise it goes in the EPG M3Us and its `tvg_id` is included in the combined guide filter. Channel numbers: explicit `number` wins; otherwise at output time the channel gets sequential auto-numbering from `output_start_number` (dotted OTA numbers like 7.1 are kept as-is, inherited source numbers are used when auto-numbering is off) and the effective number is always persisted onto the channel — so the Channels page shows/edits the real number and it survives refreshes. Settings are key/value in the `settings` table — always named keys, never positional.
