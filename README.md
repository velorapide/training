# Training dashboard — GitHub Pages setup

No server, no cron on a box. GitHub Actions fetches your data on a schedule and
publishes the page. You push once and then leave it alone.

## Repo layout

```
your-repo/
├── fetch_training.py
├── site/
│   ├── index.html          <- the page
│   └── CNAME               <- your custom domain, one line
└── .github/
    └── workflows/
        └── update-dashboard.yml
```

`data.json` is **not** in the repo. The workflow generates it at build time and
bundles it into what gets published, so your git history stays clean.

---

## 1. Create the repo

```bash
mkdir training && cd training && git init
mkdir -p site .github/workflows

# drop the files in
cp ~/Downloads/fetch_training.py .
cp ~/Downloads/index.html site/
cp ~/Downloads/update-dashboard.yml .github/workflows/

echo "training.velorapide.org" > site/CNAME    # your subdomain

printf 'data.json\nconfig.json\n' > .gitignore

git add -A && git commit -m "Training dashboard"
git branch -M main
git remote add origin git@github.com:YOURNAME/training.git
git push -u origin main
```

## 2. Add your credentials as secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `INTERVALS_ATHLETE_ID` | `i123456` |
| `INTERVALS_API_KEY` | your key |

Both from <https://intervals.icu/settings> → Developer Settings.

Secrets are encrypted and never appear in logs or in the published site. The key
is used on the runner and thrown away.

## 3. Turn on Pages

Repo → **Settings → Pages → Source: GitHub Actions**. Not "Deploy from a branch" —
this workflow publishes directly, which is what keeps `data.json` out of git.

## 4. Point your domain

At your DNS provider, a CNAME record:

```
training.velorapide.org.   CNAME   YOURNAME.github.io.
```

Then in **Settings → Pages → Custom domain**, enter the same name and tick
**Enforce HTTPS** once the certificate finishes provisioning (a few minutes).

## 5. Run it

Repo → **Actions → Update training dashboard → Run workflow**.

The "Sanity check" step prints your FTP, CTL, and how many activities came back.
If those are empty or null, the field names need adjusting — see below.

---

## If the numbers come back empty

intervals.icu returns different field names depending on the activity and how it
was recorded, and I wrote the script without being able to call the live API. Run
it locally to see the truth:

```bash
export INTERVALS_ATHLETE_ID=i123456
export INTERVALS_API_KEY=your-key
python3 fetch_training.py --dump
```

That prints a real sample of each endpoint. Send me the output and I'll correct
the field names — the script already tries several aliases per field, but it can
only guess so far.

---

## Things worth knowing

**Your data will be public.** Pages sites are served publicly, including from
private repos on most plans. Anyone who finds the URL sees your training load and
FTP. That's probably fine — it's roughly what a public Strava profile shows — but
it's a choice, not an accident. If you'd rather it weren't public, the fallback is
keeping it on a box behind auth.

**Scheduled workflows get paused after 60 days of repo inactivity.** Because this
setup deliberately doesn't commit anything, a repo you never touch can go quiet
long enough for GitHub to disable the schedule. GitHub emails you first and
re-enabling is one button. If it becomes a nuisance, the alternative is having the
workflow commit `data.json` each run — noisier history, but the repo stays active
on its own.

**Scheduled runs drift.** GitHub delays scheduled workflows under load, sometimes
by 15–30 minutes. Irrelevant here — the data is hours old anyway — but it's why
the dashboard shows how stale it is rather than pretending to be live.

**Timezone.** The workflow sets `TZ: America/New_York`. Runners are UTC, and
without that, "today" would flip over at 8pm Eastern and show tomorrow's session
all evening. If you move, change it in one place.

**Frequency.** Every 2 hours. Rides upload once or twice a day, so more often just
burns Actions minutes (free on public repos regardless). To change it, edit the
cron line — it's UTC, unlike the `TZ` above which only affects the script.

---

## Changing the page later

Edit `site/index.html`, commit, push. The `push` trigger redeploys automatically.
That's the only time you ever touch a file again.
