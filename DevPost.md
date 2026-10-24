# EZsearch: a sloth-speed way to illustrate your writing

*This is the write-up for the Hacktoberfest Open-Source AI Challenge (Week 3 - "Slow and Steady").
Repo: https://github.com/zish-s/EZsearch*

A sloth does not rush. It conserves its energy and lets the world come to it. EZsearch is built the
same way: two open models run slowly and locally on your laptop, so that *you* never have to do the
seven-step dance again. The machine waits; you don't.

---

## The seven-step tax

Illustrating a sentence means: new tab, search engine, scroll, right-click, copy, switch back to the
document, paste. Seven steps. My train of thought is gone by step three, and by step six I have
usually given up and written the sentence without a picture - which is a shame, because a good
picture does the persuading for you.

So I built the thing that removes the seven steps.

![The EZsearch popup](docs/screenshot.png)

*This is the real popup, rendered by `scripts/screenshot.py` with the real pipeline - not a mockup.*

**Select a sentence anywhere - Word, Google Docs, PowerPoint, OneNote, a browser. Press `Ctrl+Alt+F`.
A small panel appears next to the cursor with the best-matching pictures. Click one and it is pasted
straight into the document you were already typing in.**

Ctrl+click opens the page the picture came from. Right-click copies a finished attribution line.
Arrow keys and Enter work too, because not everyone wants to use a mouse.

That is the whole product. The interesting part is what it took to make the last sentence true.

---

## Three open pieces, no API keys

| Piece | Job | Licence |
|---|---|---|
| **Llama 3.2 3B** via [Ollama](https://ollama.com) | Turns the sentence into a short, photographable query | Llama 3.2 Community |
| **CLIP ViT-B/32** via fastembed (ONNX) | Scores each candidate against the *whole sentence* | MIT |
| **Wikimedia Commons + Openverse** | Supply the pictures | CC / public domain, per file |

Both sources answer without an API key. Openverse's anonymous quota is small (about 200 requests a
day at the time of writing); a free key raises it.

The pipeline is deliberately boring:

```
 sentence --> local LLM --> "lighthouse in stormy sea"
                              |
                 +------------+------------+
                 v                         v
          Wikimedia Commons          Openverse     (queried in parallel, merged)
                 +------------+------------+
                              v
                 candidate pictures (24 of them)
                              |
                 local CLIP scores each one against the whole sentence
                              v
                 the first 8, in a panel next to your cursor
```

---

## The lazy payoff

The old loop was about 30 seconds per picture: new tab, search, scroll, open the image, copy,
switch back, paste, close the tab. Twenty pictures in a document is ten minutes of context
switching - and the switching, not the searching, is what costs you the paragraph you were in the
middle of.

EZsearch is one hotkey and one click. The machine absorbs the slow part:

- **~13 s** for the first search of a new subject on this laptop, with no GPU and no API.
- **~1 s** for the same subject again: the pictures and the CLIP vectors are cached on disk.
- **Zero** network round trips for the sentence itself. Only the 2-5 word query leaves the machine.

| Stage (median, cold cache) | llama3.2:3b | gemma3:4b |
|---|---|---|
| LLM query build | 3.5 s | 4.5 s |
| Image search | 1.9 s | 1.9 s |
| Download + CLIP scoring | 8.1 s | 5.3 s |
| **Sentence to picture** | **13.4 s** | **13.3 s** |

So you trade about thirteen seconds of local compute for a thirty-second context switch. The seconds
are similar; what you get back is the paragraph you did not lose. A sloth could do it. That is the
trade this week's challenge is about: slow and steady on the machine, lazy on the human.

---

## Why open mattered, with numbers

**Privacy.** The sentence you are writing - the draft, the research note, the thing you have not
published yet - is read from the clipboard and handed to a local Ollama server. It never leaves the
machine. The only thing that crosses the network is the 2-5 word query, and the requests for the
pictures themselves.

**Cost.** $0 per use. A closed vision API bills every single selection, which is exactly the kind
of cost that makes people stop doing the thing at all. For a tool you use 20 times while writing
one chapter, that is not a rounding error.

**Swappable.** `EZSEARCH_MODEL` points at any model you have pulled. I benchmarked two:

| Model | Query build (median) | Top 1 | Top 3 |
|---|---|---|---|
| **Llama 3.2 3B** (default) | 3.5 s | 14/20 | 16/20 |
| Gemma 3 4B | 4.5 s | 15/20 | 15/20 |

Twenty sentences, ranked in the search engine's order. The two are within noise on quality, and
Gemma takes about a second longer per query, so Llama stays the default. `EZSEARCH_MODEL=gemma3:4b`
is a one-line change if you want the other trade.

**Offline-capable.** Query building and CLIP scoring run with no internet at all. Only the image
fetch needs a connection. If I lose my network halfway through a paragraph, EZsearch still finds
me the right picture.

**Licensing.** Because the sources are openly licensed, every result carries a creator and a
licence, and EZsearch copies a ready-made credit line for you. Pictures with `NC`, `ND`, fair use or
all-rights-reserved licences are filtered out before you ever see them - pasting a picture into
somebody's document is not the place to improvise on licensing.

---

## The AI feature I measured and turned off

CLIP was supposed to be the clever part. Instead of trusting the search engine's keyword order, it
reads the whole sentence and scores each picture against it. That is the kind of claim that sounds
obviously true, so I made it prove itself.

The test has two sets. The first is 12 ordinary sentences, each naming a subject a photograph of it
would also name. The second is 8 paraphrases that deliberately avoid the words in the file names:
"the four-legged animal barked at the man in the blue coat" has to find a dog when the word *dog*
never appears in the sentence. That second set is where re-ranking should earn its keep, because
the search engine has no keyword reason to put the right picture first.

For each sentence I fetch the candidates once, then rank the same pool two ways: in the order the
search engine returned it, and re-ranked by CLIP against the whole sentence. A picture counts as a
hit only when its title names a plausible subject *and* it is a photograph - a painting or engraving
of a stormy sea is not a picture of a storm.

| Ranking (20 sentences, two models) | Top 1 | Top 3 |
|---|---|---|
| Search engine order (keywords only) | 29/40 (72%) | 31/40 (78%) |
| CLIP re-ranked | 24/40 (60%) | 31/40 (78%) |

I expected the second row to win. It doesn't. CLIP ties at top three and *loses* at top one: the
search engine's own relevance beat the 600 MB of weights I had just downloaded.

One honest caveat: the hit test reads titles, which is the search engine's home turf. A photo can be
perfect for a sentence without its file name saying so, and that is exactly the case CLIP exists
for. But the paraphrased half of the set was built to test that case, and CLIP did not win there
either. A tie is not a win, so I am not going to claim one.

The numbers above use a Commons-only pool (Openverse was not configured for that run), so the two
rows are directly comparable. The app queries both sources by default; a bigger pool does not change
the finding, which is about the order, not the number of candidates.

What I changed: CLIP still scores every tile - the match bar and the "no strong match" warning are
built on that score - but it no longer reorders the grid. The app shows the search engine's order,
which measured better. `EZSEARCH_RERANK=1` puts the experiment back.

This is the least glamorous part of the project and the one I am most pleased with. The feature was
already written; the benchmark is what stopped it from making the product worse.

---

## Four bugs that cost me the most time

**1. The paste went nowhere in Word.** The whole product promise is "click, and it lands in your
document". My first version called `SetForegroundWindow` and then sent a synthesised `Ctrl+V`.
That works in a plain one-window app and silently does nothing in Word, Outlook and VS Code.

The reason is that Office puts the caret in a *child* control. `SetForegroundWindow` activates the
top-level frame, but keyboard focus stays on the frame's own window procedure, so the key press is
delivered to the wrong place and the picture evaporates.

The fix is the one nobody warns you about: capture the focused child with `GetGUIThreadInfo`
*before* the popup appears, and restore it afterwards with `AttachThreadInput` (which shares input
queues with the target thread and is what actually gets past the "you may not steal focus"
restriction) followed by `SetFocus` on the **child**, not the frame. `AttachThreadInput` must be
detached afterwards or the user loses keyboard control system-wide - so the detach lives in a
`finally` block.

**2. A verbose query matched nothing at all.** MediaWiki's search ANDs every term together, so
"lighthouse stood against violent storm" returned zero results, because "stood" and "against" are
not words that appear in file names. My first LLM prompt produced exactly that kind of query, and
the app showed an empty grid and looked broken.

Now EZsearch shortens the query until the sources actually answer: full query, then the first three
words, then two, then one. It is a two-line fix, and it turned a guaranteed empty grid into one that
almost always has something in it.

**3. A silently cached zero vector poisoned every later run.** My scorer embedded images in a
batch and had a bare `except` around it. One bad image made the whole batch fail, the handler
returned zeros, and the zeros got written to the on-disk vector cache. Every subsequent run loaded
those zeros and returned a ranking of all 0.000 - and because the cache made it *fast*, it looked
like the model was just being uncertain.

The fix was to never cache a zero vector, to retry a failed batch one image at a time, and to fail
loudly enough that I noticed. It is the kind of bug that would have survived to the demo.

**4. The first picture EZsearch showed me was a nineteenth-century oil painting.** Search Commons for
"lighthouse in a storm" and the top result is *The Irwin Lighthouse, Storm Raging* by John Wilson
Carmichael. It is a beautiful painting. It is not a photograph of a lighthouse, and it is not what
you want in a report.

Commons is an archive, so paintings and engravings sit next to photographs, and keyword search
cannot tell them apart. My first fix looked for words like "painting" in the title - which missed
the Carmichael entirely, because its title never says what it is. The signal that works is the
file's **categories**: that painting lives in "19th-century oil marine paintings" and "Oil
paintings of lighthouses".

Now a candidate is dropped when its categories or its title say it is a painting, drawing,
engraving or diagram. EZsearch asks Commons for twice as many results to make up for the ones it
filters out, and falls back to artwork only if the grid would otherwise be empty. `EZSEARCH_ALLOW_ART=1`
turns the filter off.

---

## What the community already knew

I did not invent the Win32 recipe. The `AttachThreadInput` sandwich is written up in
[After 4 months solo: shipping a Windows tray AI hotkey on .NET 8 + WPF (and the Win32 paste-back rabbit hole)](https://dev.to/phantasmat2018/after-4-months-solo-shipping-a-windows-tray-ai-hotkey-on-net-8-wpf-and-the-win32-paste-back-50hm)
by [phantasmat2018](https://dev.to/phantasmat2018). It is the post that saved me from the
"`SetForegroundWindow` works in Notepad and nowhere else" phase, down to the detail that the detach
has to live in a `finally`.

On the AI side, [Local Embeddings with Fastembed, Rig & Rust](https://dev.to/joshmo_dev/local-embeddings-with-fastembed-rig-rust-3581)
by [joshmo_dev](https://dev.to/joshmo_dev) is what made ONNX embeddings feel like something a laptop
could do, and [Build an offline visual defect search system with CLIP and VectorAI DB](https://dev.to/actiandev/build-an-offline-visual-defect-search-system-with-clip-and-vectorai-db-4pll)
by [actiandev](https://dev.to/actiandev) is a good picture of what CLIP is actually for: comparing
images to each other, where it is excellent, rather than re-scoring a keyword search, where this
benchmark says it is not.

---

## What I would build next

**Local-folder mode.** Point CLIP at your own photo folders and search them with the same hotkey -
fully offline, no network at all, and the pictures are yours so there is no licensing question to
answer. It is the same scorer and the same popup; only the candidate source changes.

---

## Run it yourself

```bash
git clone https://github.com/zish-s/EZsearch.git
cd EZsearch
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
ollama pull llama3.2:3b
python ezsearch.py
```

First run downloads the CLIP weights (~600 MB, cached afterwards). After that it starts instantly
and ranks entirely offline.

```bash
python -m pytest        # 54 offline tests
python ezsearch.py --doctor
python ezsearch.py --search "a sentence"   # headless
```

Windows is the tested platform. macOS and Linux have focus and clipboard paths, but Windows is where
the app was built and where the paste-back was verified against a native child control.

---

## Licence

MIT. The code is mine, the models keep their licences, and the pictures keep theirs - which is why
every single one of them tells you where it came from.
