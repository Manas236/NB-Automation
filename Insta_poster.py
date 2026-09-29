"""
Instagram publishing for Newsband posts - the only place that talks to the Instagram Graph API.

    dashboard (app.py)  ->  Insta_poster.publish_post(png_path)  ->  Graph API

Meta's image API has no upload endpoint: it only takes an `image_url` and downloads the JPEG
itself. So a local post goes  PNG -> JPEG bytes -> an image host -> that URL is given to
Instagram -> container created -> published -> the hosted copy is deleted again. The dashboard
never has to be reachable from outside; only the image host's URL is.

Credentials come from the environment (or a .env file next to this script, see .env.example):
    INSTAGRAM_ACCOUNT_ID    the Instagram professional account's user id (numeric)
    INSTAGRAM_ACCESS_TOKEN  a long-lived user access token with instagram_business_content_publish
    NB_IMAGE_HOST           cloudinary (default) or imgbb - where the JPEG is hosted
    NB_IMAGE_HOST_FALLBACK  the other host, tried automatically if Meta cannot fetch the first
    CLOUDINARY_CLOUD_NAME   cloudinary account's cloud name
    CLOUDINARY_API_KEY/_API_SECRET   signed upload (lets the copy be deleted after publishing)
    CLOUDINARY_UPLOAD_PRESET         alternative to key/secret: an unsigned preset (no cleanup)
    IMGBB_API_KEY           for NB_IMAGE_HOST=imgbb (publishes fine; on 2026-09-17 Meta's
                            fetcher refused i.ibb.co URLs with code 9004, hence the fallback)
    INSTAGRAM_APP_ID        (informational; not needed for publishing)
    INSTAGRAM_APP_SECRET    (only used for appsecret_proof when INSTAGRAM_API_HOST=graph.facebook.com)
    INSTAGRAM_API_HOST      graph.instagram.com (default, "Instagram API with Instagram Login")
                            or graph.facebook.com (Facebook-Login flow)
    INSTAGRAM_API_VERSION   default v26.0
    NB_API_MIN_GAP_SECONDS  hard floor between two publishes, default 120 (never below 60)

Pacing: every publish here - dashboard queue, autopilot, this CLI - is refused (PacingError)
if the previous Instagram API attempt, per the meta/ sidecars, was under MIN_API_GAP ago.
Two posts seconds apart is what got the account's API access suspended on 2026-09-19.

CLI:
    python Insta_poster.py check                          # verify credentials, print account
    python Insta_poster.py post output/<slug>.png "caption"   # publish a local image (or an https URL)
    python Insta_poster.py upload output/<slug>.png       # just host the image and print the URL
    python Insta_poster.py caption meta/<slug>.json       # show the caption a sidecar produces
Nothing here prints the access token, app secret or any image-host key.
"""

import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import random
import re
import sys
import time
from datetime import datetime

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

try:  # optional: read .env so the dashboard/CLI need no manual `set` of variables
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:  # python-dotenv not installed - plain environment variables still work
    pass

TIMEOUT = 30                 # seconds per HTTP call
UPLOAD_TIMEOUT = 90          # image host upload (a post JPEG is ~200 KB)
IMGBB_URL = "https://api.imgbb.com/1/upload"
CLOUDINARY_URL = "https://api.cloudinary.com/v1_1/{cloud}/image/{action}"
DEFAULT_IMAGE_HOST = "cloudinary"
JPEG_QUALITY = 92
STATUS_POLL_EVERY = 3        # container status: images are usually FINISHED within seconds
STATUS_POLL_MAX = 120        # ...but give up after this many seconds
MAX_CAPTION = 2200           # Instagram's caption limit
# Hard floor between ANY two calls that publish to Instagram - the dashboard button, the
# autopilot, the CLI. Two posts 20-30 s apart is what got the account's API access suspended on
# 2026-09-19; publish_post() refuses (PacingError) rather than send inside this window. The
# autopilot keeps its own, much longer gap on top; the dashboard queue adds API_GAP_JITTER so
# manual posts land 2-3 min apart, never on a fixed beat. NB_API_MIN_GAP_SECONDS overrides,
# but never below 60.
MIN_API_GAP = max(60, int(os.environ.get("NB_API_MIN_GAP_SECONDS") or 120))
API_GAP_JITTER = 60          # random extra seconds the queue waits on top of MIN_API_GAP


class InstagramError(Exception):
    """Anything that stops a post going out. The message is safe to show in the dashboard:
    tokens/secrets are redacted before it is raised."""

    def __init__(self, message, code=None, subcode=None, step=None):
        super().__init__(redact(message))
        self.code, self.subcode, self.step = code, subcode, step


class PacingError(InstagramError):
    """Raised instead of publishing when the last API attempt was under MIN_API_GAP ago.
    Nothing was sent and nothing is written to the sidecar; `wait` says how long to hold."""

    def __init__(self, wait):
        mins, secs = divmod(int(wait + 0.999), 60)
        super().__init__(f"pacing: last Instagram publish was too recent - wait "
                         f"{f'{mins} min {secs} s' if mins else f'{secs} s'}", step="pacing")
        self.wait = wait


class IncompleteError(InstagramError):
    """Raised instead of publishing when the post has no caption to go with it. Nothing is sent
    and nothing is written to the sidecar - the post is not broken, it is unfinished, and
    re-generating it from its article link is what fixes it."""

    def __init__(self, reason):
        super().__init__(f"not ready to post - {reason}", step="incomplete")
        self.reason = reason


def caption_problem(meta):
    """Why this post's caption is not ready, or '' when it is. A caption is the story's first
    line plus its hashtags (build_caption); with either missing the post would go out bare, or
    with the headline standing in - which is not what the owner wants on a live account."""
    meta = meta or {}
    if not (meta.get("intro") or "").strip():
        return "the story's first line is missing"
    tags = meta.get("hashtags") or {}
    if tags.get("error"):
        return f"hashtags failed: {tags['error']}"
    if not (tags.get("tags") or []):
        return "no hashtags were generated"
    return ""


def parse_stamp(text):
    """One of the sidecar's naive local-time stamps, or None."""
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def last_api_attempt():
    """When did anything last talk to the Instagram API - the autopilot, the dashboard button,
    the CLI - successfully or not? Derived from the sidecars rather than held in memory, because
    the Flask reloader restarts the whole process on every code edit and an in-memory timer
    would reset to 'safe to post now' each time. Failed attempts count: they hit Meta's API
    just as hard."""
    from scrape_post import META_DIR
    if not os.path.isdir(META_DIR):
        return None
    latest = None
    for f in os.listdir(META_DIR):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(META_DIR, f), encoding="utf-8") as fh:
                ig = json.load(fh).get("instagram") or {}
        except (OSError, ValueError):
            continue
        for key in ("published_at", "attempted_at"):
            stamp = parse_stamp(ig.get(key))
            if stamp and (latest is None or stamp > latest):
                latest = stamp
    return latest


def api_gap_remaining(min_gap=None, jitter=0):
    """Seconds to wait before the next publish is allowed: MIN_API_GAP (or `min_gap`) plus a
    random 0..`jitter` seconds since the last API attempt. 0 means go ahead now."""
    last = last_api_attempt()
    if last is None:
        return 0.0
    need = (MIN_API_GAP if min_gap is None else min_gap) + random.uniform(0, max(0, jitter))
    return max(0.0, need - (datetime.now() - last).total_seconds())


# ----------------------------------------------------------------- config ----

def config():
    """Credentials + endpoint from the environment. Raises InstagramError if incomplete."""
    account = (os.environ.get("INSTAGRAM_ACCOUNT_ID") or "").strip()
    token = (os.environ.get("INSTAGRAM_ACCESS_TOKEN") or "").strip()
    missing = [n for n, v in (("INSTAGRAM_ACCOUNT_ID", account), ("INSTAGRAM_ACCESS_TOKEN", token)) if not v]
    if missing:
        raise InstagramError("Instagram is not configured: set " + ", ".join(missing)
                             + " in .env (see .env.example)", step="config")
    host = (os.environ.get("INSTAGRAM_API_HOST") or "graph.instagram.com").strip().lower()
    host = re.sub(r"^https?://", "", host).strip("/")
    version = (os.environ.get("INSTAGRAM_API_VERSION") or "v26.0").strip()
    if not re.fullmatch(r"v\d+\.\d+", version):
        raise InstagramError(f"INSTAGRAM_API_VERSION must look like v26.0, not {version!r}", step="config")
    return {
        "account_id": account,
        "token": token,
        "image_host": (os.environ.get("NB_IMAGE_HOST") or DEFAULT_IMAGE_HOST).strip().lower(),
        "fallback_host": (os.environ.get("NB_IMAGE_HOST_FALLBACK") or "").strip().lower(),
        "keep_hosted": _flag("NB_IMAGE_KEEP"),
        "cloud_name": (os.environ.get("CLOUDINARY_CLOUD_NAME") or "").strip(),
        "cloud_key": (os.environ.get("CLOUDINARY_API_KEY") or "").strip(),
        "cloud_secret": (os.environ.get("CLOUDINARY_API_SECRET") or "").strip(),
        "cloud_preset": (os.environ.get("CLOUDINARY_UPLOAD_PRESET") or "").strip(),
        "cloud_folder": (os.environ.get("CLOUDINARY_FOLDER") or "newsband-ig").strip("/ "),
        "imgbb_key": (os.environ.get("IMGBB_API_KEY") or "").strip(),
        "imgbb_expiration": _int("IMGBB_EXPIRATION", 3600),
        "app_id": (os.environ.get("INSTAGRAM_APP_ID") or "").strip(),
        "app_secret": (os.environ.get("INSTAGRAM_APP_SECRET") or "").strip(),
        "base": f"https://{host}/{version}",
        "host": host,
    }


def _flag(name):
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    try:
        return int((os.environ.get(name) or "").strip())
    except ValueError:
        return default


def configuration_error():
    """'' when a dashboard publish can run, otherwise a one-line reason for the UI."""
    try:
        cfg = config()
    except InstagramError as e:
        return str(e)
    if cfg["image_host"] == "cloudinary":
        if not cfg["cloud_name"]:
            return ("Image host not configured: set CLOUDINARY_CLOUD_NAME and "
                    "CLOUDINARY_API_KEY/CLOUDINARY_API_SECRET in .env, then restart")
        if not ((cfg["cloud_key"] and cfg["cloud_secret"]) or cfg["cloud_preset"]):
            return ("Cloudinary needs either CLOUDINARY_API_KEY + CLOUDINARY_API_SECRET or "
                    "CLOUDINARY_UPLOAD_PRESET in .env, then restart")
        return ""
    if cfg["image_host"] == "imgbb":
        return "" if cfg["imgbb_key"] else "IMGBB_API_KEY is not set in .env"
    return f"NB_IMAGE_HOST={cfg['image_host']!r} is not a known image host (cloudinary, imgbb)"


def is_configured():
    """Everything a publish needs: Instagram credentials *and* a usable image host."""
    return not configuration_error()


# --------------------------------------------------------------- safety ----

_SECRET_RE = re.compile(r"(access_token|appsecret_proof|app_secret|client_secret|key)=([^&\s\"']+)", re.I)


def redact(text):
    """Strip tokens/secrets out of any text that might end up in a log, sidecar or the UI."""
    text = str(text)
    for name in ("INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_APP_SECRET", "IMGBB_API_KEY",
                 "CLOUDINARY_API_SECRET", "CLOUDINARY_API_KEY"):
        val = os.environ.get(name)
        if val and len(val) >= 8:
            text = text.replace(val, f"<{name}>")
    text = _SECRET_RE.sub(r"\1=<redacted>", text)
    # long opaque tokens (EAAB..., IGAA...) that slipped in some other way
    text = re.sub(r"\b(EAA|IGQ|IGA)[A-Za-z0-9_-]{40,}", "<token>", text)
    return text


def is_url(s):
    return bool(re.match(r"https?://", str(s), re.I))


def to_jpeg_bytes(path):
    """Instagram accepts JPEG only; the posts are PNG. Convert in memory (nothing is written
    next to the PNG, so output/ retention is untouched)."""
    from PIL import Image
    buf = io.BytesIO()
    with Image.open(path) as im:
        im.convert("RGB").save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def upload_image(path, cfg=None, log=None):
    """Push a local image (as JPEG) to the configured host and return the URL Instagram can
    fetch. See _upload() for the reference used to delete the copy again afterwards."""
    return _upload(path, cfg or config(), log)["url"]


def _upload(path, cfg, log=None, host=None):
    """PNG -> JPEG -> image host. Returns {"url", "ref", "host"}; `ref` is what
    delete_hosted() needs to remove the copy once Instagram has the picture ('' if the host
    cannot delete). Raises InstagramError with step='upload'."""
    if not os.path.isfile(path):
        raise InstagramError(f"image file not found: {path}", step="upload")
    name = os.path.splitext(os.path.basename(path))[0]
    data = to_jpeg_bytes(path)
    host = host or cfg["image_host"]
    if log:
        log(f"Uploading {name}.jpg ({len(data) // 1024} KB) to {host}…")
    if host == "cloudinary":
        out = _upload_cloudinary(data, name, cfg)
    elif host == "imgbb":
        out = _upload_imgbb(data, name, cfg)
    else:
        raise InstagramError(f"NB_IMAGE_HOST={host!r} is not a known image host "
                             "(cloudinary, imgbb)", step="upload")
    if log:
        log("Uploaded.")
    return out


def _upload_cloudinary(data, name, cfg):
    """Upload to Cloudinary. Signed (API key + secret) when available - that is what lets the
    copy be deleted again after publishing; otherwise an unsigned upload preset."""
    if not cfg["cloud_name"]:
        raise InstagramError("CLOUDINARY_CLOUD_NAME is not set in .env - it is needed to host the "
                             "image for Instagram to download (Meta's API has no direct upload)",
                             step="upload")
    # unique per attempt: a re-post of the same slug must not collide with a stale copy
    public_id = f"{name[:80]}-{int(time.time())}"
    params = {"public_id": public_id, "folder": cfg["cloud_folder"]}
    signed = bool(cfg["cloud_key"] and cfg["cloud_secret"])
    if signed:
        params["timestamp"] = str(int(time.time()))
        params["signature"] = _cloudinary_signature(params, cfg["cloud_secret"])
        params["api_key"] = cfg["cloud_key"]
    elif cfg["cloud_preset"]:
        params["upload_preset"] = cfg["cloud_preset"]
    else:
        raise InstagramError("Cloudinary needs either CLOUDINARY_API_KEY + CLOUDINARY_API_SECRET "
                             "or CLOUDINARY_UPLOAD_PRESET in .env", step="upload")
    url = CLOUDINARY_URL.format(cloud=cfg["cloud_name"], action="upload")
    try:
        r = requests.post(url, data=params, files={"file": (f"{name}.jpg", data, "image/jpeg")},
                          timeout=UPLOAD_TIMEOUT)
    except requests.Timeout:
        raise InstagramError(f"upload: cloudinary did not answer within {UPLOAD_TIMEOUT}s", step="upload")
    except requests.RequestException as e:
        raise InstagramError(f"upload: network error - {type(e).__name__}: {e}", step="upload")
    try:
        body = r.json()
    except ValueError:
        raise InstagramError(f"upload: cloudinary HTTP {r.status_code}, non-JSON reply: "
                             f"{r.text[:200]!r}", step="upload")
    if r.status_code >= 400 or not isinstance(body, dict) or not body.get("secure_url"):
        err = (body.get("error") or {}) if isinstance(body, dict) else {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise InstagramError(f"upload: cloudinary refused (HTTP {r.status_code}): "
                             f"{msg or json.dumps(body)[:200]}", step="upload")
    return {"url": body["secure_url"], "ref": body.get("public_id") or "", "host": "cloudinary"}


def _cloudinary_signature(params, secret):
    """Cloudinary's signature: sha1 of the sorted 'k=v&...' of the signed params + the secret."""
    payload = "&".join(f"{k}={params[k]}" for k in sorted(params) if k not in ("file", "api_key"))
    return hashlib.sha1((payload + secret).encode()).hexdigest()


def delete_hosted(ref, cfg=None, log=None, host="cloudinary"):
    """Remove the hosted copy once Instagram has downloaded and published it. Only
    Cloudinary (signed) can delete; imgbb expires by itself. Best effort: a failure here
    never fails a post that already went out. Returns True if deleted."""
    cfg = cfg or config()
    if not ref or host != "cloudinary" or not (cfg["cloud_key"] and cfg["cloud_secret"]):
        return False
    params = {"public_id": ref, "timestamp": str(int(time.time()))}
    params["signature"] = _cloudinary_signature(params, cfg["cloud_secret"])
    params["api_key"] = cfg["cloud_key"]
    try:
        r = requests.post(CLOUDINARY_URL.format(cloud=cfg["cloud_name"], action="destroy"),
                          data=params, timeout=TIMEOUT)
        ok = r.status_code < 400 and (r.json() or {}).get("result") == "ok"
    except (requests.RequestException, ValueError):
        ok = False
    if log:
        log("Hosted copy deleted." if ok else "Hosted copy could not be deleted (harmless).")
    return ok


def _is_fetch_failure(err):
    """Did Instagram fail *because it could not download the image*? Code 9004 /
    subcode 2207052 is 'media could not be fetched from this URI'; 2207052 has also been
    seen with a generic code. Anything else (bad token, aspect ratio, rate limit) must not
    trigger a pointless retry on another host."""
    if err.step != "create container":
        return False
    if err.code == 9004 or err.subcode == 2207052:
        return True
    return "could not be fetched" in str(err).lower() or "unable to fetch" in str(err).lower()


def _upload_imgbb(data, name, cfg):
    """imgbb. The copy auto-expires after IMGBB_EXPIRATION seconds; it only has to outlive the
    container creation. Meta's fetcher has been seen to refuse i.ibb.co URLs (2026-09-17: code
    9004 / subcode 2207052) and to accept them (2026-09-18), hence the two-host arrangement."""
    if not cfg["imgbb_key"]:
        raise InstagramError("IMGBB_API_KEY is not set in .env - it is needed to host the image for "
                             "Instagram to download (Meta's API has no direct upload)", step="upload")
    try:
        r = requests.post(IMGBB_URL, params={"key": cfg["imgbb_key"], "expiration": cfg["imgbb_expiration"]},
                          data={"name": name, "image": base64.b64encode(data).decode()}, timeout=UPLOAD_TIMEOUT)
    except requests.Timeout:
        raise InstagramError(f"upload: imgbb did not answer within {UPLOAD_TIMEOUT}s", step="upload")
    except requests.RequestException as e:
        raise InstagramError(f"upload: network error - {type(e).__name__}: {e}", step="upload")
    try:
        body = r.json()
    except ValueError:
        raise InstagramError(f"upload: imgbb HTTP {r.status_code}, non-JSON reply: {r.text[:200]!r}", step="upload")
    if not isinstance(body, dict) or not body.get("success"):
        err = (body.get("error") if isinstance(body, dict) else None) or {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise InstagramError(f"upload: imgbb refused (HTTP {r.status_code}): {msg or 'no detail'}", step="upload")
    url = ((body.get("data") or {}).get("url")) or ""
    if not is_url(url):
        raise InstagramError(f"upload: imgbb reply had no image url: {json.dumps(body)[:200]}", step="upload")
    return {"url": url, "ref": "", "host": "imgbb"}


# ------------------------------------------------------------ transport ----

def _auth(cfg):
    p = {"access_token": cfg["token"]}
    if cfg["host"] == "graph.facebook.com" and cfg["app_secret"]:
        p["appsecret_proof"] = hmac.new(cfg["app_secret"].encode(), cfg["token"].encode(), hashlib.sha256).hexdigest()
    return p


def _call(cfg, method, path, step, **params):
    """One Graph API request. Returns the parsed JSON object or raises InstagramError with a
    readable, redacted message (network, timeout, HTTP error, non-JSON body, API error body)."""
    url = f"{cfg['base']}/{path.lstrip('/')}"
    data = {**params, **_auth(cfg)}
    try:
        if method == "GET":
            r = requests.get(url, params=data, timeout=TIMEOUT)
        else:
            r = requests.post(url, data=data, timeout=TIMEOUT)
    except requests.Timeout:
        raise InstagramError(f"{step}: Instagram did not answer within {TIMEOUT}s", step=step)
    except requests.RequestException as e:
        raise InstagramError(f"{step}: network error - {type(e).__name__}: {e}", step=step)

    try:
        body = r.json()
    except ValueError:
        snippet = re.sub(r"\s+", " ", r.text)[:200]
        raise InstagramError(f"{step}: HTTP {r.status_code}, non-JSON reply: {snippet!r}", step=step)
    if not isinstance(body, dict):
        raise InstagramError(f"{step}: unexpected reply type {type(body).__name__}", step=step)

    err = body.get("error")
    if err or r.status_code >= 400:
        err = err if isinstance(err, dict) else {}
        msg = err.get("error_user_msg") or err.get("message") or f"HTTP {r.status_code}"
        title = err.get("error_user_title")
        code, sub = err.get("code"), err.get("error_subcode")
        detail = f"{title}: {msg}" if title and title not in msg else msg
        codes = " ".join(f"{k}={v}" for k, v in (("code", code), ("subcode", sub)) if v is not None)
        raise InstagramError(f"{step}: {detail}" + (f" ({codes})" if codes else ""),
                             code=code, subcode=sub, step=step)
    return body


# ---------------------------------------------------------------- API ----

def check_account(cfg=None):
    """Cheap credential test: the account's id + username. Raises InstagramError if the token
    or account id is wrong. Works on both hosts."""
    cfg = cfg or config()
    body = _call(cfg, "GET", cfg["account_id"], "check", fields="id,username")
    return {"id": body.get("id"), "username": body.get("username")}


def create_media(image_url, caption, cfg=None):
    """Step 1: create an image container. Returns the container (creation) id."""
    cfg = cfg or config()
    if not is_url(image_url):
        raise InstagramError(f"create container: image_url must be http(s), got {image_url!r}", step="create container")
    body = _call(cfg, "POST", f"{cfg['account_id']}/media", "create container",
                 image_url=image_url, caption=trim_caption(caption))
    cid = body.get("id")
    if not cid:
        raise InstagramError(f"create container: reply had no id: {json.dumps(body)[:200]}", step="create container")
    return str(cid)


def wait_for_container(container_id, cfg=None, log=None):
    """Poll the container until it is FINISHED. Images normally finish in a few seconds;
    ERROR/EXPIRED raise, and anything still IN_PROGRESS after STATUS_POLL_MAX s raises too."""
    cfg = cfg or config()
    deadline = time.time() + STATUS_POLL_MAX
    while True:
        body = _call(cfg, "GET", container_id, "container status", fields="status_code,status")
        status = body.get("status_code") or ""
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise InstagramError(f"container status: {status} - {body.get('status') or 'no detail'}",
                                 step="container status")
        if time.time() >= deadline:
            raise InstagramError(f"container status: still {status or 'unknown'} after {STATUS_POLL_MAX}s",
                                 step="container status")
        if log:
            log(f"  container {status or 'pending'}, waiting…")
        time.sleep(STATUS_POLL_EVERY)


def publish_media(container_id, cfg=None):
    """Step 2: publish a FINISHED container. Returns the Instagram media id."""
    cfg = cfg or config()
    body = _call(cfg, "POST", f"{cfg['account_id']}/media_publish", "publish", creation_id=container_id)
    mid = body.get("id")
    if not mid:
        raise InstagramError(f"publish: reply had no id: {json.dumps(body)[:200]}", step="publish")
    return str(mid)


def media_permalink(media_id, cfg=None):
    """Public instagram.com link of a published post ('' if the lookup fails - not fatal)."""
    cfg = cfg or config()
    try:
        return _call(cfg, "GET", media_id, "permalink", fields="permalink").get("permalink") or ""
    except InstagramError:
        return ""


def post_to_instagram(image, caption, log=None):
    """The whole flow. `image` is a local file (the post PNG - converted to JPEG and hosted on
    imgbb first) or an https URL Instagram can already fetch. Returns a dict that is safe to
    persist and show:
        {"status": "published", "media_id": ..., "permalink": ..., "container_id": ...,
         "published_at": ISO timestamp, "image_url": ..., "caption": ...}
    Raises InstagramError (redacted message) on any failure; never raises anything else
    for API/network/upload problems."""
    log = log or (lambda *_: None)
    cfg = config()
    if is_url(image):
        image_url, ref, up_host = image, "", ""
        log("Creating media container…")
        cid = create_media(image_url, caption, cfg)
    else:
        up = _upload(image, cfg, log)
        image_url, ref, up_host = up["url"], up["ref"], up["host"]
        log("Creating media container…")
        try:
            cid = create_media(image_url, caption, cfg)
        except InstagramError as e:
            # Meta could not download the picture from this host (it has happened to both
            # hosts on different days). Put the same JPEG on the other one and try once more.
            if not (_is_fetch_failure(e) and cfg["fallback_host"]):
                raise
            log(f"{cfg['image_host']} URL could not be fetched by Instagram "
                f"({e}) - retrying via {cfg['fallback_host']}…")
            delete_hosted(ref, cfg, host=up["host"])
            up = _upload(image, cfg, log, host=cfg["fallback_host"])
            image_url, ref, up_host = up["url"], up["ref"], up["host"]
            cid = create_media(image_url, caption, cfg)
    log(f"Media container created ({cid}).")
    wait_for_container(cid, cfg, log)
    log("Publishing…")
    mid = publish_media(cid, cfg)
    link = media_permalink(mid, cfg)
    if ref and not cfg["keep_hosted"]:
        delete_hosted(ref, cfg, log, host=up_host)     # Instagram has its own copy now
    log("Success.")
    return {"status": "published", "media_id": mid, "permalink": link, "container_id": cid,
            "published_at": datetime.now().isoformat(timespec="seconds"),
            "image_url": image_url, "caption": trim_caption(caption)}


# --------------------------------------------------- publish a gallery post ----

def publish_post(png_path, force=False, log=None):
    """Publish one finished post from output/: caption built from its meta/<stem>.json sidecar,
    the result merged back into that sidecar (success *and* failure) so the dashboard shows the
    same state no matter who published - the owner's button or the autopilot.

    Returns the sidecar's instagram block. Raises InstagramError (already recorded in the
    sidecar) if the post did not go out."""
    from scrape_post import update_meta
    stem = os.path.splitext(os.path.basename(png_path))[0]
    meta = read_sidecar(stem)
    prev = meta.get("instagram") or {}
    if prev.get("status") == "published" and not force:
        raise InstagramError("already published (use Post again to repost)", step="duplicate")
    problem = caption_problem(meta)
    if problem:                   # no caption content: the post is unfinished, don't send it
        raise IncompleteError(problem)
    wait = api_gap_remaining()
    if wait:                      # too soon after the previous publish: hold, don't send
        raise PacingError(wait)
    try:
        result = post_to_instagram(png_path, build_caption(meta, fallback_title=stem.replace("-", " ")), log)
        if prev.get("status") == "published" and prev.get("media_id"):
            result["previous_media_ids"] = (prev.get("previous_media_ids") or []) + [prev["media_id"]]
    except InstagramError as e:
        failed = {"status": "failed", "error": str(e), "step": e.step,
                  "attempted_at": datetime.now().isoformat(timespec="seconds")}
        if prev.get("status") == "published":     # keep the earlier success visible on the card
            failed.update({k: prev[k] for k in ("media_id", "permalink", "published_at") if prev.get(k)})
        update_meta(stem, instagram=failed)
        raise
    return (update_meta(stem, instagram=result) or {}).get("instagram") or result


def read_sidecar(stem):
    """meta/<stem>.json, or {} for a post made straight from make_post.py."""
    from scrape_post import META_DIR
    try:
        with open(os.path.join(META_DIR, stem + ".json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------- caption ----

def trim_caption(caption):
    caption = (caption or "").strip()
    return caption if len(caption) <= MAX_CAPTION else caption[:MAX_CAPTION - 1].rstrip() + "…"


def build_caption(meta, fallback_title=""):
    """Caption from a post's sidecar: the story's first line, then the generated hashtags in
    lowercase. No headline (owner's call - it's already on the image). Only when there is no
    first line does the headline / filename title stand in, so the caption is never empty."""
    from scrape_post import strip_dateline   # 'Kalyan: ...' -> '...' (older sidecars kept it)
    meta = meta or {}
    parts = []
    intro = strip_dateline((meta.get("intro") or meta.get("headline") or fallback_title or "").strip())
    if intro:
        parts.append(intro)
    tags = [t.lower() for t in ((meta.get("hashtags") or {}).get("tags") or []) if t]
    if tags:
        parts.append(" ".join(tags))
    return trim_caption("\n\n".join(parts))


# ----------------------------------------------------------------- CLI ----

def main(argv=None):
    p = argparse.ArgumentParser(description="Publish an image to Instagram (Newsband).")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="verify the credentials and print the account username")
    pp = sub.add_parser("post", help="publish a local image (or an https URL) with a caption")
    pp.add_argument("image", help="output/<slug>.png, any image file, or an https URL")
    pp.add_argument("caption", nargs="?", default="")
    pp.add_argument("--caption-file", help="read the caption from a file (UTF-8)")
    pu = sub.add_parser("upload", help="only upload a local image to imgbb and print the URL")
    pu.add_argument("image")
    pc = sub.add_parser("caption", help="print the caption a meta/<slug>.json sidecar would produce")
    pc.add_argument("meta_json")
    args = p.parse_args(argv)

    try:
        if args.cmd == "check":
            cfg = config()
            print(f"Endpoint: {cfg['base']}  account id: {cfg['account_id']}")
            acct = check_account(cfg)
            print(f"OK - @{acct.get('username') or '?'} (id {acct.get('id')})")
        elif args.cmd == "upload":
            print(upload_image(args.image, log=print))
        elif args.cmd == "caption":
            with open(args.meta_json, encoding="utf-8") as fh:
                print(build_caption(json.load(fh)))
        else:
            caption = args.caption
            if args.caption_file:
                with open(args.caption_file, encoding="utf-8") as fh:
                    caption = fh.read()
            wait = api_gap_remaining()          # same floor as the dashboard and the autopilot
            if wait:
                raise PacingError(wait)
            res = post_to_instagram(args.image, caption, log=print)
            print(f"Instagram Media ID: {res['media_id']}")
            if res["permalink"]:
                print(f"Permalink: {res['permalink']}")
    except InstagramError as e:
        print(f"FAILED - {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
