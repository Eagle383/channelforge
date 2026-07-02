"""channelforge web app."""
import json
import os

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

from . import channels_dvr, csv_io, db, importer, jobs, m3u, refresh, rules

BASE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="channelforge")
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
env = Environment(loader=FileSystemLoader(os.path.join(BASE, "templates")), autoescape=True)

VERSION = "0.1.0"


@app.on_event("startup")
def startup():
    db.init()
    jobs.start_scheduler()


def render(name, request, **ctx):
    ctx.update(page=name.replace(".html", ""), version=VERSION, running=jobs.running_job(),
               flash=request.query_params.get("flash", ""))
    return HTMLResponse(env.get_template(name).render(**ctx))


def back(request, flash=""):
    url = request.headers.get("referer") or "/"
    url = url.split("?")[0]
    if flash:
        from urllib.parse import quote
        url += f"?flash={quote(flash)}"
    return RedirectResponse(url, status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    stats = {
        "sources": db.q1("SELECT COUNT(*) n FROM sources WHERE active = 1")["n"],
        "channels": db.q1("SELECT COUNT(*) n FROM channels WHERE active = 1")["n"],
        "children": db.q1("SELECT COUNT(*) n FROM source_channels WHERE present = 1")["n"],
        "unassigned": db.q1("SELECT COUNT(*) n FROM source_channels WHERE present = 1 AND ignored = 0 AND channel_id IS NULL")["n"],
        "unhealthy": db.q1("SELECT COUNT(*) n FROM source_channels WHERE present = 1 AND healthy = 0")["n"],
        "dvr": channels_dvr.ping() if db.get_setting("channels_dvr_url") else "not configured",
    }
    recent = db.q("SELECT * FROM jobs ORDER BY id DESC LIMIT 8")
    return render("dashboard.html", request, stats=stats, recent=recent)


# ---------- sources ----------
@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request):
    rows = db.q("""SELECT s.*, (SELECT COUNT(*) FROM source_channels sc WHERE sc.source_id = s.id AND sc.present = 1) n
                   FROM sources s ORDER BY s.priority, s.name""")
    order = json.loads(db.get_setting("provider_order") or "[]")
    found = {m3u.provider_of(r["external_id"]) for r in db.q("SELECT DISTINCT external_id FROM source_channels WHERE present = 1")} - {""}
    providers = [p for p in order if p in found] + sorted(found - set(order))
    return render("sources.html", request, sources=rows, providers=providers)


@app.post("/sources/provider_order")
async def sources_provider_order(request: Request):
    form = await request.form()
    order = [p.strip() for p in str(form.get("order", "")).split(",") if p.strip()]
    db.set_setting("provider_order", json.dumps(order))
    return PlainTextResponse("ok")


@app.post("/sources/save")
def sources_save(request: Request, source_id: str = Form(""), name: str = Form(...), url: str = Form(...),
                 epg_url: str = Form(""), stream_format: str = Form("HLS"), priority: int = Form(100),
                 active: str = Form(""), check_streams: str = Form("")):
    vals = (name, url, epg_url, stream_format, priority, 1 if active else 0, 1 if check_streams else 0)
    if source_id.isdigit():
        db.execute("UPDATE sources SET name=?, url=?, epg_url=?, stream_format=?, priority=?, active=?, check_streams=? WHERE id=?", vals + (int(source_id),))
    else:
        db.execute("INSERT INTO sources(name, url, epg_url, stream_format, priority, active, check_streams) VALUES(?,?,?,?,?,?,?)", vals)
    return back(request, "saved")


@app.post("/sources/delete")
def sources_delete(request: Request, source_id: int = Form(...)):
    db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    return back(request, "deleted")


# ---------- channels ----------
@app.get("/channels", response_class=HTMLResponse)
def channels_page(request: Request, q: str = "", show: str = "all"):
    where, params = "1=1", []
    if q:
        where += " AND c.name LIKE ?"
        params.append(f"%{q}%")
    if show == "inactive":
        where += " AND c.active = 0"
    rows = db.q(f"""SELECT c.*, (SELECT COUNT(*) FROM source_channels sc WHERE sc.channel_id = c.id AND sc.present = 1) n_children
                    FROM channels c WHERE {where} ORDER BY c.name COLLATE NOCASE""", params)
    total = db.q1("SELECT COUNT(*) n FROM channels")["n"]
    return render("channels.html", request, channels=rows, q=q, show=show, total=total)


@app.post("/channels/save")
def channels_save(request: Request, channel_id: str = Form(""), name: str = Form(...), number: str = Form(""),
                  gracenote_id: str = Form(""), tvg_id: str = Form(""), logo: str = Form(""),
                  grp: str = Form(""), active: str = Form("")):
    vals = (name, number, gracenote_id, tvg_id, logo, grp, 1 if active else 0)
    if channel_id.isdigit():
        db.execute("UPDATE channels SET name=?, number=?, gracenote_id=?, tvg_id=?, logo=?, grp=?, active=? WHERE id=?", vals + (int(channel_id),))
    else:
        db.execute("INSERT INTO channels(name, number, gracenote_id, tvg_id, logo, grp, active) VALUES(?,?,?,?,?,?,?)", vals)
    return back(request, "saved")


@app.post("/channels/delete")
def channels_delete(request: Request, channel_id: int = Form(...)):
    db.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    return back(request, "deleted")


# ---------- assignments (children) ----------
@app.get("/assign", response_class=HTMLResponse)
def assign_page(request: Request, q: str = "", show: str = "unassigned"):
    where, params = "sc.present = 1", []
    if show == "unassigned":
        where += " AND sc.channel_id IS NULL AND sc.ignored = 0"
    elif show == "ignored":
        where += " AND sc.ignored = 1"
    elif show == "unhealthy":
        where += " AND sc.healthy = 0"
    if q:
        where += " AND sc.name LIKE ?"
        params.append(f"%{q}%")
    rows = db.q(f"""SELECT sc.*, s.name AS source_name, c.name AS channel_name
                    FROM source_channels sc JOIN sources s ON s.id = sc.source_id
                    LEFT JOIN channels c ON c.id = sc.channel_id
                    WHERE {where} ORDER BY sc.name COLLATE NOCASE""", params)
    counts = {
        "unassigned": db.q1("SELECT COUNT(*) n FROM source_channels WHERE present=1 AND channel_id IS NULL AND ignored=0")["n"],
        "ignored": db.q1("SELECT COUNT(*) n FROM source_channels WHERE present=1 AND ignored=1")["n"],
        "all": db.q1("SELECT COUNT(*) n FROM source_channels WHERE present=1")["n"],
    }
    return render("assign.html", request, rows=rows, q=q, show=show, counts=counts)


def _auto_rule(sc_name, action, target_id):
    """Create an equals rule mirroring a manual assign/ignore, then re-run rules
    so every other copy of the channel follows immediately."""
    if not sc_name or db.q1("SELECT id FROM rules WHERE match_field = 'name' AND match_type = 'equals' AND pattern = ?", (sc_name,)):
        return ""
    db.execute("INSERT INTO rules(name, priority, active, match_field, match_type, pattern, action, target_channel_id, set_field, set_value) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (f"auto: {sc_name}", 100, 1, "name", "equals", sc_name, action, target_id, "", ""))
    assigned, ignored = rules.apply_all()
    return f"; rule created ({assigned} more assigned, {ignored} ignored)"


@app.post("/assign/set")
def assign_set(request: Request, sc_id: int = Form(...), target: str = Form("")):
    target = target.strip()
    sc = db.q1("SELECT name FROM source_channels WHERE id = ?", (sc_id,))
    auto = db.get_setting("auto_rule_on_assign") == "1" and sc is not None
    extra = ""
    if target.casefold() == "ignore":
        db.execute("UPDATE source_channels SET ignored = 1, channel_id = NULL WHERE id = ?", (sc_id,))
        if auto:
            extra = _auto_rule(sc["name"], "ignore", None)
    elif not target:
        db.execute("UPDATE source_channels SET ignored = 0, channel_id = NULL WHERE id = ?", (sc_id,))
    else:
        row = db.q1("SELECT id FROM channels WHERE id = ? OR name = ? COLLATE NOCASE",
                    (int(target) if target.isdigit() else -1, target))
        if row is None:
            cur = db.execute("INSERT INTO channels(name) VALUES(?)", (target,))
            cid = cur.lastrowid
        else:
            cid = row["id"]
        db.execute("UPDATE source_channels SET channel_id = ?, ignored = 0 WHERE id = ?", (cid, sc_id))
        if auto:
            extra = _auto_rule(sc["name"], "assign", cid)
    return back(request, "updated" + extra)


# ---------- rules ----------
@app.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request, q: str = ""):
    where, params = "1=1", []
    if q:
        where += " AND (r.name LIKE ? OR r.pattern LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    rows = db.q(f"""SELECT r.*, s.name AS source_name, c.name AS target_name
                    FROM rules r LEFT JOIN sources s ON s.id = r.source_id
                    LEFT JOIN channels c ON c.id = r.target_channel_id
                    WHERE {where} ORDER BY r.priority, r.id""", params)
    total = db.q1("SELECT COUNT(*) n FROM rules")["n"]
    return render("rules.html", request, rules=rows, q=q, total=total,
                  match_fields=rules.MATCH_FIELDS, match_types=rules.MATCH_TYPES, set_fields=rules.SET_FIELDS)


@app.post("/rules/save")
def rules_save(request: Request, rule_id: str = Form(""), name: str = Form(""), priority: int = Form(100),
               match_field: str = Form("name"), match_type: str = Form("equals"), pattern: str = Form(...),
               action: str = Form("assign"), target: str = Form(""), set_field: str = Form(""),
               set_value: str = Form(""), active: str = Form("")):
    target_id = None
    if action == "assign":
        row = db.q1("SELECT id FROM channels WHERE id = ? OR name = ? COLLATE NOCASE",
                    (int(target) if target.strip().isdigit() else -1, target.strip()))
        if row is None:
            return back(request, f"unknown channel: {target}")
        target_id = row["id"]
    vals = (name, priority, 1 if active else 0, match_field, match_type, pattern, action, target_id, set_field, set_value)
    if rule_id.isdigit():
        db.execute("UPDATE rules SET name=?, priority=?, active=?, match_field=?, match_type=?, pattern=?, action=?, target_channel_id=?, set_field=?, set_value=? WHERE id=?", vals + (int(rule_id),))
    else:
        db.execute("INSERT INTO rules(name, priority, active, match_field, match_type, pattern, action, target_channel_id, set_field, set_value) VALUES(?,?,?,?,?,?,?,?,?,?)", vals)
    return back(request, "saved")


@app.post("/rules/delete")
def rules_delete(request: Request, rule_id: int = Form(...)):
    db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    return back(request, "deleted")


# ---------- csv round-trip ----------
EXPORTERS = {"channels": csv_io.export_channels, "rules": csv_io.export_rules, "assignments": csv_io.export_assignments}
IMPORTERS = {"channels": csv_io.import_channels, "rules": csv_io.import_rules, "assignments": csv_io.import_assignments}


@app.get("/export/{what}.csv")
def export_csv(what: str):
    fn = EXPORTERS.get(what)
    if fn is None:
        return PlainTextResponse("unknown export", status_code=404)
    return Response(fn(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="channelforge_{what}.csv"'})


@app.post("/import/{what}")
async def import_csv(request: Request, what: str, file: UploadFile):
    fn = IMPORTERS.get(what)
    if fn is None:
        return PlainTextResponse("unknown import", status_code=404)
    result = fn(await file.read())
    return back(request, result)


# ---------- legacy migration ----------
@app.get("/migrate", response_class=HTMLResponse)
def migrate_page(request: Request):
    return render("migrate.html", request)


@app.post("/migrate")
async def migrate_run(request: Request, playlists: UploadFile = None, parents: UploadFile = None,
                      child_to_parent: UploadFile = None, station_mappings: UploadFile = None):
    files = {}
    for key, f in (("playlists", playlists), ("parents", parents),
                   ("child_to_parent", child_to_parent), ("station_mappings", station_mappings)):
        if f is not None and f.filename:
            files[key] = await f.read()
    lines = []
    importer.import_legacy(files, log=lines.append)
    return render("migrate.html", request, results=lines)


# ---------- outputs ----------
@app.get("/outputs", response_class=HTMLResponse)
def outputs_page(request: Request):
    d = refresh.out_dir()
    files = sorted(f for f in os.listdir(d) if not f.startswith("."))
    base = db.get_setting("base_url") or str(request.base_url).rstrip("/")
    return render("outputs.html", request, files=files, base=base)


@app.post("/outputs/add_to_dvr")
def outputs_add_to_dvr(request: Request):
    d = refresh.out_dir()
    files = sorted(f for f in os.listdir(d) if not f.startswith("."))
    base = db.get_setting("base_url") or str(request.base_url).rstrip("/")
    return back(request, channels_dvr.add_output_sources(base, files))


@app.get("/out/{fname}")
def outputs_file(fname: str):
    path = os.path.join(refresh.out_dir(), os.path.basename(fname))
    if not os.path.isfile(path):
        return PlainTextResponse("not found", status_code=404)
    media = "application/xml" if fname.endswith(".xml") else "audio/x-mpegurl"
    return FileResponse(path, media_type=media, filename=fname)


# ---------- jobs ----------
@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    rows = db.q("SELECT * FROM jobs ORDER BY id DESC LIMIT 30")
    return render("jobs.html", request, jobs=rows)


@app.get("/jobs/{job_id}/log")
def job_log(job_id: int):
    row = db.q1("SELECT log, status FROM jobs WHERE id = ?", (job_id,))
    if row is None:
        return PlainTextResponse("not found", status_code=404)
    return PlainTextResponse(row["log"] + ("\n[running...]" if row["status"] == "running" else ""))


@app.post("/jobs/run")
def jobs_run(request: Request, job_type: str = Form(...)):
    job_id = jobs.start_job(job_type)
    return back(request, f"started {job_type} (job {job_id})" if job_id else "a job is already running")


# ---------- settings ----------
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    s = {r["key"]: r["value"] for r in db.q("SELECT * FROM settings")}
    return render("settings.html", request, s=s, dvr_status=channels_dvr.ping())


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    for key in db.DEFAULT_SETTINGS:
        if key in form:
            db.set_setting(key, str(form[key]).strip())
    return back(request, "saved")
