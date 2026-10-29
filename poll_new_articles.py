"""
Poll newsband.in's Google News sitemap, turn every new article into a post and (optionally)
publish it to Instagram - the unattended version of what the dashboard does by hand.

    python poll_new_articles.py                   # loop forever: detect -> scrape -> render -> post
    python poll_new_articles.py --once            # a single pass, then exit
    python poll_new_articles.py --no-post         # only generate posts, publish nothing
    python poll_new_articles.py --dry-run         # only list what it *would* do
    python poll_new_articles.py --interval 300    # override the poll interval (seconds)
    python poll_new_articles.py --min-gap 45      # minutes between two Instagram publishes
    python poll_new_articles.py --all-dates       # don't restrict to articles published today

app.py imports this module and runs run_forever() in a background thread, so keeping the
dashboard open is enough to have the whole pipeline running (see STATUS / the /autopilot
routes there).

Safety rails, because this publishes to a live Instagram account without anyone watching:
  * only articles whose sitemap publication date is *today* (IST) are eligible (--all-dates off);
  * at most MAX_PER_POLL articles are handled per pass, the rest wait for the next one;
  * at most MAX_PER_DAY posts are published per calendar day (Instagram's own limit is 100/24h);
  * never two publishes back to back. Inside the dashboard every rendered post goes into the
    same publish queue as the Post button, which sends one at a time 2-3 min apart
    (Insta_poster.MIN_API_GAP + random jitter); standalone, MIN_PUBLISH_GAP (plus jitter) passes
    between two publishes. Rendering stays fast, sending is a drip. This rail was missing on
    18 Sep 2026: five stories went out 25 seconds apart, twice, which reads as bot activity and
    got the account's API access suspended. Volume was never the problem - cadence was. Do not
    remove it;
  * a URL is recorded in seen_urls.json as soon as it is handled - success or failure - so a
    broken article can never wedge the loop or be posted twice;
  * a post whose sidecar already says "published" is never published again.
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

from scrape_post import process

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SITEMAP_URL = "https://www.newsband.in/news-sitemap.php"
STATE_PATH = os.path.join(BASE_DIR, "seen_urls.json")
DAY_STATE_PATH = os.path.join(BASE_DIR, "autopost_state.json")
MODE_PATH = os.path.join(BASE_DIR, "autopilot_mode.json")   # the dashboard's mode choice + daily limit
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NewsbandPostBot/1.0"}
TIMEOUT = 30
DEFAULT_INTERVAL = 15 * 60   # matches Cloudflare's Cache-Control: max-age=1800 on the sitemap
MAX_PER_POLL = 10            # how many new articles are *rendered* per pass (publishing is paced)
MAX_PER_DAY = 10             # hard stop for a day's automatic publishing (default - the dashboard can change it)
DAY_CAP_RANGE = (1, 50)      # what the dashboard's daily-limit control allows; Instagram's own limit is 100/24h
MIN_PUBLISH_GAP = 30 * 60    # standalone CLI only: seconds between two publishes (see QUEUE)
PUBLISH_JITTER = 5 * 60      # random extra wait on top, so the cadence is not machine-regular

# Set by app.py to its publish-queue function: QUEUE(png_path) -> bool. When set (i.e. inside
# the dashboard), Create & post hands every rendered post to that queue, which sends them one
# at a time 2-3 min apart (Insta_poster.MIN_API_GAP + jitter) - the same drip the Post button
# uses. Before this the autopilot published one post and held the rest for its own 30-min slot,
# releasing one per 15-min poll, so a busy day's stories trickled out or never went at all.
QUEUE = None
IST = timezone(timedelta(hours=5, minutes=30))   # the paper's own day, whatever this PC is set to

LOG_LINES = 60               # how much of the log the dashboard keeps

# Live state for the dashboard (app.py reads it; only this module writes it).
STATUS = {
    "enabled": False,        # is the autopilot thread meant to be polling?
    "running": False,        # is the thread alive?
    "posting": True,         # does it publish to Instagram, or only generate?
    "only_today": True,
    "max_per_day": MAX_PER_DAY,
    "interval": DEFAULT_INTERVAL,
    "current": "idle",
    "last_poll": "",
    "next_poll": "",
    "next_publish": "",      # set while a rendered post is being held back for its pacing slot
    "last_error": "",
    "sitemap_entries": 0,
    "today_new": 0,
    "generated": 0,          # counters for *this* process
    "published": 0,
    "failed": 0,
    "version": 0,            # bumped whenever a post is created/published - the page reloads on it
    "log": [],
}
_LOCK = threading.Lock()
ENABLED = threading.Event()  # cleared = paused (the dashboard's Pause button)
ENABLED.set()
POSTING = threading.Event()  # set = create AND publish; cleared = create only, the owner publishes
POSTING.set()
_day_cap = MAX_PER_DAY       # the live daily cap: Options.max_per_day reads it, set_day_cap() changes it


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)
    with _LOCK:
        STATUS["log"] = (STATUS["log"] + [line])[-LOG_LINES:]


def set_status(**fields):
    with _LOCK:
        STATUS.update(fields)


def bump_version():
    with _LOCK:
        STATUS["version"] += 1


# ------------------------------------------------------------- sitemap ----

def fetch_sitemap_entries():
    """[{'loc', 'title', 'published': datetime|None}] - newest first, as the sitemap lists them."""
    r = requests.get(SITEMAP_URL, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "xml")
    entries = []
    for u in soup.find_all("url"):
        loc = u.find("loc")
        if not loc:
            continue
        date_el = u.find("publication_date")
        title_el = u.find("title")
        entries.append({
            "loc": loc.get_text(strip=True),
            "title": title_el.get_text(strip=True) if title_el else "",
            "published": parse_date(date_el.get_text(strip=True) if date_el else ""),
        })
    return entries


def parse_date(text):
    try:
        return datetime.fromisoformat(text.strip())
    except ValueError:
        return None


def is_today(dt):
    """Was the article published on the paper's current calendar day (IST)?"""
    return bool(dt) and dt.astimezone(IST).date() == datetime.now(IST).date()


# --------------------------------------------------------------- state ----

def load_seen():
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return set(json.load(fh))
    except (OSError, ValueError):
        return set()


def save_seen(seen):
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(sorted(seen), fh, indent=2)


def load_day_state():
    """How much has already been published *today*, so the daily cap survives a restart of
    the dashboard (the Flask reloader restarts this thread on every code edit)."""
    today = datetime.now(IST).date().isoformat()
    try:
        with open(DAY_STATE_PATH, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    if state.get("date") != today:
        state = {"date": today, "published": 0, "failed": 0, "generated": 0}
    return state


def save_day_state(state):
    with open(DAY_STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


_DAY_LOCK = threading.Lock()


def bump_day(field):
    """Add one to today's `field` counter on disk. Read-modify-write under a lock, because the
    autopilot thread and the dashboard's queue worker both count, and a dict held across a
    whole poll pass would overwrite what the other one wrote."""
    with _DAY_LOCK:
        state = load_day_state()
        state[field] = state.get(field, 0) + 1
        save_day_state(state)
    with _LOCK:
        STATUS[field] = STATUS.get(field, 0) + 1


def autopilot_queued():
    """How many autopilot posts are sitting in the dashboard queue right now (per the sidecars)."""
    from scrape_post import META_DIR
    n = 0
    if os.path.isdir(META_DIR):
        for f in os.listdir(META_DIR):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(META_DIR, f), encoding="utf-8") as fh:
                    ig = json.load(fh).get("instagram") or {}
            except (OSError, ValueError, AttributeError):
                continue
            n += ig.get("status") == "queued" and ig.get("by") == "autopilot"
    return n


def cap_reached(opts):
    """Today's publish cap: what already went out plus what is queued to go out."""
    return load_day_state().get("published", 0) + autopilot_queued() >= opts.max_per_day


def record_publish(ok):
    """Called by the dashboard queue worker when it has sent (or failed to send) an autopilot post."""
    bump_day("published" if ok else "failed")
    bump_version()


_SETTINGS_LOCK = threading.Lock()


def load_settings():
    """What was last chosen on the dashboard: {"post": bool, "max_per_day": int}, either key
    possibly missing (never chosen - then .env decides)."""
    try:
        with open(MODE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(**fields):
    with _SETTINGS_LOCK:
        data = load_settings()
        data.update(fields)
        with open(MODE_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh)


def load_mode():
    """The mode last chosen on the dashboard (True = create + post, False = create only), or
    None if it was never set - then the NB_AUTOPOST switch in .env decides."""
    post = load_settings().get("post")
    return None if post is None else bool(post)


def clamp_day_cap(n):
    return max(DAY_CAP_RANGE[0], min(DAY_CAP_RANGE[1], int(n)))


def load_day_cap():
    """The daily cap last chosen on the dashboard, or None - then NB_MAX_PER_DAY decides."""
    try:
        return clamp_day_cap(load_settings()["max_per_day"])
    except (KeyError, TypeError, ValueError):
        return None


def set_day_cap(n):
    """The dashboard's daily-limit control. Applies from the next publish decision and is
    persisted like the mode. Returns the value actually stored (clamped to DAY_CAP_RANGE)."""
    global _day_cap
    n = clamp_day_cap(n)
    _day_cap = n
    save_settings(max_per_day=n)
    set_status(max_per_day=n)
    log(f"daily publish limit set to {n} from the dashboard")
    return n


def set_mode(post):
    """Switch between 'create + post' and 'create only' while the loop is running. Persisted,
    because the Flask reloader restarts this thread on every code edit and the choice must not
    silently flip back to auto-posting."""
    if post:
        POSTING.set()
    else:
        POSTING.clear()
    save_settings(post=bool(post))
    set_status(posting=bool(post))
    log("mode: create + post to Instagram automatically" if post
        else "mode: create only - new posts wait in the gallery for you to publish them")


def last_api_attempt():
    """When did anything last talk to the Instagram API - this poller, the dashboard button or
    the CLI, successfully or not? Lives in Insta_poster (shared with the dashboard's manual
    queue); derived from the sidecars so it survives the reloader restarting this thread."""
    import Insta_poster as insta
    return insta.last_api_attempt()


def gap_remaining(opts):
    """Seconds still to wait before another publish is allowed. 0 means go ahead."""
    import Insta_poster as insta
    if not opts or opts.min_gap <= 0:      # pacing "off" still leaves Insta_poster's hard floor
        return insta.api_gap_remaining(jitter=insta.API_GAP_JITTER)
    return insta.api_gap_remaining(min_gap=opts.min_gap, jitter=opts.jitter)


def already_handled(url):
    """Did the owner already make (and publish) this story by hand in the dashboard? Their
    post is not in seen_urls.json - only the poller writes that - so check the sidecars by
    article URL before regenerating and re-posting it."""
    from scrape_post import META_DIR
    if not os.path.isdir(META_DIR):
        return ""
    for f in os.listdir(META_DIR):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(META_DIR, f), encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        if meta.get("url") == url and (meta.get("instagram") or {}).get("status") == "published":
            return f[:-5]
    return ""


def already_published(png_path):
    """True if this post's sidecar says it is already on Instagram - the last line of defence
    against a double post (seen_urls.json is the first)."""
    import Insta_poster as insta
    stem = os.path.splitext(os.path.basename(png_path))[0]
    return ((insta.read_sidecar(stem).get("instagram") or {}).get("status") == "published")


# ---------------------------------------------------------------- poll ----

def poll_once(seen, opts):
    """One pass: release whatever is waiting for a publish slot, then fetch the sitemap and
    handle every eligible new article. Returns `seen`."""
    drain_pending(opts)             # posts rendered earlier but never sent go first
    set_status(current="checking sitemap", last_poll=datetime.now().strftime("%d %b %H:%M:%S"))
    try:
        entries = fetch_sitemap_entries()
    except Exception as e:                      # noqa: BLE001 - a bad fetch must not kill the loop
        log(f"sitemap fetch failed: {e}")
        set_status(current="idle", last_error=f"sitemap fetch failed: {e}")
        return seen
    set_status(sitemap_entries=len(entries), last_error="")

    fresh = [e for e in entries if e["loc"] not in seen]
    if opts.only_today:
        eligible = [e for e in fresh if is_today(e["published"])]
        stale = [e for e in fresh if not is_today(e["published"])]
        if stale and not opts.dry_run:
            # not from today: never post these, but remember them so they aren't re-examined
            for e in stale:
                seen.add(e["loc"])
            save_seen(seen)
            log(f"{len(stale)} older article(s) marked as seen (not today's)")
        elif stale:
            log(f"DRY-RUN {len(stale)} older article(s) would be marked as seen (not today's)")
    else:
        eligible = fresh
    set_status(today_new=len(eligible))

    if not eligible:
        log(f"no new articles ({len(entries)} in sitemap)")
        set_status(current="idle")
        return seen

    eligible.reverse()                          # oldest first, so posts go out in story order
    batch, waiting = eligible[:opts.max_per_poll], eligible[opts.max_per_poll:]
    log(f"{len(eligible)} new article(s) for today"
        + (f" - handling {len(batch)}, {len(waiting)} left for the next poll" if waiting else ""))

    for e in batch:
        if not ENABLED.is_set():
            log("paused - stopping this pass")
            break
        handle_article(e, seen, opts)
    set_status(current="idle")
    return seen


def handle_article(entry, seen, opts):
    """Scrape + render one article, then publish it unless that is switched off or capped."""
    url, title = entry["loc"], entry["title"]
    done = already_handled(url)
    if done:
        log(f"SKIP {title or url}: already on Instagram as {done}.png")
        if not opts.dry_run:
            seen.add(url)
            save_seen(seen)
        return
    if opts.dry_run:
        log(f"DRY-RUN would post: {title or url}")
        return
    set_status(current=f"generating: {title or url}")
    try:
        headline, _image_url, post_path, _intro = process(url, opts.focus)
    except Exception as e:                      # noqa: BLE001 - one bad article, not the whole run
        log(f"FAIL scrape/render {url}: {e}")
        seen.add(url)
        save_seen(seen)
        bump_day("failed")
        return
    seen.add(url)                               # handled: never scrape or post this URL again
    save_seen(seen)
    from scrape_post import update_meta
    # mark it as ours, so catch_up() can finish it if this thread dies before publishing and
    # never touches a post the owner made by hand and deliberately left unpublished. In
    # create-only mode the post is additionally marked manual: switching back to auto-posting
    # later must not send out everything that piled up while the owner was choosing by hand.
    update_meta(os.path.splitext(os.path.basename(post_path))[0], source="autopilot",
                publish="auto" if opts.post else "manual")
    bump_day("generated")
    bump_version()
    log(f"OK   {headline} -> {os.path.basename(post_path)}")

    if not opts.post:
        log(f"     create-only mode: {os.path.basename(post_path)} is waiting in the gallery")
        return
    if cap_reached(opts):
        log(f"daily publish cap reached ({opts.max_per_day}) - post generated, publish it from "
            f"the dashboard if you want it out")
        return
    publish(post_path, opts)


def publish(post_path, opts=None):
    """Hand a finished post to Instagram. Inside the dashboard (QUEUE set) that means putting it
    in the publish queue, which sends it 2-3 min after whatever went before. Standalone (CLI) it
    publishes directly, if the pacing gap allows it. Every outcome ends up in the post's
    sidecar, so the dashboard card shows it exactly as if the owner had pressed the button.
    Returns True if it was queued / went out, False if it was skipped or held back for a later
    slot (a held post keeps no Instagram block, so the next poll's drain_pending() picks it up)."""
    import Insta_poster as insta
    name = os.path.basename(post_path)
    if already_published(post_path):
        log(f"SKIP {name}: sidecar already says published")
        return False
    if not insta.is_configured():
        log(f"SKIP {name}: {insta.configuration_error()}")
        set_status(last_error=insta.configuration_error())
        return False
    problem = insta.caption_problem(insta.read_sidecar(os.path.splitext(name)[0]))
    if problem:   # no caption content - it stays in the gallery, flagged, and is never retried
        log(f"NOT READY {name}: {problem} - left unpublished, re-generate it from the dashboard")
        return False
    if QUEUE is not None:
        return bool(QUEUE(post_path))
    wait = gap_remaining(opts)
    if wait:
        due = datetime.now() + timedelta(seconds=wait)
        log(f"HOLD {name}: pacing - next publish slot about {due.strftime('%H:%M')} "
            f"({int(wait // 60)} min away)")
        set_status(current="idle", next_publish=due.strftime("%d %b %H:%M"))
        return False
    set_status(current=f"publishing: {name}")
    try:
        result = insta.publish_post(post_path, log=lambda m: log(f"  {m}"))
    except insta.PacingError as e:              # a manual publish just went out: hold, not a failure
        due = datetime.now() + timedelta(seconds=e.wait)
        log(f"HOLD {name}: {e}")
        set_status(current="idle", next_publish=due.strftime("%d %b %H:%M"))
        return False
    except insta.InstagramError as e:
        log(f"POST FAIL {name}: {e}")
        bump_day("failed")
        set_status(last_error=str(e))
        bump_version()
        return False
    except Exception as e:                      # noqa: BLE001 - never let the loop die on a post
        log(f"POST FAIL {name}: {insta.redact(f'{type(e).__name__}: {e}')}")
        bump_day("failed")
        return False
    bump_day("published")
    bump_version()
    log(f"POSTED {name} -> media {result.get('media_id')} {result.get('permalink') or ''}")
    set_status(next_publish="")
    return True


# ---------------------------------------------------------------- loop ----

class Options:
    """Plain settings holder - argparse fills it for the CLI, app.py builds one for the thread."""

    def __init__(self, interval=DEFAULT_INTERVAL, focus="center", post=True, only_today=True,
                 max_per_poll=MAX_PER_POLL, max_per_day=MAX_PER_DAY, dry_run=False,
                 min_gap=MIN_PUBLISH_GAP, jitter=PUBLISH_JITTER):
        self.interval, self.focus, self.post, self.only_today = interval, focus, post, only_today
        self.max_per_poll, self.max_per_day, self.dry_run = max_per_poll, max_per_day, dry_run
        self.min_gap, self.jitter = min_gap, jitter

    # `post` is the live mode switch (POSTING) so the dashboard can flip it mid-run and every
    # place that asks "should this be published?" sees the new answer immediately
    @property
    def post(self):
        return POSTING.is_set()

    @post.setter
    def post(self, value):
        if value:
            POSTING.set()
        else:
            POSTING.clear()

    # the same for the daily cap: the dashboard's daily-limit control changes it mid-run
    @property
    def max_per_day(self):
        return _day_cap

    @max_per_day.setter
    def max_per_day(self, value):
        global _day_cap
        _day_cap = int(value)


def pending_posts():
    """Posts this poller rendered today but never got as far as publishing - the thread is
    restarted whenever a project .py file is edited (Flask's reloader), which can land between
    the render and the publish. Only autopilot-made posts with no Instagram attempt at all
    qualify: a *failed* one is left for the owner's Retry button, and a post they made by hand
    is never touched."""
    from make_post import OUT_DIR
    from scrape_post import META_DIR
    from Insta_poster import caption_problem
    today = datetime.now().date().isoformat()
    out = []
    if not os.path.isdir(META_DIR):
        return out
    for f in sorted(os.listdir(META_DIR)):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(META_DIR, f), encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        if meta.get("source") != "autopilot" or meta.get("instagram"):
            continue
        if meta.get("publish") == "manual":     # rendered in create-only mode: the owner's call
            continue
        # no first line / no hashtags: unpublishable, and skipping it here keeps it from
        # standing at the head of the drip queue and blocking every post behind it
        if caption_problem(meta):
            continue
        if not str(meta.get("created") or "").startswith(today):
            continue
        png = os.path.join(OUT_DIR, f[:-5] + ".png")
        if os.path.exists(png):
            out.append((meta.get("created"), png))
    return [png for _, png in sorted(out)]


def drain_pending(opts, limit=None):
    """Release posts that were rendered but never published. Inside the dashboard every one of
    them goes into the publish queue (which does the 2-3 min spacing); standalone, at most one
    per call and only when the pacing gap has elapsed. Runs at the top of every poll, so a
    held-back post goes out on a later pass without needing a restart."""
    if not opts.post or opts.dry_run:
        return
    if limit is None:
        limit = 10 ** 6 if QUEUE is not None else 1
    sent = 0
    for png in pending_posts():
        if sent >= limit or not ENABLED.is_set() or cap_reached(opts):
            return
        log(f"pending: {os.path.basename(png)} was rendered earlier and is still unpublished")
        if not publish(png, opts):
            return              # held back, or refused - everything behind it waits as well
        sent += 1


def catch_up(opts):
    """Publish what a previous run left half-done, before looking for anything new."""
    drain_pending(opts)


def run_forever(opts=None, stop=None):
    """The autopilot loop. `stop` is a threading.Event that ends it; ENABLED pauses it."""
    opts = opts or Options()
    stop = stop or threading.Event()
    set_status(running=True, posting=opts.post, only_today=opts.only_today, interval=opts.interval,
               enabled=ENABLED.is_set(), max_per_day=opts.max_per_day)
    day = load_day_state()
    log(f"autopilot started - polling every {opts.interval // 60} min, "
        f"{'publishing to Instagram' if opts.post else 'generating posts only'}, "
        f"{'today only' if opts.only_today else 'all dates'}, "
        f"caps {opts.max_per_poll} rendered/poll and {opts.max_per_day} published/day, "
        + ("publishes queued 2-3 min apart " if QUEUE is not None
           else f"at least {opts.min_gap // 60} min between publishes ")
        + f"({day['published']} published so far today)")
    seen = load_seen()
    if ENABLED.is_set():
        catch_up(opts)
    while not stop.is_set():
        if ENABLED.is_set():
            set_status(enabled=True)
            try:
                seen = poll_once(seen, opts)
            except Exception as e:              # noqa: BLE001 - the loop outlives any single pass
                log(f"poll pass crashed: {type(e).__name__}: {e}")
                set_status(current="idle", last_error=f"{type(e).__name__}: {e}")
        else:
            set_status(enabled=False, current="paused")
        nxt = datetime.now() + timedelta(seconds=opts.interval)
        set_status(next_poll=nxt.strftime("%d %b %H:%M:%S"))
        stop.wait(opts.interval)
    set_status(running=False, current="stopped")
    log("autopilot stopped")


def main():
    p = argparse.ArgumentParser(description="Poll newsband.in's sitemap, make posts and publish them.")
    p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                   help="seconds between polls (default 900 = 15 min)")
    p.add_argument("--once", action="store_true", help="poll a single time and exit")
    p.add_argument("--focus", choices=["top", "center", "bottom"], default="center")
    p.add_argument("--no-post", dest="post", action="store_false",
                   help="only generate posts, do not publish them to Instagram")
    p.add_argument("--all-dates", dest="only_today", action="store_false",
                   help="handle every unseen article, not just today's")
    p.add_argument("--max-per-poll", type=int, default=MAX_PER_POLL)
    p.add_argument("--max-per-day", type=int, default=MAX_PER_DAY)
    p.add_argument("--min-gap", type=int, default=MIN_PUBLISH_GAP // 60,
                   help="minutes between two publishes (default 30; 0 disables pacing)")
    p.add_argument("--dry-run", action="store_true", help="list what would happen, change nothing")
    args = p.parse_args()
    opts = Options(args.interval, args.focus, args.post, args.only_today,
                   args.max_per_poll, args.max_per_day, args.dry_run, args.min_gap * 60)

    if not os.path.exists(STATE_PATH):
        # first ever run: remember what is already published so old stories are never backfilled
        try:
            seen = {e["loc"] for e in fetch_sitemap_entries()}
        except Exception as e:                  # noqa: BLE001
            log(f"could not seed on first run: {e}")
            seen = set()
        save_seen(seen)
        log(f"first run: seeded {len(seen)} existing article(s) as already-seen")

    if args.once:
        poll_once(load_seen(), opts)
        return
    run_forever(opts)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
