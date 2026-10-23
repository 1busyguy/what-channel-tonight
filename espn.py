"""Fetch + normalize ESPN public scoreboard data, with an on-disk last-good cache."""
import json, os, time, datetime as dt
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo
import requests

BASE = os.environ.get("ESPN_BASE", "https://site.api.espn.com/apis/site/v2/sports")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cache")
UA = {"Accept": "application/json"}  # note: UAs containing "Bot" get HTTP 403
ET = ZoneInfo("America/New_York")

_session = requests.Session()
_session.headers.update(UA)

stats = {"ok": 0, "cached": 0, "missing": 0}


def _get_json(url, params=None, tries=3):
    last = None
    for i in range(tries):
        try:
            r = _session.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
        except Exception as e:  # network / json errors
            last = repr(e)
        time.sleep(0 if os.environ.get("ESPN_BASE") else 1.5 * (i + 1))
    raise RuntimeError(last)


def _cached_fetch(cache_path, url, params, validate):
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    try:
        data = _get_json(url, params)
        if not validate(data):
            raise RuntimeError("payload failed validation")
        tmp = cache_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, cache_path)
        stats["ok"] += 1
        return data, "live"
    except Exception as e:
        if os.path.exists(cache_path):
            stats["cached"] += 1
            print(f"  ! {url} {params} failed ({e}); using last good cache")
            with open(cache_path) as f:
                return json.load(f), "cache"
        stats["missing"] += 1
        print(f"  ! {url} {params} failed ({e}); no cache")
        return None, "missing"


def fetch_teams(lg):
    url = f"{BASE}/{lg['sport']}/{lg['league']}/teams"
    data, _ = _cached_fetch(os.path.join(CACHE, "teams", lg["key"] + ".json"), url, None,
                            lambda d: len(d["sports"][0]["leagues"][0]["teams"]) >= 20)
    if not data:
        return []
    out = []
    for t in data["sports"][0]["leagues"][0]["teams"]:
        t = t["team"]
        if not t.get("isActive", True) or t.get("isAllStar"):
            continue
        out.append({
            "id": t["id"], "slug": t["slug"], "name": t["displayName"],
            "short": t.get("shortDisplayName") or t.get("name"), "abbr": t["abbreviation"],
            "location": t.get("location", ""), "color": t.get("color", "333333"),
        })
    return sorted(out, key=lambda x: x["name"])


def _bcasts(comp):
    """Return {'national': [...], 'home': [...], 'away': [...]} of TV/streaming names."""
    res = {"national": [], "home": [], "away": []}
    geo = comp.get("geoBroadcasts") or []
    for g in geo:
        kind = (g.get("type") or {}).get("shortName", "TV")
        if kind.lower() == "radio":
            continue
        mkt = ((g.get("market") or {}).get("type") or "National").lower()
        name = (g.get("media") or {}).get("shortName")
        if name and mkt in res and name not in res[mkt]:
            res[mkt].append(name)
    if not any(res.values()):
        for b in comp.get("broadcasts") or []:
            mkt = (b.get("market") or "national").lower()
            for n in b.get("names") or []:
                if mkt in res and n not in res[mkt]:
                    res[mkt].append(n)
    return res


def normalize(ev, lg):
    comps = ev.get("competitions") or [{}]
    comp = comps[0]
    # NASCAR: competitions = practice/qualifying/race; pick the race (last one / type RACE).
    if lg["sport"] == "racing" and len(comps) > 1:
        race = [c for c in comps if (c.get("type") or {}).get("abbreviation", "").upper() == "RACE"]
        comp = race[0] if race else comps[-1]
    start = comp.get("date") or ev["date"]
    try:
        utc = dt.datetime.strptime(start, "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        utc = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
    et = utc.astimezone(ET)
    teams = {}
    for c in comp.get("competitors") or []:
        t = c.get("team")
        if not t:
            continue
        teams[c.get("homeAway", "home")] = {
            "id": t.get("id"), "name": t.get("displayName"), "short": t.get("shortDisplayName"),
            "abbr": t.get("abbreviation"), "score": c.get("score"),
        }
    st = (ev.get("status") or comp.get("status") or {}).get("type", {})
    venue = comp.get("venue") or {}
    addr = venue.get("address") or {}
    tbd = bool(comp.get("timeValid") is False)
    return {
        "id": ev["id"], "league": lg["key"], "name": ev.get("name", ""), "short_name": ev.get("shortName", ""),
        "start_utc": utc.isoformat(), "date_et": et.date().isoformat(),
        "time_et": "TBD" if tbd else et.strftime("%-I:%M %p ET"),
        "day_label": et.strftime("%a, %b %-d"), "home": teams.get("home"), "away": teams.get("away"),
        "venue": venue.get("fullName"), "city": ", ".join(x for x in [addr.get("city"), addr.get("state")] if x),
        "state": st.get("state", "pre"), "status": st.get("shortDetail") or st.get("description", ""),
        "broadcasts": _bcasts(comp), "neutral": comp.get("neutralSite", False),
        "note": ((comp.get("notes") or [{}])[0] or {}).get("headline", ""),
    }


def fetch_league(lg, today_et, days_back, days_ahead):
    url = f"{BASE}/{lg['sport']}/{lg['league']}/scoreboard"
    days = [today_et + dt.timedelta(days=d) for d in range(-days_back, days_ahead + 1)]

    def one(day):
        params = {"dates": day.strftime("%Y%m%d"), "limit": 400, **(lg.get("params") or {})}
        path = os.path.join(CACHE, lg["key"], day.isoformat() + ".json")
        data, src = _cached_fetch(path, url, params, lambda d: isinstance(d.get("events"), list))
        return data, src

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(one, days))
    events, seen, sources = [], set(), {}
    for day, (data, src) in zip(days, results):
        sources[day.isoformat()] = src
        for ev in (data or {}).get("events", []):
            if ev["id"] in seen:
                continue
            try:
                n = normalize(ev, lg)
            except Exception as e:
                print("  ! normalize failed", ev.get("id"), e)
                continue
            seen.add(ev["id"])
            events.append(n)
    events.sort(key=lambda e: e["start_utc"])
    return events, sources
