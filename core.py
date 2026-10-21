"""
flowsearch_core.py - the "brain" of FlowSearch (no GUI code in here).

Pipeline for one sentence:
  1. LOCAL LLM (Ollama, open-weight)  -> turns a sentence into a short image-search query
  2. OPEN IMAGE SOURCES               -> Wikimedia Commons (no key) or Openverse (free key)
  3. LOCAL CLIP (open-weight, ONNX)   -> re-ranks candidates by how well they match the WHOLE sentence
  4. Clipboard helpers                -> put the chosen picture on the clipboard as an image
"""
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
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests
from PIL import Image

# ----------------------------------------------------------------------------
# Config (override with environment variables, no code edits needed)
# ----------------------------------------------------------------------------
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
LLM_MODEL = os.getenv("FLOWSEARCH_MODEL", "llama3.2:3b")
N_CANDIDATES = 15          # how many images we fetch before re-ranking
UA = "FlowSearch/0.1 (DEV challenge project; local desktop tool)"
SYSTEM = platform.system()  # "Windows", "Darwin" (macOS) or "Linux"

STOPWORDS = set(
    "a an the and or of to in on at for with by is are was were be been it its this that these "
    "those as from into about over under than then so such very just also can could should would "
    "will may might has have had do does did not no we our you your they their he she his her i my".split()
)


# ----------------------------------------------------------------------------
# 1. Sentence -> search query  (local LLM, with a no-AI fallback)
# ----------------------------------------------------------------------------
def fallback_query(sentence: str) -> str:
    """Used when Ollama is not running: keep the first few meaningful words."""
    words = [w for w in re.findall(r"[A-Za-z']+", sentence.lower())
             if w not in STOPWORDS and len(w) > 2]
    return " ".join(words[:5]) or sentence[:40]


def build_query(sentence: str) -> tuple[str, bool]:
    """Returns (query, used_llm)."""
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
                "model": LLM_MODEL,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "keep_alive": "30m",             # keep the model loaded so later calls are fast
                "options": {"temperature": 0, "num_predict": 60},
            },
            timeout=25,
        )
        r.raise_for_status()
        query = json.loads(r.json()["response"])["query"].strip()
        if 2 <= len(query) <= 80:
            return query, True
    except Exception:
        pass
    return fallback_query(sentence), False


# ----------------------------------------------------------------------------
# 2. Open image sources. Each returns a list of dicts with the same keys:
#    thumb, full, page, title, creator, license
# ----------------------------------------------------------------------------
def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def search_commons(query: str, n: int) -> list[dict]:
    """Wikimedia Commons: no API key, generous limits, every file has a license."""
    params = {
        "action": "query", "format": "json", "origin": "*",
        "generator": "search", "gsrnamespace": 6, "gsrlimit": n,
        "gsrsearch": f"{query} filetype:bitmap",
        "prop": "imageinfo", "iiprop": "url|extmetadata|mime", "iiurlwidth": 1000,
    }
    r = requests.get("https://commons.wikimedia.org/w/api.php", params=params,
                     headers={"User-Agent": UA}, timeout=10)
    r.raise_for_status()
    pages = (r.json().get("query") or {}).get("pages", {}).values()
    out = []
    for p in sorted(pages, key=lambda p: p.get("index", 0)):
        info = (p.get("imageinfo") or [None])[0]
        if not info or info.get("mime") not in ("image/jpeg", "image/png"):
            continue
        meta = info.get("extmetadata", {})
        out.append({
            "thumb": info["thumburl"], "full": info["thumburl"],
            "page": info["descriptionurl"],
            "title": p.get("title", "").removeprefix("File:"),
            "creator": _strip_html(meta.get("Artist", {}).get("value", "")) or "Unknown",
            "license": meta.get("LicenseShortName", {}).get("value", ""),
        })
    return out


_token = {"value": None, "expires": 0.0}


def _openverse_token():
    cid, secret = os.getenv("OPENVERSE_CLIENT_ID"), os.getenv("OPENVERSE_CLIENT_SECRET")
    if not (cid and secret):
        return None
    if _token["value"] and time.time() < _token["expires"]:
        return _token["value"]
    r = requests.post(
        "https://api.openverse.org/v1/auth_tokens/token/",
        data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret},
        headers={"User-Agent": UA}, timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    _token["value"] = data["access_token"]
    _token["expires"] = time.time() + int(data.get("expires_in", 3600)) - 60
    return _token["value"]


def search_openverse(query: str, n: int) -> list[dict]:
    """Openverse: bigger catalogue (Flickr etc.). Needs a free key for useful rate limits."""
    token = _openverse_token()
    if not token:
        raise RuntimeError("no Openverse key set")
    r = requests.get(
        "https://api.openverse.org/v1/images/",
        params={"q": query, "page_size": n, "mature": "false"},
        headers={"User-Agent": UA, "Authorization": f"Bearer {token}"}, timeout=10,
    )
    r.raise_for_status()
    out = []
    for it in r.json().get("results", []):
        lic = f'{it.get("license", "").upper()} {it.get("license_version", "")}'.strip()
        out.append({
            "thumb": it.get("thumbnail") or it["url"], "full": it["url"],
            "page": it.get("foreign_landing_url") or it["url"],
            "title": it.get("title") or "", "creator": it.get("creator") or "Unknown",
            "license": lic,
        })
    return out


def search_images(query: str, n: int = N_CANDIDATES) -> list[dict]:
    try:
        results = search_openverse(query, n)
        if results:
            return results
    except Exception:
        pass  # no key / rate limited -> fall back to Commons
    return search_commons(query, n)


def credit_text(c: dict) -> str:
    return f'"{c["title"]}" by {c["creator"]} ({c["license"]}) - {c["page"]}'


def _get_bytes(url: str, timeout: int = 10) -> bytes:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    return r.content


def load_thumbs(cands: list[dict]) -> list[dict]:
    """Download all thumbnails in parallel; drop the ones that fail."""
    def one(c):
        try:
            c["img"] = Image.open(io.BytesIO(_get_bytes(c["thumb"]))).convert("RGB")
            return c
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        return [c for c in pool.map(one, cands) if c]


def full_image(c: dict) -> Image.Image:
    """The picture that actually gets pasted (max 1600px so documents stay light)."""
    try:
        img = Image.open(io.BytesIO(_get_bytes(c["full"], timeout=20))).convert("RGB")
    except Exception:
        img = c["img"]
    img.thumbnail((1600, 1600))
    return img


# ----------------------------------------------------------------------------
# 3. CLIP re-ranker (open-weight model running locally through ONNX)
# ----------------------------------------------------------------------------
class Ranker:
    """Loads CLIP in a background thread so the app starts instantly."""

    def __init__(self):
        self.text_model = None
        self.image_model = None
        self.error = None
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

    def rank(self, sentence: str, cands: list[dict]) -> list[dict]:
        if not self.ready or not cands:
            return cands
        text_vec = np.array(list(self.text_model.embed([sentence[:200]])))[0].astype("float32")
        img_vecs = np.array(list(self.image_model.embed([c["img"] for c in cands]))).astype("float32")
        text_vec /= np.linalg.norm(text_vec)
        img_vecs /= np.linalg.norm(img_vecs, axis=1, keepdims=True)
        for c, score in zip(cands, img_vecs @ text_vec):
            c["score"] = float(score)
        return sorted(cands, key=lambda c: -c["score"])


# ----------------------------------------------------------------------------
# 4. Put an image on the system clipboard (so Ctrl+V pastes a real picture)
# ----------------------------------------------------------------------------
def copy_image(img: Image.Image) -> None:
    if SYSTEM == "Windows":
        import win32clipboard
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "BMP")
        dib = buf.getvalue()[14:]          # strip the 14-byte BMP file header -> raw DIB
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib)
        finally:
            win32clipboard.CloseClipboard()
    elif SYSTEM == "Darwin":
        path = os.path.join(tempfile.gettempdir(), "flowsearch_clip.png")
        img.save(path, "PNG")
        subprocess.run(
            ["osascript", "-e",
             f'set the clipboard to (read (POSIX file "{path}") as «class PNGf»)'],
            check=True)
    else:  # Linux (needs: sudo apt install xclip)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        subprocess.run(["xclip", "-selection", "clipboard", "-t", "image/png"],
                       input=buf.getvalue(), check=True)