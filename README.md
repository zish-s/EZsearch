# EZsearch

**Select a sentence. Press the shortcut key. Click a picture. It gets pasted right in your document, whether it's Word, Docs or PowerPoint.**

Imagine you're finished with your report, and then you remember you need to paste photos! It can be such a hectic task: Google search, then try to find a good quality, not blurred, not watermarked, perfect picture describing what you want, then copy from Google, go back to your document and paste.

I'm an avid lazy person, someone who chose to study comp sci because apparently all you need is a laptop and you can work while sitting anywhere. Hence, to simplify this tedious process I built EZsearch.

![The EZsearch popup](docs/screenshot.png)

---

## How it works

1. You select a sentence anywhere - Word, Google Docs, PowerPoint, OneNote, a browser.
2. You press **Ctrl+Shift+X** (an earlier build used Ctrl+Alt+F, which turns out to be Word's
   *Insert Footnote* accelerator - Word accepted the keystroke *and* added a footnote).
3. A small popup appears next to the cursor with up to eight matching pictures, plus a
   **+ show more** tile for the rest of the scored pool.
4. You click one. The picture is pasted **after your sentence** - the sentence stays. No tab
   switching: if Word ignores the synthetic keystrokes, the capture and the paste both go over
   Word's own automation instead.

| You want to... | Do this |
|---|---|
| Paste the picture | **Click** it |
| Show more of the scored pool | **m** or click **+ show more** |
| Open the page the picture came from | **Ctrl+click** it |
| Copy a ready-made credit line | **Right-click** it |
| Move the selection | **Arrow keys** |
| Paste the selected picture | **Enter** |
| Paste the *n*-th picture | **1** - **9** |
| Open the source page / copy the credit | **o** / **c** |
| Close | **Esc** |

---

## The open pieces (the point of the challenge)

Everything that makes a decision runs **on your machine, with open weights**:

| Piece | What it does | Licence |
|---|---|---|
| **Llama 3.2 3B** via [Ollama](https://ollama.com) | Turns the sentence into a short, photographable search query | Llama 3.2 Community Licence |
| **CLIP ViT-B/32** via fastembed / ONNX | Scores every candidate against the *whole sentence*, not just keywords | MIT |
| **Wikimedia Commons + Openverse** | Supply the images | CC / public domain per file |

Nothing is sent to a hosted AI service. Only a 2-5 word query leaves the machine, and only to look
up pictures - your sentence never does.

**Why open mattered here**

- **Privacy.** Drafts, research notes and unpublished writing never reach an AI server.
- **Cost.** $0 per use. A closed vision/LLM API bills every single selection.
- **Swappable.** Point `EZSEARCH_MODEL` at any model you have pulled. The benchmark below compares
  two of them, with numbers.
- **Offline-capable.** Query building and CLIP scoring run with no internet at all. Only the image
  fetch needs a connection.
- **Licensing.** Because the sources are openly licensed, every result carries a creator and a
  licence, and the tool copies a ready-made credit line for you.

---

## Install

Requires **Python 3.10+**.

```bash
git clone https://github.com/zish-s/EZsearch.git
cd EZsearch

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# the local LLM (open weights, ~2 GB)
ollama pull llama3.2:3b

python ezsearch.py
```

The first run downloads the CLIP weights once (~600 MB, cached afterwards). After that the app
starts instantly and ranks entirely offline.

You can also install it as a package, which gives you an `ezsearch` command:

```bash
pip install -e .
ezsearch
```

### Platform notes

- **Windows** - works as-is. Uses `pywin32` to put a real image on the clipboard, and Win32
  focus handling so the paste lands in the right control inside Word, Outlook or VS Code.
- **macOS** - allow your terminal under *System Settings > Privacy & Security > Accessibility* and
  *Input Monitoring*.
- **Linux** - X11 only. `sudo apt install xclip python3-tk`. `xdotool` is optional but improves
  focus handling.

---

## Configuration

Everything is an environment variable - no code edits needed.

| Variable | Default | Meaning |
|---|---|---|
| `EZSEARCH_HOTKEY` | `<ctrl>+<shift>+x` | Global hotkey (Ctrl+Shift+X - two keys, avoids Word's accelerators such as Ctrl+Alt+F = footnote) |
| `EZSEARCH_MODEL` | `llama3.2:3b` | Any model you have pulled in Ollama |
| `EZSEARCH_CANDIDATES` | `24` | How many candidates to fetch before scoring |
| `EZSEARCH_SHOWN` | `8` | How many tiles the popup shows |
| `EZSEARCH_MIN_SCORE` | `0.18` | CLIP similarity below which we say "no strong match" |
| `EZSEARCH_QUERY_WEIGHT` | `0.25` | How much of the score comes from the query rather than the sentence |
| `EZSEARCH_RERANK` | `0` | Set to `1` to let CLIP reorder results (the benchmark says don't) |
| `EZSEARCH_EMBED_BATCH` | `8` | Pictures per CLIP batch (higher is faster, up to a point) |
| `EZSEARCH_ALLOW_ART` | `0` | Set to `1` to allow paintings and engravings in results |
| `EZSEARCH_LLM_TIMEOUT` | `45` | Seconds to wait for the local model |
| `EZSEARCH_CACHE` | `~/.ezsearch` | Where downloaded images and vectors are cached |
| `EZSEARCH_PREVIEW_WIDTH` | `320` | Width of the grid previews |
| `EZSEARCH_FULL_WIDTH` | `1600` | Width of the picture that gets pasted |
| `EZSEARCH_OPENVERSE` | `1` | Set to `0` to search Wikimedia Commons only |
| `EZSEARCH_OPENVERSE_PHOTOS` | `1` | Prefer photographs over illustrations on Openverse |
| `EZSEARCH_OPENVERSE_TIMEOUT` | `8` | Seconds per Openverse attempt (it is slow in bursts) |
| `OPENVERSE_CLIENT_ID` / `OPENVERSE_CLIENT_SECRET` | unset | Optional: a free key raises Openverse's anonymous rate limit |
| `OLLAMA_URL` | `http://localhost:11434/api/generate` | Point at a different Ollama |

Openverse works without a key on its anonymous quota (about 200 requests a day at the time of
writing); a free key raises the limit. If Openverse is slow or unreachable, EZsearch gives it a
longer read timeout and one retry, then skips it for a minute and keeps using Commons.

---

## Architecture

```
 selection
    |
    |  Win32 / AppleScript: remember the focused control
    v
 sentence ---> LOCAL LLM (Ollama, Llama 3.2 3B) ---> "lighthouse storm"
    |                                                   |
    |                                        Wikimedia Commons + Openverse
    |                                        (queried in parallel, merged)
    |                                                   |
    |                                          candidate pictures
    |                                                   |
    |                                   LOCAL CLIP (ONNX, ViT-B/32)
    |                                   scores each picture against the sentence
    v                                                   |
 popup  <-----------------------------------------------+
    |
    |  click
    v
 clipboard <- full-size image (downloaded on demand, 1600px)
    |
    |  restore the focused control, then Ctrl+V
    v
 your document
```

### The four problems worth knowing about

**1. Pasting into the app you were typing in.** Word puts the caret in a *child* control, so
`SetForegroundWindow` alone is not enough - the frame activates but the key press lands on the
wrong window and the picture disappears. EZsearch captures the focused child before the popup
appears and restores it with `AttachThreadInput` + `SetFocus`, which is the part that makes the
whole product work. And because Word can just as quietly ignore a synthetic **Ctrl+C** as a
synthetic Ctrl+V, both halves have a second way in: the selection is read over Word's automation
(and COM must be initialised on *every thread*, which the pipeline thread is not), and the chosen
picture is pasted over `Selection.Paste()` when keystrokes go missing.

**2. A chatty query that matches nothing - or matches too little to choose from.** MediaWiki ANDs
every term together, so "lighthouse stood against violent storm" returns zero results - "stood" and
"against" are not words that appear in file names. And a narrow topic can go the other way: "roundabout
in chandigarh" matches exactly one Commons file. The query ladder is therefore about *precision first,
variety second*: the query as given and the same words without filler ("roundabout chandigarh") lead
the grid; when filename search is thin, Commons' **category space** is mined ("Road bridges in
Chandigarh", "National highways in Chandigarh"); the nearest broader phrasing then tops the pool up,
so Show more still has pages. Only when nothing topical exists do the shorter prefixes take over.

**3. Not showing you six pictures of nothing - nor six of the same picture.** CLIP is only as good as
the pool it scores, so EZsearch pulls 24 candidates and shows 8 at a time over the whole scored pool
with a **+ show more** tile. Three kinds of duplication are collapsed: the same upload twice (page
and URL), the same work uploaded again under other names (a Commons category named "*title* by
*artist*" is the identity key), and pictures that are literally identical pixels (a 64-bit picture
hash of the preview). Every tile carries a match-strength bar, and if nothing really matches you are
told so instead of being shown noise. Commons is an archive rather than a stock library, so paintings,
engravings and diagrams are dropped by their Commons categories (set `EZSEARCH_ALLOW_ART=1` to keep
them).

**4. Doing the slow things at the same time.** Downloading a picture is network-bound and embedding
it is CPU-bound, so EZsearch does both at once: pictures are embedded in small batches the moment
they arrive. Batching alone is roughly a 2x win over one ONNX call per image. Images and CLIP
vectors are also cached on disk, so the second time you search for the same thing it is instant
(about 1 s instead of 8 s).

---

## Benchmarks

Measured on this machine by `python benchmark.py` - see
[`benchmark_results.md`](benchmark_results.md) for the full report. Windows 11, i5-13420H,
CPU only, Python 3.12. The numbers below use a Commons-only candidate pool (Openverse was not
configured for the benchmark run) so both models see exactly the same pool; the app queries both
sources by default.

### Latency

| Stage | llama3.2:3b | gemma3:4b |
|---|---|---|
| LLM query build | 3.54 s | 4.53 s |
| Image search (Commons API) | 1.92 s | 1.86 s |
| Download + CLIP scoring (overlapped) | 8.12 s | 5.31 s |
| **Total: sentence to picture** | **13.39 s** | **13.32 s** |

Those are medians over 20 sentences with an empty cache, which is the worst case. Everything the
second search needs is cached, so repeating a subject takes **about 1 second**. Eight pictures are
embedded per ONNX call; doing that one at a time made the cold case twice as slow.

### Does CLIP re-ranking help?

I expected this to be the easy win. It wasn't. The same candidate pool, ranked two ways:

| Ranking (20 sentences, two models) | Top 1 | Top 3 |
|---|---|---|
| Search engine order (keywords only) | 29/40 (72%) | 31/40 (78%) |
| CLIP re-ranked | 24/40 (60%) | 31/40 (78%) |

A hit means the picture's title names a plausible subject *and* it is a photograph. The metric
rewards titles, which is the search engine's home turf - but it is the same metric for both rows,
and on the paraphrased half, where the sentence avoids the words in the file name, CLIP did not win
either.

So CLIP still scores every tile - the match bar and the "no strong match" warning are built on it -
but it no longer reorders the grid. `EZSEARCH_RERANK=1` turns reordering back on if you want to
reproduce the experiment.

---

## Privacy

- The selected sentence is read from the clipboard and handed to **your local Ollama server**.
  It is never sent to a remote API.
- The only thing that goes out over the network is a 2-5 word search query, and requests for
  pictures.
- CLIP runs locally through ONNX. The picture you pick is downloaded and pasted; it is never
  uploaded anywhere.
- Nothing is stored beyond the cache in `~/.ezsearch` (downloaded pictures and CLIP vectors).
  Delete that folder and EZsearch has no memory of anything.

---

## Licences and attribution

EZsearch only shows pictures it is allowed to show you. Every candidate carries its creator and
its licence, and the right-click menu copies a finished attribution line:

> "Lighthouse in a storm" by Jane Doe (CC BY-SA 4.0) - Wikimedia Commons - https://commons.wikimedia.org/wiki/File:Lighthouse.jpg

Restrictive licences (`NC`, `ND`, fair use, all-rights-reserved) are filtered out by default.
Pictures with an unclear licence are skipped too, unless that would leave you with an empty grid.

The code is MIT licensed. The models keep their own licences. The pictures keep theirs - that is
the whole point of showing you where each one came from.

---

## Development

```bash
pip install -r requirements.txt
pip install pytest

python -m pytest                 # 64 offline tests, no network, no models
python benchmark.py              # latency + "does CLIP actually help" + model comparison
python ezsearch.py --doctor      # check that everything is in place
python ezsearch.py --search "a sentence"    # run the pipeline headless
python scripts/screenshot.py     # render the popup for the README
```

### Layout

| File | What is in it |
|---|---|
| `ezsearch.py` | Entry point: hotkey, pipeline, headless CLI |
| `ezsearch_core.py` | LLM, image sources, CLIP scorer, clipboard - no GUI code |
| `ezsearch_focus.py` | Win32 / macOS focus capture and restore (the paste-back) |
| `ezsearch_ui.py` | The popup window |
| `benchmark.py` | The measurement harness |
| `tests/` | Offline unit tests |

---

## Roadmap

- [x] Local LLM query builder
- [x] Local CLIP scoring and a match-strength bar (reordering measured, off by default)
- [x] Reliable paste-back into the app you were typing in
- [ ] Local-folder mode: point CLIP at your own photo folders and search them with the same hotkey
- [ ] System tray icon and start-on-login
- [ ] Insert the picture and its credit line together

---

## Licence

MIT - see [LICENSE](LICENSE).
