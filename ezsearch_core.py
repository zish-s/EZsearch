"""
ezsearch_core.py - the "brain" of EZsearch (no GUI code in here).

Pipeline for one sentence:
  1. LOCAL LLM (Ollama, open-weight)  -> turns a sentence into a short image-search query
  2. OPEN IMAGE SOURCES               -> Wikimedia Commons (no key) + Openverse (free key),
                                        queried at the same time and merged
  3. LOCAL CLIP (open-weight, ONNX)   -> re-ranks candidates by how well they match the
                                        WHOLE sentence (and, a little, the search query)
  4. Clipboard + focus helpers        -> put the chosen picture on the clipboard and paste it back

Everything here is offline-capable except step 2, which is the only thing that
touches the network. No sentence ever leaves the machine.
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import os
import platform
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import requests
from PIL import Image

# ----------------------------------------------------------------------------
# Config (override with environment variables, no code edits needed)
# ----------------------------------------------------------------------------
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_TAGS = os.getenv("OLLAMA_TAGS", "http://localhost:11434/api/tags")
LLM_MODEL = os.getenv("EZSEARCH_MODEL", "llama3.2:3b")
LLM_TIMEOUT = float(os.getenv("EZSEARCH_LLM_TIMEOUT", "45"))
"""How long to wait for the local model. A cold model can take a minute to load,
so this is generous - the app warms the model up in the background at start-up."""

N_CANDIDATES = int(os.getenv("EZSEARCH_CANDIDATES", "24"))
"""How many candidates to pull before re-ranking. A re-ranker is only as good
as the pool it ranks, so this is deliberately generous - but every extra
candidate costs a download and a CLIP embedding, and the embedding is the
slowest part."""

N_SHOWN = int(os.getenv("EZSEARCH_SHOWN", "8"))
"""How many tiles to show in the popup."""

MIN_SCORE = float(os.getenv("EZSEARCH_MIN_SCORE", "0.18"))
"""CLIP cosine similarity below which we tell the user we found nothing good.
Typical text/image cosine scores for a decent match sit around 0.22-0.30."""

QUERY_WEIGHT = float(os.getenv("EZSEARCH_QUERY_WEIGHT", "0.25"))
"""How much of the final score comes from the LLM's short query, versus the
whole sentence. The sentence is the better signal; the query is the sharper one."""

EMBED_BATCH = max(1, int(os.getenv("EZSEARCH_EMBED_BATCH", "8")))
"""Pictures are embedded in batches of this size. One image per ONNX call is
about twice as slow as eight, so this is the difference between a search that
takes 20 seconds and one that takes 7."""

RERANK = os.getenv("EZSEARCH_RERANK", "0") == "1"
"""Should CLIP reorder the results, or only score them?

Off by default, and that is a measurement talking. On the 20-sentence test in
benchmark.py, CLIP re-ranking put a matching picture first 24 times out of 40,
while the search engine's own order managed 29. It tied at top three (31/40),
so it is not useless - but it is not an improvement either. CLIP still scores
every tile (the match bar and the "no strong match" warning depend on it), and
EZSEARCH_RERANK=1 brings the reordering back."""

PREVIEW_WIDTH = int(os.getenv("EZSEARCH_PREVIEW_WIDTH", "320"))
FULL_WIDTH = int(os.getenv("EZSEARCH_FULL_WIDTH", "1600"))

UA = "EZsearch/1.0 (local desktop tool; https://github.com/zish-s/EZsearch)"
SYSTEM = platform.system()  # "Windows", "Darwin" (macOS) or "Linux"

CACHE_DIR = Path(os.getenv("EZSEARCH_CACHE", Path.home() / ".ezsearch"))

STOPWORDS = set(
    "a an the and or of to in on at for with by is are was were be been it its this that these "
    "those as from into about over under than then so such very just also can could should would "
    "will may might has have had do does did not no we our you your they their he she his her i my "
    "there here when where which who whom while during before after above below again further "
    "once more most other some any both each few own same s t don now".split()
)

# Licences we are willing to paste into somebody's document without asking.
# "nc" (non commercial) and "nd" (no derivatives) are excluded on purpose.
#
# The two sources spell the same licence differently: Commons uses
# "CC BY-SA 4.0", Openverse uses "BY-SA 4.0" (the "CC" is implied). So we
# compare tokens rather than substrings.
_BLOCKED_TOKENS = {
    "nc", "nd", "noncommercial", "copyright", "copyrighted", "fairuse",
    "derivatives", "nonfree",
}
_FREE_TOKENS = {
    "cc0", "pdm", "gfdl", "gnu", "fal", "attribution", "odbl", "odc",
    "publicdomain", "sharealike",
}


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def first_sentence(text: str) -> str:
    """The first sentence of a selection - that is what gets illustrated."""
    text = (text or "").strip()
    if not text:
        return ""
    match = re.search(r"(?<=[.!?])\s+", text)
    if match and len(text) > 40:
        return text[:match.start()].strip()
    return text


def licence_ok(licence: str) -> bool:
    """True when a licence is free enough to paste into a document.

    Tokens, not substrings: "BY 4.0" and "CC BY 4.0" are the same licence, and
    "CC BY-NC 4.0" must not sneak through because it contains the word "by".
    """
    text = (licence or "").strip().lower()
    if not text:
        return False
    tokens = set(re.findall(r"[a-z0-9]+", text))
    if tokens & _BLOCKED_TOKENS:
        return False
    if tokens & _FREE_TOKENS:
        return True
    if "by" in tokens and ("cc" in tokens or "sa" in tokens):
        return True
    if "public" in tokens and "domain" in tokens:
        return True
    if "free" in tokens and ("art" in tokens or "documentation" in tokens):
        return True
    # Openverse's bare "BY 4.0".
    if "by" in tokens and len(text) <= 20:
        return True
    return False


def credit_text(c: dict) -> str:
    """A ready-made attribution line for the picture."""
    title = c.get("title") or "Untitled"
    creator = c.get("creator") or "Unknown"
    licence = c.get("license") or "Unknown licence"
    page = c.get("page") or ""
    source = c.get("source") or ""
    line = f'"{title}" by {creator} ({licence})'
    if source:
        line += f" - {source}"
    if page:
        line += f" - {page}"
    return line


# ----------------------------------------------------------------------------
# 1. Sentence -> search query  (local LLM, with a no-AI fallback)
# ----------------------------------------------------------------------------
def fallback_query(sentence: str) -> str:
    """Used when Ollama is not running: keep the first few meaningful words."""
    words = [
        w for w in re.findall(r"[A-Za-z']+", sentence.lower())
        if w not in STOPWORDS and len(w) > 2
    ]
    return " ".join(words[:6]) or sentence[:60]


def ollama_available(model: str = LLM_MODEL) -> bool:
    """Is Ollama running and does it have the model we want?"""
    try:
        r = requests.get(OLLAMA_TAGS, timeout=2)
        r.raise_for_status()
        names = {m.get("name", "") for m in r.json().get("models", [])}
        if not names:
            return False
        return any(n == model or n.startswith(model.split(":")[0]) for n in names)
    except Exception:
        return False


def query_variants(query: str) -> list[str]:
    """Progressively less noisy phrasings of one query.

    MediaWiki's search ANDs every term together, so both kinds of failure
    happen: a chatty sentence is too narrow ("roundabout in chandigarh"
    matches one single Commons file, because "in" is part of the AND), and a
    bloated query matches nothing. We send the original, then the same words
    without filler, then progressively shorter prefixes - resolve_query picks
    whichever phrasing answers with a pool worth choosing from.
    """
    words = query.split()
    variants = [query]
    cleaned = " ".join(w for w in words if w.lower() not in STOPWORDS)
    if cleaned and cleaned != query:
        variants.append(cleaned)
    for size in (3, 2, 1):
        if len(words) > size:
            variants.append(" ".join(words[:size]))
    ordered = []
    for v in variants:
        if v and v not in ordered:
            ordered.append(v)
    return ordered


def warm_up(model: str = LLM_MODEL) -> None:
    """Pull the model into memory so the first real search is not a cold start.

    The very first inference after `ollama pull` loads the weights and takes
    the best part of a minute. Doing that in the background at start-up means
    the user does not pay for it.
    """
    def worker():
        try:
            requests.post(
                OLLAMA_URL,
                json={"model": model, "prompt": "hi", "stream": False, "keep_alive": "30m"},
                timeout=180,
            )
        except Exception:
            pass
    threading.Thread(target=worker, daemon=True).start()


def build_query(sentence: str, model: str = LLM_MODEL) -> tuple[str, bool]:
    """Returns (query, used_llm)."""
    sentence = first_sentence(sentence)
    prompt = (
        "You pick photographs to illustrate sentences in documents.\n"
        "Read the sentence and reply with JSON only: {\"query\": \"...\"}\n"
        "Rules for the query: 2 to 5 words, a concrete visible subject a photographer could "
        "have shot, no abstract words, no quotes inside the query.\n\n"
        f"Sentence: {sentence[:400]}"
    )
    try:
        r = requests.post(
            OLLAMA_URL,
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "keep_alive": "30m",             # keep the model loaded so later calls are fast
                "options": {"temperature": 0, "num_predict": 60},
            },
            timeout=LLM_TIMEOUT,
        )
        r.raise_for_status()
        query = json.loads(r.json()["response"])["query"].strip().strip('"')
        if 2 <= len(query) <= 80:
            return query, True
    except Exception:
        pass
    return fallback_query(sentence), False


# ----------------------------------------------------------------------------
# 2. Open image sources. Each returns a list of dicts with the same keys:
#    thumb, full, page, title, creator, license, source
# ----------------------------------------------------------------------------
ARTWORK_WORDS = (
    "painting", "engraving", "etching", "lithograph", "woodcut", "watercolour",
    "watercolor", "oil on canvas", "fresco", "mural", "drawing", "sketch",
    "illustration", "diagram", "coat of arms", "woodblock", "aquatint",
)
"""Words that mean "this is a picture of a picture", not a photograph.

Commons is an archive, so a search for "stormy sea" happily returns nineteenth
century oil paintings. Pasting one into a report is usually not what you meant.
"""

ARTWORK_CATEGORIES = re.compile(
    r"\b(paintings?|drawings?|engravings?|prints?|etchings?|lithographs?|"
    r"watercolou?rs?|illustrations?|sketches|woodcuts?|artworks?|"
    r"marine art|oil on canvas|fresco(?:es)?|murals?|tapestr(?:y|ies))\b",
    re.IGNORECASE,
)
"""Commons categories are a far better signal than titles.

"The Irwin Lighthouse, Storm Raging by John Wilson Carmichael" says nothing in
its title, but it sits in "19th-century oil marine paintings".
"""

_BY_ARTIST = re.compile(
    # Commons names a specific *work* as "<title> by <artist>" - and the same
    # artwork can be uploaded several times under unrelated file names. When a
    # candidate's categories say "Stürmische See mit Leuchtturm by Carl
    # Blechen", it is a painting no matter what its file title calls it.
    r"\bby\s+[A-Z][A-Za-z'’\-]+(?:\s[A-Z][A-Za-z'’\-]+){0,3}\s*$",
)


def is_artwork(title: str) -> bool:
    """Does this title look like a painting, drawing or diagram?"""
    text = title.lower().replace("_", " ")
    return any(re.search(rf"\b{re.escape(word)}\b", text) for word in ARTWORK_WORDS)


def is_artwork_category(categories: list[str]) -> bool:
    return any(
        ARTWORK_CATEGORIES.search(c) or _BY_ARTIST.search(c) for c in categories
    )


def _commons_candidate(p: dict) -> dict | None:
    """One Commons file page -> one candidate dict (None for unusable pages)."""
    info = (p.get("imageinfo") or [None])[0]
    if not info or info.get("mime") not in ("image/jpeg", "image/png"):
        return None
    meta = info.get("extmetadata", {})
    licence = _strip_html(
        meta.get("LicenseShortName", {}).get("value", "")
        or meta.get("License", {}).get("value", "")
        or meta.get("UsageTerms", {}).get("value", "")
    )
    thumb = info.get("thumburl") or info.get("url")
    if not thumb:
        return None
    title = p.get("title", "").removeprefix("File:").rsplit(".", 1)[0]
    categories = [c.get("title", "") for c in p.get("categories", [])]
    # A category named "<something> by <artist>" names one specific *work*
    # of art; two uploads of the same painting carry the same category, so
    # it doubles as an identity key.
    work_key = ""
    for cat in categories:
        if _BY_ARTIST.search(cat):
            work_key = _norm_key_text(cat)
            break
    return {
        "thumb": thumb,
        # The original can be enormous, so the fallback is the preview
        # rendering rather than the source file. full_image() asks Commons
        # for a 1600px version when it can.
        "full": thumb,
        "page": info.get("descriptionurl", ""),
        "file_title": p.get("title", ""),
        "title": title,
        "creator": _strip_html(meta.get("Artist", {}).get("value", "")) or "Unknown",
        "license": licence,
        "source": "Wikimedia Commons",
        "work_key": work_key,
        "artwork": is_artwork(title) or is_artwork_category(categories),
    }


def _prefer_photographs(candidates: list[dict]) -> list[dict]:
    """Drop artwork unless the pool would otherwise be empty."""
    if os.getenv("EZSEARCH_ALLOW_ART", "0") != "1":
        photos = [c for c in candidates if not c.get("artwork")]
        if photos:
            return photos
    return candidates


def _commons_pages(params: dict, n: int) -> list[dict]:
    """Run a Commons batch query and return the mapped, de-artworked pages."""
    params = {"action": "query", "format": "json",
              "prop": "imageinfo|categories", "iiprop": "url|extmetadata|mime|size",
              "iiurlwidth": PREVIEW_WIDTH, "cllimit": "max", "clshow": "!hidden",
              **params}
    r = requests.get("https://commons.wikimedia.org/w/api.php", params=params,
                     headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    pages = (r.json().get("query") or {}).get("pages", {})
    ranked = sorted(pages.values(), key=lambda p: p.get("index", p.get("title", "")))
    out = [c for c in (_commons_candidate(p) for p in ranked) if c]
    return _prefer_photographs(out)[:n]


def search_commons(query: str, n: int) -> list[dict]:
    """Wikimedia Commons: no API key, generous limits, every file has a licence."""
    # Ask for more than we need: roughly half of a Commons result page is
    # paintings and engravings, and those get filtered out below.
    fetch = min(max(n * 2, 12), 50)
    return _commons_pages({
        "generator": "search", "gsrnamespace": 6, "gsrlimit": fetch,
        "gsrsearch": f"{query} filetype:bitmap",
    }, n)


_TRAFFIC_OR = ("(roundabout OR junction OR circle OR crossing OR intersection"
               " OR road OR street OR traffic OR bridge OR highway OR transport)")
"""Nouns that get a *place* into Commons' road/transport categories."""


def commons_category_files(query: str, n: int) -> list[dict]:
    """Mine Commons *category* space when filename search comes up dry.

    Commons files are rarely named after what they show: "roundabout
    chandigarh" has neither file hits nor a category with those words, but the
    photographs live in category space - "Road bridges in Chandigarh",
    "National highways in Chandigarh" and friends. Search that namespace for
    the query's location word plus traffic nouns, walk the best-matching
    categories, and return their files under the same rules as any other
    Commons answer (licences, artwork filter, same dict shape).
    """
    words = [w for w in query.split() if w.lower() not in STOPWORDS]
    if len(words) < 2:          # a lone keyword: file search is already enough
        return []
    place = words[-1].lower()

    try:
        r = requests.get("https://commons.wikimedia.org/w/api.php", params={
            "action": "query", "format": "json", "list": "search",
            "gsrsearch": f"{words[-1]} {_TRAFFIC_OR}", "gsrnamespace": 14,
            "gsrlimit": 30, "srprop": "",
        }, headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        hits = [h.get("title", "") for h in (r.json().get("query") or {}).get("search", [])]
    except Exception:
        # Category mining is a bonus; a network hiccup must not sink the search.
        return []

    def score(cat: str) -> int:
        text = cat.lower()
        if place not in text:
            return -999         # other cities' categories stay out
        value = 0
        if re.search(r"roundabout|junction|circle|crossing|intersection", text):
            value += 3          # the closest thing to the user's noun first
        if re.search(r"\broad|street|traffic|transport|highway|bridge", text):
            value += 2
        if re.search(r"rail|railway|bus|metro", text):
            value -= 2          # rail/bus stops are not road scenes
        return value

    ranked_cats = [c for c in sorted(hits, key=score, reverse=True) if score(c) > 0]
    out: list[dict] = []
    for cat in ranked_cats[:2]:
        try:
            got = _commons_pages({
                "generator": "categorymembers", "gcmtitle": cat,
                "gcmtype": "file", "gcmlimit": str(min(max(n, 12), 40)),
            }, n)
        except Exception:
            continue
        for c in got:
            out.append(c)
            if len(out) >= n:
                return out
    return out


_token = {"value": None, "expires": 0.0}
_openverse_cooldown = {"until": 0.0}


def _openverse_token():
    cid, secret = os.getenv("OPENVERSE_CLIENT_ID"), os.getenv("OPENVERSE_CLIENT_SECRET")
    if not (cid and secret):
        return None
    if _token["value"] and time.time() < _token["expires"]:
        return _token["value"]
    try:
        r = requests.post(
            "https://api.openverse.org/v1/auth_tokens/token/",
            data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret},
            headers={"User-Agent": UA}, timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        # A bad or expired key should not take the source down - fall back to
        # the anonymous quota instead.
        return None
    _token["value"] = data["access_token"]
    _token["expires"] = time.time() + int(data.get("expires_in", 3600)) - 60
    return _token["value"]


def search_openverse(query: str, n: int) -> list[dict]:
    """Openverse: bigger catalogue (Flickr etc.).

    Openverse allows anonymous requests (about 200 a day at the time of
    writing); a free client key raises the limit. If a key is configured we
    use it, otherwise we ask anonymously.
    """
    if os.getenv("EZSEARCH_OPENVERSE", "1") == "0":
        raise RuntimeError("Openverse disabled by EZSEARCH_OPENVERSE=0")
    if time.time() < _openverse_cooldown["until"]:
        raise RuntimeError("Openverse is cooling down after a failure")

    token = _openverse_token()
    headers = {"User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # Anonymous callers are capped at 20 results per page; authenticated ones
    # can ask for more. Asking for 24 anonymously is a 401, not an empty page.
    page_size = min(n, 500 if token else 20)
    timeout = int(os.getenv("EZSEARCH_OPENVERSE_TIMEOUT", "8"))

    params = {"q": query, "mature": "false", "license_type": "commercial"}
    if os.getenv("EZSEARCH_OPENVERSE_PHOTOS", "1") == "1":
        params["category"] = "photograph"

    # Openverse has slow stretches; wait a little longer and try twice before
    # declaring the source down for the minute.
    results = None
    last_error: Exception | None = None
    for attempt in range(2):
        if "category" not in params and os.getenv("EZSEARCH_OPENVERSE_PHOTOS", "1") == "1":
            params["category"] = "photograph"       # restored for the retry
        try:
            r = requests.get(
                "https://api.openverse.org/v1/images/",
                params={**params, "page_size": page_size},
                headers=headers, timeout=timeout,
            )
            r.raise_for_status()
            results = r.json().get("results", [])
            if not results and "category" in params:
                # The photograph filter was too narrow for this query - try again wider.
                params.pop("category")
                r = requests.get(
                    "https://api.openverse.org/v1/images/",
                    params={**params, "page_size": page_size},
                    headers=headers, timeout=timeout,
                )
                r.raise_for_status()
                results = r.json().get("results", [])
            break
        except Exception as e:
            last_error = e
            time.sleep(1.5)
    if results is None:
        # Do not make every search pay the timeout: skip Openverse for a while.
        # Short lockout: an anonymous Openverse failure once trapped the app in
        # a ten-minute Commons-only grid, which looked like "the app is broken"
        # to the user sitting in front of it. A minute cools a hiccup down.
        _openverse_cooldown["until"] = time.time() + 60
        raise last_error or RuntimeError("Openverse request failed")
    out = []
    for it in results:
        licence = f'{it.get("license", "").upper()} {it.get("license_version", "")}'.strip()
        thumb = it.get("thumbnail") or it.get("url")
        if not thumb:
            continue
        out.append({
            "thumb": thumb,
            "full": it.get("url", ""),
            "page": it.get("foreign_landing_url") or it.get("url", ""),
            "file_title": "",
            "title": it.get("title") or "",
            "creator": it.get("creator") or "Unknown",
            "license": licence,
            "source": "Openverse",
        })
    return out


def _norm_key_text(text: str) -> str:
    """Lower-case alphanumeric identity for a title or a Commons category."""
    text = text.strip()
    text = re.sub(r"^category:", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^file:", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\.(jpe?g|png|gif|tiff?|webp|bmp|svg)$", "",
                  text, flags=re.IGNORECASE)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return " ".join(text.split())


def _norm_title_key(c: dict) -> str:
    """Cheap identity for "the same picture twice".

    The same artwork is uploaded to Commons several times under unrelated file
    names ("Asleep in the deep (NYPL Hades-464375-1710679)" and three siblings
    that differ only in the numeric ids). Lowercasing and throwing away the
    parenthesised ids collapses those.
    """
    return _norm_key_text(c.get("file_title") or c.get("title") or "")


def _dedupe(candidates: list[dict]) -> list[dict]:
    seen_pages, seen_images, seen_titles, seen_works = set(), set(), {}, set()
    out = []
    for c in candidates:
        page = c.get("page") or ""
        image = c.get("thumb") or ""
        # The title check only compares inside one source: another source may
        # legitimately have an identical title for a different picture.
        source = c.get("source") or ""
        title_key = _norm_title_key(c)
        work = c.get("work_key") or ""
        if (page and page in seen_pages) or (image and image in seen_images) or (
                title_key and title_key in seen_titles.get(source, ())) or (
                work and work in seen_works):
            continue
        if page:
            seen_pages.add(page)
        if image:
            seen_images.add(image)
        if title_key:
            seen_titles.setdefault(source, set()).add(title_key)
        if work:
            seen_works.add(work)
        out.append(c)
    return out


def _search_all(query: str, n: int, allow_any_licence: bool) -> list[dict]:
    """Ask every configured source at once and merge the answers.

    Commons needs no key and always answers; Openverse adds Flickr and friends
    and also works anonymously, on a small daily quota. When both answer we
    interleave them so that one source cannot crowd the other out of the grid.
    """
    def commons():
        for attempt in range(2):            # a Commons hiccup must not silently
            try:                            # drop the bigger source from the grid
                return search_commons(query, n)
            except Exception:
                time.sleep(1.0 + attempt)
        return []

    def openverse():
        try:
            return search_openverse(query, n)
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=2) as pool:
        commons_future = pool.submit(commons)
        openverse_future = pool.submit(openverse)
        commons_results = commons_future.result()
        openverse_results = openverse_future.result()

    merged: list[dict] = []
    for a, b in zip(commons_results, openverse_results):
        merged.extend([a, b])
    merged.extend(commons_results[len(openverse_results):])
    merged.extend(openverse_results[len(commons_results):])

    if not allow_any_licence:
        free = [c for c in merged if licence_ok(c.get("license", ""))]
        # Better to show something with an unclear licence than nothing at all.
        return _dedupe(free if free else merged)
    return _dedupe(merged)


MIN_POOL = 6
"""A pool below this is not a choice, only a verdict: keep rewriting the query."""

TOPICAL_MIN = 2
"""A few on-topic hits beat pages of lookalikes: they lead the grid."""


def resolve_query(query: str, n: int = N_CANDIDATES,
                  allow_any_licence: bool = False) -> tuple[str, list[dict]]:
    """Search, preferring topical precision, then variety.

    The first two variants (the query as given, and the same words without
    filler) are what the user *meant*: they lead the grid. If filename search
    alone finds little - "roundabout chandigarh" matches one Commons file -
    Commons' category space is mined as well. Once those topical hits exist,
    the nearest broader phrasing tops the pool up so "show more" still has
    pages. Only when nothing topical comes back do the broader prefixes play
    their old role: the biggest pool wins.
    """
    variants = query_variants(query)
    topical, broader = variants[:2], variants[2:]

    pool: list[dict] = []
    pool_variant = topical[0] if topical else query
    for v in topical:
        got = _search_all(v, n, allow_any_licence)
        if got:
            pool = _dedupe(pool + got)
            pool_variant = v
        if len(pool) >= MIN_POOL:
            return v, pool[:n]
    if pool:
        # Filename search alone misses categories (see commons_category_files).
        pool = _dedupe(pool + commons_category_files(query, n))
    if pool and (len(pool) >= TOPICAL_MIN or not broader):
        for v in broader:       # top the topical head up for variety
            got = _search_all(v, min(n, 20), allow_any_licence)
            pool = _dedupe(pool + got)
            if len(pool) >= MIN_POOL:
                break
        return pool_variant, pool[:n]

    best: list[dict] = []
    best_variant = query
    for v in broader:
        candidates = _search_all(v, min(n, 20), allow_any_licence)
        if len(candidates) > len(best):
            best, best_variant = candidates, v
        if len(candidates) >= MIN_POOL:
            return v, candidates
    return best_variant, best


def search_images(query: str, n: int = N_CANDIDATES, allow_any_licence: bool = False) -> list[dict]:
    """Find candidate pictures for a query, shortening it if nothing comes back."""
    return resolve_query(query, n, allow_any_licence)[1]


# ----------------------------------------------------------------------------
# 3. Downloads, with a disk cache so the same picture is never fetched twice
# ----------------------------------------------------------------------------
def _cache_root() -> Path:
    root = CACHE_DIR / "images"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cache_path(url: str) -> Path:
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return _cache_root() / f"{key}.bin"


def _get_bytes(url: str, timeout: int = 15, max_bytes: int = 25_000_000) -> bytes:
    """Download a URL, refusing anything unreasonably large.

    Commons originals can be 50 MB TIFFs and Openverse links to Flickr
    originals, which is not something to paste into a document.
    """
    with requests.get(url, headers={"User-Agent": UA}, timeout=timeout, stream=True) as r:
        r.raise_for_status()
        length = r.headers.get("Content-Length")
        if length and int(length) > max_bytes:
            raise RuntimeError(f"image is too large ({int(length) // 1_000_000} MB)")
        data = r.content
    if len(data) > max_bytes:
        raise RuntimeError("image is too large")
    return data


def fetch_cached(url: str, timeout: int = 15) -> bytes:
    """Download a URL once; every later call reads from the local cache."""
    path = _cache_path(url)
    if path.exists() and path.stat().st_size:
        try:
            return path.read_bytes()
        except OSError:
            pass
    data = _get_bytes(url, timeout=timeout)
    try:
        path.write_bytes(data)
    except OSError:
        pass
    return data


def load_previews(cands: list[dict]) -> list[dict]:
    """Download all previews in parallel; drop the ones that fail."""
    def one(c):
        try:
            c["img"] = Image.open(io.BytesIO(fetch_cached(c["thumb"]))).convert("RGB")
            return c
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        return [c for c in pool.map(one, cands) if c]


_commons_full_cache: dict[str, str] = {}


def _commons_full_url(file_title: str) -> str:
    """Ask Commons for a bigger rendering of a file we already found."""
    if not file_title:
        return ""
    if file_title in _commons_full_cache:
        return _commons_full_cache[file_title]
    url = ""
    try:
        r = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query", "format": "json",
                "titles": file_title, "prop": "imageinfo",
                "iiprop": "url", "iiurlwidth": FULL_WIDTH,
            },
            headers={"User-Agent": UA}, timeout=10,
        )
        r.raise_for_status()
        pages = (r.json().get("query") or {}).get("pages", {})
        for page in pages.values():
            info = (page.get("imageinfo") or [None])[0]
            if info:
                url = info.get("thumburl") or info.get("url", "")
                break
    except Exception:
        url = ""
    _commons_full_cache[file_title] = url
    return url


def full_image(c: dict) -> Image.Image:
    """The picture that actually gets pasted (max 1600px so documents stay light)."""
    url = ""
    if c.get("source") == "Wikimedia Commons":
        url = _commons_full_url(c.get("file_title", ""))
    if not url:
        url = c.get("full") or c.get("thumb") or ""
    try:
        img = Image.open(io.BytesIO(fetch_cached(url, timeout=25))).convert("RGB")
    except Exception:
        # Too big, gone, or the network blinked - the preview is always safe.
        img = c.get("img")
        if img is None:
            raise
    img = img.copy()
    img.thumbnail((FULL_WIDTH, FULL_WIDTH))
    return img


# ----------------------------------------------------------------------------
# 4. CLIP re-ranker (open-weight model running locally through ONNX)
# ----------------------------------------------------------------------------
class Ranker:
    """Loads CLIP in a background thread so the app starts instantly."""

    def __init__(self, cache_dir: Path | None = None):
        self.text_model = None
        self.image_model = None
        self.error = None
        self._text_cache: dict[str, np.ndarray] = {}
        self._vector_dir = (cache_dir or CACHE_DIR) / "vectors"
        self._lock = threading.Lock()
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            from fastembed import ImageEmbedding, TextEmbedding
            self.text_model = TextEmbedding("Qdrant/clip-ViT-B-32-text")
            self.image_model = ImageEmbedding("Qdrant/clip-ViT-B-32-vision")
        except Exception as e:  # first run needs internet to download the weights once
            self.error = str(e)

    @property
    def ready(self) -> bool:
        return self.text_model is not None and self.image_model is not None

    def wait_ready(self, timeout: float = 30.0) -> bool:
        """Block until CLIP is loaded. Returns False on timeout."""
        deadline = time.time() + timeout
        while not self.ready and time.time() < deadline:
            time.sleep(0.2)
        return self.ready

    # -- embedding caches ---------------------------------------------------
    def _embed_text(self, text: str) -> np.ndarray:
        key = text[:300]
        with self._lock:
            if key in self._text_cache:
                return self._text_cache[key]
        vec = np.array(list(self.text_model.embed([key])))[0].astype("float32")
        vec = vec / (np.linalg.norm(vec) or 1.0)
        with self._lock:
            self._text_cache[key] = vec
        return vec

    def _vector_path(self, url: str) -> Path:
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self._vector_dir / f"{key}.npy"

    def _load_vector(self, c: dict) -> np.ndarray | None:
        """Return a cached vector for this candidate, if there is a usable one."""
        url = c.get("thumb", "")
        if not url:
            return None
        path = self._vector_path(url)
        if not path.exists():
            return None
        try:
            cached = np.load(path)
        except Exception:
            return None
        return cached if np.any(cached) else None      # never trust a zero vector

    def _save_vector(self, c: dict, vec: np.ndarray) -> None:
        url = c.get("thumb", "")
        if not url:
            return
        try:
            self._vector_dir.mkdir(parents=True, exist_ok=True)
            np.save(self._vector_path(url), vec)
        except Exception:
            pass

    def embed_images(self, cands: list[dict]) -> list[tuple[dict, np.ndarray]]:
        """Embed several pictures in one ONNX batch.

        fastembed's session is much faster with a batch than with one image at
        a time (roughly 2x on a laptop CPU), so the pipeline embeds pictures in
        small groups as they arrive.
        """
        pairs: list[tuple[dict, np.ndarray]] = []
        todo: list[dict] = []
        for c in cands:
            cached = self._load_vector(c)
            if cached is not None:
                pairs.append((c, cached))
            elif c.get("img") is not None:
                todo.append(c)
        if not todo:
            return pairs

        vectors: list[np.ndarray | None]
        try:
            vectors = [np.asarray(v, dtype="float32")
                       for v in self.image_model.embed([c["img"] for c in todo])]
            if len(vectors) != len(todo):
                raise RuntimeError("the model returned the wrong number of vectors")
        except Exception:
            # One bad picture must not lose the whole batch: retry one by one.
            vectors = []
            for c in todo:
                try:
                    vectors.append(np.asarray(
                        list(self.image_model.embed([c["img"]]))[0], dtype="float32"))
                except Exception as e:
                    self.error = f"image embedding failed: {e}"
                    vectors.append(None)

        for c, vec in zip(todo, vectors):
            if vec is None:
                continue
            vec = vec / (np.linalg.norm(vec) or 1.0)
            if not np.any(vec):
                continue
            self._save_vector(c, vec)
            pairs.append((c, vec))
        return pairs

    def embed_image(self, c: dict) -> np.ndarray | None:
        """Embed one candidate, reusing a vector we have already computed."""
        pairs = self.embed_images([c])
        return pairs[0][1] if pairs else None

    # -- ranking ------------------------------------------------------------
    def score(self, sentence: str, vectors: list[tuple[dict, np.ndarray]],
              query: str = "") -> list[dict]:
        """Turn image vectors into a ranked list."""
        if not vectors:
            return []
        sentence_vec = self._embed_text(sentence[:300])
        img_vecs = np.array([v for _, v in vectors], dtype="float32")

        if query:
            query_vec = self._embed_text(query[:300])
            weight = float(np.clip(QUERY_WEIGHT, 0.0, 1.0))
            scores = (1 - weight) * (img_vecs @ sentence_vec) + weight * (img_vecs @ query_vec)
        else:
            scores = img_vecs @ sentence_vec

        for (c, _), score in zip(vectors, scores):
            c["score"] = float(score)
        return [c for c, _ in sorted(vectors, key=lambda pair: -pair[0].get("score", 0.0))]

    def rank(self, sentence: str, cands: list[dict], query: str = "") -> list[dict]:
        """Re-rank candidates that already have their previews loaded."""
        if not self.ready or not cands:
            for c in cands:
                c.setdefault("score", 0.0)
            return cands
        return self.score(sentence, self.embed_images(cands), query)


# ----------------------------------------------------------------------------
# 4b. Download and rank at the same time
# ----------------------------------------------------------------------------
def fetch_preview(c: dict) -> Image.Image:
    """Get one candidate's preview, reusing it if it was already decoded."""
    img = c.get("img")
    if img is not None:
        return img
    data = fetch_cached(c["thumb"])
    return Image.open(io.BytesIO(data)).convert("RGB")


def _ahash(img: Image.Image) -> int | None:
    """A 64-bit average hash of the preview. None when it cannot be computed."""
    try:
        grey = list(img.convert("L").resize((8, 8), Image.LANCZOS).getdata())
    except Exception:
        return None
    average = sum(grey) // len(grey)
    value = 0
    for pixel in grey:
        value = (value << 1) | (pixel >= average)
    return value


def _looks_like_same_image(hashes: list[int], h: int | None) -> bool:
    if h is None:
        return False
    return any((h ^ seen).bit_count() <= 2 for seen in hashes)


def rank_candidates(ranker, sentence: str, query: str, cands: list[dict],
                    limit: int | None = None) -> list[dict]:
    """Fetch previews and score them, overlapping the network with the CPU.

    Downloads run in parallel, and pictures are embedded in small batches as
    they arrive. Both halves matter: one-image-at-a-time ONNX calls were the
    single slowest thing in the app, and batching them is roughly a 2x win.

    Every picture gets a CLIP score. Whether that score also reorders the grid
    is controlled by EZSEARCH_RERANK (off by default).
    """
    if not cands:
        return []
    if not ranker.ready:
        previews = load_previews(cands)
        for c in previews:
            c.setdefault("score", 0.0)
        return previews[:limit] if limit else previews

    vectors: list[tuple[dict, np.ndarray]] = []
    batch: list[dict] = []

    def flush() -> None:
        if batch:
            vectors.extend(ranker.embed_images(batch))
            batch.clear()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_preview, c): c for c in cands}
        seen_hashes: list[int] = []         # the search APIs happily return the
        for future in as_completed(futures):  # same picture twice under other names
            c = futures[future]
            try:
                c["img"] = future.result()
            except Exception:
                continue
            image_hash = _ahash(c["img"])
            if _looks_like_same_image(seen_hashes, image_hash):
                continue                    # a tile that looks identical adds noise
            if image_hash is not None:
                seen_hashes.append(image_hash)
            batch.append(c)
            if len(batch) >= EMBED_BATCH:
                flush()
    flush()

    ranked = ranker.score(sentence, vectors, query)
    if not RERANK:
        # Keep the search engine's order; CLIP scores are only used for the
        # match bar and the confidence warning.
        scored = [c for c in cands if "score" in c]
        return scored[:limit] if limit else scored
    return ranked[:limit] if limit else ranked


# ----------------------------------------------------------------------------
# 5. Put an image on the system clipboard (so Ctrl+V pastes a real picture)
# ----------------------------------------------------------------------------
def _windows_copy_image(img: Image.Image) -> None:
    import win32clipboard

    buf = io.BytesIO()
    img.convert("RGB").save(buf, "BMP")
    dib = buf.getvalue()[14:]          # strip the 14-byte BMP file header -> raw DIB
    for attempt in range(5):
        try:
            win32clipboard.OpenClipboard()
            break
        except Exception:
            time.sleep(0.1 * (attempt + 1))
    else:
        raise RuntimeError("could not open the clipboard")
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib)
    finally:
        win32clipboard.CloseClipboard()


def _macos_copy_image(img: Image.Image) -> None:
    path = os.path.join(tempfile.gettempdir(), "ezsearch_clip.png")
    img.save(path, "PNG")
    subprocess.run(
        ["osascript", "-e",
         f'set the clipboard to (read (POSIX file "{path}") as «class PNGf»)'],
        check=True)


def _linux_copy_image(img: Image.Image) -> None:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    subprocess.run(["xclip", "-selection", "clipboard", "-t", "image/png"],
                   input=buf.getvalue(), check=True)


def copy_image(img: Image.Image) -> None:
    if SYSTEM == "Windows":
        _windows_copy_image(img)
    elif SYSTEM == "Darwin":
        _macos_copy_image(img)
    else:
        _linux_copy_image(img)


def word_selection_paste() -> bool:
    """Paste the clipboard into the running Word, at its caret, over COM.

    If Word ignores synthesised Ctrl+V as silently as it can ignore a
    synthesised Ctrl+C, this is the way the picture still lands: automation
    does not depend on keyboard focus at all. Return True when the paste
    happened, so the caller can skip sending any keys.
    """
    if SYSTEM != "Windows":
        return False
    try:
        import pythoncom
        pythoncom.CoInitialize()      # per-thread; see selection fallback
    except Exception:
        return False
    try:
        import win32com.client
        word = win32com.client.GetActiveObject("Word.Application")
        word.ActiveWindow.Selection.Paste()
        return True
    except Exception:
        return False
    finally:
        pythoncom.CoUninitialize()
