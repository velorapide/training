#!/usr/bin/env python3
"""
Pulls training data from intervals.icu and writes data.json for the dashboard.

Stdlib only - no pip install needed.

Setup:
    export INTERVALS_ATHLETE_ID=i123456
    export INTERVALS_API_KEY=xxxxxxxx
    ./fetch_training.py /var/www/training/data.json

Or put them in a config file next to this script (config.json):
    {"athlete_id": "i123456", "api_key": "xxxxxxxx"}

Dump raw API responses to see the real field names:
    ./fetch_training.py --dump
"""

import base64
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "https://intervals.icu/api/v1"
TIMEOUT = 30

# Which planned-workout names count as a benchmark worth counting down to.
BENCHMARK_WORDS = ("ftp test", "ramp test", "20 min test", "benchmark", "race", "time trial")


# --------------------------------------------------------------------------- config

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
        sys.exit("Missing athlete id or api key. Set INTERVALS_ATHLETE_ID and "
                 "INTERVALS_API_KEY, or create config.json next to this script.")
    if not str(athlete).startswith("i"):
        athlete = "i" + str(athlete)
    return str(athlete), str(key)


def get(path, athlete, key, **params):
    """GET an intervals.icu endpoint, return decoded JSON (or [] on 404)."""
    url = f"{BASE}/athlete/{athlete}{path}"
    if params:
        qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        url = f"{url}?{qs}"
    token = base64.b64encode(f"API_KEY:{key}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        body = e.read().decode()[:300]
        sys.exit(f"HTTP {e.code} on {path}\n{body}")
    except urllib.error.URLError as e:
        sys.exit(f"Could not reach intervals.icu: {e.reason}")


# --------------------------------------------------------------------------- helpers

def pick(d, *names, default=None):
    """First present, non-null field from a list of candidate names."""
    for n in names:
        v = d.get(n)
        if v is not None:
            return v
    return default


def day(s):
    """'2026-07-18T06:16:53' or '2026-07-18' -> date."""
    if not s:
        return None
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def is_benchmark(name, category=""):
    hay = f"{name or ''} {category or ''}".lower()
    return any(w in hay for w in BENCHMARK_WORDS)


def zone2_pct(act):
    """Share of moving time in zone 2, if intervals.icu reported zone times."""
    zt = pick(act, "icu_zone_times", "zone_times")
    if not zt:
        return None
    try:
        if isinstance(zt, dict):
            vals = [zt.get(f"z{i}", 0) or 0 for i in range(1, 8)]
        else:
            vals = [z.get("secs", z.get("time", 0)) if isinstance(z, dict) else (z or 0)
                    for z in zt]
        total = sum(vals)
        if total <= 0 or len(vals) < 2:
            return None
        return round(vals[1] / total * 100)
    except (TypeError, AttributeError, KeyError):
        return None


# --------------------------------------------------------------------------- build

def build(athlete, key):
    today = dt.date.today()
    start_90 = today - dt.timedelta(days=90)
    end_14 = today + dt.timedelta(days=14)

    profile = get("", athlete, key) or {}
    activities = get("/activities", athlete, key,
                     oldest=start_90.isoformat(), newest=today.isoformat()) or []
    events = get("/events", athlete, key,
                 oldest=today.isoformat(), newest=end_14.isoformat()) or []
    wellness = get("/wellness", athlete, key,
                   oldest=start_90.isoformat(), newest=today.isoformat()) or []

    if isinstance(wellness, dict):
        wellness = list(wellness.values())
    wellness = sorted([w for w in wellness if isinstance(w, dict)],
                      key=lambda w: str(pick(w, "id", "date", default="")))

    # ---- athlete numbers
    ftp = pick(profile, "icu_ftp", "ftp")
    sport = (profile.get("sportSettings") or [{}])
    if not ftp and isinstance(sport, list) and sport:
        ftp = pick(sport[0], "ftp", "icu_ftp")
    eftp = None
    for a in sorted(activities, key=lambda a: str(pick(a, "start_date_local", default="")),
                    reverse=True):
        eftp = pick(a, "icu_eftp", "eftp")
        if eftp:
            break
    weight = None
    for w in reversed(wellness):
        weight = pick(w, "weight")
        if weight:
            break
    weight = weight or pick(profile, "icu_weight", "weight")

    # ---- form
    latest = wellness[-1] if wellness else {}
    ctl = pick(latest, "ctl", default=0) or 0
    atl = pick(latest, "atl", default=0) or 0
    ctl_series = [round(pick(w, "ctl", default=0) or 0, 1) for w in wellness][-90:]

    # ---- today's plan and what's already done
    planned_today, planned_all = [], []
    for e in events:
        d = day(pick(e, "start_date_local", "start_date", "date"))
        if not d:
            continue
        cat = str(pick(e, "category", default=""))
        if cat.upper() == "NOTE":
            continue
        item = {
            "date": d.isoformat(),
            "name": pick(e, "name", default="Session"),
            "type": pick(e, "type", default=""),
            "load": pick(e, "icu_training_load", "training_load"),
            "duration_s": pick(e, "moving_time", "icu_planned_time", "duration"),
            "description": (pick(e, "description", default="") or "")[:400],
            "benchmark": is_benchmark(pick(e, "name", default=""), cat),
        }
        planned_all.append(item)
        if d == today:
            planned_today.append(item)
    planned_all.sort(key=lambda x: x["date"])

    done_today = []
    for a in activities:
        if day(pick(a, "start_date_local", "start_date")) == today:
            done_today.append({
                "name": pick(a, "name", default="Ride"),
                "load": pick(a, "icu_training_load", "training_load"),
                "duration_s": pick(a, "moving_time", default=0),
                "watts": pick(a, "icu_average_watts", "average_watts"),
            })

    nxt = next((p for p in planned_all if p["benchmark"]), None)
    next_benchmark = None
    if nxt:
        next_benchmark = dict(nxt)
        next_benchmark["days"] = (day(nxt["date"]) - today).days

    # ---- last 7 days, planned vs actual
    by_day = {}
    for a in activities:
        d = day(pick(a, "start_date_local", "start_date"))
        if d and (today - d).days < 7:
            e = by_day.setdefault(d.isoformat(), {"actual": 0, "planned": 0, "name": ""})
            e["actual"] += pick(a, "icu_training_load", "training_load", default=0) or 0
            e["name"] = e["name"] or pick(a, "name", default="")
    week = []
    for i in range(6, -1, -1):
        d = (today - dt.timedelta(days=i)).isoformat()
        row = by_day.get(d, {"actual": 0, "planned": 0, "name": ""})
        week.append({"date": d, "actual": round(row["actual"]), "name": row["name"]})

    # ---- recent rides
    recent = []
    for a in sorted(activities, key=lambda a: str(pick(a, "start_date_local", default="")),
                    reverse=True)[:8]:
        recent.append({
            "date": (day(pick(a, "start_date_local", "start_date")) or today).isoformat(),
            "name": pick(a, "name", default="Ride"),
            "load": pick(a, "icu_training_load", "training_load"),
            "duration_s": pick(a, "moving_time", default=0),
            "watts": pick(a, "icu_average_watts", "average_watts"),
            "z2": zone2_pct(a),
        })

    return {
        "generated": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "athlete": {
            "name": pick(profile, "name", "firstname", default=""),
            "ftp": ftp,
            "eftp": round(eftp) if eftp else None,
            "weight_kg": round(weight, 1) if weight else None,
            "wkg": round(ftp / weight, 2) if ftp and weight else None,
        },
        "form": {
            "ctl": round(ctl, 1),
            "atl": round(atl, 1),
            "tsb": round(ctl - atl, 1),
            "ctl_series": ctl_series,
        },
        "today": {"date": today.isoformat(), "planned": planned_today, "completed": done_today},
        "next_benchmark": next_benchmark,
        "upcoming": planned_all[:8],
        "week": week,
        "recent": recent,
    }


def main():
    athlete, key = load_config()

    if "--dump" in sys.argv:
        today = dt.date.today()
        for label, path, params in [
            ("PROFILE", "", {}),
            ("ACTIVITIES", "/activities",
             {"oldest": (today - dt.timedelta(days=14)).isoformat(), "newest": today.isoformat()}),
            ("EVENTS", "/events",
             {"oldest": today.isoformat(), "newest": (today + dt.timedelta(days=14)).isoformat()}),
            ("WELLNESS", "/wellness",
             {"oldest": (today - dt.timedelta(days=3)).isoformat(), "newest": today.isoformat()}),
        ]:
            data = get(path, athlete, key, **params)
            sample = data[0] if isinstance(data, list) and data else data
            print(f"\n===== {label} =====")
            print(json.dumps(sample, indent=2)[:2500])
        return

    out = sys.argv[1] if len(sys.argv) > 1 else "data.json"
    data = build(athlete, key)

    tmp = out + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, out)          # atomic: the page never reads a half-written file
    print(f"wrote {out} at {data['generated']}")


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  (used in get())
    main()
