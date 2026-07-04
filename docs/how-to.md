# channelforge, step by step

A complete walkthrough from empty Docker host to a self-maintaining Channels DVR lineup, with real examples. The [README](../README.md) covers concepts and reference tables; this is the "do this, then this" version.

Throughout, replace `192.168.1.10` with your server's address.

## 0. What you need

- A running [Channels DVR](https://getchannels.com) server (example: `http://192.168.1.10:8089`)
- Docker
- At least one M3U source. Examples used below:
  - A combined FAST-channels feed such as [FastChannels](https://github.com/kineticman/FastChannels) (example: `http://192.168.1.10:5523`)
  - An OTA tuner feed whose entries look like `channel-id="hdhomerun.4.7"`

## 1. Run it

```yaml
# docker-compose.yml
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

```bash
docker compose up -d
```

Open `http://192.168.1.10:5100`. Everything lives in the mounted `data/` volume — back that folder up and you can rebuild the container freely.

## 2. Settings first

Go to **Settings** and set:

| Field | Example | Why |
|---|---|---|
| Channels DVR server URL(s) | `http://192.168.1.10:8089` | Where outputs get registered. Comma-separate for several DVRs |
| External base URL | `http://192.168.1.10:5100` | The URL the DVR uses to reach channelforge |
| Auto-number channels starting at | `1000` | Your FAST channels get 1000, 1001, … instead of feed-supplied numbers |
| Auto-create channels | `On` | Hands-off lineup building (see step 5) |

Leave the rest at defaults for now. Save.

## 3. Add sources

**Sources → Add source**, once per playlist:

| Field | Example |
|---|---|
| Name | `FAST` |
| M3U URL | `http://192.168.1.10:5523/playlist.m3u` |
| XMLTV guide URL | `http://192.168.1.10:5523/epg.xml` |
| Stream format | `HLS` |
| Priority | `10` |

**Priority is the whole ballgame when the same channel exists in several sources: lower number wins.** Put your OTA tuner at `1` so real broadcast streams always beat FAST copies. If you run two variants of a feed (say one with Gracenote IDs and one without), give the one you *want* to win the lower number.

If one source combines many providers (`samsung.*`, `pluto.*`, `stirr.*` channel-ids), drag the **Provider priority** list on the Sources page into your preferred order — it breaks ties *within* that source.

## 4. First refresh

Dashboard → **Refresh sources & outputs**. Watch the live log:

```
refreshing sources...
  FAST: 3503 channels (+3503 new, ~0 changed, -0 gone)
total channels across sources: 3503
rules: 0 assigned, 0 ignored, 0 field changes; name-match: 0 assigned, 3489 channels created
outputs: auto-numbered 3480 channels (1000-4479)
outputs: cf_gracenote_hls_01.m3u (461 channels), cf_epg_hls_01.m3u (1200 channels), ...
refresh complete
```

With *Auto-create channels* on, your entire lineup builds itself on the first pass.

## 5. Assignment: how channels come together

A **channel** is one entry in your lineup ("CNN"); a **source channel** is one entry in one playlist ("CNN from Pluto"). Three mechanisms link them, in order:

1. **Rules** (Rules page) — run first, never override manual work. Examples:
   - *name equals `CNN` → assign to channel `CNN`* (exact matches are O(1) — use thousands freely)
   - *group contains `Latino` → ignore* (junk you never want to see again)
   - *name regex `^ESPN.*` → assign to `ESPN`*
2. **Normalized-name auto-assign** — anything still unmatched attaches to an existing channel whose collapsed name matches (`"123 GO!"` = `"123GO!"`).
3. **Auto-create** (if enabled) — still nothing? The channel is created.

Whatever's left lands on the **Assign** page. Type a channel name to assign (creating it if new) or `ignore` — each manual action also writes the matching rule, so it sticks across refreshes.

**Test rules without a full refresh:** Dashboard → **apply_rules** runs just the rule pass in seconds.

## 6. Channel numbers

- A number you set by hand always wins. On the **Channels** page, click any number, type, press Enter — saved instantly.
- With *auto-number starting at* set, everything else gets sequential numbers from your base.
- **OTA numbers survive**: an entry with `channel-id="hdhomerun.4.7"` keeps number `4.7` even when the feed renumbers it to something like `10135` — dotted tuner numbers are recovered from the id itself.
- Whatever number a channel actually uses is saved onto it, so numbers are stable across refreshes and visible/editable on the Channels page.

Changed your mind about a number? Clear it (click, delete, Enter) and the next refresh re-derives it.

## 7. Genres → DVR collections

Channels DVR builds collections from `tvc-guide-genres`. Most feeds only set `group-title`, so channelforge resolves genres in this order and emits the result:

1. Per-channel **Genres override** (edit panel on the Channels page)
2. The winning stream's own `tvc-guide-genres`
3. The channel's **Group** field
4. The winning stream's `group-title` ← rescues most FAST feeds

**Validate before the DVR sees it:** on the Channels page, the Genres column shows the resolved value. Use the genre dropdown — picking "Sports" shows exactly what a Sports collection will contain; **"(no genres)"** lists channels that still need an override. Example: setting the override `Sports, News` on one channel and leaving another to inherit `Broadcast` from its tuner's group-title both show up in the column exactly as they'll be emitted.

## 8. Register everything in Channels DVR

Outputs page → **Add all to Channels DVR**. Each output M3U becomes a custom channel source in the DVR (named like `CF Gracenote (HLS) [01]`), with the combined XMLTV guide attached to the EPG lineups. Re-clicking updates in place and prunes sources channelforge created that no longer exist — it never touches sources it didn't create.

Then in the DVR: Settings → scan the new sources, and your collections pick up the genres automatically.

## 9. Automate the whole thing

In **Settings**:

- **Daily schedule** (defaults shown; times are in your configured time zone):
  - `01:00` health checks — *before* the refresh, so dead streams fail over immediately
  - `01:30` refresh
  - `03:00` DVR m3u re-pull
  - `04:00` reset DVR passes
- **Output/guide rebuild interval**: leave at `60` so `cf_guide.xml` is rebuilt hourly from current state before Channels DVR's hourly XMLTV refresh.
- **Pre-refresh hook**: paste your FastChannels address (just `192.168.1.10:5523` is enough — the force-refresh endpoint is filled in). Before each refresh, channelforge triggers a full FastChannels rescrape, watches its progress, and continues the moment every source has finished (the *max minutes* field is only a ceiling).
- **Push outputs to DVR after each refresh**: `On`. New/changed channels flow to the DVR without you touching anything.

Example of a fully automated night in the job log:

```
01:30:00  pre-refresh hook: POST http://192.168.1.10:5523/api/sources/force-refresh -> 200
01:30:00  pre-refresh hook: waiting for 18 sources to rescrape (up to 30 min)...
01:36:40  pre-refresh hook: 16/18 sources rescraped
01:37:40  pre-refresh hook: scraper idle, 2 sources had nothing new; continuing
01:37:40  refreshing sources...
01:38:20  outputs: cf_gracenote_hls_01.m3u (461 channels), ...
01:38:25  dvr: added/updated 3 sources
```

## 10. Bulk editing with CSV

Channels, rules, and assignments each round-trip through CSV: **Export CSV** → edit in a spreadsheet → **Import CSV** (rows match by `id`; blank id inserts new). Fastest way to renumber a hundred channels, add genres in bulk, or hand-tune assignments.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Channel numbers blank | Numbers are assigned during a refresh — run one |
| Genres column blank | Run a refresh after adding sources; if still blank, the feed has no `group-title` either — set overrides |
| A channel has 0 streams | All its source entries disappeared from the feeds (Channels page → edit shows each stream's health: `ok` / `down` / `gone`) |
| Wrong stream is playing | Check the `▶ provider` badge, then fix source Priority (Sources page) or set the channel's preferred source |
| DVR shows stale channels | Outputs → *Add all to Channels DVR* re-registers and prunes; or enable auto-push |
| Refresh waits forever on the hook | Lower the *max minutes* ceiling — sources that never scrape are detected and skipped after ~1 min of scraper idleness |
| Job died with "interrupted by restart" | The container restarted mid-job (e.g. an image pull); just run it again |
