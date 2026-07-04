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
    """Redirect to the referring page, keeping its filters/search intact."""
    from urllib.parse import parse_qsl, urlencode, urlsplit
    ref = urlsplit(request.headers.get("referer") or "/")
    params = [(k, v) for k, v in parse_qsl(ref.query) if k != "flash"]
    if flash:
        params.append(("flash", flash))
    url = (ref.path or "/") + (f"?{urlencode(params)}" if params else "")
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
def channels_page(request: Request, q: str = "", show: str = "all", prov: str = "", genre: str = "", sort: str = "name"):
    where, params = "1=1", []
    if q:
        where += " AND c.name LIKE ?"
        params.append(f"%{q}%")
    if show == "inactive":
        where += " AND c.active = 0"
    order = {"number": "(c.number = ''), CAST(c.number AS REAL), c.name COLLATE NOCASE",
             }.get(sort, "c.name COLLATE NOCASE")
    rows = db.q(f"""SELECT c.*,
                    (SELECT COUNT(*) FROM source_channels sc WHERE sc.channel_id = c.id AND sc.present = 1) n_children
                    FROM channels c WHERE {where} ORDER BY {order}""", params)
    # per channel: providers (channel-id prefix, else source name)
    provs = {}
    for k in db.q("""SELECT sc.channel_id cid, sc.external_id ext, s.name sname
                     FROM source_channels sc JOIN sources s ON s.id = sc.source_id
                     WHERE sc.present = 1 AND sc.channel_id IS NOT NULL"""):
        provs.setdefault(k["cid"], set()).add(m3u.provider_of(k["ext"]) or k["sname"])
    all_provs = sorted({p for ps in provs.values() for p in ps}, key=str.casefold)
    pick_pool = refresh.assigned_children()

    def enrich(r):
        ovr = json.loads(r["attrs"] or "{}")
        best, _ = refresh.pick_stream(pick_pool.get(r["id"], []), r["preferred_source_id"])
        geff = refresh.effective_genres(r, best)   # what the outputs will emit
        gset = {t.strip() for t in geff.split(",") if t.strip()}
        return dict(r, provs=", ".join(sorted(provs.get(r["id"], ()), key=str.casefold)),
                    genres_eff=geff, genres_set=gset,
                    genres_ovr=ovr.get("tvc-guide-genres", ""),
                    pick=(m3u.provider_of(best["external_id"]) or best["src_name"]) if best else "")

    rows = [enrich(r) for r in rows]
    all_genres = sorted({g for r in rows for g in r["genres_set"]}, key=str.casefold)
    if prov:
        rows = [r for r in rows if prov in provs.get(r["id"], ())]
    if genre == "-":
        rows = [r for r in rows if not r["genres_set"]]
    elif genre:
        rows = [r for r in rows if genre in r["genres_set"]]
    if sort == "source":
        rows.sort(key=lambda r: (r["provs"].casefold(), r["name"].casefold()))
    total = db.q1("SELECT COUNT(*) n FROM channels")["n"]
    return render("channels.html", request, channels=rows, q=q, show=show, total=total,
                  all_provs=all_provs, prov=prov, all_genres=all_genres, genre=genre, sort=sort)


@app.get("/channels/{channel_id}/edit", response_class=HTMLResponse)
def channel_edit(channel_id: int):
    """Edit-panel fragment, fetched on demand when a row is expanded — the list
    page must stay free of per-row forms (browser extensions that scan forms,
    like password managers, freeze for ~20s per click on thousands of inputs)."""
    r = db.q1("SELECT * FROM channels WHERE id = ?", (channel_id,))
    if not r:
        return HTMLResponse("channel not found", status_code=404)
    ovr = json.loads(r["attrs"] or "{}")
    pool = refresh.assigned_children().get(channel_id, [])
    best, _ = refresh.pick_stream(pool, r["preferred_source_id"])
    kids = [{"prov": m3u.provider_of(k["external_id"]) or k["src_name"], "src": k["src_name"],
             "ext": k["external_id"], "healthy": k["healthy"], "present": k["present"],
             "win": best is not None and k["id"] == best["id"]} for k in pool]
    c = dict(r, genres_eff=refresh.effective_genres(r, best),
             genres_ovr=ovr.get("tvc-guide-genres", ""), cats_ovr=ovr.get("tvc-guide-categories", ""), kids=kids)
    return HTMLResponse(env.get_template("channel_edit.html").render(c=c))


@app.post("/channels/save")
def channels_save(request: Request, channel_id: str = Form(""), name: str = Form(...), number: str = Form(""),
                  gracenote_id: str = Form(""), tvg_id: str = Form(""), logo: str = Form(""),
                  grp: str = Form(""), active: str = Form(""), genres: str = Form(""), categories: str = Form("")):
    row = db.q1("SELECT attrs FROM channels WHERE id = ?", (int(channel_id),)) if channel_id.isdigit() else None
    attrs = json.loads(row["attrs"] or "{}") if row else {}
    for key, v in (("tvc-guide-genres", genres.strip()), ("tvc-guide-categories", categories.strip())):
        if v:
            attrs[key] = v
        else:
            attrs.pop(key, None)
    vals = (name, number, gracenote_id, tvg_id, logo, grp, 1 if active else 0, json.dumps(attrs))
    if channel_id.isdigit():
        db.execute("UPDATE channels SET name=?, number=?, gracenote_id=?, tvg_id=?, logo=?, grp=?, active=?, attrs=? WHERE id=?", vals + (int(channel_id),))
    else:
        db.execute("INSERT INTO channels(name, number, gracenote_id, tvg_id, logo, grp, active, attrs) VALUES(?,?,?,?,?,?,?,?)", vals)
    return back(request, "saved")


@app.post("/channels/set_number")
def channels_set_number(request: Request, channel_id: int = Form(...), number: str = Form("")):
    db.execute("UPDATE channels SET number = ? WHERE id = ?", (number.strip(), channel_id))
    return back(request, "number saved")


@app.post("/channels/delete")
def channels_delete(request: Request, channel_id: int = Form(...)):
    db.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    return back(request, "deleted")


# ---------- possible-duplicates review ----------
@app.get("/dupes", response_class=HTMLResponse)
def dupes_page(request: Request, q: str = "", confidence: str = "all", page: int = 1, per_page: int = 50):
    groups = rules.find_possible_duplicates()
    provs, kids, guide = {}, {}, {}
    pick_pool = refresh.assigned_children()
    provider_rank = {p: i for i, p in enumerate(json.loads(db.get_setting("provider_order") or "[]"))}
    unranked_provider = len(provider_rank)
    guide_samples = {r["tvg_id"]: r["sample"] for r in db.q("SELECT tvg_id, sample FROM guide_signatures WHERE n >= 8")}

    def add_guide(cid, label, value):
        value = (value or "").strip()
        if not value:
            return
        guide.setdefault(cid, {}).setdefault(label, set()).add(value)

    for k in db.q("""SELECT sc.channel_id cid, sc.external_id ext, sc.attrs, s.name sname
                     FROM source_channels sc JOIN sources s ON s.id = sc.source_id
                     WHERE sc.present = 1 AND sc.channel_id IS NOT NULL"""):
        provs.setdefault(k["cid"], set()).add(m3u.provider_of(k["ext"]) or k["sname"])
        kids[k["cid"]] = kids.get(k["cid"], 0) + 1
        attrs = db.attrs_of(k)
        add_guide(k["cid"], "station", attrs.get("tvc-guide-stationid"))
        tvg_id = attrs.get("tvg-id")
        add_guide(k["cid"], "tvg", tvg_id)
        add_guide(k["cid"], "title", attrs.get("tvc-guide-title") or attrs.get("tvg-name"))
        add_guide(k["cid"], "desc", attrs.get("tvc-guide-description") or attrs.get("tvg-description"))
        add_guide(k["cid"], "lineup", guide_samples.get(tvg_id))

    def guide_hint(c):
        data = {k: set(v) for k, v in guide.get(c["id"], {}).items()}
        if c["tvg_id"]:
            add = guide_samples.get(c["tvg_id"])
            if add:
                data.setdefault("lineup", set()).add(add)
        parts = []
        for label in ("station", "tvg", "title", "lineup"):
            vals = sorted(data.get(label, ()), key=str.casefold)
            if vals:
                parts.append(f"{label}: {vals[0]}")
        descs = sorted(data.get("desc", ()), key=str.casefold)
        if descs:
            desc = descs[0]
            parts.append((desc[:90] + "...") if len(desc) > 90 else desc)
        return " | ".join(parts)

    def best_stream_info(c):
        best, _fmt = refresh.pick_stream(pick_pool.get(c["id"], []), c["preferred_source_id"])
        if not best:
            return "", "", "", 999999, unranked_provider, 999999999
        provider = m3u.provider_of(best["external_id"])
        label = provider or best["src_name"]
        if best["src_name"] and provider:
            label = f"{best['src_name']} / {provider}"
        priority = best["src_priority"]
        rank = provider_rank.get(provider, unranked_provider)
        detail = f"source priority {priority}"
        if provider:
            detail += f", provider rank {rank + 1 if rank < unranked_provider else 'unranked'}"
        return label, best["url"], detail, priority, rank, best["id"]

    for g in groups:
        rows = []
        for c in g["channels"]:
            best_label, best_url, best_detail, best_priority, best_provider_rank, best_id = best_stream_info(c)
            rows.append(dict(c, provs=", ".join(sorted(provs.get(c["id"], ()), key=str.casefold)),
                             n_children=kids.get(c["id"], 0), guide_hint=guide_hint(c),
                             best_stream=best_label, best_stream_url=best_url, best_detail=best_detail,
                             best_priority=best_priority, best_provider_rank=best_provider_rank,
                             best_stream_id=best_id))
        g["channels"] = sorted(rows, key=lambda c: (
            c["best_priority"], c["best_provider_rank"], c["best_stream_id"], c["name"].casefold()))
    groups.sort(key=lambda g: (
        g.get("confidence_rank", 9),
        g["channels"][0]["best_priority"] if g["channels"] else 999999,
        g["channels"][0]["name"].casefold() if g["channels"] else ""))
    if q:
        needle = q.casefold()
        groups = [g for g in groups if needle in g["why"].casefold()
                  or any(needle in c["name"].casefold() or needle in c["guide_hint"].casefold()
                         for c in g["channels"])]
    counts = {"all": len(groups), "strong": 0, "medium": 0, "weak": 0}
    for g in groups:
        if g.get("confidence") in counts:
            counts[g["confidence"]] += 1
    if confidence not in counts:
        confidence = "all"
    if confidence != "all":
        groups = [g for g in groups if g.get("confidence") == confidence]
    total = len(groups)
    per_page = min(max(per_page, 10), 200)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(page, 1), pages)
    start = (page - 1) * per_page
    return render("dupes.html", request, groups=groups[start:start + per_page],
                  total=total, counts=counts, q=q, confidence=confidence,
                  page_no=page, pages=pages, per_page=per_page)


@app.post("/dupes/merge")
def dupes_merge(request: Request, keeper_id: int = Form(...), merge_ids: list[int] = Form([])):
    losers = [i for i in merge_ids if i != keeper_id]
    if keeper_id not in merge_ids or not losers:
        return back(request, "pick a keeper and at least one other channel to merge")
    n = rules.merge_channels(keeper_id, losers)
    keeper = db.q1("SELECT name FROM channels WHERE id = ?", (keeper_id,))
    return back(request, f"merged {n} channels into '{keeper['name']}'" if n else "nothing merged")


@app.post("/dupes/dismiss")
def dupes_dismiss(request: Request, group_ids: str = Form(...)):
    ids = sorted({int(x) for x in group_ids.split(",") if x.strip().isdigit()})
    pairs = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]]
    db.executemany("INSERT OR IGNORE INTO dupe_dismissed(a, b) VALUES(?, ?)", pairs)
    return back(request, "dismissed — this group won't be suggested again")


@app.post("/dupes/dismiss_many")
def dupes_dismiss_many(request: Request, group_sets: str = Form(...)):
    pairs = []
    for group in group_sets.split(";"):
        ids = sorted({int(x) for x in group.split(",") if x.strip().isdigit()})
        pairs.extend((a, b) for i, a in enumerate(ids) for b in ids[i + 1:])
    db.executemany("INSERT OR IGNORE INTO dupe_dismissed(a, b) VALUES(?, ?)", pairs)
    return back(request, f"dismissed {len(pairs)} weak duplicate hints")


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
    return render("settings.html", request, s=s, dvr_status=channels_dvr.ping(),
                  tznow=db.local_now().strftime("%H:%M"))


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    for key in db.DEFAULT_SETTINGS:
        if key in form:
            db.set_setting(key, str(form[key]).strip())
    return back(request, "saved")
