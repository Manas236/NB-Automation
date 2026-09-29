"""Score the hashtag suggester against eval/labels.yaml.

    python eval/run.py                    # dev split: metrics + diff vs the saved baseline
    python eval/run.py --split holdout    # the frozen holdout - only after you have stopped tuning
    python eval/run.py --split all
    python eval/run.py --save-baseline    # freeze current output as the thing to diff against
    python eval/run.py --show <slug>      # full candidate table for one article

Unlabelled articles are skipped by the metrics but still take part in the baseline
diff, so the regression guard works before any labelling is done.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

import hashtags

BASE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(BASE, "corpus")
LABELS = os.path.join(BASE, "labels.yaml")
SPLITS = os.path.join(BASE, "splits.json")
BASELINE = os.path.join(BASE, "baseline.json")


def norm(tag):
    """#NaviMumbai, navimumbai and #navi_mumbai are the same label."""
    return tag.strip().lstrip("#").replace("_", "").replace(" ", "").lower()


def load():
    corpus = {}
    for f in sorted(os.listdir(CORPUS)):
        if f.endswith(".json"):
            art = json.load(open(os.path.join(CORPUS, f), encoding="utf-8"))
            corpus[art["slug"]] = art
    labels = (yaml.safe_load(open(LABELS, encoding="utf-8")) or {}).get("articles") or {}
    splits = json.load(open(SPLITS, encoding="utf-8")) if os.path.exists(SPLITS) else {}
    return corpus, labels, splits


def run_one(art):
    a = hashtags.Article(headline=art["headline"], paragraphs=art["paragraphs"],
                         dateline=art.get("dateline", ""), category=art.get("category", ""),
                         url=art.get("url", ""))
    return hashtags.suggest(a)


def score(slugs, corpus, labels, results):
    """Per-article outcome + the aggregate. Only labelled articles count."""
    rows, agg = [], {"must": 0, "must_hit": 0, "forbid_hits": 0, "judged": 0, "good": 0,
                     "unjudged": 0, "emitted": 0, "clean": 0, "labelled": 0}
    for slug in slugs:
        tags = [c.tag for c in results[slug].candidates if c.selected]
        emitted = {norm(t) for t in tags}
        agg["emitted"] += len(emitted)
        lab = labels.get(slug) or {}
        if not lab.get("labelled"):
            rows.append((slug, tags, None, None, None))
            continue
        agg["labelled"] += 1
        must = {norm(t) for t in lab.get("must") or []}
        accept = {norm(t) for t in lab.get("accept") or []}
        forbid = {norm(t) for t in lab.get("forbid") or []}
        missed = must - emitted
        violated = emitted & forbid
        unjudged = emitted - must - accept - forbid
        agg["must"] += len(must)
        agg["must_hit"] += len(must & emitted)
        agg["forbid_hits"] += len(violated)
        agg["judged"] += len(emitted & (must | accept | forbid))
        agg["good"] += len(emitted & (must | accept))
        agg["unjudged"] += len(unjudged)
        agg["clean"] += not missed and not violated
        rows.append((slug, tags, sorted(missed), sorted(violated), sorted(unjudged)))
    return rows, agg


def pct(n, d):
    return "n/a" if not d else f"{100.0 * n / d:5.1f}%  ({n}/{d})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=["dev", "holdout", "all"])
    ap.add_argument("--save-baseline", action="store_true")
    ap.add_argument("--show", metavar="SLUG", help="print the full candidate table for one article")
    args = ap.parse_args()

    corpus, labels, splits = load()

    if args.show:
        hashtags.print_result(run_one(corpus[args.show]))
        return

    slugs = [s for s in corpus if args.split == "all" or splits.get(s) == args.split]
    slugs.sort()
    print(f"running {len(slugs)} article(s) [{args.split}] ...", flush=True)
    results = {s: run_one(corpus[s]) for s in slugs}

    if args.save_baseline:
        base = json.load(open(BASELINE, encoding="utf-8")) if os.path.exists(BASELINE) else {}
        base.update({s: [c.tag for c in r.candidates if c.selected] for s, r in results.items()})
        json.dump(base, open(BASELINE, "w", encoding="utf-8"), ensure_ascii=False, indent=1, sort_keys=True)
        print(f"baseline saved for {len(results)} article(s) -> {os.path.relpath(BASELINE)}")
        return

    rows, agg = score(slugs, corpus, labels, results)

    print("\n--- per article " + "-" * 60)
    for slug, tags, missed, violated, unjudged in rows:
        print(f"\n{slug[:64]}")
        print("   tags    : " + (" ".join(tags) or "(none)"))
        if missed is None:
            print("   (not labelled yet - excluded from the metrics)")
            continue
        if missed:
            print("   MISSED  : " + " ".join(missed))
        if violated:
            print("   FORBID  : " + " ".join(violated))
        if unjudged:
            print("   unjudged: " + " ".join(unjudged))

    print("\n--- summary " + "-" * 64)
    n = len(slugs)
    print(f"articles          : {n} in split, {agg['labelled']} labelled")
    if agg["labelled"]:
        print(f"must-recall       : {pct(agg['must_hit'], agg['must'])}")
        print(f"forbidden emitted : {agg['forbid_hits']}")
        print(f"precision (judged): {pct(agg['good'], agg['judged'])}")
        print(f"clean articles    : {pct(agg['clean'], agg['labelled'])}")
        print(f"unjudged tags     : {agg['unjudged']}  <- label these to make the number mean more")
    print(f"tags per article  : {agg['emitted'] / n:.2f}" if n else "")

    # regression guard: works with or without labels
    if os.path.exists(BASELINE):
        base = json.load(open(BASELINE, encoding="utf-8"))
        changed = []
        for slug in slugs:
            was = {norm(t) for t in base.get(slug, [])}
            now = {norm(t) for t in (c.tag for c in results[slug].candidates if c.selected)}
            if slug in base and was != now:
                changed.append((slug, sorted(now - was), sorted(was - now)))
        print(f"\n--- vs baseline " + "-" * 60)
        print(f"{len(changed)} of {sum(s in base for s in slugs)} article(s) changed")
        for slug, added, removed in changed:
            print(f"  {slug[:56]}")
            if added:
                print("    + " + " ".join(added))
            if removed:
                print("    - " + " ".join(removed))
    else:
        print("\n(no baseline yet - run with --save-baseline to freeze current behaviour)")


if __name__ == "__main__":
    main()
