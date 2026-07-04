# channelforge

Forge one clean channel lineup for [Channels DVR](https://getchannels.com) out of many M3U sources — FAST channel feeds, OTA tuners, and anything else that speaks M3U.

Point it at your playlists, map their channels onto one canonical lineup (by hand or with rules), and channelforge serves ready-to-use M3Us plus a combined XMLTV guide that Channels DVR pulls over HTTP. One click registers everything in Channels DVR; from then on it can keep itself up to date on a daily schedule.

- **Sources** — any number of M3U playlists, each with optional XMLTV guide and a priority.
- **One lineup** — each channel streams from its best available source, with automatic failover when a stream goes unhealthy.
- **Rules** — assign/ignore/rewrite source channels automatically. Exact-match rules use an O(1) index (use thousands freely); regex fully supported.
- **Hands-off assignment** — after rules, unmatched channels auto-assign by normalized name ("123 GO!" = "123GO!"), optionally auto-creating the channel; assigning or ignoring by hand creates the matching rule for you.
- **Provider priority** — combined feeds carrying many providers (`samsung.*`, `stirr.*`, ...) pick the winning duplicate stream by a drag-and-drop provider order.
- **Outputs** — chunked M3Us split by guide type (Gracenote vs XMLTV) and stream format (HLS vs MPEG-TS), plus one combined, filtered guide XML.
- **DVR integration** — one-click (or automatic) registration of all outputs as Channels DVR custom channel sources, across one or many DVR servers.
- **Bulk editing** — channels, rules, and assignments all round-trip through CSV: export, edit in a spreadsheet, import back.
- **Automations** — daily refresh, stream health checks, and Channels DVR maintenance (reset passes + guide re-download, refresh M3U sources).

## Quick start (Docker)

```yaml
services:
  channelforge:
    image: ghcr.io/eagle383/channelforge:main
    container_name: channelforge
    ports:
      - "5100:5100"
    volumes:
      - /opt/channelforge_data:/app/data
    environment:
      - TZ=America/Chicago
    restart: unless-stopped
```

`docker compose up -d`, then open `http://<host>:5100`. All state lives in one SQLite database plus generated output files under the mounted `data/` volume.

## Setup walkthrough

> Want the long version? **[docs/how-to.md](docs/how-to.md)** is a complete step-by-step with worked examples — sources, rules, numbers, genres/collections, automation, and troubleshooting.

Ten minutes, start to finish:

1. **Settings** — set your Channels DVR server URL (e.g. `http://192.168.1.10:8089`; comma-separate to update several servers at once) and the **external base URL** of channelforge itself (e.g. `http://192.168.1.10:5100`) so the DVR knows where to fetch playlists from.
2. **Sources** — add each M3U playlist with a name, URL, optional XMLTV guide URL, stream format, and a **priority** (lower number wins when the same channel exists in several sources — put your OTA tuner at 1).
3. **Refresh** — hit *Refresh sources & outputs* on the dashboard. It fetches every source, applies rules, and generates outputs; watch the live log while it runs.
4. **Assign** — the Assign page lists source channels that aren't matched to a lineup channel yet. Type a channel name to assign (creates the channel if new), or `ignore` for junk you never want.
5. **Rules** — instead of assigning thousands by hand, add rules: *if name equals X → assign to channel Y*, *if group contains Z → ignore*. Use **Apply rules now** to test without a full refresh. Rules never override manual assignments.
6. **Outputs** — click **Add all to Channels DVR**. Every output M3U becomes a custom channel source in the DVR, with the combined guide attached to the XMLTV lineups. Re-clicking updates in place and removes stale entries; it never touches sources it didn't create.
7. **Automate** — in Settings, set daily times for refresh/health/DVR maintenance and switch **Push outputs to DVR after each refresh** on. From here it runs itself.

## Concepts

**Channels vs. source channels.** A *channel* is one entry in your canonical lineup ("CNN"). A *source channel* is one entry in one M3U ("CNN from Pluto", "CNN from Samsung TV Plus"). Assignment links them: many source channels can point at one channel.

**Stream selection.** At output time each channel picks a stream from its assigned source channels: healthy streams first, source priority order within those, then the provider order (Sources page, drag to reorder) as the tie-break inside combined feeds, and an optional per-channel *preferred source* that overrides priority. If a stream starts failing health checks, the next refresh fails over automatically.

**Duplicate merging.** Automatic merges only delete canonical channels when there is hard duplicate evidence: the same guide station ID, the same normalized name (`123 GO!` / `123GO!`), or a plain channel plus its provider rename (`Duck Dynasty` / `Duck Dynasty by A&E`). Softer name matches and guide-data clues like matching `tvg-id` values, repeated long guide descriptions, or strongly overlapping XMLTV programme lineups stay on the duplicate-review page for a manual verdict. Programme-lineup matching fingerprints same-title/same-start-time XMLTV entries for every assigned source `tvg-id`, including channels whose output uses Gracenote instead of the combined XMLTV guide. That review page buckets candidates as strong, medium, or weak so you can work guide/lineup evidence first and bulk-dismiss weak name-only noise. Each group is ordered by the same stream picker used for outputs, so source priority and provider order put the suggested keeper first.

**Outputs.** Channels with a Gracenote station ID land in the *gracenote* M3Us (the DVR's own guide data); everything else lands in the *epg* M3Us paired with the combined XMLTV guide, filtered to only the channels you actually use. Each group is further split by stream format and chunked (default 1200 channels per file) because Channels DVR handles several medium playlists better than one giant one.

**Genres and DVR collections.** Channels DVR builds collections from the `tvc-guide-genres` attribute, but combined feeds usually carry the genre only in `group-title`. channelforge resolves every channel's genres with one rule — your per-channel override, else the winning stream's genres, else the channel's Group, else the winning stream's `group-title` — and emits the result as `tvc-guide-genres`. The Channels page shows exactly that resolved value, so the Genres column and genre filter are a faithful preview of your collections; filter by "(no genres)" to find channels that still need one.

**Channel numbers.** A number you set manually (editable inline on the Channels page) always wins. Otherwise, set *auto-number starting at* to number every channel sequentially from your chosen base; dotted OTA numbers like `7.1` are kept as-is — even when a combined feed hides them inside the channel id (`hdhomerun.4.7`) and renumbers the entry itself — and with auto-numbering off channels keep their source-supplied numbers. Whatever number a channel actually uses is saved onto it at refresh, so numbers are always visible, editable, and stable across refreshes and updates.

## Settings reference

| Setting | Meaning |
|---|---|
| Channels DVR server URL(s) | One or more DVR servers, comma-separated; all DVR actions run against every server |
| Push outputs to DVR after each refresh | Automatically re-register outputs (and prune stale ones) at the end of every refresh |
| External base URL | How the DVR reaches channelforge; required for auto-push, derived from your browser URL otherwise |
| Max channels per m3u | Chunk size for output playlists |
| Auto-number channels starting at | Blank = keep source numbers; see *Channel numbers* above |
| Manual assign/ignore creates a rule | Assign page actions also insert the matching equals rule (default on) |
| Auto-assign by normalized name | Unmatched source channels attach to channels by collapsed-name match (default on) |
| Auto-create channels | Create the channel when nothing matches — hands-off lineup building (default off) |
| Pre-refresh hook | Address of a FastChannels server (its force-refresh endpoint is filled in for you) or any full hook URL; POSTed before each refresh so the upstream source rebuilds first. Blank = off |
| Pre-refresh wait | Max minutes to wait after the hook before fetching sources. For a FastChannels hook, its scrape status is polled and the refresh continues as soon as every enabled source has rescraped |
| Health: fail threshold | Consecutive failed checks before a stream is marked unhealthy |
| Health: concurrency | Parallel stream checks |
| Time zone | IANA zone (e.g. `America/Chicago`) for the schedule and log timestamps; blank = server default |
| Daily schedule | HH:MM per job, blank = off. Defaults to a night run that ends by 04:00: health 01:00 → refresh 01:30 → DVR m3u re-pull 03:00 → reset passes 04:00 |

## Jobs

| Job | What it does |
|---|---|
| refresh | (POST pre-refresh hook + wait, if set) → fetch all sources → apply rules → regenerate outputs (→ push to DVR if enabled) |
| apply_rules | Just the rule pass — quick iteration while building rules |
| health | Probe every stream, mark unhealthy ones for failover |
| reset_passes | Pause/resume every DVR pass and force all guide lineups to re-download |
| refresh_dvr_m3u | Ask every DVR server to re-pull its M3U sources |

One job runs at a time; the dashboard tails the live log. Job history and logs are kept for a week.

## CSV round-trip

Channels, rules, and assignments each export to CSV and import back (matched by `id`; blank id inserts). Fastest way to bulk-edit anything. There is also a one-time importer for legacy playlist-manager CSV exports under **Migrate**.

## Running from source (development)

Docker is the intended way to run channelforge. If you're hacking on it:

```bash
pip install -r requirements.txt
python main.py          # web UI on http://localhost:5100 (CF_PORT / CF_DATA_DIR to override)
```

## Security

channelforge has **no authentication** — anyone who can reach port 5100 can administer it and use it to reach your DVR. Keep it on your LAN. Don't expose it to the internet; if you need remote access, put it behind a VPN or an authenticating reverse proxy.

## License

[MIT](LICENSE)
