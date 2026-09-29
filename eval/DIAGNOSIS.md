# Diagnosis log — dev-label review (2026-09-22)

Causes, kept separate from [labels.yaml](labels.yaml) (ground truth) so every fix can be argued
from evidence. Each item cites the dev article(s) it was observed in.

## Fix log

| Fix | Date | Addresses | Status |
|---|---|---|---|
| #1 safety: type-agnostic crime guard + bare-surname collision guard (`hashtags.py`) | 2026-09-22 | B1, C4 (in crime/accident stories), E1, E2, R5 business rule | **installed** — snapshots `snapshots/dev_prefix_2026-09-22.txt` → `snapshots/dev_fix1_2026-09-22.txt` |
| #2 catalogue taxonomy (`tags.yaml`) | 2026-09-22 | A9, A10, A11 (`#Festival` "celebration(s)", `#Wildlife` "rescued"; `#Temple` "Mandir" not touched) | **installed** — snapshots `snapshots/dev_prefix2_2026-09-22.txt` → `snapshots/dev_fix2_2026-09-22.txt` |
| #3 catalogue gaps (A1–A8) | — | | not started |
| #4 ranking (D1, D2) | — | | not started |

Fix #1 side effects, known and accepted for now:
- In a crime/accident story every **single-word** unknown ORG is treated as a possible name. That
  correctly stops `#Jagtale`, `#Kamble` and `#Imran`, but it also drops `#Turbhe`, a place — and a
  `must` under R6. It was dropped by the org zone filter before, so no metric moved. It stops
  mattering once Turbhe is a catalogue place (A2): the guard only touches non-catalogue entities.
- `#TurbheMIDC` is now dropped as "carrying a person's name", because "Turbhe" counted as a
  possible name. It was dropped before too; only the stated reason changed.
- `#Sofa` (forbid) and `#Ganesh` (accept) are now dropped as "reads as a personal name". The
  outcome is right for `#Sofa`. For `#Ganesh` it comes from A10: the Ganesh-immersion story is
  flagged crime-sensitive only because it mentions the fire brigade. Expect `#Ganesh` back, and
  `#NaviMumbai` possibly out again, once fix #2 lands. **Resolved by fix #2:** `#Ganesh` is back
  and the Ganesh-immersion story is no longer flagged crime-sensitive. `#NaviMumbai` stayed, because
  the slot the wrong `#Fire` had held is now free.

Fix #2 — what changed in `tags.yaml` (8 lines):
- `#Accident` and `#Fire` no longer have a parent. They are not children of `#Crime`, so weak
  fire/accident evidence can't back off into `#Crime`. Both stay `sensitive: true` (R5).
- `#Crime` weak triggers: `"fire"` removed.
- `#Fire` = actual fires only:
  - removed the fire-service triggers `"fire brigade"`, `"firefighters"`, `"fire tenders"`;
  - removed the bare weak trigger `"fire"`, which also matched inside "Kharghar **Fire** Station" / "**fire** vehicle";
  - added the fire-event phrases `"fire erupted"`, `"flames"`, `"engulfed"`, `"charred"`;
  - rewrote the description without fire-service wording.
- `#Festival`: `"celebration"`, `"celebrations"` removed. Demoting them to weak triggers would not
  have been enough: the birthday-party story scores z = 0.9, above the 0.5 bar.
- `#Wildlife`: `"rescued"` removed. Same reason: the lift rescue scores z = 1.7.

Fix #2 side effects, known:
- **R4 (never tag a minor) has no mechanism of its own.** Until fix #2 the lift-rescue child was
  covered by the crime guard, because the story was wrongly flagged crime-sensitive. Now only the
  "person not in the headline" filter covers them. A child named in the headline of a non-crime,
  non-fire story could be tagged. Logged as B8.
- The lift-rescue story now ships a single tag (`#Kharghar`). Correct, but thin. Its must
  `#FireBrigade` needs the catalogue entry (A12, fix #3).
- The `#Fire` description changed, so every topic's semantic z shifted slightly (z is computed
  over the whole catalogue). No dev article changed because of it beyond the five listed in the
  snapshot.
- Operational: the running dashboard caches `tags.yaml` per process and the reloader doesn't
  watch it. Installing required touching `hashtags.py` (mtime only) to force a reload.

## A. Catalogue (tags.yaml) — gaps, taxonomy and over-broad triggers

| # | Problem | Observed in | Note |
|---|---|---|---|
| A1 | No `#Chess` — "chess" is only a trigger on `#Sports` | praggnanandhaa-gukesh | `#Cricket`/`#Football` exist as `#Sports` children; chess does not |
| A2 | Hyperlocal places missing: `#Digha`, `#Turbhe`, `#Pisavali`, `#EshwarNagar`, `#SubhashTekdi`, `#Morivali` | sofa-factory, birthday-sword, pisavali, murder-in-subhash, where-is-the-road | `#Digha` is a dateline with no place tag at all → **unreachable must** |
| A3 | No national/international coverage (`#India`, `#Russia`, `#USA`, `#Tariffs`, `#Sanctions`) | trump-signs, praggnanandhaa-gukesh | `#Business` scored −0.1 on a Business & Finance story |
| A4 | Suburbs/nodes have no parent linking them to their city | firefighters-rescue, sofa-factory, gita-lessons | `#NaviMumbai` never inferred from Kharghar/Digha/Vashi |
| A5 | No `#RajeshMore` (Kalyan Rural MLA) | 1-235-students | Also invisible to NER (C1) |
| A6 | No crime subtypes (`#Murder`) | man-killed, murder-in-subhash | `accept` only — minor |
| A7 | BNMC alias lacks "Bhiwandi Nizampur **City** Municipal Corporation" | 175-kg-mangur | Explicit name missed → "Municipal Corporation" resolved **via dateline Thane → `#TMC`** (wrong civic body) |
| A8 | No Ambernath civic body entry and no `by_place` for Ambernath | six-year-old-arch | "Municipal Council/Corporation" falls to generic `#CivicIssues` at 1.7 — just under `FLOOR` (see D5) |
| A9 | **Taxonomy: `#Fire → #Accident → #Crime`, plus `"fire"` is a weak trigger on `#Crime`** | firefighters-rescue, sofa-factory | **Fixed (#2).** `#Crime` no longer emitted on either story |
| A10 | **`#Fire` is `sensitive: true` and `"fire brigade"` is a full trigger → any fire-brigade mention sets `crime_context`** | nmmc-oversees (Ganesh immersion) | **Fixed (#2).** Fire-service words are no longer `#Fire` evidence; `sensitive` kept for real fires (R5). The sofa fire still guards its named people |
| A11 | Over-broad triggers | birthday-sword (`#Festival` ← "celebration"), firefighters-rescue (`#Wildlife` ← "rescued"), 1-235-students (`#Temple` ← "Mandir") | **`#Festival`/`#Wildlife` fixed (#2).** `#Temple` ← "Mandir" is a match inside a proper name (B6) and was left for later |
| A12 | No fire-service tag (`#FireBrigade`) | firefighters-rescue, nmmc-oversees | Depends on Q4 — what `#Fire` should mean |

## B. Filters / guards / matching logic (hashtags.py)

| # | Problem | Observed in | Note |
|---|---|---|---|
| B1 | **Crime-person guard tests `c.type == "person"` — people typed `org` bypass it** | man-killed, murder-in-subhash | **Fixed (#1).** Loosened-filter re-run: `#RohitJagtale` and `#ImranFishShop` no longer emitted |
| B4 | Semantic retrieval conflates violent-death topics | man-killed (`#Suicide` z 2.8 on a stabbing) | Caught by backoff; nothing stops it where `#Crime` evidence is weaker |
| B5 | Catalogue-first matching consumes spans; overlapping NER entities are discarded | murder-in-subhash (`Ulhasnagar Crime Branch` lost) | Also the mechanism behind E2 |
| B6 | Aliases match inside a larger proper name | 1-235-students ("Vidya **Mandir**" → `#Temple`; "Young Reader **Festival**" → `#Festival`), birthday-sword ("**Maharashtra** Police Act" → `#Maharashtra`) | `#Festival` and `#Maharashtra` **shipped** |
| B7 | Descriptive resolution falls back to a *district* dateline when the named body isn't recognised | 175-kg-mangur (`#TMC` for a Bhiwandi story) | Resolution rule (2) behaving as designed on bad input; root cause A7 |

| B8 | **R4 (never tag a minor) has no code path of its own** | firefighters-rescue | Exposed by fix #2: the child was protected only because the story was wrongly crime-flagged. Now only the "not in the headline" filter protects a named child on a non-crime story |

(B2 and B3 were reclassified as A10 and A9 — catalogue, not code.)

## C. NER / type classification (spaCy `en_core_web_sm`)

| # | Problem | Observed in | Note |
|---|---|---|---|
| C1 | Indian personal names missed entirely | 1-235-students (Rajesh More), murder-in-subhash (Atish Manohar, Pratik Kshetre) | "Kalyan Rural MLA" and "MLA" read as PERSON |
| C2 | Non-entities typed `person` | gita-lessons (`#GoldenJubilee` **shipped**), bmc-frames (`#TurfLicenceFee`, `#ReadyReckoner`) | |
| C3 | Common nouns typed `org` | sofa-factory (`#Sofa` **shipped**), nmmc (`#Ganesh` shipped), where-is-the-road (`#Driver`), 1-235-students (`#Marathi`) | |
| C4 | **People typed `org`** | man-killed (`#RohitJagtale`, `#AniketKharat`, `#Jagtale`), murder-in-subhash (`#RohitJagdale`, `#AniketKharat`), 175-kg-mangur (`#Imran`) | Root cause of B1 |
| C5 | Places typed `person`/`org` | where-is-the-road (`#ChikhloliMIDC`, `#Morivali` as person), sofa-factory (`#EshwarNagar` org), birthday-sword (`#TurbheMIDC` org), 1-235-students (`#Dahanu`, `#Nerul`, `#Panvel` org), murder-in-subhash (`Tekdi` person) | Tag text is often fine; the type decides which cap/filter applies |
| C6 | Merged / truncated / fragment spans | karan-johar (`#KaranJoharDharmaProductions`), trump (`#LindseyOGrahamSanctioning`, `#SharadchandraPawar` ← party name "NCP (Sharadchandra Pawar)"), gita-lessons (`#SharadManisha`, `#APaithaniSareeHome`) | `#SharadchandraPawar` would falsely imply the politician |

## D. Ranking / MMR / type caps / displacement

| # | Problem | Observed in | Note |
|---|---|---|---|
| D1 | Generic parent outranks the specific child | gita-lessons, nmmc-oversees | gita-lessons **resolved as a side effect of #2**: `#Festival` lost its "celebrations" evidence and `#Ganeshotsav` (must) took the slot. The immersion story still misses `#Ganeshotsav` (1.8, under the floor) |
| D2 | `TYPE_CAPS["phrase"] = 1` kills real entities that arrived as phrases | praggnanandhaa-gukesh | `#India` 3.3, `#Netherlands` 2.7 capped; article shipped 2 tags |
| D3 | `TYPE_CAPS["place"] = 2` drops a valid wider place | 1-235-students | `#Maharashtra` 2.9 capped |
| D4 | Mangled noun chunks reach the output | trump (`#OilBuyers`), shivshahi (`#ShivshahiDriver`), msedcl (`#TopPriority`) | Headline +3 outweighs phrase tier −1.2 |
| D5 | Catalogue tag just under `FLOOR` | six-year-old-arch (`#CivicIssues` 1.7), nmmc (`#Ganeshotsav` 1.8) | Root causes are A8 and D1, not the floor itself |
| D6 | Specific police force loses to the generic via backoff | man-killed (`#ThanePolice` 2.2 < 2.5) | Harmless; costs specificity |
| D7 | **Forbidden tags displace a must** | birthday-sword | `#Festival` 4.1 and `#Maharashtra` 3.1 (both wrong) outrank `#NaviMumbai` 2.5 → must missed |
| D8 | Wrong tags one slot from shipping | bmc-frames (`#Cricket` 4.1, `#Football` 4.0 "outranked"), 1-235-students (`#Temple` 3.2) | Latent violations — any upstream change can release them |

## E. Surname collisions into the catalogue — safety

| # | Problem | Observed in | Note |
|---|---|---|---|
| E1 | A catalogue politician matches an accused person's bare surname | man-killed (`#NareshMhaske` ← "Mhaske", 0.5), murder-in-subhash (−0.7) | **Fixed (#1)** — `#NareshMhaske` is no longer a candidate in either story |
| E2 | **The collision replaces the accused with the politician** | murder-in-subhash | **Fixed (#1)** — the bare surname no longer claims the span; `#SahilMhaske` now appears as a person and the crime guard drops it |

## Review status

| Article | Status |
|---|---|
| 1-235-students | owner-labelled, authoritative |
| man-killed-after-cigarette-row | reviewed by owner |
| murder-in-subhash-tekdi | reviewed by owner (+ `#UlhasnagarCrimeBranch` accept, `#Tom` forbid) |
| karan-johar, praggnanandhaa-gukesh | rule-applied, no open questions |
| the other 13 | rule-applied, labels in place, carry `OPEN: Q…` markers in labels.yaml |

## Changes made to Claude's own drafts during rule application

| Article | Change | Basis |
|---|---|---|
| bmc-frames | `#Business` forbid → accept | P1 — true (new licence fee on commercial operators), not misleading |
| gita-lessons | `#GoldenJubilee` forbid → accept | P1 — the story's actual hook |
| karan-johar | `#Culture` forbid → accept | P1; you accepted `#Culture` on DEV 1 |
| nmmc-oversees | `#Culture` forbid → accept | P1 |
| nmmc-oversees | `#Ganeshotsav` accept → **must** | consistency with gita-lessons, where it is must |
| six-year-old-arch | `#Politics` forbid → accept | P1 — article reports explicit political allegations (unlike DEV 1) |
| sofa-factory | `#EshwarNagar` forbid → accept | P2 — locality precedent `#SubhashTekdi` |
| sofa-factory | `#Digha` accept → **must**, `#NaviMumbai` must → accept | consistency with firefighters-rescue / gita-lessons (dateline node = must, city = accept); pending Q2 |
| where-is-the-road | `#ChikhloliMIDC` forbid → accept | P2 |
| firefighters-rescue | added `#PrashantDarekar` to forbid | the one crew member not yet listed; pending Q3 |
