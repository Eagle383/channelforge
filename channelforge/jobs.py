"""Background job runner (one heavy job at a time) and daily scheduler."""
import datetime
import threading
import time
import traceback

from . import channels_dvr, db, health, refresh, rules

_lock = threading.Lock()

JOB_TYPES = {
    "refresh": refresh.run_refresh,
    "apply_rules": rules.apply_all,
    "health": health.run_health_checks,
    "reset_passes": channels_dvr.reset_passes,
    "refresh_dvr_m3u": channels_dvr.refresh_m3u_playlists,
}


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def start_job(job_type, extra=None):
    """Start job in a thread. Returns job id, or None if one is already running."""
    fn = extra or JOB_TYPES.get(job_type)
    if fn is None:
        return None
    if not _lock.acquire(blocking=False):
        return None
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


def scheduler_loop():
    """Fire jobs at their configured HH:MM once per day."""
    fired = {}  # key -> date fired
    while True:
        now = datetime.datetime.now()
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
        time.sleep(20)


def start_scheduler():
    threading.Thread(target=scheduler_loop, daemon=True).start()
