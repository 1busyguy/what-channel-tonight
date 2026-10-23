#!/usr/bin/env python3
"""Build the What Channel Tonight static site.

    python build.py            # fetch ESPN data + rebuild dist/
    python build.py --offline  # rebuild from cached data only

Safety rules: data for each league/day falls back to the last good cached copy;
the site is built into a temp dir and only swapped into dist/ if every check passes.
"""
import collections, datetime as dt, html, json, os, re, shutil, sys
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import markdown, yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

import espn

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
TMP = os.path.join(ROOT, ".tmp_dist")
SNAP = os.path.join(ROOT, "data", "snapshot.json")


def load_yaml(name):
    with open(os.path.join(ROOT, name)) as f:
        return yaml.safe_load(f)


CFG = load_yaml("config.yaml")
CFG["SITE_URL"] = os.environ.get("SITE_URL", CFG["SITE_URL"]).rstrip("/")
CFG["SITE_NAME"] = os.environ.get("SITE_NAME", CFG["SITE_NAME"])
CFG["GOATCOUNTER_CODE"] = os.environ.get("GOATCOUNTER_CODE", CFG.get("GOATCOUNTER_CODE") or "")
BASE_PATH = urlparse(CFG["SITE_URL"]).path.rstrip("/")
AFF = load_yaml("affiliates.yaml")
NETRULES = load_yaml("networks.yaml")
NOTES = load_yaml("leagues_notes.yaml")
FEATURED = load_yaml("featured.yaml") or {}
TZ = ZoneInfo(CFG.get("TIMEZONE", "America/New_York"))
NOW = dt.datetime.now(TZ)
TODAY = NOW.date()
UPDATED = NOW.strftime("%b %-d, %Y, %-I:%M %p ET")
UPDATED_ISO = NOW.isoformat(timespec="seconds")
LEAGUES = CFG["LEAGUES"]
LG = {l["key"]: l for l in LEAGUES}


def url(path):
    return BASE_PATH + path


def abs_url(path):
    return CFG["SITE_URL"] + path


# ---------------------------------------------------------------- networks
def classify(name, market="local"):
    for r in NETRULES:
        if market == "national" and r.get("regex") == ".*":
            return {"name": name, "label": name, "kind": "cable",
                    "services": [s for s in ["youtube-tv", "fubo", "sling", "directv-stream", "hulu-live"] if s in AFF],
                    "note": "A national channel. Most live TV streaming services carry it, but check the lineup."}
        hit = False
        if "names" in r and name.lower() in [n.lower() for n in r["names"]]:
            hit = True
        elif "regex" in r and re.search(r["regex"], name):
            hit = True
        if hit:
            return {"name": name, "label": r.get("label", name) if "names" in r else name,
                    "kind": r["kind"], "services": [s for s in r.get("services", []) if s in AFF],
                    "note": r.get("note", "")}
    return {"name": name, "label": name, "kind": "regional", "services": [], "note": ""}


KIND_LABEL = {"broadcast": "Broadcast TV", "cable": "Cable", "streaming": "Streaming", "regional": "Regional TV",
              "ota": "Local TV (free OTA)", "team-app": "In-market stream", "league-pass": "League package"}


def watch_rows(ev, side=None):
    """Channels relevant to a viewer. side=None => everything; 'home'/'away' => national + that side."""
    rows = []
    b = ev["broadcasts"]
    start = dt.datetime.fromisoformat(ev["start_utc"]).astimezone(TZ)
    nfl_regional = ev.get("league") == "nfl" and start.weekday() == 6 and start.hour < 19
    for n in b["national"]:
        c = classify(n, "national")
        if nfl_regional and n.upper() in ("CBS", "FOX"):
            rows.append({**c, "where": "Regional", "note": f"Sunday afternoon {n.upper()} games are regional: shown on local {n.upper()} stations in the teams' markets (and other markets the network assigns). Out of market: NFL Sunday Ticket."})
        else:
            rows.append({**c, "where": "National"})
    for s in ("home", "away"):
        if side and s != side:
            continue
        team = ev.get(s) or {}
        for n in b[s]:
            c = classify(n)
            if c["kind"] == "league-pass":
                rows.append({**c, "where": "Out-of-market"})
            else:
                rows.append({**c, "where": f"{team.get('short') or s.title()} local"})
    return rows


def services_for(rows, extra=()):
    seen, out = set(), []
    lists = [r["services"] for r in rows]
    for i in range(max([len(l) for l in lists] or [0])):  # round-robin so every channel gets an option early
        for l in lists:
            if i < len(l) and l[i] not in seen:
                seen.add(l[i]); out.append(l[i])
    for s in extra:
        if s not in seen and s in AFF:
            seen.add(s); out.append(s)
    return [{"key": s, **AFF[s]} for s in out]


def channel_sentence(rows):
    nat = [r["label"] for r in rows if r["where"] == "National"]
    reg = [r["label"] for r in rows if r["where"] == "Regional"]
    loc = [r["label"] for r in rows if r["where"].endswith("local")]
    parts = []
    if nat:
        parts.append(join_and(nat) + " (national)")
    if reg:
        parts.append(join_and(reg) + " (regional: your local station)")
    if loc:
        parts.append(join_and(loc) + " (local)")
    return "; ".join(parts) if parts else "TV listing not announced yet"


def join_and(xs):
    xs = list(dict.fromkeys(xs))
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


# ---------------------------------------------------------------- data
def load_data(offline=False):
    snap = {}
    if os.path.exists(SNAP):
        with open(SNAP) as f:
            snap = json.load(f)
    data = {"leagues": {}, "teams": {}, "sources": {}}
    for lg in LEAGUES:
        k = lg["key"]
        if offline:
            ev = snap.get("leagues", {}).get(k, [])
            src = {"offline": "snapshot"}
        else:
            print(f"Fetching {k} ...")
            ev, src = espn.fetch_league(lg, TODAY, CFG["DAYS_BACK"], CFG["DAYS_AHEAD"])
            if all(v == "missing" for v in src.values()) and snap.get("leagues", {}).get(k):
                print(f"  ! {k}: every request failed; using last snapshot")
                ev = snap["leagues"][k]
        data["leagues"][k] = ev
        data["sources"][k] = collections.Counter(src.values())
        if lg.get("team_pages"):
            teams = snap.get("teams", {}).get(k, []) if offline else espn.fetch_teams(lg)
            if not teams:
                teams = snap.get("teams", {}).get(k, [])
            data["teams"][k] = teams
    total = sum(len(v) for v in data["leagues"].values())
    if total == 0:
        sys.exit("ABORT: no events for any league (API down and no cache). dist/ left untouched.")
    for k in ("nfl", "nba", "nhl"):
        if len(data["teams"].get(k, [])) < 28:
            sys.exit(f"ABORT: only {len(data['teams'].get(k, []))} {k} teams. dist/ left untouched.")
    os.makedirs(os.path.dirname(SNAP), exist_ok=True)
    with open(SNAP, "w") as f:
        json.dump({"saved": UPDATED_ISO, "leagues": data["leagues"], "teams": data["teams"]}, f)
    return data


def is_upcoming(ev):
    if ev["state"] == "post":
        return False
    start = dt.datetime.fromisoformat(ev["start_utc"])
    return ev["state"] == "in" or start > NOW - dt.timedelta(hours=5)


def team_events(events, team_id):
    return [e for e in events if (e.get("home") or {}).get("id") == team_id or (e.get("away") or {}).get("id") == team_id]


def side_of(ev, team_id):
    return "home" if (ev.get("home") or {}).get("id") == team_id else "away"


def local_networks(evs, team_id):
    c = collections.Counter()
    for e in evs:
        for n in e["broadcasts"][side_of(e, team_id)]:
            if classify(n)["kind"] != "league-pass":
                c[n] += 1
    return [classify(n) for n, _ in c.most_common(4)]


# ---------------------------------------------------------------- JSON-LD
def sports_event_ld(ev, lg):
    d = {"@type": "SportsEvent", "name": ev["name"], "startDate": ev["start_utc"],
         "eventStatus": "https://schema.org/EventScheduled",
         "sport": {"football": "American football", "basketball": "Basketball", "hockey": "Ice hockey",
                   "baseball": "Baseball", "racing": "Auto racing"}.get(lg["sport"], lg["sport"]),
         "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode"}
    if ev.get("venue"):
        d["location"] = {"@type": "Place", "name": ev["venue"],
                         "address": {"@type": "PostalAddress", "addressLocality": ev.get("city") or ""}}
    for s, key in (("home", "homeTeam"), ("away", "awayTeam")):
        if ev.get(s):
            d[key] = {"@type": "SportsTeam", "name": ev[s]["name"]}
    if ev.get("home") and ev.get("away"):
        d["competitor"] = [d["homeTeam"], d["awayTeam"]]
    return d


def faq_ld(faqs):
    return {"@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": q["q"], "acceptedAnswer": {"@type": "Answer", "text": q["a"]}} for q in faqs]}


def ld_json(items, page):
    graph = [{"@type": "WebPage", "name": page["title"], "url": abs_url(page["path"]),
              "description": page["description"], "dateModified": UPDATED_ISO,
              "isPartOf": {"@type": "WebSite", "name": CFG["SITE_NAME"], "url": abs_url("/")}}]
    if page.get("crumbs"):
        graph.append({"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": n, "item": abs_url(p)} for i, (n, p) in enumerate(page["crumbs"])]})
    graph += items
    return json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False).replace("</", "<\\/")


# ---------------------------------------------------------------- render
env = Environment(loader=FileSystemLoader(os.path.join(ROOT, "templates")), autoescape=select_autoescape(["html"]),
                  trim_blocks=True, lstrip_blocks=True)
env.globals.update(url=url, abs_url=abs_url, CFG=CFG, UPDATED=UPDATED, UPDATED_ISO=UPDATED_ISO, LEAGUES=LEAGUES,
                   KIND_LABEL=KIND_LABEL, watch_rows=watch_rows, services_for=services_for, AFF=AFF,
                   channel_sentence=channel_sentence, TODAY=TODAY.isoformat())
PAGES = []


def write(path, tpl, **ctx):
    page = ctx.setdefault("page", {})
    page["path"] = path
    page.setdefault("noindex", False)
    out = env.get_template(tpl).render(**ctx)
    fp = os.path.join(TMP, path.lstrip("/"), "index.html") if path.endswith("/") else os.path.join(TMP, path.lstrip("/"))
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w") as f:
        f.write(out)
    if not page["noindex"]:
        PAGES.append({"path": path, "priority": page.get("priority", "0.6"), "html_len": len(out), "file": fp})


def day_groups(evs):
    g = collections.OrderedDict()
    for e in evs:
        g.setdefault(e["date_et"], {"label": e["day_label"], "events": []})["events"].append(e)
    return g


def team_faqs(team, lg, nxt, today_game, rows, svc, locals_):
    noun = lg["noun"]
    if today_game:
        a1 = (f"The {team['name']} play {opp_phrase(today_game, team['id'])} today at {today_game['time_et']}. "
              f"Channel: {channel_sentence(rows)}.")
    elif nxt:
        a1 = (f"The {team['name']} don't play tonight. Their next {noun} is {nxt['day_label']} at {nxt['time_et']} "
              f"{opp_phrase(nxt, team['id'])}, on {channel_sentence(rows)}.")
    else:
        a1 = f"The {team['name']} have no {noun}s scheduled in the next two weeks."
    faqs = [{"q": f"What channel is the {team['short']} {noun} on tonight?", "a": a1}]
    if svc:
        faqs.append({"q": f"How can I watch the {team['short']} {noun} without cable?",
                     "a": "Legal streaming options for the next game: " + ", ".join(s["name"] for s in svc[:6]) +
                          ". Channel lineups vary by ZIP code, so check that the service carries the channel in your area."})
    if locals_:
        faqs.append({"q": f"What is the {team['short']} local TV channel?",
                     "a": f"Recent {team['short']} schedule listings show these local broadcasters: " +
                          ", ".join(l["label"] for l in locals_) + ". Local broadcasts are only available in the team's home territory."})
    n = NOTES.get(lg["key"], {})
    faqs.append({"q": f"Can I watch the {team['short']} if I live out of market?", "a": n.get("out_of_market", "")})
    return faqs


def opp_phrase(ev, team_id):
    if side_of(ev, team_id) == "home":
        return f"vs. the {(ev.get('away') or {}).get('name', 'TBD')}"
    return f"at the {(ev.get('home') or {}).get('name', 'TBD')}"


def build(data):
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP)
    shutil.copytree(os.path.join(ROOT, "static"), os.path.join(TMP, "static"))
    leagues_ctx = []
    for lg in LEAGUES:
        evs = data["leagues"][lg["key"]]
        up = [e for e in evs if is_upcoming(e)]
        today = [e for e in evs if e["date_et"] == TODAY.isoformat()]
        nxt_day = next((e["date_et"] for e in up if e["date_et"] > TODAY.isoformat()), None)
        leagues_ctx.append({"lg": lg, "today": today, "upcoming": up,
                            "next_day": [e for e in up if e["date_et"] == nxt_day] if nxt_day else [],
                            "teams": data["teams"].get(lg["key"], [])})
    LC = {x["lg"]["key"]: x for x in leagues_ctx}
    env.globals["LC"] = LC
    featured_links = [(f["title"], f["path"]) for f in FEATURED.get("links", [])]
    env.globals["FEATURED_LINKS"] = featured_links

    # ---- hub
    n_today = sum(len(x["today"]) for x in leagues_ctx)
    items = [sports_event_ld(e, x["lg"]) for x in leagues_ctx for e in x["today"]][:40]
    page = {"title": f"What Channel Is the Game On Tonight? TV & Streaming for {TODAY.strftime('%b %-d')}",
            "h1": "What channel is the game on tonight?",
            "description": f"Every NFL, NBA, NHL, MLB, college football and NASCAR event today ({TODAY.strftime('%A, %b %-d')}) with start times in ET, the TV channel, and legal ways to stream it. Updated twice a day.",
            "priority": "1.0", "crumbs": [("Home", "/")]}
    page["ld"] = ld_json(items, {**page, "path": "/"})
    write("/", "hub.html", page=page, leagues_ctx=leagues_ctx, n_today=n_today)

    # ---- league pages
    for x in leagues_ctx:
        lg = x["lg"]; k = lg["key"]
        ovr = (FEATURED.get("league_titles") or {}).get(k, {})
        h1 = ovr.get("h1") or f"What channel is the {lg['name']} {lg['noun']} on tonight?"
        page = {"title": ovr.get("title") or f"What Channel Is the {lg['name']} {lg['noun'].title()} On Tonight? ({TODAY.strftime('%b %-d')})",
                "h1": h1,
                "description": ovr.get("description") or f"Today's {lg['name']} schedule with start times (ET), TV channels and legal streaming options, plus the next 2 weeks of {lg['name']} {lg['noun']}s.",
                "priority": "0.9", "crumbs": [("Home", "/"), (lg["name"], f"/{k}/")]}
        show = x["today"] or x["next_day"]
        page["ld"] = ld_json([sports_event_ld(e, lg) for e in show][:40], {**page, "path": f"/{k}/"})
        write(f"/{k}/", "league.html", page=page, x=x, lg=lg, notes=NOTES.get(k, {}),
              groups=day_groups([e for e in x["upcoming"] if e["date_et"] >= TODAY.isoformat()]),
              intro=ovr.get("intro", ""))

    # ---- team pages
    team_count = 0
    for x in leagues_ctx:
        lg = x["lg"]; k = lg["key"]
        if not lg.get("team_pages"):
            continue
        evs = data["leagues"][k]
        for t in x["teams"]:
            tev = team_events(evs, t["id"])
            up = [e for e in tev if is_upcoming(e)]
            nxt = up[0] if up else None
            today_game = nxt if nxt and nxt["date_et"] == TODAY.isoformat() else None
            side = side_of(nxt, t["id"]) if nxt else "home"
            rows = watch_rows(nxt, side) if nxt else []
            notes = NOTES.get(k, {})
            svc = services_for(rows, notes.get("extra_services", []))
            locs = local_networks(tev, t["id"])
            faqs = team_faqs(t, lg, nxt, today_game, rows, svc, locs)
            feat = (FEATURED.get("teams") or {}).get(f"{k}/{t['slug']}", {})
            path = f"/{k}/{t['slug']}/"
            if today_game:
                desc = f"The {t['name']} play today at {today_game['time_et']} on {channel_sentence(rows)}. How to watch or stream it legally, in-market and out-of-market."
            elif nxt:
                desc = f"No {t['short']} {lg['noun']} tonight. Next: {nxt['day_label']}, {nxt['time_et']} on {channel_sentence(rows)}. TV channel and legal streaming options."
            else:
                desc = f"What channel the {t['name']} play on, local TV and legal streaming options, updated twice daily."
            page = {"title": f"What Channel Is the {t['short']} {lg['noun'].title()} On Tonight? | {t['name']} TV & Streaming",
                    "h1": f"What channel is the {t['name']} {lg['noun']} on tonight?",
                    "description": desc[:300], "priority": "0.8",
                    "crumbs": [("Home", "/"), (lg["name"], f"/{k}/"), (t["name"], path)]}
            ld_items = [sports_event_ld(e, lg) for e in up[:3]] + [faq_ld(faqs)]
            page["ld"] = ld_json(ld_items, {**page, "path": path})
            others = [o for o in x["teams"] if o["id"] != t["id"]]
            write(path, "team.html", page=page, lg=lg, team=t, nxt=nxt, today_game=today_game, rows=rows, svc=svc,
                  upcoming=up[:6], locals_=locs, faqs=faqs, notes=notes, feat=feat, others=others,
                  opp=lambda e: opp_phrase(e, t["id"]))
            team_count += 1

    # ---- special: Thursday Night Football
    nfl_up = LC["nfl"]["upcoming"]
    tnf = next((e for e in nfl_up if dt.datetime.fromisoformat(e["start_utc"]).astimezone(TZ).weekday() == 3
                and any("Prime" in n for n in e["broadcasts"]["national"])), None)
    rows = watch_rows(tnf) if tnf else []
    page = {"title": "Where to Watch Thursday Night Football Tonight (Prime Video): Channel & Legal Options",
            "h1": "Where to watch Thursday Night Football tonight",
            "description": ("Thursday Night Football streams on Prime Video. " +
                            (f"Next: {tnf['name']}, {tnf['day_label']} at {tnf['time_et']}. " if tnf else "") +
                            "How to watch with or without Prime, including free local TV in the teams' markets.")[:300],
            "priority": "0.8", "crumbs": [("Home", "/"), ("NFL", "/nfl/"), ("Thursday Night Football", "/nfl/thursday-night-football/")]}
    tnf_faqs = [
        {"q": "What channel is Thursday Night Football on?",
         "a": "Thursday Night Football streams on Prime Video. It is not on a cable channel nationally; in the two teams' home markets it also airs free on a local broadcast TV station."
              + (f" The next game is {tnf['name']} on {tnf['day_label']} at {tnf['time_et']}." if tnf else "")},
        {"q": "Can I watch Thursday Night Football for free?",
         "a": "If you live in one of the two teams' home markets, the game is simulcast on a local over-the-air station, so an antenna works. Everywhere else you need Prime Video (included with Amazon Prime, or a standalone Prime Video subscription). NFL+ also streams it on phones and tablets."},
        {"q": "Is Thursday Night Football on YouTube TV, Sling or Fubo?",
         "a": "No. Outside the teams' local markets, Thursday Night Football is exclusive to Prime Video. Live TV services only show it through the local station in the teams' home markets."},
    ]
    page["ld"] = ld_json(([sports_event_ld(tnf, LG["nfl"])] if tnf else []) + [faq_ld(tnf_faqs)],
                         {**page, "path": "/nfl/thursday-night-football/"})
    write("/nfl/thursday-night-football/", "tnf.html", page=page, tnf=tnf, rows=rows, faqs=tnf_faqs,
          svc=services_for(rows, ["prime-video", "nfl-plus", "antenna"]))

    # ---- guides (markdown files in content/guides/*.md with YAML front matter)
    guides = []
    gdir = os.path.join(ROOT, "content", "guides")
    for fn in sorted(os.listdir(gdir)) if os.path.isdir(gdir) else []:
        if not fn.endswith(".md"):
            continue
        raw = open(os.path.join(gdir, fn)).read()
        _, fm, body = raw.split("---", 2)
        meta = yaml.safe_load(fm)
        slug = meta.get("slug") or fn[:-3]
        path = f"/guides/{slug}/"
        page = {"title": meta["title"], "h1": meta.get("h1", meta["title"]), "description": meta["description"],
                "priority": "0.7", "crumbs": [("Home", "/"), ("Guides", "/guides/"), (meta["title"], path)]}
        faqs = meta.get("faq") or []
        page["ld"] = ld_json([faq_ld(faqs)] if faqs else [], {**page, "path": path})
        body_html = markdown.markdown(body, extensions=["tables"])
        body_html = re.sub(r'href="/go/([a-z0-9-]+)/"', lambda m: f'href="{url("/go/" + m.group(1) + "/")}" rel="sponsored nofollow"', body_html)
        body_html = re.sub(r'href="/(?!go/)', f'href="{BASE_PATH}/', body_html)
        write(path, "guide.html", page=page, body=body_html, faqs=faqs, meta=meta)
        guides.append({"title": meta["title"], "path": path, "description": meta["description"], "date": str(meta.get("date", ""))})
    page = {"title": f"Guides: How to Watch Sports Without the Guesswork | {CFG['SITE_NAME']}", "h1": "Guides",
            "description": "Plain-English guides to finding the right channel and legal streaming options for NFL, NBA, NHL, MLB, college football and NASCAR.",
            "priority": "0.6", "crumbs": [("Home", "/"), ("Guides", "/guides/")]}
    page["ld"] = ld_json([], {**page, "path": "/guides/"})
    write("/guides/", "guides.html", page=page, guides=sorted(guides, key=lambda g: g["date"], reverse=True))

    # ---- static pages
    for slug, title, desc in [("about", "About & Affiliate Disclosure", f"About {CFG['SITE_NAME']}, where our TV listings come from, and how we make money."),
                              ("privacy", "Privacy Policy", f"{CFG['SITE_NAME']} privacy policy: no ads trackers, no accounts, cookie-free analytics.")]:
        page = {"title": f"{title} | {CFG['SITE_NAME']}", "h1": title, "description": desc, "priority": "0.3",
                "crumbs": [("Home", "/"), (title, f"/{slug}/")]}
        page["ld"] = ld_json([], {**page, "path": f"/{slug}/"})
        write(f"/{slug}/", f"{slug}.html", page=page)

    # ---- /go/<service>/ click-tracking redirects (noindex, not in sitemap)
    for key, s in AFF.items():
        write(f"/go/{key}/", "go.html", page={"title": f"Going to {s['name']}...", "h1": f"Going to {s['name']}",
                                              "description": "", "noindex": True}, svc=s, key=key)
    write("/404.html", "404.html", page={"title": "Page not found", "h1": "Page not found", "description": "", "noindex": True})

    # ---- sitemap + robots
    sm = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for p in PAGES:
        sm.append(f"<url><loc>{html.escape(abs_url(p['path']))}</loc><lastmod>{UPDATED_ISO}</lastmod><priority>{p['priority']}</priority></url>")
    sm.append("</urlset>")
    open(os.path.join(TMP, "sitemap.xml"), "w").write("\n".join(sm) + "\n")
    open(os.path.join(TMP, "robots.txt"), "w").write(
        f"User-agent: *\nDisallow: {url('/go/')}\nAllow: /\n\nSitemap: {abs_url('/sitemap.xml')}\n")
    open(os.path.join(TMP, ".nojekyll"), "w").write("")
    if CFG.get("INDEXNOW_KEY"):
        open(os.path.join(TMP, CFG["INDEXNOW_KEY"] + ".txt"), "w").write(CFG["INDEXNOW_KEY"])
    if CFG.get("CNAME"):
        open(os.path.join(TMP, "CNAME"), "w").write(CFG["CNAME"] + "\n")
    return team_count


def validate(team_count, data):
    errs = []
    need = sum(len(data["teams"].get(l["key"], [])) for l in LEAGUES if l.get("team_pages"))
    if team_count != need or team_count < 94:
        errs.append(f"team pages {team_count} != {need}")
    for p in PAGES:
        txt = open(p["file"]).read()
        if p["html_len"] < 2500 or "<h1" not in txt:
            errs.append(f"thin page {p['path']} ({p['html_len']} bytes)")
        if re.search(r">\s*None\s*<", txt):
            errs.append(f"'None' leaked into {p['path']}")
    if errs:
        sys.exit("ABORT: validation failed, dist/ left untouched:\n  " + "\n  ".join(errs[:20]))


def main():
    offline = "--offline" in sys.argv
    data = load_data(offline)
    team_count = build(data)
    validate(team_count, data)
    old = DIST + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(DIST):
        os.rename(DIST, old)
    os.rename(TMP, DIST)
    shutil.rmtree(old, ignore_errors=True)
    n_html = sum(1 for _, _, fs in os.walk(DIST) for f in fs if f.endswith(".html"))
    print(f"OK: {len(PAGES)} indexable pages ({team_count} team pages), {n_html} HTML files total -> {DIST}")
    print("Fetch stats:", espn.stats, {k: dict(v) for k, v in data["sources"].items()})


if __name__ == "__main__":
    main()
