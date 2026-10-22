"""
Offline tests for the parts of EZsearch that decide *what* gets pasted.

Nothing here touches the network, the CLIP weights or Ollama - the point is
that the logic is pinned down and cheap to check.
"""
from __future__ import annotations

import json

import pytest

import ezsearch_core as core


# ----------------------------------------------------------------------------
# Query building
# ----------------------------------------------------------------------------
def test_fallback_query_drops_stopwords():
    query = core.fallback_query("The old lighthouse stood against the storm")
    assert "the" not in query.split()
    assert "old" in query.split()
    assert len(query.split()) <= 6


def test_fallback_query_never_returns_empty():
    assert core.fallback_query("!!!") != ""
    assert core.fallback_query("") == ""


def test_first_sentence_keeps_the_subject():
    text = "A lighthouse stood against the storm. The harbour was empty afterwards."
    assert core.first_sentence(text) == "A lighthouse stood against the storm."


def test_first_sentence_leaves_short_text_alone():
    assert core.first_sentence("One short line.") == "One short line."


def test_first_sentence_of_empty_text():
    assert core.first_sentence("") == ""
    assert core.first_sentence("   ") == ""


# ----------------------------------------------------------------------------
# Licence filtering
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("licence", [
    "CC BY 4.0",
    "CC BY-SA 4.0",
    "CC0",
    "Public domain",
    "Attribution",
    "GFDL",
    "BY 4.0",              # Openverse omits the "CC"
    "BY-SA 4.0",           # ... and this one too
    "CC BY 2.5 FR",
])
def test_free_licences_are_allowed(licence):
    assert core.licence_ok(licence) is True


@pytest.mark.parametrize("licence", [
    "CC BY-NC 4.0",
    "CC BY-ND 4.0",
    "CC BY-NC-ND 4.0",
    "Attribution-NonCommercial-NoDerivs",
    "Copyrighted free use",
    "Fair use",
    "All rights reserved",
    "",
])
def test_restrictive_licences_are_rejected(licence):
    assert core.licence_ok(licence) is False


def test_licence_check_is_token_based_not_substring():
    # "nd" must not match inside an innocent word.
    assert core.licence_ok("CC BY 4.0") is True
    assert core.licence_ok("Unknown licence") is False


# ----------------------------------------------------------------------------
# Attribution
# ----------------------------------------------------------------------------
def test_credit_text_contains_everything_needed():
    candidate = {
        "title": "Lighthouse at dusk",
        "creator": "Jane Doe",
        "license": "CC BY-SA 4.0",
        "page": "https://commons.wikimedia.org/wiki/File:Lighthouse.jpg",
        "source": "Wikimedia Commons",
    }
    credit = core.credit_text(candidate)
    assert "Lighthouse at dusk" in credit
    assert "Jane Doe" in credit
    assert "CC BY-SA 4.0" in credit
    assert "Wikimedia Commons" in credit
    assert credit.startswith('"')


def test_credit_text_survives_missing_fields():
    credit = core.credit_text({})
    assert credit  # never raises, never returns an empty string


def test_strip_html():
    assert core._strip_html('<a href="https://x">Jane Doe</a>') == "Jane Doe"
    assert core._strip_html("") == ""


# ----------------------------------------------------------------------------
# Deduplication
# ----------------------------------------------------------------------------
def test_dedupe_removes_the_same_picture_twice():
    a = {"page": "https://p/1", "thumb": "https://i/1"}
    b = {"page": "https://p/1", "thumb": "https://i/1"}
    c = {"page": "https://p/2", "thumb": "https://i/2"}
    assert len(core._dedupe([a, b, c])) == 2


def test_dedupe_keeps_the_same_image_from_two_sources():
    a = {"page": "https://commons/File:X", "thumb": "https://img/x.jpg"}
    b = {"page": "https://openverse/x", "thumb": "https://img/x.jpg"}
    c = {"page": "https://openverse/y", "thumb": "https://img/y.jpg"}
    assert len(core._dedupe([a, b, c])) == 2


# ----------------------------------------------------------------------------
# Source parsing (recorded responses, no network)
# ----------------------------------------------------------------------------
COMMONS_RESPONSE = {
    "query": {
        "pages": {
            "7": {
                "index": 1,
                "title": "File:Lighthouse.jpg",
                "imageinfo": [{
                    "url": "https://upload.wikimedia.org/lighthouse.jpg",
                    "thumburl": "https://upload.wikimedia.org/thumb/lighthouse.jpg/400px-lighthouse.jpg",
                    "descriptionurl": "https://commons.wikimedia.org/wiki/File:Lighthouse.jpg",
                    "mime": "image/jpeg",
                    "extmetadata": {
                        "Artist": {"value": '<a href="https://example.org">Jane Doe</a>'},
                        "LicenseShortName": {"value": "CC BY-SA 4.0"},
                    },
                }],
            },
            "9": {
                "index": 2,
                "title": "File:Diagram.svg",
                "imageinfo": [{
                    "url": "https://upload.wikimedia.org/diagram.svg",
                    "thumburl": "https://upload.wikimedia.org/thumb/diagram.svg/400px-diagram.svg.png",
                    "descriptionurl": "https://commons.wikimedia.org/wiki/File:Diagram.svg",
                    "mime": "image/svg+xml",
                    "extmetadata": {},
                }],
            },
        }
    }
}


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeDownload:
    """Mimics the streaming response that _get_bytes now expects."""

    def __init__(self, content=b"image-bytes", headers=None):
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_search_commons_parses_and_skips_non_photos(monkeypatch):
    monkeypatch.setattr(core.requests, "get",
                        lambda *a, **k: _FakeResponse(COMMONS_RESPONSE))
    results = core.search_commons("lighthouse", 10)

    assert len(results) == 1, "the SVG must be dropped"
    hit = results[0]
    assert hit["title"] == "Lighthouse"
    assert hit["creator"] == "Jane Doe"
    assert hit["license"] == "CC BY-SA 4.0"
    assert hit["source"] == "Wikimedia Commons"
    assert hit["thumb"].startswith("https://upload.wikimedia.org/thumb/")
    assert hit["file_title"] == "File:Lighthouse.jpg"


@pytest.mark.parametrize("title", [
    "The Storm, oil on canvas",
    "Engraving of a lighthouse",
    "Sketch_of_a_boat",
    "A watercolour view of the bay",
])
def test_artwork_titles_are_recognised(title):
    assert core.is_artwork(title)


def test_a_photograph_title_is_not_artwork():
    assert not core.is_artwork("Trwyn Du Lighthouse in stormy seas")


def test_artwork_categories_are_recognised():
    assert core.is_artwork_category(["Category:19th-century oil marine paintings"])
    assert core.is_artwork_category(["Category:Prints of lighthouses"])
    assert not core.is_artwork_category(["Category:Lighthouses in England"])


def test_search_commons_drops_paintings(monkeypatch):
    payload = {
        "query": {"pages": {
            "1": {"index": 1, "title": "File:The Irwin Lighthouse, Storm Raging.jpg",
                  "categories": [{"title": "Category:19th-century oil marine paintings"}],
                  "imageinfo": [{
                      "thumburl": "https://upload.wikimedia.org/painting.jpg",
                      "descriptionurl": "https://commons.wikimedia.org/wiki/File:Storm.jpg",
                      "mime": "image/jpeg",
                      "extmetadata": {"LicenseShortName": {"value": "CC BY 4.0"}},
                  }]},
            "2": {"index": 2, "title": "File:Lighthouse in a storm.jpg",
                  "categories": [{"title": "Category:Lighthouses in England"}],
                  "imageinfo": [{
                      "thumburl": "https://upload.wikimedia.org/photo.jpg",
                      "descriptionurl": "https://commons.wikimedia.org/wiki/File:Lighthouse.jpg",
                      "mime": "image/jpeg",
                      "extmetadata": {"LicenseShortName": {"value": "CC BY 4.0"}},
                  }]},
        }}
    }
    monkeypatch.setattr(core.requests, "get",
                        lambda *a, **k: _FakeResponse(payload))
    results = core.search_commons("storm", 10)
    assert [c["title"] for c in results] == ["Lighthouse in a storm"]


def test_search_images_merges_both_sources(monkeypatch):
    monkeypatch.setattr(core, "search_commons",
                        lambda q, n: [{"page": f"https://commons/{i}", "thumb": f"https://c/{i}",
                                       "full": "", "title": f"c{i}", "creator": "A",
                                       "license": "CC BY 4.0", "source": "Wikimedia Commons"}
                                      for i in range(3)])
    monkeypatch.setattr(core, "search_openverse",
                        lambda q, n: [{"page": f"https://openverse/{i}", "thumb": f"https://o/{i}",
                                       "full": "", "title": f"o{i}", "creator": "B",
                                       "license": "BY 4.0", "source": "Openverse"}
                                      for i in range(3)])
    results = core.search_images("lighthouse", 6)
    sources = [r["source"] for r in results]
    assert sources.count("Wikimedia Commons") == 3
    assert sources.count("Openverse") == 3
    # interleaved, so neither source can crowd the other out of the grid
    assert sources[0] != sources[1]


def test_search_images_survives_a_dead_source(monkeypatch):
    def boom(query, n):
        raise RuntimeError("openverse is down")
    monkeypatch.setattr(core, "search_commons",
                        lambda q, n: [{"page": "https://commons/1", "thumb": "https://c/1",
                                       "full": "", "title": "c1", "creator": "A",
                                       "license": "CC BY 4.0", "source": "Wikimedia Commons"}])
    monkeypatch.setattr(core, "search_openverse", boom)
    results = core.search_images("lighthouse", 6)
    assert len(results) == 1


def test_search_images_falls_back_when_every_licence_is_restrictive(monkeypatch):
    monkeypatch.setattr(core, "search_commons",
                        lambda q, n: [{"page": "https://commons/1", "thumb": "https://c/1",
                                       "full": "", "title": "c1", "creator": "A",
                                       "license": "CC BY-NC 4.0", "source": "Wikimedia Commons"}])
    monkeypatch.setattr(core, "search_openverse", lambda q, n: [])
    results = core.search_images("lighthouse", 6)
    assert len(results) == 1, "an empty grid is worse than a licence we should double check"


# ----------------------------------------------------------------------------
# Caching
# ----------------------------------------------------------------------------
def test_cached_download_is_only_fetched_once(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "CACHE_DIR", tmp_path)
    calls = []

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        return _FakeDownload()

    monkeypatch.setattr(core.requests, "get", fake_get)
    assert core.fetch_cached("https://example.org/a.jpg") == b"image-bytes"
    assert core.fetch_cached("https://example.org/a.jpg") == b"image-bytes"
    assert len(calls) == 1, "the second call must come from the disk cache"


def test_oversized_images_are_refused(monkeypatch):
    def fake_get(url, *args, **kwargs):
        return _FakeDownload(b"x" * 10, headers={"Content-Length": "99999999999"})

    monkeypatch.setattr(core.requests, "get", fake_get)
    with pytest.raises(RuntimeError):
        core._get_bytes("https://example.org/huge.jpg")


# ----------------------------------------------------------------------------
# CLIP embeddings: batching and the zero-vector rule
# ----------------------------------------------------------------------------
class _FakeImageModel:
    def __init__(self, vectors):
        self.vectors = list(vectors)
        self.calls = 0

    def embed(self, images):
        self.calls += 1
        for vector in self.vectors[:len(images)]:
            yield vector


class _FlakyImageModel:
    """Fails on a batch, works one image at a time (a real failure mode)."""

    def __init__(self):
        self.calls = 0

    def embed(self, images):
        self.calls += 1
        if len(images) > 1:
            raise RuntimeError("batch failed")
        yield [1.0, 0.0]


def _bare_ranker(tmp_path, model):
    ranker = core.Ranker.__new__(core.Ranker)     # skip the background loader
    ranker.image_model = model
    ranker._vector_dir = tmp_path / "vectors"
    ranker.error = None
    return ranker


def _candidate(n):
    from PIL import Image
    return {"thumb": f"https://example.org/{n}.jpg", "img": Image.new("RGB", (8, 8))}


def test_zero_vectors_are_never_cached(tmp_path):
    ranker = _bare_ranker(tmp_path, _FakeImageModel([[0.0, 0.0]]))
    assert ranker.embed_images([_candidate(1)]) == []
    assert not list((tmp_path / "vectors").glob("*.npy")), \
        "a zero vector in the cache poisons every later run"


def test_pictures_are_embedded_in_one_batch_and_reused(tmp_path):
    model = _FakeImageModel([[1.0, 0.0], [0.0, 1.0]])
    ranker = _bare_ranker(tmp_path, model)
    cands = [_candidate(1), _candidate(2)]

    assert len(ranker.embed_images(cands)) == 2
    assert model.calls == 1, "two pictures should be one ONNX call"

    assert len(ranker.embed_images(cands)) == 2
    assert model.calls == 1, "the second search must reuse the cached vectors"


def test_a_bad_picture_does_not_lose_the_whole_batch(tmp_path):
    model = _FlakyImageModel()
    ranker = _bare_ranker(tmp_path, model)
    pairs = ranker.embed_images([_candidate(1), _candidate(2)])
    assert len(pairs) == 2, "the batch is retried one image at a time"
    assert model.calls == 3


class _FakeRanker:
    """Scores every candidate and returns them in reverse order."""

    ready = True

    def embed_images(self, cands):
        import numpy as np
        return [(c, np.array([1.0, 0.0], dtype="float32")) for c in cands]

    def score(self, sentence, vectors, query=""):
        for c, _ in vectors:
            c["score"] = 0.5
        return sorted((c for c, _ in vectors), key=lambda c: c["thumb"], reverse=True)


def test_search_order_is_kept_when_reranking_is_off(monkeypatch):
    monkeypatch.setattr(core, "RERANK", False)
    monkeypatch.setattr(core, "EMBED_BATCH", 2)
    monkeypatch.setattr(core, "fetch_preview", lambda c: object())
    cands = [_candidate(1), _candidate(2), _candidate(3)]
    out = core.rank_candidates(_FakeRanker(), "a sentence", "query", cands, limit=3)
    assert [c["thumb"] for c in out] == [c["thumb"] for c in cands]
    assert all("score" in c for c in out), "the match bar still needs a score"


def test_reranking_can_be_turned_on(monkeypatch):
    monkeypatch.setattr(core, "RERANK", True)
    monkeypatch.setattr(core, "EMBED_BATCH", 2)
    monkeypatch.setattr(core, "fetch_preview", lambda c: object())
    cands = [_candidate(1), _candidate(2), _candidate(3)]
    out = core.rank_candidates(_FakeRanker(), "a sentence", "query", cands, limit=3)
    assert [c["thumb"] for c in out] == sorted(
        (c["thumb"] for c in cands), reverse=True)


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
def test_config_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("EZSEARCH_MODEL", "gemma3:4b")
    monkeypatch.setenv("EZSEARCH_CANDIDATES", "12")
    import importlib
    reloaded = importlib.reload(core)
    try:
        assert reloaded.LLM_MODEL == "gemma3:4b"
        assert reloaded.N_CANDIDATES == 12
    finally:
        monkeypatch.delenv("EZSEARCH_MODEL")
        monkeypatch.delenv("EZSEARCH_CANDIDATES")
        importlib.reload(core)
