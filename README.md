# Newsband post generator

Makes the standard 1080x1440 red-template Instagram post from a newsband.in article.

Setup (once): `pip install -r requirements.txt`

## Dashboard (easiest)
    python app.py          (or double-click start_dashboard.bat)

It prints two addresses: `http://127.0.0.1:5000` for this PC and `http://<LAN-IP>:5000`
(e.g. http://192.168.1.45:5000) for any other PC/phone on the same network.
Paste one or more article links, click **Generate post**, then **Download**. Everything made so
far is listed underneath with download/delete buttons. Finished PNGs also live in `output\`.

First time only: run `allow_firewall.bat` as Administrator so Windows Firewall lets other
machines reach port 5000. The dashboard has no login - keep it on a trusted network.

## Command line: from an article URL
    python scrape_post.py https://www.newsband.in/article_detail/<slug>

Several at once: put one URL per line in `urls.txt` and run

    python scrape_post.py -f urls.txt

It pulls the headline (`<h2>`) and the main image from the page, tries the `_2` version of the
image first and falls back to `_1`, saves the photo in `downloads\`, and renders the post to `output\`.
`--focus top|bottom` works here too.

## Hashtags
Every post made from an article link also gets up to 5 suggested hashtags. They appear on the
dashboard card under **Generated hashtags** with a **Copy** button; **Why?** opens a table of
every candidate the system considered, with its score, where the evidence came from and why it
was picked or dropped. They are also stored in `meta\<slug>.json`.

    python hashtags.py https://www.newsband.in/article_detail/<slug>     # print the candidate table
    python hashtags.py --backfill                                        # add hashtags to older posts

Rules of thumb: a tag needs actual evidence in the article (a name, an alias, or "the civic body"
resolved through the dateline). Nothing is inferred from a related thing — "Thane" alone never
produces #TMC. Weak stories get fewer than 5 tags rather than made-up ones. People not in the
catalogue are only tagged from the headline, and never in crime stories.

Editing the tags: everything editorial is in `tags.yaml` — the hashtag, its spellings/aliases,
the trigger phrases for topic tags, and which more-general tag it backs off to. Add a new
politician, place or topic there; no code change needed. First run needs
`python -m spacy download en_core_web_sm`; the embedding model downloads itself once.

## Posting to Instagram
Every card on the dashboard has an **Instagram** section with a **Post to Instagram** button. It
publishes the card's image with a caption made of the first line and the generated hashtags
(in lowercase), then shows **Published** with the Instagram media ID (stored in `meta\<slug>.json`, so a
post already published shows **Post again** instead - you are asked to confirm). Failures show the
reason and a **Retry** button; the post itself is never affected.

Setup: copy `.env.example` to `.env` and fill in `INSTAGRAM_ACCOUNT_ID`, `INSTAGRAM_ACCESS_TOKEN`
(plus app id/secret) and the Cloudinary credentials, then restart the dashboard. Instagram's API
cannot take an upload - it downloads the picture from a URL - so the poster puts a JPEG copy on
Cloudinary, gives Instagram that link, and deletes the copy again the moment the post is live.
imgbb is kept as an automatic fallback: if Instagram cannot fetch the Cloudinary URL the same
JPEG is re-uploaded there and the post is retried once. Nothing on this PC is exposed.

    python Insta_poster.py check                                  # are the credentials OK?
    python check_instagram_token.py                               # expiry / time remaining when Meta provides it (does not refresh)
    python Insta_poster.py caption meta\<slug>.json               # preview a caption
    python Insta_poster.py upload output\<slug>.png               # only host the image, print the URL
    python Insta_poster.py post output\<slug>.png "caption"       # publish by hand

## Automatic posting (autopilot)
Start the dashboard and leave it open - that is the whole pipeline. Every 15 minutes it checks
newsband.in's news sitemap, and for each article published **today** that it has not seen before it
scrapes the story, renders the post, generates the hashtags and publishes it to Instagram by itself.
The strip at the top of the page shows whether it is on, when it last looked, what it is doing now,
how many posts it has made and published today, and a **Log** button with the running commentary;
the page refreshes itself when a new post appears. **Pause** stops it publishing without closing
anything - already-running work finishes, nothing new starts.

The strip also has a two-way mode switch that decides what happens to each new article:

- **Create & post** - the post is rendered and published to Instagram by itself (one post per
  pacing slot, see below).
- **Create only** - the post is rendered and lands in the gallery with its **Post to Instagram**
  button; nothing goes out until you press it.

The choice takes effect immediately, is remembered across restarts (`autopilot_mode.json`), and
posts made in *Create only* mode stay yours: switching back to *Create & post* later never sends
them out behind your back.

Safety rails, since this posts to a live account unattended: only today's articles are eligible,
at most 10 articles are rendered per check, 10 posts are published per day, and posts go out
one at a time through the same queue as the Post button, 2–3 minutes apart; a story that is already
published (by the autopilot or by your own button) is never posted twice, and an article that
fails is recorded as handled so it cannot loop. Everything it makes appears in the gallery like
any other post, with the same **Published / Failed / Retry** state on the card.

If the dashboard is restarted in the middle of a batch (any code edit does that), the post it
was working on is finished off the next time it starts - posts it made today but never published
are published then. Posts you made yourself from the form are never published automatically.

Turn any of it off in `.env` (`NB_AUTOPILOT=0` starts paused, `NB_AUTOPOST=0` starts in *Create
only* mode until a mode is picked on the dashboard, `NB_POLL_INTERVAL`, `NB_MAX_PER_DAY`, ...),
then restart the dashboard.

Running the poller on its own instead of inside the dashboard:

    python poll_new_articles.py                # loop: detect -> scrape -> render -> publish
    python poll_new_articles.py --once         # a single pass
    python poll_new_articles.py --once --dry-run   # show what it would do, change nothing
    python poll_new_articles.py --no-post      # generate posts, publish nothing

`seen_urls.json` is the list of articles it has already handled; delete it to start over (it then
re-seeds from the current sitemap so it never backfills old stories). `autopost_state.json` holds
today's counters for the daily cap.

## Command line: from a local photo + headline
    python make_post.py -i path\to\photo.jpg -t "Your headline here"

Output: `output\<headline-slug>.png`. Add `-o file.png` to choose the name.

## Many posts at once
Make a CSV with columns `image,headline,out,focus` (only the first two are required) and run:

    python make_post.py -b posts.csv

## Options
- `--focus top|center|bottom` — which part of the photo to keep when it is cropped to fit (default center).

## Assets
`assets/BG.jpg` is the red background, `assets/logo.png` the logo inside the white pill, and (optional) `assets/globe.png` the footer icon - if it is missing a globe is drawn instead. Swap any file to rebrand.

## Tweaking the layout
All positions, sizes and colours are constants at the top of `make_post.py`.

## Running on the website server

To run the dashboard and autopilot on the newsband.in server at `automation.newsband.in`, with a login, see [deploy/DEPLOY.md](deploy/DEPLOY.md).
