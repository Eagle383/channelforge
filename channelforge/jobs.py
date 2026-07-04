"""Background job runner (one heavy job at a time) and daily scheduler."""
import datetime
import threading
import time
import traceback

from . import channels_dvr, db, health, refresh, rules

_lock = threading.Lock()

JOB_TYPES = {
    "refresh": refresh.run_refresh,
    "quick_refresh": refresh.run_quick_refresh,
    "outputs": refresh.run_outputs,
    "apply_rules": rules.apply_all,
    "dedupe": refresh.run_dedupe,
    "health": health.run_health_checks,
    "reset_passes": channels_dvr.reset_passes,
    "refresh_dvr_m3u": channels_dvr.refresh_m3u_playlists,
}


def _now():
    return db.local_now().strftime("%Y-%m-%d %H:%M:%S")


def start_job(job_type, extra=None):
    """Start job in a thread. Returns job id, or None if one is already running."""
    fn = extra or JOB_TYPES.get(job_type)
    if fn is None:
        return None
    if not _lock.acquire(blocking=False):
        return None
    cutoff = (db.local_now() - datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("DELETE FROM jobs WHERE started < ? AND status != 'running'", (cutoff,))
    job_id = db.execute("INSERT INTO jobs(type, status, started) VALUES(?, 'running', ?)", (job_type, _now())).lastrowid

    def log(line):
        db.execute("UPDATE jobs SET log = log || ? WHERE id = ?", (f"{_now()}  {line}\n", job_id))

    def run():
        try:
            fn(log)
            db.execute("UPDATE jobs SET status = 'done', finished = ? WHERE id = ?", (_now(), job_id))
        except Exception:
            log("FAILED:\n" + traceback.format_exc())
            db.execute("UPDATE jobs SET status = 'failed', finished = ? WHERE id = ?", (_now(), job_id))
        finally:
            _lock.release()

    threading.Thread(target=run, daemon=True).start()
    return job_id


def running_job():
    return db.q1("SELECT * FROM jobs WHERE status = 'running' ORDER BY id DESC LIMIT 1")


def _interval_minutes(key):
    try:
        raw = (db.get_setting(key) or "").strip()
    except Exception:
        return 0
    if not raw.isdigit():
        return 0
    return max(0, int(raw))


def _interval_due(key, now, fired):
    minutes = _interval_minutes(key)
    if minutes <= 0:
        return False
    last = fired.get(key)
    return last is None or (now - last).total_seconds() >= minutes * 60


def scheduler_loop():
    """Fire daily jobs at HH:MM and interval jobs when due."""
    fired = {"schedule.outputs_interval_min": db.local_now()}  # key -> date/datetime fired
    while True:
        now = db.local_now()
        hhmm = now.strftime("%H:%M")
        today = now.date()
        for key, job_type in (("schedule.refresh", "refresh"), ("schedule.health", "health"),
                              ("schedule.reset_passes", "reset_passes"), ("schedule.refresh_dvr_m3u", "refresh_dvr_m3u")):
            try:
                configured = db.get_setting(key)
            except Exception:
                continue
            if configured and configured == hhmm and fired.get(key) != today:
                fired[key] = today
                start_job(job_type)
        key = "schedule.outputs_interval_min"
        if _interval_due(key, now, fired):
            fired[key] = now
            start_job("outputs")
        time.sleep(20)


def start_scheduler():
    threading.Thread(target=scheduler_loop, daemon=True).start()
