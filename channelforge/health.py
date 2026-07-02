"""Stream health checks with bounded concurrency."""
import asyncio
import datetime

import httpx

from . import db


async def _check(client, sem, sc):
    async with sem:
        try:
            async with client.stream("GET", sc["url"], timeout=10) as r:
                if r.status_code >= 400:
                    return sc["id"], False
                async for _ in r.aiter_bytes():
                    break  # first chunk is enough
            return sc["id"], True
        except Exception:
            return sc["id"], False


async def _run(rows, concurrency):
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        return await asyncio.gather(*[_check(client, sem, r) for r in rows])


def run_health_checks(log=lambda s: None):
    threshold = int(db.get_setting("health_fail_threshold", "3") or 3)
    concurrency = int(db.get_setting("health_concurrency", "20") or 20)
    rows = db.q("""
        SELECT sc.id, sc.url, sc.fail_count FROM source_channels sc
        JOIN sources s ON s.id = sc.source_id
        WHERE s.active = 1 AND s.check_streams = 1 AND sc.present = 1
          AND sc.ignored = 0 AND sc.channel_id IS NOT NULL
    """)
    if not rows:
        log("health: no sources have stream checking enabled")
        return
    log(f"health: checking {len(rows)} streams (concurrency {concurrency})...")
    results = asyncio.run(_run(rows, concurrency))
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fails = {r["id"]: r["fail_count"] for r in rows}
    updates = []
    ok = bad = 0
    for sc_id, healthy in results:
        if healthy:
            ok += 1
            updates.append((1, 0, ts, sc_id))
        else:
            bad += 1
            count = fails.get(sc_id, 0) + 1
            updates.append((0 if count >= threshold else 1, count, ts, sc_id))
    db.executemany("UPDATE source_channels SET healthy = ?, fail_count = ?, last_checked = ? WHERE id = ?", updates)
    log(f"health: {ok} ok, {bad} failing (marked unhealthy after {threshold} consecutive fails)")
