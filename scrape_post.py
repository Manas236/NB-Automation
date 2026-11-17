"""
Scrape a newsband.in article and turn it into an Instagram post.

Usage:
    python scrape_post.py <article-url> [<article-url> ...]
    python scrape_post.py --file urls.txt          # one URL per line

For each article it grabs the <h2> headline, the main image
(preferring the blog_feature_img/..._2 crop, falling back to blog_main_img/..._1)
and the opening sentence of the body (shown in the dashboard, not on the post),
downloads the photo to downloads/, then hands headline + photo to make_post.py.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from make_post import OUT_DIR, make_post, prune_dir, slugify

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DL_DIR = os.path.join(BASE_DIR, "downloads")
META_DIR = os.path.join(BASE_DIR, "meta")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NewsbandPostBot/1.0"}
TIMEOUT = 30


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r


# don't treat these as sentence ends when cutting the intro to its first sentence
ABBREVS = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "no", "rs", "vs", "adv", "lt", "col", "gen", "smt", "shri"}


def first_sentence(text):
    """Cut a paragraph down to its first sentence (skips abbreviations like Dr. / Rs.)."""
    for m in re.finditer(r"[.!?]+[\"”’']?(?=\s)", text):
        word = text[:m.start()].rsplit(None, 1)[-1].lstrip("(\"“")
        if word.lower() in ABBREVS or (len(word) == 1 and word.isupper()) or word.isdigit():
            continue
        return text[:m.end()].strip()
    return text.strip()


def same_text(a, b):
    """True when two strings match ignoring case, punctuation and spacing (curly vs straight
    quotes, a trailing full stop...)."""
    norm = lambda s: re.sub(r"[\W_]+", "", (s or "").lower())
    return bool(norm(a)) and norm(a) == norm(b)


BYLINE_RE = re.compile(r"^(?:By|Reported by|Written by)\s+[A-Z][\w.\-]*(?:\s+[A-Z][\w.\-]*){0,3}$")


def article_intro(paragraphs, headline=""):
    """First line of the story: the opening sentence of its first real paragraph. Takes the
    paragraphs article_body() already extracted (dateline stripped) rather than re-reading the
    page - walking <p> missed every story whose body is bare <div>s. A sentence that just
    repeats the headline is skipped in favour of the next one. '' if there is no body."""
    for text in paragraphs or []:
        if len(text) > 20:
            sentence = first_sentence(strip_dateline(text))
            if same_text(sentence, headline):
                rest = strip_dateline(text)[len(sentence):].strip()   # headline + story in one paragraph
                if len(rest) <= 20:
                    continue
                sentence = first_sentence(rest)
            return sentence
    return ""


DATELINE_RE = re.compile(r"^([A-Z][A-Za-z .()\-]{1,30}?)\s*:\s*")   # 'Samarkand (Uzbekistan):' too


def strip_dateline(text):
    """'Kalyan: Jagruti Mitra Mandal ...' -> 'Jagruti Mitra Mandal ...'. Catches datelines the
    site didn't put in <strong>. Only a short place-like prefix at the very start is dropped
    (capitalised, letters only, <=3 words), so colons later in the sentence and times like
    '10:30' are left alone."""
    m = DATELINE_RE.match(text or "")
    if m and len(m.group(1).split()) <= 3:
        return text[m.end():]
    return text or ""


def article_body(soup, headline=""):
    """Full story text for the hashtag system: (dateline, paragraphs, category).

    Text lives in `.maan-card-body .maan-text` as a mix of <p> and bare <div>s (some
    stories have no <p> at all), preceded by an author/date `.row`, so we drop that row
    and split the container's text on newlines instead of trusting <p>. The dateline
    ('Dombivli:' / 'NAVI MUMBAI:') is either its own line or the first paragraph's
    prefix; it is stripped from the text and returned separately. Category comes from
    `.news_category` and is '' when the site left it blank. Some stories open the body with
    the headline again and/or a byline ('By Vikram Gaikwad'); those leading lines are dropped
    first, otherwise the headline became the intro and the dateline under it was missed."""
    box = soup.select_one(".maan-card-body .maan-text") or soup.select_one(".maan-card-body")
    if box is None:
        return "", [], ""
    box = BeautifulSoup(str(box), "lxml")   # work on a copy, the caller still needs the soup
    for junk in box.select(".row, .news_category, script, style"):
        junk.decompose()
    # A paragraph ends at a <br> or a block tag; everything else is inline and has to stay on
    # the same line. Splitting on every text node instead chopped sentences wherever the story
    # had an inline tag ("...unique theme at Sector" | "4, Sanpada").
    for br in box.find_all("br"):
        br.replace_with("\n")
    for blk in box.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "h5", "blockquote", "tr"]):
        blk.append("\n")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in box.get_text(" ").split("\n")]
    paragraphs = [ln for ln in lines if len(ln.split()) >= 1]
    while paragraphs and (same_text(paragraphs[0], headline) or BYLINE_RE.match(paragraphs[0])):
        paragraphs = paragraphs[1:]

    dateline = ""
    if paragraphs:
        first = paragraphs[0]
        if re.fullmatch(r"[A-Z][A-Za-z .()\-]{1,30}:", first):    # dateline on its own line
            dateline, paragraphs = first[:-1].strip(), paragraphs[1:]
        else:
            m = DATELINE_RE.match(first)
            if m and len(m.group(1).split()) <= 3:
                dateline, paragraphs[0] = m.group(1).strip(), first[m.end():]
    if dateline.isupper():
        dateline = " ".join(w.capitalize() for w in dateline.split())   # NAVI MUMBAI -> Navi Mumbai
    paragraphs = [p for p in paragraphs if len(p.split()) >= 3]

    cat = soup.select_one(".news_category")
    category = cat.get_text(" ", strip=True) if cat else ""
    return dateline, paragraphs, category


SITE_SUFFIX_RE = re.compile(r"\s*[|\-–—:]\s*News\s*band(?:\.in)?\s*$", re.I)


def strip_site_suffix(title):
    """'Alcaraz battles ... | Newsband' -> 'Alcaraz battles ...'."""
    return SITE_SUFFIX_RE.sub("", (title or "").strip()).strip()


def jsonld_headline(soup):
    """headline from the page's NewsArticle JSON-LD block, '' if absent/unparseable."""
    import json
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or "")
        except ValueError:
            continue
        for d in (data if isinstance(data, list) else [data]):
            if isinstance(d, dict) and d.get("headline"):
                return str(d["headline"]).strip()
    return ""


def scrape_article(url):
    """Return (headline, image_url, intro) for a newsband.in article page."""
    a = scrape_article_full(url)
    return a["headline"], a["image_url"], a["intro"]


def scrape_article_full(url):
    """Everything the pipeline needs from an article page, as a dict:
    headline, image_url, intro, dateline, paragraphs, category."""
    r = fetch(url)
    if "article_detail" not in r.url:
        # unknown slugs silently redirect to the homepage
        raise ValueError(f"not an article page (redirected to {r.url})")
    soup = BeautifulSoup(r.text, "lxml")

    # main photo: the blog_main_img <img> in .maan-post-img; og:image as backup
    img_box = soup.select_one(".maan-post-img")
    img = img_box.find("img") if img_box else soup.find("img", src=re.compile("blog_main_img"))
    image_url = img.get("src") if img else None
    if not image_url:
        og = soup.find("meta", property="og:image")
        image_url = og["content"] if og and og.get("content") else None
    if not image_url:
        raise ValueError("could not find the main image")

    # The headline sits just above the photo: <h1 class="nb-article-h1"> since 14 Nov 2026,
    # a plain <h2> before that. Backups: JSON-LD headline, then og:title - but <title>/og:title
    # carry a " | Newsband" site suffix, which ended up printed on posts when the site moved
    # from <h2> to <h1> and only og:title matched. strip_site_suffix() guards every source.
    tag = (soup.select_one(".maan-title-text h1, .maan-title-text h2")
           or (img.find_previous(["h1", "h2"]) if img else None))
    headline = tag.get_text(" ", strip=True) if tag else ""
    if not headline:
        headline = jsonld_headline(soup)
    if not headline:
        og = soup.find("meta", property="og:title")
        headline = og["content"].strip() if og and og.get("content") else ""
    headline = strip_site_suffix(headline)
    if not headline or headline.lower() == "newsband":
        raise ValueError("could not find the headline")

    dateline, paragraphs, category = article_body(soup, headline)
    return {"headline": headline, "image_url": urljoin(url, image_url),
            "intro": article_intro(paragraphs, headline),
            "dateline": dateline, "paragraphs": paragraphs, "category": category}


def variant_url(image_url, n):
    """The alternate image lives in a sibling folder with a different suffix:
        uploads/blog_main_img/<hash>_1.jpg  ->  uploads/blog_feature_img/<hash>_2.jpg
    Zero-padding is preserved (_01 -> _02)."""
    def bump(m):
        return "_" + str(n).zfill(len(m.group(1))) + m.group(2)
    url = re.sub(r"_(\d+)(\.[A-Za-z0-9]+)$", bump, image_url)
    if n == 2:
        url = url.replace("/blog_main_img/", "/blog_feature_img/")
    return url


def download_image(image_url, name):
    """Try the blog_feature_img/_2 version first, fall back to the original. Returns local path."""
    os.makedirs(DL_DIR, exist_ok=True)
    candidates = [variant_url(image_url, 2), image_url]
    last_err = None
    for cand in candidates:
        try:
            r = fetch(cand)
        except requests.HTTPError as e:
            last_err = e
            continue
        ext = os.path.splitext(cand.split("?")[0])[1] or ".jpg"
        path = os.path.join(DL_DIR, name + ext)
        with open(path, "wb") as fh:
            fh.write(r.content)
        prune_dir(DL_DIR)
        return path, cand
    raise RuntimeError(f"image download failed: {last_err}")


def save_meta(post_path, url, headline, intro, image_url, hashtags=None, extra=None):
    """Sidecar meta/<png stem>.json so the headline, intro and hashtags survive past the
    render (the PNG itself carries none of them, and the poller has no UI to show them in).
    Lives outside output/ because prune_dir() counts every file in a folder.
    `hashtags` is hashtags.Result.to_dict() (tags + full candidate table) or an error dict."""
    if os.path.dirname(os.path.abspath(post_path)) != OUT_DIR:
        return  # custom -o path: not in the gallery, nothing to show it in
    os.makedirs(META_DIR, exist_ok=True)
    stem = os.path.splitext(os.path.basename(post_path))[0]
    meta = {"headline": headline, "intro": intro, "url": url,
            "image_url": image_url, "created": datetime.now().isoformat(timespec="seconds")}
    meta.update(extra or {})
    # A re-render of the same story must not lose what happened to the post afterwards: the
    # Instagram block is the record of whether this slug is already live, and wiping it would
    # let the autopilot publish the same story twice.
    try:
        with open(os.path.join(META_DIR, stem + ".json"), encoding="utf-8") as fh:
            old = json.load(fh)
        if isinstance(old, dict) and old.get("instagram"):
            meta["instagram"] = old["instagram"]
    except (OSError, ValueError):
        pass
    if hashtags is not None:
        meta["hashtags"] = hashtags
    with open(os.path.join(META_DIR, stem + ".json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    # drop meta whose post has since been pruned away
    for f in os.listdir(META_DIR):
        if f.endswith(".json") and not os.path.exists(os.path.join(OUT_DIR, f[:-5] + ".png")):
            try:
                os.remove(os.path.join(META_DIR, f))
            except OSError:
                pass


def update_meta(stem, **fields):
    """Merge `fields` into meta/<stem>.json (created minimal if missing - e.g. a post made
    straight from make_post.py). Used for state added after the render, such as the
    Instagram publish result. Returns the merged dict."""
    os.makedirs(META_DIR, exist_ok=True)
    path = os.path.join(META_DIR, stem + ".json")
    try:
        with open(path, encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        meta = {}
    meta.update(fields)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    return meta


def suggest_hashtags(url, a):
    """Run hashtags.suggest() on a scraped article dict; never lets a hashtag failure
    break post generation - returns an {'error': ...} dict instead."""
    try:
        from hashtags import Article, suggest
        art = Article(headline=a["headline"], paragraphs=a["paragraphs"], dateline=a["dateline"],
                      category=a["category"], url=url)
        return suggest(art).to_dict()
    except Exception as e:  # noqa: BLE001 - the post must still be produced
        return {"error": f"{type(e).__name__}: {e}", "tags": []}


def process(url, focus="center", out=None):
    """Returns (headline, image_url_used, png_path, intro). The intro is the story's
    first sentence - for the caption/dashboard only, it is not drawn on the post.
    Hashtags are generated too and stored in the meta sidecar (see save_meta)."""
    a = scrape_article_full(url)
    headline, image_url, intro = a["headline"], a["image_url"], a["intro"]
    slug = slugify(headline)
    image_path, used_url = download_image(image_url, slug)
    post_path = make_post(image_path, headline, out, focus)
    hashtags = suggest_hashtags(url, a)
    save_meta(post_path, url, headline, intro, used_url, hashtags,
              extra={"dateline": a["dateline"], "category": a["category"]})
    return headline, used_url, post_path, intro


def main():
    p = argparse.ArgumentParser(description="Scrape newsband.in article(s) into Instagram posts.")
    p.add_argument("urls", nargs="*", help="article URL(s)")
    p.add_argument("--file", "-f", help="text file with one article URL per line")
    p.add_argument("--focus", choices=["top", "center", "bottom"], default="center",
                   help="which part of the photo to keep when cropping")
    p.add_argument("--out", "-o", help="output file (single URL only)")
    args = p.parse_args()

    urls = list(args.urls)
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            urls += [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    if not urls:
        p.error("give at least one article URL, or --file urls.txt")
    if args.out and len(urls) > 1:
        p.error("--out only works with a single URL")

    failed = 0
    for url in urls:
        try:
            headline, used_url, post_path, intro = process(url, args.focus, args.out)
            print(f"OK   {headline}\n     intro: {intro}\n     image: {used_url}\n     post:  {post_path}")
        except Exception as e:
            failed += 1
            print(f"FAIL {url}\n     {e}", file=sys.stderr)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
