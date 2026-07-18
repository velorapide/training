#!/usr/bin/env python3
"""
Pulls training data from intervals.icu and writes data.json for the dashboard.

Stdlib only - no pip install needed.

    export INTERVALS_ATHLETE_ID=i123456
    export INTERVALS_API_KEY=xxxxxxxx
    ./fetch_training.py _site/data.json

See the raw API response and its real field names:
    ./fetch_training.py --dump
"""

import base64
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://intervals.icu/api/v1"
TIMEOUT = 45

HISTORY_DAYS = 1825         # ~5 years, so the All-time totals are real
CALENDAR_BACK = 60          # planned events fetched this far back
CALENDAR_FWD = 45           # ...and this far forward

BENCHMARK_WORDS = ("ftp test", "ramp test", "20 min test", "20min test",
                   "benchmark", "race", "time trial")

# Cloudflare fronts intervals.icu and rejects Python's default user agent
# with a 1010 "access denied".
USER_AGENTS = [
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
]


# --------------------------------------------------------------------- plumbing

def load_config():
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = {}
    path = os.path.join(here, "config.json")
    if os.path.exists(path):
        with open(path) as fh:
            cfg = json.load(fh)
    athlete = os.environ.get("INTERVALS_ATHLETE_ID") or cfg.get("athlete_id")
    key = os.environ.get("INTERVALS_API_KEY") or cfg.get("api_key")
    if not athlete or not key:
        sys.exit("Missing athlete id or api key.")
    if not str(athlete).startswith("i"):
        athlete = "i" + str(athlete)
    return str(athlete), str(key)


def get(path, athlete, key, **params):
    url = f"{BASE}/athlete/{athlete}{path}"
    if params:
        qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        url = f"{url}?{qs}"
    token = base64.b64encode(f"API_KEY:{key}".encode()).decode()

    last = None
    for ua in USER_AGENTS:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "User-Agent": ua,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            body = e.read().decode()[:400]
            if e.code == 403 and "1010" in body:
                last = f"Cloudflare blocked the request (1010) on {path}"
                continue
            if e.code == 401:
                sys.exit("HTTP 401 - credentials rejected. Check the two secrets.")
            sys.exit(f"HTTP {e.code} on {path}\n{body}")
        except urllib.error.URLError as e:
            sys.exit(f"Could not reach intervals.icu: {e.reason}")
    sys.exit(last or "request failed")


def pick(d, *names, default=None):
    for n in names:
        v = d.get(n)
        if v is not None:
            return v
    return default


def day(s):
    if not s:
        return None
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def num(v, nd=1):
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def is_benchmark(name, category=""):
    hay = f"{name or ''} {category or ''}".lower()
    return any(w in hay for w in BENCHMARK_WORDS)


def zone_seconds(act):
    """Normalise whatever shape icu_zone_times comes back in to a list of seconds."""
    zt = pick(act, "icu_zone_times", "zone_times")
    if not zt:
        return None
    try:
        if isinstance(zt, dict):
            out = []
            for i in range(1, 8):
                v = zt.get(f"z{i}", zt.get(str(i)))
                if v is None:
                    break
                out.append(int(v or 0))
            return out or None
        out = []
        for z in zt:
            if isinstance(z, dict):
                out.append(int(pick(z, "secs", "time", "seconds", default=0) or 0))
            else:
                out.append(int(z or 0))
        return out or None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------- shaping

def shape_activity(a):
    d = day(pick(a, "start_date_local", "start_date"))
    return {
        "id": pick(a, "id"),
        "date": d.isoformat() if d else None,
        "start": str(pick(a, "start_date_local", default=""))[11:16],
        "name": pick(a, "name", default="Activity"),
        "type": pick(a, "type", "sport", default="Ride"),
        "trainer": bool(pick(a, "trainer", "icu_trainer", default=False)),
        "load": pick(a, "icu_training_load", "training_load"),
        "if": num(pick(a, "icu_intensity", "intensity"), 2),
        "np": num(pick(a, "icu_weighted_avg_watts", "weighted_average_watts"), 0),
        "watts": num(pick(a, "icu_average_watts", "average_watts"), 0),
        "hr": num(pick(a, "average_heartrate", "icu_average_hr"), 0),
        "max_hr": num(pick(a, "max_heartrate"), 0),
        "cadence": num(pick(a, "average_cadence"), 0),
        "secs": pick(a, "moving_time", default=0) or 0,
        "meters": num(pick(a, "distance", default=0), 0),
        "elev": num(pick(a, "total_elevation_gain", "icu_elevation_gain"), 0),
        "cal": pick(a, "calories"),
        "decoupling": num(pick(a, "decoupling", "icu_decoupling"), 1),
        "efficiency": num(pick(a, "icu_efficiency_factor", "efficiency_factor"), 2),
        "eftp": num(pick(a, "icu_eftp", "eftp"), 0),
        "zones": zone_seconds(a),
    }


def shape_event(e, today):
    d = day(pick(e, "start_date_local", "start_date", "date"))
    if not d:
        return None
    cat = str(pick(e, "category", default=""))
    if cat.upper() == "NOTE":
        return None
    return {
        "date": d.isoformat(),
        "name": pick(e, "name", default="Session"),
        "type": pick(e, "type", default=""),
        "load": pick(e, "icu_training_load", "training_load"),
        "secs": pick(e, "moving_time", "icu_planned_time", "duration"),
        "description": (pick(e, "description", default="") or "")[:600],
        "benchmark": is_benchmark(pick(e, "name", default=""), cat),
    }


# --------------------------------------------------------------------- build

def build(athlete, key):
    today = dt.date.today()
    hist_start = today - dt.timedelta(days=HISTORY_DAYS)

    profile = get("", athlete, key) or {}
    acts_raw = get("/activities", athlete, key,
                   oldest=hist_start.isoformat(), newest=today.isoformat()) or []
    events_raw = get("/events", athlete, key,
                     oldest=(today - dt.timedelta(days=CALENDAR_BACK)).isoformat(),
                     newest=(today + dt.timedelta(days=CALENDAR_FWD)).isoformat()) or []
    wellness_raw = get("/wellness", athlete, key,
                       oldest=hist_start.isoformat(), newest=today.isoformat()) or []

    if isinstance(wellness_raw, dict):
        wellness_raw = list(wellness_raw.values())
    wellness_raw = sorted([w for w in wellness_raw if isinstance(w, dict)],
                          key=lambda w: str(pick(w, "id", "date", default="")))

    acts = [shape_activity(a) for a in acts_raw]
    acts = [a for a in acts if a["date"]]
    acts.sort(key=lambda a: a["date"], reverse=True)

    events = [e for e in (shape_event(e, today) for e in events_raw) if e]
    events.sort(key=lambda e: e["date"])

    # ---- athlete
    ftp = pick(profile, "icu_ftp", "ftp")
    sport = profile.get("sportSettings")
    if not ftp and isinstance(sport, list) and sport:
        ftp = pick(sport[0], "ftp", "icu_ftp")
    eftp = next((a["eftp"] for a in acts if a.get("eftp")), None)

    weight = next((num(pick(w, "weight"), 1) for w in reversed(wellness_raw)
                   if pick(w, "weight")), None) or num(pick(profile, "icu_weight", "weight"), 1)

    # ---- wellness series
    wl = []
    for w in wellness_raw:
        d = day(pick(w, "id", "date"))
        if not d:
            continue
        wl.append({
            "date": d.isoformat(),
            "ctl": num(pick(w, "ctl"), 1),
            "atl": num(pick(w, "atl"), 1),
            "weight": num(pick(w, "weight"), 1),
            "rhr": num(pick(w, "restingHR", "resting_hr"), 0),
            "hrv": num(pick(w, "hrv"), 1),
        })
    latest = wl[-1] if wl else {}
    ctl = latest.get("ctl") or 0
    atl = latest.get("atl") or 0

    # ---- today / next
    planned_today = [e for e in events if e["date"] == today.isoformat()]
    done_today = [a for a in acts if a["date"] == today.isoformat()]
    upcoming = [e for e in events if e["date"] >= today.isoformat()]
    nb = next((e for e in upcoming if e["benchmark"]), None)
    next_benchmark = dict(nb, days=(day(nb["date"]) - today).days) if nb else None

    # ---- calendar: one entry per day, planned + actual
    cal = {}
    cal_floor = (today - dt.timedelta(days=CALENDAR_BACK)).isoformat()
    for e in events:
        c = cal.setdefault(e["date"], {"p": [], "a": [], "pl": 0, "al": 0})
        c["p"].append({"name": e["name"], "load": e["load"], "secs": e["secs"]})
        c["pl"] += e["load"] or 0
    for a in acts:
        if a["date"] < cal_floor:
            continue
        c = cal.setdefault(a["date"], {"p": [], "a": [], "pl": 0, "al": 0})
        c["a"].append({"name": a["name"], "load": a["load"], "secs": a["secs"],
                       "type": a["type"], "meters": a["meters"]})
        c["al"] += a["load"] or 0

    # ---- daily load across the whole history window, for the year view.
    # Deliberately tiny: one short entry per day, not per activity.
    daily = {}
    for a in acts:
        b = daily.setdefault(a["date"], {"l": 0, "s": 0, "n": 0, "m": 0, "e": 0})
        b["l"] += a["load"] or 0
        b["s"] += a["secs"] or 0
        b["m"] += a["meters"] or 0
        b["e"] += a["elev"] or 0
        b["n"] += 1
    for b in daily.values():
        b["l"] = round(b["l"])
        b["m"] = round(b["m"])
        b["e"] = round(b["e"])

    # ---- weekly buckets
    def week_start(d):
        return d - dt.timedelta(days=d.weekday())
    weeks = {}

    def wbucket(ws):
        return weeks.setdefault(ws, {"week": ws, "actual": 0, "planned": 0,
                                     "secs": 0, "meters": 0, "elev": 0, "count": 0})
    for a in acts:
        b = wbucket(week_start(day(a["date"])).isoformat())
        b["actual"] += a["load"] or 0
        b["secs"] += a["secs"] or 0
        b["meters"] += a["meters"] or 0
        b["elev"] += a["elev"] or 0
        b["count"] += 1
    for e in events:
        wbucket(week_start(day(e["date"])).isoformat())["planned"] += e["load"] or 0
    weekly = sorted(weeks.values(), key=lambda b: b["week"])[-26:]
    for b in weekly:
        b["actual"] = round(b["actual"])
        b["planned"] = round(b["planned"])
        b["hours"] = round(b["secs"] / 3600, 2)
        b["miles"] = round(b["meters"] / 1609.34)
        b["feet"] = round(b["elev"] * 3.28084)

    # ---- monthly buckets
    months = {}
    for a in acts:
        b = months.setdefault(a["date"][:7], {"month": a["date"][:7], "secs": 0,
                                              "meters": 0, "elev": 0, "load": 0, "count": 0})
        b["secs"] += a["secs"] or 0
        b["meters"] += a["meters"] or 0
        b["elev"] += a["elev"] or 0
        b["load"] += a["load"] or 0
        b["count"] += 1
    monthly = sorted(months.values(), key=lambda b: b["month"])[-12:]
    for b in monthly:
        b["hours"] = round(b["secs"] / 3600, 1)
        b["miles"] = round(b["meters"] / 1609.34)
        b["feet"] = round(b["elev"] * 3.28084)
        b["load"] = round(b["load"])

    # ---- time in zone
    def zone_totals(days_back):
        cutoff = (today - dt.timedelta(days=days_back)).isoformat()
        total = None
        for a in acts:
            if a["date"] < cutoff or not a["zones"]:
                continue
            if total is None:
                total = [0] * len(a["zones"])
            for i, s in enumerate(a["zones"][:len(total)]):
                total[i] += s or 0
        return total

    # ---- season totals
    jan = dt.date(today.year, 1, 1).isoformat()
    ytd_acts = [a for a in acts if a["date"] >= jan]
    ytd = {
        "rides": len(ytd_acts),
        "hours": round(sum(a["secs"] or 0 for a in ytd_acts) / 3600, 1),
        "miles": round(sum(a["meters"] or 0 for a in ytd_acts) / 1609.34),
        "feet": round(sum(a["elev"] or 0 for a in ytd_acts) * 3.28084),
        "load": round(sum(a["load"] or 0 for a in ytd_acts)),
        "year": today.year,
    }

    def best(field):
        pool = [a for a in acts if a.get(field)]
        return max(pool, key=lambda a: a[field]) if pool else None
    bests = {"longest": best("secs"), "farthest": best("meters"),
             "climbiest": best("elev"), "hardest": best("load")}
    # keep the payload small: bests only need a headline, not the whole activity
    bests = {k: ({"name": v["name"], "date": v["date"], "secs": v["secs"],
                  "meters": v["meters"], "elev": v["elev"], "load": v["load"]} if v else None)
             for k, v in bests.items()}

    fields = {
        "activity": sorted(acts_raw[0].keys()) if acts_raw else [],
        "event": sorted(events_raw[0].keys()) if events_raw else [],
        "wellness": sorted(wellness_raw[-1].keys()) if wellness_raw else [],
        "athlete": sorted(profile.keys()) if isinstance(profile, dict) else [],
    }

    return {
        "generated": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "athlete": {
            "name": pick(profile, "name", "firstname", default=""),
            "ftp": ftp, "eftp": eftp, "weight_kg": weight,
            "wkg": round(ftp / weight, 2) if ftp and weight else None,
        },
        "form": {
            "ctl": round(ctl, 1), "atl": round(atl, 1), "tsb": round(ctl - atl, 1),
            "series": [{"date": w["date"], "ctl": w["ctl"], "atl": w["atl"]}
                       for w in wl if w["ctl"] is not None][-120:],
        },
        "today": {"date": today.isoformat(), "planned": planned_today, "completed": done_today},
        "next_benchmark": next_benchmark,
        "upcoming": upcoming[:10],
        "calendar": cal,
        "daily": daily,
        "earliest": min(daily) if daily else None,
        "weekly": weekly,
        "monthly": monthly,
        "zones_30": zone_totals(30),
        "zones_90": zone_totals(90),
        "ytd": ytd,
        "bests": bests,
        "wellness": wl[-120:],
        "recent": acts[:40],
        "_fields": fields,
    }


def main():
    athlete, key = load_config()

    if "--dump" in sys.argv:
        today = dt.date.today()
        for label, path, params in [
            ("PROFILE", "", {}),
            ("ACTIVITIES", "/activities",
             {"oldest": (today - dt.timedelta(days=14)).isoformat(),
              "newest": today.isoformat()}),
            ("EVENTS", "/events",
             {"oldest": today.isoformat(),
              "newest": (today + dt.timedelta(days=14)).isoformat()}),
            ("WELLNESS", "/wellness",
             {"oldest": (today - dt.timedelta(days=3)).isoformat(),
              "newest": today.isoformat()}),
        ]:
            data = get(path, athlete, key, **params)
            sample = data[0] if isinstance(data, list) and data else data
            print(f"\n===== {label} =====")
            print(json.dumps(sample, indent=2)[:3000])
        return

    out = sys.argv[1] if len(sys.argv) > 1 else "data.json"
    d = os.path.dirname(os.path.abspath(out))
    if d:
        os.makedirs(d, exist_ok=True)
    data = build(athlete, key)
    tmp = out + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    os.replace(tmp, out)
    print(f"wrote {out} at {data['generated']}  "
          f"({len(data['recent'])} recent, {len(data['weekly'])} weeks, "
          f"{len(data['calendar'])} calendar days)")


if __name__ == "__main__":
    main()
