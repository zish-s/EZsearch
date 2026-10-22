"""
benchmark.py - measure EZsearch so the numbers in the write-up are real.

Three things get measured:

  1. Latency.  How long each stage takes, and how long the whole thing takes
     from "sentence" to "picture on screen".
  2. Does CLIP actually earn its keep?  The same candidate pool is ranked two
     ways - in the order the search engine returned it (keywords only) and
     re-ranked by CLIP against the whole sentence.  We report how often a
     picture of the right subject lands in the top 1 and the top 3.
  3. Which local model writes the better query?  Llama and Gemma are both
     asked to turn each sentence into a search query, and the end-to-end hit
     rate says which one is actually helping.

Each model is measured with its own empty image and vector cache, so one model
never gets a free ride from the other model's downloads.

Run it with:

    python benchmark.py                       # every model that is installed
    python benchmark.py --models llama3.2:3b,gemma3:4b
    python benchmark.py --out benchmark_results.md
    python benchmark.py --reuse-cache         # the warm case: cache already full
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import statistics
import sys
import time
from pathlib import Path

import ezsearch_core as core

# A small test set of ordinary sentences, each with the words a good picture
# of it would have in its title.
CASES = [
    ("The old lighthouse stood against a violent storm.", ["lighthouse"]),
    ("She poured a cup of steaming coffee on the balcony.", ["coffee"]),
    ("Snow covered the empty street at dawn.", ["snow", "street"]),
    ("A crowded farmers market fills the square every Sunday.", ["market"]),
    ("The cat curled up asleep on the windowsill.", ["cat"]),
    ("Rusty bicycles leaned against the brick wall.", ["bicycle", "bike"]),
    ("Golden sunlight poured across the wheat field.", ["wheat", "field", "sun"]),
    ("A fisherman mended his nets on the harbour wall.", ["fisherman", "net", "harbour", "harbor"]),
    ("The train pulled into the empty mountain station.", ["train", "station"]),
    ("Children built a sandcastle on the wide beach.", ["sandcastle", "beach", "castle"]),
    ("He poured himself a dark cup of tea by the window.", ["tea", "cup"]),
    ("Bees moved between the lavender in the summer garden.", ["bee", "lavender", "garden"]),
]

# The same sentences, but written so that the words in the picture's file name
# are NOT the words in the sentence. This is the case CLIP is for: the picture
# is right, but the search engine had no reason to put it first.
PARAPHRASE_CASES = [
    ("A tall brick chimney pushed smoke into the grey sky.",
     ["chimney", "smokestack", "chimney", "factory", "power station", "industrial"]),
    ("The four-legged animal barked at the man in the blue coat.",
     ["dog"]),
    ("She steered the small wooden vessel across the bay.",
     ["sailboat", "yacht", "boat", "sailing", "ship"]),
    ("The machine pressed a page of letters onto paper.",
     ["printer", "typewriter", "printing"]),
    ("Snow-white covered the quiet road at first light.",
     ["snow", "winter", "road"]),
    ("A green vegetable, long and thin, lay on the board.",
     ["leek", "celery", "asparagus", "cucumber", "zucchini", "spring onion"]),
    ("They dug a deep hole in the sand and filled it with water.",
     ["sandcastle", "hole", "sand", "beach"]),
    ("A ball of wool turned into a warm knitted thing.",
     ["yarn", "knitting", "wool", "knit", "sweater", "scarf"]),
]

# A picture of a painting of a storm is not a picture of a storm. The word
# list lives in ezsearch_core so the app and the benchmark agree on it.
def is_photograph(candidate: dict) -> bool:
    """Is this a photograph rather than a painting, map or diagram?

    Commons is full of paintings and engravings, and a keyword search cannot
    tell them apart - a picture of a stormy sea is not a photograph of one.
    """
    return not core.is_artwork(
        f'{candidate.get("title", "")} {candidate.get("page", "")}')


def _words(text: str) -> str:
    """Normalise a title or file path so word boundaries actually work."""
    return re.sub(r"[_\-]+", " ", text.lower())


def hits(candidate: dict, keywords: list[str]) -> bool:
    """Does this picture look like a photograph of the expected subject?

    Matching is on whole words. Substring matching would count "cattle" as a
    hit for "cat" and make every number in this file meaningless.
    """
    haystack = _words(f'{candidate.get("title", "")} {candidate.get("page", "")}')
    if not any(re.search(rf"\b{re.escape(k.lower())}(?:s|es)?\b", haystack)
               for k in keywords):
        return False
    return is_photograph(candidate)


def run_model(model: str, ranker, cases, set_name: str, limit: int = 8) -> dict:
    """Run every test sentence through one LLM and collect the numbers."""
    rows = []
    for sentence, keywords in cases:
        t0 = time.perf_counter()
        query, used_llm = core.build_query(sentence, model=model)
        llm_s = time.perf_counter() - t0

        t0 = time.perf_counter()
        used_query, candidates = core.resolve_query(query, n=core.N_CANDIDATES)
        search_s = time.perf_counter() - t0

        # Download and rank in one step - the real pipeline overlaps them.
        t0 = time.perf_counter()
        ranked = core.rank_candidates(ranker, sentence, used_query, candidates, limit=limit)
        fetch_rank_s = time.perf_counter() - t0

        keyword_order = candidates[:limit]
        rows.append({
            "set": set_name,
            "sentence": sentence,
            "query": used_query,
            "original_query": query,
            "used_llm": used_llm,
            "llm_s": llm_s,
            "search_s": search_s,
            "download_s": fetch_rank_s,
            "clip_s": 0.0,
            "total_s": llm_s + search_s + fetch_rank_s,
            "keyword_hit1": any(hits(c, keywords) for c in keyword_order[:1]),
            "keyword_hit3": any(hits(c, keywords) for c in keyword_order[:3]),
            "clip_hit1": any(hits(c, keywords) for c in ranked[:1]),
            "clip_hit3": any(hits(c, keywords) for c in ranked[:3]),
            "results": len(candidates),
        })
        print(f"  {sentence[:46]:<48} {used_query[:26]:<28} {rows[-1]['total_s']:.2f}s")
    return {"model": model, "rows": rows}


def summarise(result: dict, set_name: str) -> dict:
    rows = [r for r in result["rows"] if r["set"] == set_name]
    med = lambda key: statistics.median(r[key] for r in rows) if rows else 0.0
    n = len(rows)
    return {
        "model": result["model"],
        "set": set_name,
        "n": n,
        "llm_s": med("llm_s"),
        "search_s": med("search_s"),
        "download_s": med("download_s"),
        "total_s": med("total_s"),
        "keyword_hit1": sum(r["keyword_hit1"] for r in rows),
        "keyword_hit3": sum(r["keyword_hit3"] for r in rows),
        "clip_hit1": sum(r["clip_hit1"] for r in rows),
        "clip_hit3": sum(r["clip_hit3"] for r in rows),
        "llm_used": sum(1 for r in rows if r["used_llm"]),
    }


def markdown(results: list[dict], details: list[dict]) -> str:
    out = []
    out.append("# EZsearch benchmark\n")
    out.append(f"Generated on {platform.system()} {platform.machine()}, "
               f"Python {platform.python_version()}, CPU only.\n")

    out.append("## Latency (median over the test set)\n")
    out.append("Each model starts from an empty image and vector cache, so the first search "
               "is measured cold - which is what a new user gets.\n")
    header = " | ".join(f'{r["model"]} ({r["set"]})' for r in results)
    out.append(f"| Stage | {header} |")
    out.append("|---|" + "---|" * len(results))
    for key, label in [("llm_s", "LLM query build"),
                       ("search_s", "Image search (API)"),
                       ("download_s", "Download + CLIP scoring (overlapped)"),
                       ("total_s", "**Total: sentence to picture**")]:
        cells = " | ".join(f"{r[key]:.2f} s" for r in results)
        out.append(f"| {label} | {cells} |")
    out.append("")

    out.append("## Does re-ranking actually help?\n")
    out.append("The same candidate pool, ranked two ways: in the order the search engine "
               "returned it, and re-ranked by CLIP against the whole sentence. A picture "
               "counts as a hit only if it is a *photograph* of the subject - a painting of "
               "a stormy sea is not a picture of a storm.\n")
    for set_name, label in (
        ("direct", "### 1. Sentences that name their subject"),
        ("paraphrase", "### 2. Paraphrased sentences (the picture's file name does "
                       "not contain the sentence's words)"),
    ):
        picked = [r for r in results if r["set"] == set_name]
        if not picked:
            continue
        out.append(label + "\n")
        out.append("| Ranking | Top 1 | Top 3 |")
        out.append("|---|---|---|")
        for r in picked:
            out.append(f"| {r['model']} - search order (keywords only) "
                       f"| {r['keyword_hit1']}/{r['n']} "
                       f"({100*r['keyword_hit1']//max(r['n'],1)}%) "
                       f"| {r['keyword_hit3']}/{r['n']} "
                       f"({100*r['keyword_hit3']//max(r['n'],1)}%) |")
            out.append(f"| {r['model']} - CLIP re-ranked "
                       f"| {r['clip_hit1']}/{r['n']} "
                       f"({100*r['clip_hit1']//max(r['n'],1)}%) "
                       f"| {r['clip_hit3']}/{r['n']} "
                       f"({100*r['clip_hit3']//max(r['n'],1)}%) |")
        out.append("")

    if len(results) > 1:
        out.append("## Which local model writes the better query?\n")
        out.append("Both sets together (12 direct sentences + 8 paraphrases), ranked in the "
                   "order the search engine returned them - which is what the app shows by "
                   "default. The hit rate is the share of sentences whose top picture is a "
                   "photograph of the subject.\n")
        out.append("| Model | LLM time (median) | Top 1 | Top 3 |")
        out.append("|---|---|---|---|")
        for model in dict.fromkeys(r["model"] for r in results):
            picked = [r for r in results if r["model"] == model]
            n = sum(r["n"] for r in picked)
            hit1 = sum(r["keyword_hit1"] for r in picked)
            hit3 = sum(r["keyword_hit3"] for r in picked)
            llm = statistics.median(
                row["llm_s"] for d in details if d["model"] == model for row in d["rows"])
            out.append(f"| {model} | {llm:.2f} s | {hit1}/{n} ({100 * hit1 // max(n, 1)}%) "
                       f"| {hit3}/{n} ({100 * hit3 // max(n, 1)}%) |")
        out.append("")

    out.append("## Every query that was built\n")
    out.append("| Sentence | Query |")
    out.append("|---|---|")
    for r in details[0]["rows"]:
        out.append(f'| {r["sentence"]} | `{r["query"]}` |')
    out.append("")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure EZsearch.")
    parser.add_argument("--models", default="", help="comma separated Ollama model names")
    parser.add_argument("--out", default="benchmark_results.md", help="where to write the report")
    parser.add_argument("--json", default="", help="also write the raw numbers as JSON")
    parser.add_argument("--cache-dir", default=".benchmark_cache",
                        help="image/vector cache used by the runs")
    parser.add_argument("--reuse-cache", action="store_true",
                        help="keep the cache from a previous run (measures the warm case)")
    args = parser.parse_args()

    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        models = [core.LLM_MODEL]
        try:
            import requests
            tags = requests.get(core.OLLAMA_TAGS, timeout=3).json().get("models", [])
            installed = [m.get("name") for m in tags]
            for name in installed:
                if name and name not in models:
                    models.append(name)
        except Exception:
            pass

    print("Loading CLIP...")
    ranker = core.Ranker()
    deadline = time.time() + 300
    while not ranker.ready and time.time() < deadline:
        time.sleep(1)
    if not ranker.ready:
        print(f"CLIP failed to load: {ranker.error}")
        return 1
    print("CLIP ready.\n")

    results, details = [], []
    for model in models:
        print(f"=== {model} ===")
        # Every model gets its own cache, cleared first. Without this the first
        # model pays for the downloads and the second one looks impossibly fast.
        cache = Path(args.cache_dir) / re.sub(r"[^A-Za-z0-9_.-]", "_", model)
        if not args.reuse_cache and cache.exists():
            shutil.rmtree(cache, ignore_errors=True)
        cache.mkdir(parents=True, exist_ok=True)
        core.CACHE_DIR = cache
        ranker._vector_dir = cache / "vectors"
        print(f"  cache: {cache}" + ("  (warm)" if args.reuse_cache else "  (cold)"))
        print(f"  warming up {model} (the first inference loads the weights)...")
        core.warm_up(model)
        time.sleep(2)
        result = {"model": model, "rows": []}
        print("  -- direct sentences --")
        result["rows"] += run_model(model, ranker, CASES, "direct")["rows"]
        print("  -- paraphrased sentences --")
        result["rows"] += run_model(model, ranker, PARAPHRASE_CASES, "paraphrase")["rows"]
        details.append(result)
        results.append(summarise(result, "direct"))
        results.append(summarise(result, "paraphrase"))
        print()

    report = markdown(results, details)
    Path(args.out).write_text(report, encoding="utf-8")
    print(report)
    print(f"written to {args.out}")

    if args.json:
        Path(args.json).write_text(json.dumps(details, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
