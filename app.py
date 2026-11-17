"""
Newsband post dashboard.

    python app.py
    -> open http://127.0.0.1:5000

Paste one or more article links, click Generate, download the finished posts.

It also runs the autopilot (poll_new_articles.run_forever) in a background thread, so leaving
this open is the whole pipeline: newsband.in's sitemap -> scrape -> render -> hashtags ->
Instagram. The strip at the top of the page shows what it is doing and can pause it.
"""

import hmac
import json
import os
import re
import threading
import time
from urllib.parse import urlsplit

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)

import Insta_poster as insta
import poll_new_articles as autopilot
from make_post import OUT_DIR
from scrape_post import META_DIR, process, update_meta

app = Flask(__name__)
app.secret_key = os.environ.get("NB_SECRET", "newsband-local-dashboard")

URL_RE = re.compile(r"https?://\S+")


# ------------------------------------------------------------------ login ----
# On the office LAN the dashboard had no login. On the public server (automation.newsband.in,
# see deploy/) anyone who found the URL could publish to the paper's Instagram or delete posts,
# so NB_DASH_PASSWORD turns on HTTP Basic auth for every route; wsgi.py refuses to start
# without it. Unset (the Windows PC) = no login, exactly as before.
DASH_USER = os.environ.get("NB_DASH_USER", "newsband")
DASH_PASSWORD = os.environ.get("NB_DASH_PASSWORD", "")


@app.before_request
def require_login():
    if not DASH_PASSWORD:
        return None
    auth = request.authorization
    ok = (auth is not None and auth.type == "basic"
          and hmac.compare_digest((auth.username or "").encode(), DASH_USER.encode())
          and hmac.compare_digest((auth.password or "").encode(), DASH_PASSWORD.encode()))
    if not ok:
        return app.response_class("Login required.", 401,
                                  {"WWW-Authenticate": 'Basic realm="Newsband automation"'})
    # The browser re-sends Basic credentials on *any* request to this host, including a form
    # another site submits - so a POST must come from our own pages (CSRF guard).
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            abort(403)
    return None


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", results=None, gallery=list_outputs())


@app.route("/generate", methods=["POST"])
def generate():
    urls = URL_RE.findall(request.form.get("urls", ""))
    focus = request.form.get("focus", "center")
    if focus not in ("top", "center", "bottom"):
        focus = "center"
    if not urls:
        flash("Paste at least one article link.")
        return redirect(url_for("index"))

    results = []
    for url in dict.fromkeys(urls):  # de-dupe, keep order
        t0 = time.time()
        try:
            headline, image_url, post_path, intro = process(url, focus)
            fname = os.path.basename(post_path)
            results.append({
                "ok": True, "url": url, "headline": headline, "intro": intro,
                "image_url": image_url, "file": fname,
                "secs": round(time.time() - t0, 1),
                "hashtags": hashtag_view(load_meta(fname)),
                "instagram": instagram_view(load_meta(fname), fname),
            })
        except Exception as e:
            results.append({"ok": False, "url": url, "error": str(e)})
    return render_template("index.html", results=results, gallery=list_outputs())


@app.route("/output/<path:filename>")
def output_file(filename):
    """Preview (inline) or download (?dl=1) a generated post."""
    if not safe_name(filename):
        abort(404)
    return send_from_directory(OUT_DIR, filename, as_attachment=bool(request.args.get("dl")))


@app.route("/meta/<path:filename>")
def meta_file(filename):
    """Raw sidecar JSON (headline, intro, hashtags + full candidate table) for a post."""
    if not safe_name(filename):
        abort(404)
    meta = load_meta(filename)
    if not meta:
        abort(404)
    return app.response_class(json.dumps(meta, ensure_ascii=False, indent=2), mimetype="application/json")


@app.route("/delete/<path:filename>", methods=["POST"])
def delete_file(filename):
    if safe_name(filename):
        try:
            os.remove(os.path.join(OUT_DIR, filename))
        except FileNotFoundError:
            pass
    return redirect(url_for("index"))


# ------------------------------------------------------------- Instagram ----
# The dashboard only calls Insta_poster; all Graph API / image-host logic lives there.
# The autopilot publishes through the same Insta_poster.publish_post().
#
# The button does NOT publish directly. Clicking "Post to Instagram" on several cards in a row
# used to send them 20-30 s apart - the exact burst pattern that got the account's API access
# suspended on 2026-09-19 - and Create-only mode made that the normal way of working. So clicks
# go into a queue and one worker thread sends them one at a time, waiting MIN_API_GAP plus a
# random 0-API_GAP_JITTER s (2-3 min) after the previous API attempt, whoever made it. The
# queue is mirrored into the sidecars ("status": "queued") so the cards show it, and so a
# reloader restart re-queues whatever was still waiting instead of forgetting it.
# The autopilot's Create & post mode feeds this same queue (autopilot.QUEUE), so its posts go
# out at the same 2-3 min spacing, interleaved in order with anything clicked by hand.

_queue = []                      # filenames in click order; [0] is the one being waited on
_queue_force = {}                # filename -> force flag it was queued with
_queue_due = {}                  # filename -> "HH:MM" the worker plans to send it (head only)
_queue_auto = set()              # filenames the autopilot queued (counted against its daily cap)
_queue_sending = None            # the filename publish_post() is working on right now, if any
_queue_lock = threading.Lock()
_queue_wake = threading.Event()
_queue_thread = None


def queue_position(filename):
    with _queue_lock:
        return _queue.index(filename) + 1 if filename in _queue else 0


def enqueue_publish(filename, force, prev, auto=False):
    """Put one card's publish in the queue and mark its sidecar. A repost keeps the earlier
    media id / permalink in the block so the card can still show the live post. `auto` = queued
    by the autopilot rather than a click (recorded as "by", so a restart re-queues it as such)."""
    stem = filename[:-4]
    block = {"status": "queued", "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "by": "autopilot" if auto else "manual"}
    if prev.get("status") == "published":
        block.update({k: prev[k] for k in ("media_id", "permalink", "published_at") if prev.get(k)})
    meta = update_meta(stem, instagram=block)
    with _queue_lock:
        _queue.append(filename)
        _queue_force[filename] = force
        if auto:
            _queue_auto.add(filename)
    ensure_queue_worker()
    _queue_wake.set()
    autopilot.log(f"QUEUED {filename} for Instagram ({'autopilot' if auto else 'manual'}, "
                  f"position {queue_position(filename)})")
    return meta


def unqueue_autopilot(reason, keep=0, again="when auto-posting resumes"):
    """Pause / Create only: take the autopilot's not-yet-sent posts back out of the queue. They
    keep publish=auto and no Instagram block, so drain_pending() queues them again on resume;
    posts the owner clicked stay queued. `keep` = leave the first that many in (a lowered daily
    limit). The post being sent right now is never touched - it can't be called back."""
    with _queue_lock:
        mine = [f for f in _queue if f in _queue_auto][keep:]
    removed = 0
    for f in mine:
        with _queue_lock:
            if f not in _queue or f == _queue_sending:
                continue
            _queue.remove(f)
            _queue_force.pop(f, None)
            _queue_due.pop(f, None)
            _queue_auto.discard(f)
        update_meta(f[:-4], instagram={})
        removed += 1
    if removed:
        autopilot.log(f"{reason}: {removed} autopilot post(s) taken out of the queue - "
                      f"they are queued again {again}")
        autopilot.bump_version()


def autopilot_enqueue(png_path):
    """autopilot.QUEUE: the Create & post mode's way of publishing - queue, don't send."""
    filename = os.path.basename(png_path)
    if queue_position(filename):
        return True
    prev = load_meta(filename).get("instagram") or {}
    if prev.get("status") in ("queued", "published"):
        return False
    enqueue_publish(filename, False, prev, auto=True)
    autopilot.bump_version()
    return True


def dequeue_publish(filename):
    """Take a still-waiting post out of the queue and put its sidecar back the way it was:
    published (if this was a repost) or no Instagram block at all."""
    with _queue_lock:
        if filename not in _queue:
            return None
        _queue.remove(filename)
        force = _queue_force.pop(filename, False)
        _queue_due.pop(filename, None)
        auto = filename in _queue_auto
        _queue_auto.discard(filename)
    stem = filename[:-4]
    prev = (load_meta(filename).get("instagram") or {})
    if force and prev.get("media_id"):
        restored = {"status": "published"}
        restored.update({k: prev[k] for k in ("media_id", "permalink", "published_at") if prev.get(k)})
    else:
        restored = {}
    autopilot.log(f"UNQUEUED {filename} - cancelled from the dashboard")
    if auto:   # the owner said no: the autopilot must not queue it again on its next poll
        return update_meta(stem, instagram=restored, publish="manual")
    return update_meta(stem, instagram=restored)


def requeue_from_sidecars():
    """On start-up: whatever was queued when the last process died is queued again, in the
    order it was clicked. Runs only in the reloader child, like the autopilot thread."""
    if not os.path.isdir(META_DIR):
        return
    waiting = []
    auto_posting = autopilot.POSTING.is_set() and autopilot.ENABLED.is_set()
    for f in os.listdir(META_DIR):
        if not f.endswith(".json"):
            continue
        ig = load_meta(f[:-5] + ".png").get("instagram") or {}
        if ig.get("status") == "queued" and ig.get("by") == "autopilot" and not auto_posting:
            # started paused or in Create only (always, on a local run): what unqueue_autopilot()
            # would have done, instead of letting the worker send it anyway
            update_meta(f[:-5], instagram={})
            continue
        if ig.get("status") == "queued" and os.path.exists(os.path.join(OUT_DIR, f[:-5] + ".png")):
            waiting.append((ig.get("queued_at") or "", f[:-5] + ".png", bool(ig.get("media_id")),
                            ig.get("by") == "autopilot"))
    with _queue_lock:
        for _, filename, force, auto in sorted(waiting):
            if filename not in _queue:
                _queue.append(filename)
                _queue_force[filename] = force
                if auto:
                    _queue_auto.add(filename)
    if waiting:
        autopilot.log(f"re-queued {len(waiting)} post(s) left waiting by the last restart")
        ensure_queue_worker()
        _queue_wake.set()


def ensure_queue_worker():
    global _queue_thread
    with _queue_lock:
        if _queue_thread is None or not _queue_thread.is_alive():
            _queue_thread = threading.Thread(target=queue_worker, name="ig-queue", daemon=True)
            _queue_thread.start()


def queue_worker():
    """Send queued posts one at a time, each at least MIN_API_GAP + random jitter after the last
    Instagram API attempt (manual or autopilot). Every outcome lands in the sidecar exactly as
    a direct button publish did, then the page version is bumped so open dashboards redraw."""
    global _queue_sending
    while True:
        with _queue_lock:
            filename = _queue[0] if _queue else None
        if filename is None:
            _queue_wake.wait()
            _queue_wake.clear()
            continue
        wait = insta.api_gap_remaining(jitter=insta.API_GAP_JITTER)
        due = time.time() + wait
        with _queue_lock:
            _queue_due[filename] = time.strftime("%H:%M", time.localtime(due))
        if wait:
            autopilot.log(f"HOLD {filename}: manual queue - sends at about "
                          f"{_queue_due.get(filename)} ({int(wait // 60)} min {int(wait % 60)} s)")
        while time.time() < due:                 # sleep in short steps so Cancel takes effect
            time.sleep(1)
            with _queue_lock:
                if not _queue or _queue[0] != filename:
                    break
        with _queue_lock:
            if not _queue or _queue[0] != filename:   # cancelled while waiting
                continue
            force = _queue_force.get(filename, False)
            auto = filename in _queue_auto
            _queue_sending = filename
        png_path = os.path.join(OUT_DIR, filename)
        stem = filename[:-4]
        who = "autopilot" if auto else "manual"
        outcome = None                           # True/False = sent/failed, reported to the autopilot
        try:
            result = insta.publish_post(png_path, force=force, log=lambda m: autopilot.log(f"  {m}"))
            outcome = True
            autopilot.log(f"POSTED {filename} ({who}) -> media {result.get('media_id')} "
                          f"{result.get('permalink') or ''}")
        except insta.PacingError as e:          # the autopilot got in first: wait again
            autopilot.log(f"HOLD {filename}: {e}")
            with _queue_lock:
                _queue_sending = None
            continue
        except insta.IncompleteError as e:      # lost its caption since it was queued
            update_meta(stem, instagram={})     # back to "not posted", with the reason on the card
            autopilot.log(f"DROPPED {filename} from the queue: {e}")
        except insta.InstagramError as e:       # already written to the sidecar by publish_post
            outcome = False
            autopilot.log(f"POST FAIL {filename} ({who}): {e}")
        except Exception as e:  # noqa: BLE001 - never let a publish kill the queue
            msg = insta.redact(f"{type(e).__name__}: {e}")
            if not os.path.exists(png_path):
                msg = "post image no longer exists"
            update_meta(stem, instagram={"status": "failed", "error": msg,
                                         "attempted_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
            outcome = False
            autopilot.log(f"POST FAIL {filename} ({who}): {msg}")
        with _queue_lock:
            if _queue and _queue[0] == filename:
                _queue.pop(0)
            _queue_sending = None
            _queue_force.pop(filename, None)
            _queue_due.pop(filename, None)
            _queue_auto.discard(filename)
        if auto and outcome is not None:
            autopilot.record_publish(outcome)   # the autopilot's daily counters / cap
        autopilot.bump_version()


@app.route("/instagram/post/<path:filename>", methods=["POST"])
def instagram_post(filename):
    """Queue one gallery post for publishing. JSON reply: {ok, instagram: <view>} - the view is
    the same block the template renders, so the card can redraw itself as "Queued". ?force=1
    republishes a post already marked published (explicit 'Post again')."""
    if not safe_name(filename):
        abort(404)
    png_path = os.path.join(OUT_DIR, filename)
    if not os.path.exists(png_path):
        return jsonify(ok=False, error="post image no longer exists"), 404
    meta = load_meta(filename)
    prev = meta.get("instagram") or {}
    force = request.form.get("force") == "1" or request.args.get("force") == "1"
    if not insta.is_configured():
        return jsonify(ok=False, error=insta.configuration_error()), 400
    problem = insta.caption_problem(meta)
    if problem:            # no first line / no hashtags: nothing to caption it with
        return jsonify(ok=False, error=f"not ready to post - {problem}",
                       instagram=instagram_view(meta, filename)), 400
    if prev.get("status") == "queued" or queue_position(filename):
        return jsonify(ok=False, error="this post is already waiting in the queue",
                       instagram=instagram_view(meta, filename)), 409
    if prev.get("status") == "published" and not force:
        return jsonify(ok=False, error="already published (use Post again to repost)",
                       instagram=instagram_view(meta, filename)), 409
    meta = enqueue_publish(filename, force, prev)
    return jsonify(ok=True, instagram=instagram_view(meta, filename)), 202


@app.route("/instagram/cancel/<path:filename>", methods=["POST"])
def instagram_cancel(filename):
    """Take a queued post back out before it is sent."""
    if not safe_name(filename):
        abort(404)
    meta = dequeue_publish(filename)
    if meta is None:
        return jsonify(ok=False, error="that post is not in the queue (already sent?)",
                       instagram=instagram_view(load_meta(filename), filename)), 409
    return jsonify(ok=True, instagram=instagram_view(meta, filename))


# -------------------------------------------------------------- autopilot ----
# poll_new_articles runs in a background thread here, so keeping this dashboard open is the
# whole pipeline: sitemap -> scrape -> render -> publish -> the cards below.

_autopilot_stop = threading.Event()
LOCAL = False   # True when started with `python app.py` (a PC), False under wsgi.py (the server)


def autopilot_options(local=False):
    """Settings for the background poller, all overridable from the environment. A local run
    always starts in Create only, whatever was saved: the server is the one that auto-posts,
    and a second copy posting on a PC would put every story on Instagram twice."""
    saved_mode = autopilot.load_mode()   # the mode picked on the dashboard sticks across restarts
    saved_cap = autopilot.load_day_cap()  # so does the daily limit
    if local:
        post = False
    elif saved_mode is not None:
        post = saved_mode
    else:
        post = os.environ.get("NB_AUTOPOST", "1").strip().lower() not in ("0", "false", "no", "off")
    return autopilot.Options(
        interval=int(os.environ.get("NB_POLL_INTERVAL", autopilot.DEFAULT_INTERVAL)),
        post=post,
        only_today=os.environ.get("NB_TODAY_ONLY", "1").strip().lower() not in ("0", "false", "no", "off"),
        max_per_poll=int(os.environ.get("NB_MAX_PER_POLL", autopilot.MAX_PER_POLL)),
        max_per_day=saved_cap if saved_cap is not None else
                    int(os.environ.get("NB_MAX_PER_DAY", autopilot.MAX_PER_DAY)),
        min_gap=int(os.environ.get("NB_MIN_GAP_MINUTES",
                                   autopilot.MIN_PUBLISH_GAP // 60)) * 60,
    )


def start_autopilot(local=False):
    """Start the poll loop once, in the process that actually serves requests."""
    opts = autopilot_options(local)
    if local:
        autopilot.log("local run - started in Create only, nothing is posted automatically "
                      "(the server does the posting)")
    if os.environ.get("NB_AUTOPILOT", "1").strip().lower() in ("0", "false", "no", "off"):
        autopilot.ENABLED.clear()      # thread still runs, but paused - the UI can resume it
    t = threading.Thread(target=autopilot.run_forever, args=(opts, _autopilot_stop),
                         name="autopilot", daemon=True)
    t.start()
    return opts


_background_started = False


def start_background(local=False):
    """Start the autopilot + publish-queue threads. Exactly once per process, and only in the
    process that serves requests: the dev server's reloader child (below) or the single
    gunicorn worker (wsgi.py). Two copies would race over seen_urls.json and double-post.
    `local` = the dev server on a PC: starts in Create only (see autopilot_options)."""
    global _background_started, LOCAL
    if _background_started:
        return None
    _background_started = True
    LOCAL = local
    os.makedirs(OUT_DIR, exist_ok=True)
    autopilot.QUEUE = autopilot_enqueue   # Create & post feeds the dashboard's publish queue
    opts = start_autopilot(local)
    requeue_from_sidecars()   # manual publishes still waiting when the last process died
    print(f"  autopilot: polling every {opts.interval // 60} min, "
          f"{'auto-posting to Instagram' if opts.post else 'generating posts only'}"
          f"{' (local run: auto-posting always starts off)' if local else ''}"
          f"{'' if autopilot.ENABLED.is_set() else ' (PAUSED)'}\n", flush=True)
    return opts


def _today_counts():
    """(posts published to Instagram today, autopilot posts made today still waiting for the
    owner) - read from the sidecars, so both the autopilot and the Publish button count."""
    today = time.strftime("%Y-%m-%d")
    posted = waiting = 0
    if not os.path.isdir(META_DIR):
        return posted, waiting
    for f in os.listdir(META_DIR):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(META_DIR, f), encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        ig = meta.get("instagram") or {}
        if ig.get("status") == "published":
            posted += str(ig.get("published_at") or "").startswith(today)
        elif (meta.get("source") == "autopilot" and ig.get("status") != "queued"
              and str(meta.get("created") or "").startswith(today)):
            waiting += 1
    return posted, waiting


@app.route("/autopilot/status")
def autopilot_status():
    """Live state for the strip at the top of the page (polled by the browser)."""
    day = autopilot.load_day_state()
    st = dict(autopilot.STATUS)
    # the loop only refreshes STATUS when it wakes up (every 15 min), so read the pause switch
    # itself - otherwise the strip keeps saying "on" for a quarter of an hour after Pause
    st["enabled"] = autopilot.ENABLED.is_set()
    st["posting"] = autopilot.POSTING.is_set()
    st["generated_today"] = day.get("generated", 0)
    # "posted today" = everything that went out today, the owner's own button presses included
    # (the day-state counter only counts autopilot publishes, because it feeds the daily cap)
    st["published_today"], st["waiting_today"] = _today_counts()
    st["configured"] = insta.is_configured()
    st["config_error"] = insta.configuration_error()
    st["local"] = LOCAL
    st["cap_min"], st["cap_max"] = autopilot.DAY_CAP_RANGE
    with _queue_lock:                          # the publish queue (manual + autopilot), for the strip
        st["queued"] = len(_queue)
        st["queue_next"] = _queue_due.get(_queue[0], "") if _queue else ""
    return jsonify(st)


@app.route("/autopilot/toggle", methods=["POST"])
def autopilot_toggle():
    """Pause / resume automatic publishing without restarting the dashboard."""
    if autopilot.ENABLED.is_set():
        autopilot.ENABLED.clear()
        autopilot.log("paused from the dashboard - nothing new will be posted until you resume")
        unqueue_autopilot("paused")
    else:
        autopilot.ENABLED.set()
        autopilot.log("resumed from the dashboard")
    autopilot.set_status(enabled=autopilot.ENABLED.is_set(),
                         current="paused" if not autopilot.ENABLED.is_set() else "idle")
    return jsonify(ok=True, enabled=autopilot.ENABLED.is_set())


@app.route("/autopilot/mode", methods=["POST"])
def autopilot_mode():
    """The strip's mode selector: {"post": true} = create + publish, {"post": false} = create
    only and leave publishing to the owner. Takes effect immediately and is remembered."""
    body = request.get_json(silent=True) or {}
    post = body.get("post", request.form.get("post"))
    if isinstance(post, str):
        post = post.strip().lower() not in ("0", "false", "no", "off", "manual")
    autopilot.set_mode(bool(post))
    if not post:
        unqueue_autopilot("create-only mode")
    return jsonify(ok=True, posting=autopilot.POSTING.is_set())


@app.route("/autopilot/cap", methods=["POST"])
def autopilot_cap():
    """The strip's daily-limit control: {"max_per_day": n} = the most posts the autopilot may
    publish per IST day (manual clicks are not counted). Takes effect immediately and is
    remembered. Lowered below what is already queued, the newest autopilot posts come back out
    of the queue; raised, posts held back by the old limit are queued on the next check."""
    body = request.get_json(silent=True) or {}
    try:
        n = autopilot.set_day_cap(body.get("max_per_day", request.form.get("max_per_day")))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="the daily limit must be a whole number"), 400
    room = max(0, n - autopilot.load_day_state().get("published", 0))
    unqueue_autopilot(f"daily limit lowered to {n}", keep=room,
                      again="if the limit is raised again today")
    return jsonify(ok=True, max_per_day=n)


def instagram_view(meta, filename=None):
    """Shape the sidecar's instagram block for the card. Always returns a dict so every card
    shows the Instagram section; `configured` tells the template whether the button is live.
    `filename` lets a queued card show its place in the manual queue and when it will send."""
    i = (meta or {}).get("instagram") or {}
    problem = insta.caption_problem(meta)
    return {
        "status": i.get("status") or "not_posted",       # not_posted | queued | published | failed
        # on Instagram right now: published, or a repost queued/failed while the earlier one stays up
        "live": i.get("status") == "published" or bool(i.get("media_id")),
        "ready": not problem,                           # has a first line and hashtags to caption it
        "not_ready": problem,                            # why it cannot be posted yet
        "queue_pos": queue_position(filename) if filename else 0,
        "due": _queue_due.get(filename, "") if filename else "",
        "media_id": i.get("media_id") or "",
        "permalink": i.get("permalink") or "",
        "published_at": (i.get("published_at") or "").replace("T", " "),
        "attempted_at": (i.get("attempted_at") or "").replace("T", " "),
        "error": i.get("error") or "",
        "configured": insta.is_configured(),
        "config_error": insta.configuration_error(),   # the hint shown instead of the button
    }


def safe_name(name):
    return bool(re.fullmatch(r"[A-Za-z0-9._-]+\.png", name))


def load_meta(filename):
    """Headline/intro saved alongside the post by scrape_post.save_meta(). Posts made
    straight from make_post.py have none, so the gallery falls back to the filename."""
    path = os.path.join(META_DIR, os.path.splitext(filename)[0] + ".json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def hashtag_view(meta):
    """Shape the sidecar's hashtag block for the template: tags, error, and the
    candidate table (selected first, then by score) so the owner can see why."""
    h = (meta or {}).get("hashtags")
    if not h:
        return None
    cands = h.get("candidates") or []
    return {
        "tags": h.get("tags") or [],
        "text": " ".join(h.get("tags") or []),
        "padded": h.get("padded") or [],
        "error": h.get("error"),
        "category": h.get("category") or "",
        "dateline": h.get("dateline") or "",
        "candidates": cands,
        "n_selected": sum(1 for c in cands if c.get("selected")),
    }


def list_outputs(limit=60):
    if not os.path.isdir(OUT_DIR):
        return []
    files = [f for f in os.listdir(OUT_DIR) if safe_name(f)]
    files.sort(key=lambda f: os.path.getmtime(os.path.join(OUT_DIR, f)), reverse=True)
    out = []
    for f in files[:limit]:
        mtime = os.path.getmtime(os.path.join(OUT_DIR, f))
        meta = load_meta(f)
        out.append({"file": f, "when": time.strftime("%d %b %Y, %H:%M", time.localtime(mtime)),
                    "headline": meta.get("headline"), "intro": meta.get("intro"),
                    "url": meta.get("url"), "hashtags": hashtag_view(meta),
                    "instagram": instagram_view(meta, f)})
    return out


PORT = int(os.environ.get("NB_PORT", 5000))


def lan_ip():
    """Best-effort LAN address of this machine, for the startup banner."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":  # print once, not in the reloader child
        print(f"\n  Newsband Post Maker\n  this PC:  http://127.0.0.1:{PORT}\n"
              f"  network:  http://{lan_ip()}:{PORT}   (other PCs on the same Wi-Fi/LAN)\n")
    else:
        # Only in the reloader's child: the parent just watches files, and two autopilot loops
        # would race each other over seen_urls.json and publish the same article twice.
        # local=True: this is a PC, the server does the posting - start in Create only.
        start_background(local=True)
    # host 0.0.0.0 = reachable from other machines on the network (no login - trusted LAN only)
    # use_reloader: picks up edits to make_post.py / scrape_post.py without a manual restart.
    # reloader_type="stat" (mtime polling), NOT the default watchdog: this PC has NTFS
    # last-access-time updates enabled, so merely *reading* a .py file (an import) fires a
    # "modified" event in watchdog's Windows observer. Every lazy import (hashtags.py,
    # torch/spacy on the first /generate) then restarted the server mid-request ->
    # ERR_CONNECTION_RESET in the browser. mtime doesn't change on read, so stat is immune.
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=True, reloader_type="stat")
