"""
Hashtag suggestion for newsband.in articles.

    from hashtags import Article, suggest
    result = suggest(Article(headline=..., dateline=..., category=..., paragraphs=[...], url=...))
    result.tags        -> ["#Thane", "#Crime", ...]   (up to 5, may be fewer)
    result.candidates  -> every candidate with score, evidence and the factors behind it

    python hashtags.py <article-url>          # scrape + explain, prints the candidate table

How it decides (V1):
  1. candidates come from five sources - catalogue alias/trigger matches, permitted
     descriptive-reference resolution ("the civic body" -> #TMC when unambiguous),
     spaCy PERSON/ORG entities not in the catalogue, semantic retrieval against the
     catalogue's topic entries, and headline/lede noun chunks as a last resort;
  2. every candidate is scored by one additive relevance function - headline, dateline
     and lede presence dominate because the articles are ~230 words;
  3. the final set is picked by MMR (relevance minus redundancy) with a relevance floor,
     so weak stories yield fewer than five tags instead of invented ones.

Invariant: a specific tag needs evidence in the text (alias, trigger, or a resolved
descriptive reference). Nothing is inferred from a related entity - "Thane" never
yields #TMC. The only edges the catalogue may follow are `parent` edges, which
generalise (#SlumRedevelopment -> #CivicIssues) and are used to back off, never to
specialise.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field

import numpy as np
import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOGUE_PATH = os.path.join(BASE_DIR, "tags.yaml")
CACHE_DIR = os.path.join(BASE_DIR, ".cache")
SPACY_MODEL = "en_core_web_sm"
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
VERSION = "v1"

# ---- scoring knobs (tune against eval/, not by intuition) --------------------------
WEIGHTS = {
    "headline": 3.0,        # surface match inside the headline
    "dateline": 2.0,        # the place the paper datelined the story with
    "lede": 1.5,            # match inside the first two body sentences
    "role_subject": 1.0,    # grammatical subject in headline/lede
    "role_object": 0.4,
    "role_locative": -0.6,  # "... in Thane" - merely where it happened
    "freq": 0.3,            # per extra mention, capped at 3
    "early": 0.5,           # * (1 - relative position of first mention)
    "semantic": 1.0,        # * clip(z / 3, 0, 1) for topic tags
    "semantic_neg": -0.8,   # * clip(-z / 2, 0, 1): topic trigger present but the story does not read like it (not for headline hits)
    "category": 0.6,        # site label weakly supports this tag
    "weak_only": -0.8,      # only a bare surname / weak alias matched
    "tier": {0: 0.5, 1: 0.0, 2: -0.5, 3: -1.2},  # explicit / resolved+weak / semantic-only / free phrase
}
FLOOR = 2.0            # below this a candidate is never emitted (a lone lede mention needs one more signal)
BACKOFF_BELOW = 2.5    # tier>=1 candidates under this back off to their parent tag
MAX_TAGS = 5
MMR_LAMBDA = 0.7
TYPE_CAPS = {"person": 2, "org": 2, "place": 2, "topic": 3, "phrase": 1}
SEM_Z_MIN = 1.5        # semantic-only topic candidate: z-score over the topic catalogue
SEM_COS_MIN = 0.25     # ... and an absolute floor
WEAK_TRIGGER_Z_MIN = 0.5  # a weak trigger ("shot") needs this much semantic agreement

TIER_NAMES = {0: "explicit", 1: "resolved/weak", 2: "semantic", 3: "phrase"}
LOCATIVE_PREPS = {"in", "at", "near", "from", "to", "across", "along", "outside", "inside", "around", "within", "off"}
TITLES = {"mr", "mrs", "ms", "dr", "adv", "shri", "smt", "cm", "mla", "mp", "minister", "actor", "actress",
          "corporator", "commissioner", "inspector", "dcp", "acp", "psi", "api", "senior", "former", "ex"}
GENERIC_ORG_WORDS = {"police", "corporation", "government", "court", "committee", "department", "authority",
                     "council", "ministry", "office", "administration", "commission", "board"}
# a token like these means an entity is an organisation/place/document, not a personal name
NOT_A_NAME = GENERIC_ORG_WORDS | {
    "shop", "store", "station", "branch", "company", "ltd", "limited", "pvt", "foundation", "library",
    "hospital", "school", "college", "university", "bank", "society", "trust", "mandal", "club",
    "association", "union", "party", "productions", "collective", "market", "factory", "hotel",
    "restaurant", "industries", "group", "centre", "center", "act", "sanhita", "code", "festival",
    "circle", "road", "nagar", "chowk", "sector", "plot", "furniture", "world", "olympiad", "cinema"}
BLAND_ADJ = {"new", "old", "major", "big", "huge", "special", "several", "many", "various", "recent", "latest",
             "first", "second", "third", "last", "next", "other", "another", "such", "own", "same", "local", "nearby"}
# noun-chunk heads that never make a hashtag on their own
PHRASE_STOP = {
    "incident", "case", "people", "resident", "residents", "official", "officials", "area", "city", "project",
    "work", "works", "day", "year", "time", "matter", "issue", "issues", "event", "programme", "program",
    "workshop", "part", "number", "team", "member", "members", "authority", "authorities", "government",
    "state", "district", "body", "corporation", "administration", "department", "police", "meeting",
    "report", "statement", "demand", "demands", "decision", "action", "move", "step", "effort", "efforts",
    "way", "place", "spot", "occasion", "morning", "evening", "night", "week", "month", "hour", "today",
    "yesterday", "tomorrow", "thing", "situation", "condition", "reason", "source", "sources", "information",
    "news", "story", "article", "spokesperson", "press", "media", "public", "citizen", "citizens", "local",
    "locals", "man", "woman", "boy", "girl", "youth", "person", "persons", "family", "families", "son",
    "daughter", "wife", "husband", "friend", "friends", "cop", "cops", "preparation", "preparations",
    "special", "lakh", "crore", "rs", "percent", "per", "cent", "others", "other", "everyone", "someone",
    "nobody", "anyone", "one", "it", "they", "he", "she", "who", "which", "what", "that", "this",
}


# ====================================================================== data classes
@dataclass
class Article:
    headline: str
    paragraphs: list
    dateline: str = ""
    category: str = ""
    url: str = ""

    @property
    def body(self):
        return "\n\n".join(p for p in self.paragraphs if p)


@dataclass
class Span:
    start: int
    end: int
    text: str
    zone: str          # headline | lede | body | dateline
    role: str = ""     # subject | object | locative | other | ''
    kind: str = ""     # alias | weak_alias | trigger | weak_trigger | descriptive | ner | chunk


@dataclass
class Candidate:
    tag: str
    type: str                    # person | org | place | topic | phrase
    source: str                  # alias | resolved | resolved-generic | ner | semantic | phrase | backoff
    tier: int
    spans: list = field(default_factory=list)
    features: dict = field(default_factory=dict)
    score: float = 0.0
    reasons: list = field(default_factory=list)
    selected: bool = False
    pick: int = 0                # 1-based pick order when selected
    dropped: str = ""            # why it was excluded, '' if still in play
    parent: str = ""
    label: str = ""              # text embedded for redundancy checks
    sensitive: bool = False
    unknown: bool = False        # NER entity not in the catalogue
    name: str = ""               # NER surface name, for unknown entities

    def evidence(self):
        """Short human-readable justification for the dashboard."""
        if self.source == "semantic":
            return f"semantic match only (cos {self.features.get('sem_cos', 0):.2f}, z {self.features.get('sem_z', 0):.1f})"
        if self.source == "backoff":
            return self.reasons[0] if self.reasons else "backed off"
        parts = []
        for sp in self.spans[:3]:
            where = sp.zone + (f", {sp.role}" if sp.role and sp.zone in ("headline", "lede") else "")
            parts.append(f"“{sp.text}” ({where})")
        more = len(self.spans) - 3
        return "; ".join(parts) + (f"; +{more} more" if more > 0 else "")

    def to_dict(self):
        return {
            "tag": self.tag, "type": self.type, "source": self.source,
            "tier": self.tier, "tier_name": TIER_NAMES.get(self.tier, "?"),
            "score": round(self.score, 2), "selected": self.selected, "pick": self.pick,
            "dropped": self.dropped, "evidence": self.evidence(),
            "reasons": self.reasons,
            "features": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in self.features.items()},
            "spans": [{"text": s.text, "zone": s.zone, "role": s.role, "kind": s.kind} for s in self.spans[:8]],
        }


@dataclass
class Result:
    tags: list
    candidates: list
    headline: str
    dateline: str
    category: str
    url: str = ""
    padded: list = field(default_factory=list)

    def to_dict(self):
        ordered = sorted(self.candidates, key=lambda c: (not c.selected, c.pick or 99, bool(c.dropped), -c.score))
        return {
            "version": VERSION, "tags": self.tags, "padded": self.padded,
            "headline": self.headline, "dateline": self.dateline, "category": self.category or "",
            "models": {"spacy": SPACY_MODEL, "embed": EMBED_MODEL},
            "candidates": [c.to_dict() for c in ordered],
        }


# ====================================================================== catalogue
def _alias_regex(alias):
    """Word-bounded regex for a surface form. Pure-uppercase forms (acronyms: TMC, BJP)
    are case-sensitive so 'sc' in prose never matches 'SC'."""
    words = alias.split()
    body = r"[\s\-]+".join(re.escape(w) for w in words)
    pat = r"(?<![A-Za-z0-9])" + body + r"(?:['’]s)?(?![A-Za-z0-9])"
    flags = 0 if not re.search(r"[a-z]", alias) else re.IGNORECASE
    return re.compile(pat, flags)


class Catalogue:
    def __init__(self, path=CATALOGUE_PATH):
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        self.config = data.get("config") or {}
        self.entries = {}
        self.patterns = []   # (regex, target, kind)  kind: alias|weak_alias|trigger|weak_trigger
        for e in data["tags"]:
            tag = e["tag"]
            if not re.fullmatch(r"#[A-Za-z0-9_]+", tag):
                raise ValueError(f"tags.yaml: bad hashtag {tag!r}")
            self.entries[tag] = e
            for a in e.get("aliases", []):
                self.patterns.append((_alias_regex(a), tag, "alias"))
            for a in e.get("weak_aliases", []):
                self.patterns.append((_alias_regex(a), tag, "weak_alias"))
            for a in e.get("triggers", []):
                self.patterns.append((_alias_regex(a), tag, "trigger"))
            for a in e.get("weak_triggers", []):
                self.patterns.append((_alias_regex(a), tag, "weak_trigger"))
        for tag, e in self.entries.items():
            p = e.get("parent")
            if p and p not in self.entries:
                raise ValueError(f"tags.yaml: {tag} has unknown parent {p}")
        # descriptive-reference groups
        self.descriptive = []
        for name, g in (self.config.get("descriptive") or {}).items():
            self.descriptive.append({
                "name": name, "role": g.get("role"), "generic": g.get("generic"), "fixed": g.get("fixed"),
                "by_place": {k.lower(): v for k, v in (g.get("by_place") or {}).items()},
                "patterns": [_alias_regex(p) for p in g.get("phrases", [])],
            })
        self.topic_tags = [t for t, e in self.entries.items() if e.get("description")]
        self.category_map = {k.lower(): v for k, v in (self.config.get("category_map") or {}).items()}

    def type_of(self, tag):
        return self.entries.get(tag, {}).get("type", "topic")

    def parent_of(self, tag):
        return self.entries.get(tag, {}).get("parent") or ""

    def ancestors(self, tag):
        out, seen = [], set()
        while tag and tag not in seen:
            seen.add(tag)
            tag = self.parent_of(tag)
            if tag:
                out.append(tag)
        return out

    def label_of(self, tag):
        e = self.entries.get(tag)
        if not e:
            return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tag.lstrip("#"))
        if e.get("description"):
            return e["description"]
        return (e.get("aliases") or [tag.lstrip("#")])[0] + " (" + e.get("type", "") + ")"

    def find_matches(self, text):
        """All catalogue/descriptive matches in text, longest-first non-overlapping.
        Returns list of (start, end, matched_text, target, kind) where target is a tag or
        a descriptive group dict."""
        hits = []
        for rx, tag, kind in self.patterns:
            for m in rx.finditer(text):
                hits.append((m.start(), m.end(), m.group(0), tag, kind))
        for g in self.descriptive:
            for rx in g["patterns"]:
                for m in rx.finditer(text):
                    hits.append((m.start(), m.end(), m.group(0), g, "descriptive"))
        hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
        out, last_end = [], -1
        for h in hits:
            if h[0] >= last_end:
                out.append(h)
                last_end = h[1]
        return out


# ====================================================================== models (lazy)
_nlp = None
_embedder = None
_catalogue = None
_topic_vecs = None   # (tags list, matrix)


def get_catalogue():
    global _catalogue
    if _catalogue is None:
        _catalogue = Catalogue()
    return _catalogue


def get_nlp():
    global _nlp
    if _nlp is None:
        import spacy
        _nlp = spacy.load(SPACY_MODEL)
    return _nlp


def get_embedder():
    global _embedder
    if _embedder is None:
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")   # keep the Flask log readable
        os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer(EMBED_MODEL)
    return _embedder


def embed(texts):
    return np.asarray(get_embedder().encode(list(texts), normalize_embeddings=True, show_progress_bar=False))


def topic_vectors(cat):
    """Embeddings of every topic entry's description, cached on disk keyed by content."""
    global _topic_vecs
    if _topic_vecs is not None:
        return _topic_vecs
    items = [(t, cat.entries[t]["description"]) for t in cat.topic_tags]
    key = hashlib.sha1((EMBED_MODEL + json.dumps(items)).encode("utf-8")).hexdigest()[:16]
    path = os.path.join(CACHE_DIR, f"topics_{key}.npy")
    if os.path.exists(path):
        mat = np.load(path)
    else:
        mat = embed([d for _, d in items])
        os.makedirs(CACHE_DIR, exist_ok=True)
        np.save(path, mat)
    _topic_vecs = ([t for t, _ in items], mat)
    return _topic_vecs


# ====================================================================== helpers
def camel(text):
    words = re.findall(r"[A-Za-z0-9]+", text)
    return "#" + "".join(w[:1].upper() + w[1:] for w in words)


def role_of(doc, start, end):
    """Grammatical role of the text span: is it the actor of the sentence, or just where
    something happened? Climbs compound/appos/conj links so 'Thane Municipal Corporation'
    resolves through its head noun."""
    sp = doc.char_span(start, end, alignment_mode="expand")
    if sp is None:
        return "other"
    tok = sp.root
    for _ in range(4):
        dep = tok.dep_
        if dep in ("nsubj", "nsubjpass", "csubj"):
            return "subject"
        if dep in ("dobj", "obj", "attr", "dative", "oprd"):
            return "object"
        if dep == "pobj":
            prep = tok.head
            if prep.text.lower() in LOCATIVE_PREPS and prep.head.pos_ in ("VERB", "AUX", "NOUN"):
                return "locative"
            return "object"
        if dep in ("compound", "appos", "conj", "nmod", "poss", "amod", "npadvmod", "flat"):
            tok = tok.head
            continue
        break
    return "other"


def clean_entity(text):
    toks = text.replace("’s", "").replace("'s", "").split()
    while toks and (toks[0].lower().strip(".") in TITLES or toks[0].lower() == "the"):
        toks.pop(0)
    return " ".join(t.strip(",.;:") for t in toks).strip()


def overlaps(start, end, spans):
    return any(s < end and start < e for s, e in spans)


def looks_like_person(name):
    """Does an entity's surface text read as a personal name, whatever spaCy labelled it?
    spaCy's small model types plenty of Indian names as ORG ("Rohit Jagtale", "Aniket
    Kharat"), so the crime guard cannot trust the label. Deliberately generous: in a crime
    story a false 'yes' only costs a tag, a false 'no' publishes someone's name."""
    toks = name.split()
    if not 1 <= len(toks) <= 4:
        return False
    if any(t.lower().strip(".") in NOT_A_NAME for t in toks):
        return False
    if any(len(t) > 1 and t.isupper() for t in toks):      # acronyms: CCTV, BNS, APMC
        return False
    return all(t[:1].isupper() and re.fullmatch(r"[A-Za-z.'’-]+", t) for t in toks)


def other_given_name(doc, start, own):
    """True when the surname at `start` follows a given name that is not the catalogue
    person's own: 'Sahil Mhaske' is not #NareshMhaske. Titles ('MP Mhaske') and ordinary
    words ('The Mhaske family') don't count; an initial counts unless it is the person's."""
    sp = doc.char_span(start, start + 1, alignment_mode="expand")
    if sp is None or sp.start == 0:
        return False
    prev = doc[sp.start - 1]
    word = prev.text.rstrip(".").lower()
    if not prev.text[:1].isupper() or word in TITLES or word in own:
        return False
    if len(word) == 1:                                      # "S. Mhaske"
        return word not in {w[0] for w in own}
    return prev.is_alpha and not prev.is_stop and prev.pos_ == "PROPN"


# ====================================================================== pipeline
def suggest(article, cat=None):
    cat = cat or get_catalogue()
    nlp = get_nlp()

    headline = article.headline.strip()
    body = article.body.strip()
    text = headline + "\n\n" + body if body else headline
    doc = nlp(text)
    head_end = len(headline)
    body_start = head_end + 2 if body else head_end

    # lede = first two sentences of the body
    body_sents = [s for s in doc.sents if s.start_char >= body_start]
    lede_end = body_sents[1].end_char if len(body_sents) >= 2 else (body_sents[0].end_char if body_sents else len(text))
    lede_text = text[body_start:lede_end].strip()

    def zone_of(start):
        if start < head_end:
            return "headline"
        return "lede" if start < lede_end else "body"

    cands = {}
    used = []   # (start, end) spans consumed by catalogue/descriptive matches

    def get(tag, type_, source, tier):
        c = cands.get(tag)
        if c is None:
            e = cat.entries.get(tag, {})
            c = Candidate(tag=tag, type=type_, source=source, tier=tier, parent=cat.parent_of(tag),
                          label=cat.label_of(tag), sensitive=bool(e.get("sensitive")))
            cands[tag] = c
        else:
            c.tier = min(c.tier, tier)
            if source == "alias" and c.source != "alias":
                c.source = "alias"
        return c

    # -- 1. catalogue aliases / triggers (+ descriptive phrases, resolved in step 2)
    # A catalogue person's bare surname (weak alias) right after a *different* given name is
    # another person - "Sahil Mhaske", an accused, is not #NareshMhaske - and once such a
    # name is in the story, its bare mentions are that other person too. Those matches are
    # skipped entirely, so they neither become evidence nor claim the span from NER.
    matches = cat.find_matches(text)
    other_people = set()
    for start, end, mtext, target, kind in matches:
        if kind == "weak_alias" and cat.type_of(target) == "person":
            own = {w.lower() for a in cat.entries[target].get("aliases", []) for w in a.split()}
            if other_given_name(doc, start, own):
                other_people.add((target, mtext.lower()))
    descriptive_hits = []
    for start, end, mtext, target, kind in matches:
        if kind == "weak_alias" and (target, mtext.lower()) in other_people:
            continue
        used.append((start, end))
        z = zone_of(start)
        role = role_of(doc, start, end) if z in ("headline", "lede") else ""
        if kind == "descriptive":
            descriptive_hits.append((target, Span(start, end, mtext, z, role, kind)))
            continue
        tier = 0 if kind in ("alias", "trigger") else 1
        c = get(target, cat.type_of(target), "alias", tier)
        c.spans.append(Span(start, end, mtext, z, role, kind))

    # -- dateline: the place the paper says the story is from
    dateline = (article.dateline or "").strip().rstrip(":").strip()
    dateline_places = []
    if dateline:
        for _, _, mtext, target, kind in cat.find_matches(dateline):
            if kind == "alias" and cat.type_of(target) == "place":
                c = get(target, "place", "alias", 0)
                c.spans.append(Span(0, 0, mtext, "dateline", "", "alias"))
                dateline_places.append(target)

    # -- 2. descriptive references: resolve only when unambiguous, else generic
    for g, sp in descriptive_hits:
        target, how = None, ""
        if g.get("fixed"):
            target, how = g["fixed"], "fixed reference"
        else:
            explicit = [t for t, c in cands.items()
                        if cat.entries.get(t, {}).get("role") == g["role"]
                        and any(s.kind == "alias" for s in c.spans)]
            if len(explicit) == 1:
                target, how = explicit[0], f"explicitly named in the article as “{cands[explicit[0]].spans[0].text}”"
            elif len(explicit) > 1:
                how = f"ambiguous: {len(explicit)} {g['name']} entries named"
            elif dateline and dateline.lower() in g["by_place"]:
                target, how = g["by_place"][dateline.lower()], f"dateline “{dateline}”"
            else:
                how = "no antecedent" if not dateline else f"dateline “{dateline}” not mapped"
        if target:
            c = get(target, cat.type_of(target), "resolved", 1)
            c.spans.append(Span(sp.start, sp.end, sp.text, sp.zone, sp.role, "descriptive"))
            c.reasons.append(f"“{sp.text}” resolved via {how}")
        elif g.get("generic"):
            c = get(g["generic"], cat.type_of(g["generic"]), "resolved-generic", 1)
            c.spans.append(Span(sp.start, sp.end, sp.text, sp.zone, sp.role, "descriptive"))
            c.reasons.append(f"“{sp.text}” not resolved ({how}) → generic")

    # -- 3. spaCy PERSON / ORG not covered by the catalogue
    ner_people = {}
    for ent in doc.ents:
        if ent.label_ not in ("PERSON", "ORG") or overlaps(ent.start_char, ent.end_char, used):
            continue
        name = clean_entity(ent.text)
        if not name or re.search(r"\d", name) or len(name.split()) > 5:
            continue
        toks = name.split()
        z = zone_of(ent.start_char)
        role = role_of(doc, ent.start_char, ent.end_char) if z in ("headline", "lede") else ""
        span = Span(ent.start_char, ent.end_char, ent.text, z, role, "ner")
        if ent.label_ == "PERSON":
            # a bare surname later in the story belongs to a full name seen earlier
            owner = next((c for full, c in ner_people.items() if len(toks) == 1 and toks[0] in full.split()), None)
            if owner is not None:
                owner.spans.append(span)
                continue
            if len(toks) < 2 or any(t.lower() in PHRASE_STOP for t in toks):
                continue
            c = get(camel(name), "person", "ner", 1)
            c.unknown, c.label, c.name = True, name + " (person)", name
            c.spans.append(span)
            ner_people[name] = c
        else:
            low = {t.lower() for t in toks}
            if low <= GENERIC_ORG_WORDS or len(toks) == 1 and toks[0].lower() in GENERIC_ORG_WORDS:
                continue
            c = get(camel(name), "org", "ner", 1)
            c.unknown, c.label, c.name = True, name + " (organisation)", name
            c.spans.append(span)
        used.append((ent.start_char, ent.end_char))

    # a person's bare surname in the headline ("Jarange announces...") belongs to the
    # full name spaCy only recognised later in the story
    for name, c in ner_people.items():
        for tok in name.split():
            if len(tok) < 4 or not tok[0].isupper():
                continue
            for m in re.finditer(r"(?<![A-Za-z])" + re.escape(tok) + r"(?![A-Za-z])", headline):
                if not overlaps(m.start(), m.end(), used):
                    c.spans.insert(0, Span(m.start(), m.end(), tok, "headline", role_of(doc, m.start(), m.end()), "ner"))
                    used.append((m.start(), m.end()))
    # "China Southern" and "China Southern Airlines" are one organisation
    orgs = sorted((c for c in cands.values() if c.unknown and c.type == "org"), key=lambda c: -len(c.tag))
    for i, longer in enumerate(orgs):
        for shorter in orgs[i + 1:]:
            if longer.tag.lower().startswith(shorter.tag.lower()) and not shorter.dropped:
                longer.spans.extend(shorter.spans)
                shorter.dropped = f"same organisation as {longer.tag}"

    # -- 4. semantic retrieval over the topic catalogue
    topic_tags, mat = topic_vectors(cat)
    art_vec = embed([headline + ". " + lede_text])[0]
    cos = mat @ art_vec
    zs = (cos - cos.mean()) / (cos.std() + 1e-9)
    for t, cs, z in zip(topic_tags, cos, zs):
        c = cands.get(t)
        if c is None and z >= SEM_Z_MIN and cs >= SEM_COS_MIN:
            c = get(t, "topic", "semantic", 2)
        if c is not None:
            c.features["sem_cos"], c.features["sem_z"] = float(cs), float(z)

    # -- 5. headline / lede noun chunks as low-confidence free phrases
    existing_tags = set(cands)
    for chunk in doc.noun_chunks:
        if chunk.start_char >= lede_end or overlaps(chunk.start_char, chunk.end_char, used):
            continue
        toks = [t for t in chunk if not (t.is_stop and t.pos_ in ("DET", "PRON", "ADP", "CCONJ")) and not t.is_punct]
        while toks and (toks[0].pos_ in ("DET", "PRON", "NUM") or toks[0].lower_ in BLAND_ADJ):
            toks.pop(0)
        if not toks or len(toks) > 3 or chunk.root.pos_ == "PRON":
            continue
        if any(t.like_num or t.ent_type_ in ("DATE", "TIME", "CARDINAL", "ORDINAL", "MONEY", "PERCENT", "QUANTITY") for t in toks):
            continue
        if chunk.root.lemma_.lower() in PHRASE_STOP or all(t.is_stop for t in toks):
            continue
        phrase = " ".join(t.text for t in toks)
        tag = camel(phrase)
        z = zone_of(chunk.start_char)
        # a lone common noun from the lede ("activity", "children") is never a hashtag;
        # single words only qualify from the headline, multi-word phrases from either
        if tag in existing_tags or len(tag) < 4 or (len(toks) == 1 and (z != "headline" or toks[0].pos_ != "PROPN")):
            continue
        c = get(tag, "phrase", "phrase", 3)
        c.label = phrase
        c.spans.append(Span(chunk.start_char, chunk.end_char, phrase, z, role_of(doc, chunk.start_char, chunk.end_char), "chunk"))

    # ---------------------------------------------------------------- hard filters
    crime_context = any(c.sensitive and any(s.kind == "trigger" for s in c.spans) for c in cands.values())
    # In a crime/accident story no private individual is ever tagged, and spaCy's PERSON/ORG
    # label is not trusted for that: an unknown entity that reads as a personal name is a
    # person here whatever its label, and so is an organisation carrying such a person's
    # name ("Imran Fish Shop", owned by the accused).
    name_like = [c for c in cands.values() if c.unknown and (c.type == "person" or looks_like_person(c.name))]
    name_words = {w.lower() for c in name_like for w in c.name.split() if len(w) > 2}
    for c in cands.values():
        if crime_context and c.unknown and c.type == "org":
            if looks_like_person(c.name):
                c.dropped = "reads as a personal name (NER said ORG) in a crime/accident story (never tagged)"
                continue
            if any(w.lower() in name_words for w in c.name.split()):
                c.dropped = "organisation carrying a person's name in a crime/accident story (never tagged)"
                continue
        kinds = {s.kind for s in c.spans}
        if c.source == "alias" and kinds and kinds <= {"weak_trigger"}:
            if c.features.get("sem_z", -9) < WEAK_TRIGGER_Z_MIN:
                c.dropped = f"only a weak trigger matched and the story does not read as {c.tag} (z {c.features.get('sem_z', 0):.1f})"
        if c.unknown and c.type == "person":
            if crime_context:
                c.dropped = "unnamed-in-catalogue person in a crime story (never tagged)"
            elif not any(s.zone == "headline" for s in c.spans):
                c.dropped = "person not in the catalogue and not in the headline"
        if c.unknown and c.type == "org":
            if not any(s.zone == "headline" or (s.zone == "lede" and s.role == "subject") for s in c.spans):
                c.dropped = "organisation not in the catalogue and not headline / lede subject"

    # ---------------------------------------------------------------- scoring
    text_len = max(len(text), 1)
    site_boost = set(cat.category_map.get((article.category or "").strip().lower(), []))
    for c in cands.values():
        f = c.features
        body_spans = [s for s in c.spans if s.zone != "dateline"]
        f["in_headline"] = any(s.zone == "headline" for s in c.spans)
        f["in_dateline"] = any(s.zone == "dateline" for s in c.spans)
        f["in_lede"] = any(s.zone == "lede" for s in c.spans)
        f["freq"] = len(body_spans)
        f["first_pos"] = min((s.start for s in body_spans), default=text_len) / text_len
        roles = [s.role for s in c.spans if s.zone in ("headline", "lede") and s.role]
        if "subject" in roles:
            f["role"] = "subject"
        elif "object" in roles:
            f["role"] = "object"
        elif roles and all(r == "locative" for r in roles):
            f["role"] = "locative"
        else:
            f["role"] = "other" if roles else ""
        f["weak_only"] = bool(body_spans) and all(s.kind in ("weak_alias", "weak_trigger") for s in body_spans)
        f["category_boost"] = c.tag in site_boost
        f["tier"] = c.tier

        s, why = 0.0, []
        if f["in_headline"]:
            s += WEIGHTS["headline"]; why.append(f"headline +{WEIGHTS['headline']}")
        if f["in_dateline"]:
            s += WEIGHTS["dateline"]; why.append(f"dateline +{WEIGHTS['dateline']}")
        if f["in_lede"]:
            s += WEIGHTS["lede"]; why.append(f"lede +{WEIGHTS['lede']}")
        if f["role"] in ("subject", "object", "locative"):
            w = WEIGHTS["role_" + f["role"]]
            s += w; why.append(f"{f['role']} {w:+}")
        if f["freq"] > 1:
            w = WEIGHTS["freq"] * min(f["freq"] - 1, 3)
            s += w; why.append(f"{f['freq']} mentions +{w:.1f}")
        if body_spans:
            w = WEIGHTS["early"] * (1 - f["first_pos"])
            s += w; why.append(f"early +{w:.1f}")
        if "sem_z" in f and c.type == "topic":
            w = WEIGHTS["semantic"] * float(np.clip(f["sem_z"] / 3, 0, 1))
            if w > 0.05:
                s += w; why.append(f"semantic z={f['sem_z']:.1f} +{w:.1f}")
            elif f["sem_z"] < 0 and not f["in_headline"]:
                w = WEIGHTS["semantic_neg"] * float(np.clip(-f["sem_z"] / 2, 0, 1))
                if w < -0.05:
                    s += w; why.append(f"story does not read as this topic (z={f['sem_z']:.1f}) {w:.1f}")
        if f["category_boost"]:
            s += WEIGHTS["category"]; why.append(f"site category “{article.category}” +{WEIGHTS['category']}")
        if f["weak_only"]:
            s += WEIGHTS["weak_only"]; why.append(f"weak alias only {WEIGHTS['weak_only']}")
        tw = WEIGHTS["tier"][c.tier]
        s += tw; why.append(f"{TIER_NAMES[c.tier]} {tw:+}")
        c.score = s
        c.reasons = why + c.reasons

    # ---------------------------------------------------------------- back-off
    for c in list(cands.values()):
        if c.dropped or c.tier == 0 or c.score >= BACKOFF_BELOW or not c.parent:
            continue
        p = cands.get(c.parent)
        if p is None:
            p = get(c.parent, cat.type_of(c.parent), "backoff", 1)
            p.score = c.score
            p.features = {"backoff_from": c.tag}
            p.reasons = [f"backed off from {c.tag} (score {c.score:.1f} < {BACKOFF_BELOW})"]
        elif not p.dropped:
            p.score = max(p.score, c.score)
            p.reasons.append(f"also carries {c.tag}'s weak evidence")
        c.dropped = f"weak evidence (score {c.score:.1f} < {BACKOFF_BELOW}) → backed off to {c.parent}"

    # ---------------------------------------------------------------- selection (MMR)
    pool = [c for c in cands.values() if not c.dropped and c.score >= FLOOR]
    for c in cands.values():
        if not c.dropped and c.score < FLOOR:
            c.dropped = f"below relevance floor ({c.score:.1f} < {FLOOR})"
    chosen = []
    if pool:
        vecs = embed([c.label for c in pool])
        idx = {id(c): i for i, c in enumerate(pool)}
        max_score = max(c.score for c in pool)

        def similarity(a, b):
            sim = float(vecs[idx[id(a)]] @ vecs[idx[id(b)]])
            if a.tag in cat.ancestors(b.tag) or b.tag in cat.ancestors(a.tag):
                sim = max(sim, 0.9)
            ta, tb = a.tag.lower().lstrip("#"), b.tag.lower().lstrip("#")
            if ta in tb or tb in ta:
                sim = max(sim, 0.7)
            return sim

        counts = {}
        remaining = list(pool)
        while remaining and len(chosen) < MAX_TAGS:
            best, best_val, best_pen = None, -9, 0
            for c in remaining:
                if counts.get(c.type, 0) >= TYPE_CAPS.get(c.type, 2):
                    continue
                pen = max((similarity(c, o) for o in chosen), default=0.0)
                val = MMR_LAMBDA * (c.score / max_score) - (1 - MMR_LAMBDA) * pen
                if val > best_val:
                    best, best_val, best_pen = c, val, pen
            if best is None:
                break
            chosen.append(best)
            remaining.remove(best)
            counts[best.type] = counts.get(best.type, 0) + 1
            best.selected, best.pick = True, len(chosen)
            if best_pen > 0.3:
                best.reasons.append(f"redundancy penalty {best_pen:.2f} (still picked)")
        for c in remaining:
            if counts.get(c.type, 0) >= TYPE_CAPS.get(c.type, 2):
                c.dropped = f"type cap: already {TYPE_CAPS.get(c.type, 2)} {c.type} tags"
            else:
                c.dropped = "outranked in final selection"

    tags = [c.tag for c in chosen]
    padded = []
    if cat.config.get("pad_to_five"):
        for b in cat.config.get("brand_tags") or []:
            if len(tags) + len(padded) >= MAX_TAGS:
                break
            if b not in tags:
                padded.append(b)
    return Result(tags=tags + padded, candidates=list(cands.values()), headline=headline,
                  dateline=dateline, category=article.category or "", url=article.url, padded=padded)


# ====================================================================== CLI
def print_result(res):
    print(f"\n{res.headline}\n  dateline: {res.dateline or '-'}   category: {res.category or '-'}")
    print("  TAGS: " + ("  ".join(res.tags) if res.tags else "(none cleared the floor)"))
    print(f"\n  {'':2} {'tag':28} {'score':>5}  {'type':7} {'source':16} evidence / reasons")
    for c in sorted(res.candidates, key=lambda c: (not c.selected, c.pick or 99, bool(c.dropped), -c.score)):
        mark = f"#{c.pick}" if c.selected else ("x" if c.dropped else "-")
        print(f"  {mark:2} {c.tag[:28]:28} {c.score:5.1f}  {c.type:7} {c.source:16} {c.evidence()}")
        print(f"  {'':2} {'':28} {'':5}  {'':7} {'':16} {' | '.join(c.reasons)}")
        if c.dropped:
            print(f"  {'':2} {'':28} {'':5}  {'':7} {'':16} DROPPED: {c.dropped}")


def backfill(force=False):
    """Add hashtags to every meta/<slug>.json that lacks them (or all, with force) by
    re-scraping the article - no post is re-rendered."""
    from scrape_post import META_DIR, scrape_article_full, suggest_hashtags
    done = 0
    for f in sorted(os.listdir(META_DIR)):
        if not f.endswith(".json"):
            continue
        path = os.path.join(META_DIR, f)
        with open(path, encoding="utf-8") as fh:
            meta = json.load(fh)
        if (meta.get("hashtags") and not force) or not meta.get("url"):
            continue
        try:
            a = scrape_article_full(meta["url"])
        except Exception as e:  # noqa: BLE001
            print(f"SKIP {f}: {e}")
            continue
        meta["hashtags"] = suggest_hashtags(meta["url"], a)
        meta["dateline"], meta["category"] = a["dateline"], a["category"]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        done += 1
        print(f"OK   {meta['headline'][:60]:60} {'  '.join(meta['hashtags'].get('tags', []))}")
    print(f"{done} sidecar(s) updated")


def main():
    p = argparse.ArgumentParser(description="Suggest hashtags for a newsband.in article.")
    p.add_argument("url", nargs="?", help="article URL")
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    p.add_argument("--backfill", action="store_true", help="add hashtags to existing meta/*.json sidecars")
    p.add_argument("--force", action="store_true", help="with --backfill: recompute even where hashtags exist")
    args = p.parse_args()
    if args.backfill:
        return backfill(args.force)
    if not args.url:
        p.error("give an article URL or --backfill")
    from scrape_post import scrape_article_full   # lazy: scrape_post imports this module
    a = scrape_article_full(args.url)
    art = Article(headline=a["headline"], paragraphs=a["paragraphs"], dateline=a["dateline"],
                  category=a["category"], url=args.url)
    res = suggest(art)
    if args.json:
        print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
    else:
        print_result(res)


if __name__ == "__main__":
    main()
