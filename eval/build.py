"""Build the frozen eval corpus + the labelling worksheet.

    python eval/build.py            # snapshot any sidecar article not yet in eval/corpus/
    python eval/build.py --refresh  # re-scrape even articles already snapshotted

The corpus stores the *inputs* suggest() needs (headline, paragraphs, dateline,
category), not the URL alone: articles fall out of the sitemap and the site edits
copy in place, and ground truth is worthless if the text under it can move.
labels.yaml is only ever appended to - existing blocks and your comments are never
rewritten, so re-running this after new posts land is safe.
"""
import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scrape_post

BASE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(BASE, "corpus")
LABELS = os.path.join(BASE, "labels.yaml")
SPLITS = os.path.join(BASE, "splits.json")
META = os.path.join(os.path.dirname(BASE), "meta")
HOLDOUT_SHARE = 0.3

LABELS_HEADER = """# Hashtag eval - ground truth. You own this file; nothing generates the labels.
#
# For each article set three lists, then flip `labelled` to true:
#   must    tags that HAVE to be suggested. A miss is a failure (recall).
#   accept  fine if suggested, not required. Keeps a good tag from being
#           counted as a mistake just because it wasn't on your must-list.
#   forbid  must NEVER be suggested. Emitting one is a failure - this is the
#           safety line (named accused, wrong civic body, off-topic noise).
#
# Anything the system emits that is on none of the three lists is reported as
# "unjudged" so you can come back and label it. Articles with labelled: false
# are skipped by the scorer entirely.
#
# The candidate menu under each article is what the CURRENT code considered -
# copy tags from it, but do not treat it as a suggestion. The most valuable
# labels are the tags it never proposed at all: write those into `must` by hand.
#
# Worked example (this article is not in the corpus - it is here to show the shape):
#
#   some-slug-from-a-crime-story:
#     headline: "Three held for assaulting shopkeeper in Turbhe"
#     dateline: Navi Mumbai
#     labelled: true
#     must:   ["#NaviMumbai", "#Crime"]
#     accept: ["#Turbhe", "#Police", "#NaviMumbaiPolice"]
#     forbid: ["#AjitSaroj"]        # never name the accused
#
articles:
"""


def slug_of(path):
    return os.path.splitext(os.path.basename(path))[0]


def sidecars():
    if not os.path.isdir(META):
        return []
    return sorted(os.path.join(META, f) for f in os.listdir(META) if f.endswith(".json"))


def snapshot(force=False):
    """Scrape each sidecar's article into corpus/<slug>.json. Returns (new, kept, failed)."""
    os.makedirs(CORPUS, exist_ok=True)
    new, kept, failed = [], [], []
    for path in sidecars():
        slug = slug_of(path)
        out = os.path.join(CORPUS, slug + ".json")
        if os.path.exists(out) and not force:
            kept.append(slug)
            continue
        meta = json.load(open(path, encoding="utf-8"))
        url = meta.get("url")
        if not url:
            failed.append((slug, "sidecar has no url"))
            continue
        try:
            art = scrape_post.scrape_article_full(url)
        except Exception as e:                      # dead slug, redirect, network
            failed.append((slug, f"{type(e).__name__}: {e}"))
            continue
        json.dump({"slug": slug, "url": url,
                   "headline": art["headline"], "paragraphs": art["paragraphs"],
                   "dateline": art["dateline"], "category": art["category"],
                   "intro": art["intro"],
                   "snapshot_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                  open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        new.append(slug)
        time.sleep(1)                               # be polite to their origin
    return new, kept, failed


def menu(slug):
    """The current code's candidate table for this article, as YAML comment lines."""
    path = os.path.join(META, slug + ".json")
    if not os.path.exists(path):
        return []
    cands = (json.load(open(path, encoding="utf-8")).get("hashtags") or {}).get("candidates") or []
    lines = []
    for c in sorted(cands, key=lambda c: -c.get("score", 0))[:18]:
        state = f"picked #{c['pick']}" if c.get("selected") else (c.get("dropped") or "not selected")
        lines.append("    #   {:<26} {:>5}  {:<9} {}".format(
            c.get("tag", ""), f"{c.get('score', 0):.1f}", c.get("source", ""), state[:60]))
    return lines


def append_labels():
    """Append a block per un-listed article. Never rewrites what is already there."""
    existing = open(LABELS, encoding="utf-8").read() if os.path.exists(LABELS) else LABELS_HEADER
    added = []
    for f in sorted(os.listdir(CORPUS)):
        if not f.endswith(".json"):
            continue
        art = json.load(open(os.path.join(CORPUS, f), encoding="utf-8"))
        slug = art["slug"]
        if f"\n  {slug}:\n" in existing:
            continue
        block = [f"\n  {slug}:",
                 "    headline: {}".format(json.dumps(art["headline"], ensure_ascii=False)),
                 "    dateline: {}".format(json.dumps(art["dateline"] or "", ensure_ascii=False)),
                 "    category: {}".format(json.dumps(art["category"] or "", ensure_ascii=False)),
                 "    labelled: false",
                 "    must:   []",
                 "    accept: []",
                 "    forbid: []",
                 "    # candidates the current code considered:"]
        block += menu(slug) or ["    #   (no sidecar candidate table)"]
        existing += "\n".join(block) + "\n"
        added.append(slug)
    open(LABELS, "w", encoding="utf-8").write(existing)
    return added


def write_splits():
    """Deterministic dev/holdout by slug hash, so the split never moves between runs
    and adding articles never reshuffles the ones already there."""
    splits = json.load(open(SPLITS, encoding="utf-8")) if os.path.exists(SPLITS) else {}
    for f in sorted(os.listdir(CORPUS)):
        if not f.endswith(".json"):
            continue
        slug = f[:-5]
        if slug in splits:
            continue
        h = int(hashlib.sha1(slug.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        splits[slug] = "holdout" if h < HOLDOUT_SHARE else "dev"
    json.dump(splits, open(SPLITS, "w", encoding="utf-8"), indent=1, sort_keys=True)
    return splits


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-scrape articles already snapshotted")
    args = ap.parse_args()

    new, kept, failed = snapshot(args.refresh)
    print(f"corpus: {len(new)} new, {len(kept)} already snapshotted, {len(failed)} failed")
    for slug, why in failed:
        print(f"  ! {slug}: {why}")
    added = append_labels()
    print(f"labels.yaml: {len(added)} article(s) appended, awaiting labels")
    splits = write_splits()
    print("splits: {} dev / {} holdout".format(
        sum(v == "dev" for v in splits.values()), sum(v == "holdout" for v in splits.values())))
