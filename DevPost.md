# FlowSearch: a sloth-speed way to illustrate your writing (and why it had to be open)

## The problem
Finding a picture for one sentence means: new tab, Google, scroll, right-click, copy, back to the doc, paste.
Seven steps, and my train of thought is gone by step three.

## What I built
Select a sentence anywhere, press one hotkey, click a picture. It lands in your document.
Ctrl+click opens where the photo came from. Right-click copies the credit line.

## How it works
1. Llama 3.2 3B (Ollama, local) -> turns the sentence into a short visual query
2. Wikimedia Commons / Openverse -> candidate images
3. CLIP ViT-B/32 (ONNX, local) -> re-ranks them against the *whole sentence*, not just keywords

## Why open mattered here
- **Privacy:** the sentences I write (drafts, research notes, unpublished work) never go to an AI server. Only a 2-5 word query leaves the machine.
- **Cost:** $0 per use. A closed vision/LLM API would bill every selection.
- **Swappable:** change `FLOWSEARCH_MODEL` to try another model; I tested ___ vs ___ and found ___.
- **Offline-capable:** query building and ranking run with no internet. Only image fetching needs a connection.
- **Licensing:** because the sources are openly licensed, every result carries a creator + license, and the tool copies a ready-made credit line.

## Where open beat closed (fill in with your own numbers)
- Time from hotkey to picture on my laptop: ___ s (LLM ___ s, search ___ s, CLIP ___ s)
- Re-ranking by CLIP moved the best image into the top 3 in ___ of ___ test sentences vs keyword order alone.

## What I'd build next
Local-folder mode: point CLIP at my own photo folders and search them with the same hotkey - fully offline.