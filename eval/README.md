# Hashtag eval

Measures `hashtags.suggest()` against your judgment, so a change to `WEIGHTS`,
`FLOOR` or the hard filters can be argued with numbers instead of by looking at
one article. Nothing in here is imported by the app - it never affects a post.

## Files

| Path | What it is |
|---|---|
| `corpus/<slug>.json` | Frozen article inputs (headline, paragraphs, dateline, category) scraped once from the sidecar's URL. Frozen because articles drop out of the sitemap and the site edits copy in place - ground truth is worthless if the text under it can move. |
| `labels.yaml` | **Ground truth. You own it.** `must` / `accept` / `forbid` per article + `labelled: true` when reviewed. Build only ever *appends* to this file. |
| `splits.json` | Deterministic dev (18) / holdout (9) by slug hash. Stable across runs; new articles never reshuffle the old ones. |
| `baseline.json` | The tags the current code emits. The thing a change is diffed against. |
| `build.py` | Scrapes new sidecar articles into `corpus/`, appends their blocks to `labels.yaml`, extends `splits.json`. |
| `run.py` | Runs the suggester over a split, scores it against the labels, diffs against the baseline. |

## Use

```
python eval/build.py                  # pull in articles posted since last time
python eval/run.py                    # dev split: metrics + what changed vs baseline
python eval/run.py --show <slug>      # full candidate table for one article
python eval/run.py --save-baseline    # freeze current behaviour as the new reference
python eval/run.py --split holdout    # only after you have stopped tuning
```

## Labelling

Three lists per article, then `labelled: true`:

- **must** - has to be suggested; a miss is a failure. The valuable ones are tags the
  code never proposed: those are the real misses and no menu will show them to you.
- **accept** - fine if suggested, not required. Stops a good tag being scored as a
  mistake just because it wasn't on the must-list.
- **forbid** - must never be suggested. Named accused, wrong civic body, off-topic noise.

Anything emitted that is on none of the three is reported as `unjudged`, so the file
fills in as you go. Unlabelled articles are excluded from the metrics but still take
part in the baseline diff, so the regression guard works from day one.

## Metrics

- **must-recall** - `must` tags actually emitted. The "is it finding things" number.
- **forbidden emitted** - count of `forbid` tags emitted. Should be 0; this is the
  safety line, not a tuning knob.
- **precision (judged)** - emitted tags that are must-or-accept, over emitted tags you
  have judged either way.
- **clean articles** - no miss, no violation. The number that matters end to end.
- **tags per article** - guards against a change that just emits more of everything.

## Honest limits

27 articles is enough to catch a gross regression and to decide a yes/no question
(*should an unknown ORG in the lede be allowed through?*). It is **not** enough to fit
13 weights - with ~5 tags each that is ~135 decisions, and 9 of the articles are held
out. Treat it as a regression guard and a decision procedure first. The corpus grows
itself: re-run `build.py` and every post the autopilot has made since is pulled in.
