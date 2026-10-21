# FlowSearch (desktop) - select a sentence, click a picture, done

Select a sentence in Word / Google Docs / PowerPoint, press **Ctrl+Alt+F**, click a picture, and it is
pasted into your document. No tab switching. Ctrl+click opens the photo's source page.

## Open pieces (the point of the challenge)
- **Llama 3.2 3B via Ollama** (open weights, local) - turns a sentence into a photographable search query.
- **CLIP ViT-B/32 via fastembed/ONNX** (open weights, local) - re-ranks results by the whole sentence.
- **Wikimedia Commons / Openverse** - openly licensed images, so every picture comes with a credit line.

## Setup
1. Install Python 3.10+ and Ollama (https://ollama.com), then: `ollama pull llama3.2:3b`
2. `pip install -r requirements.txt`
3. `python flowsearch.py`  (first run downloads the CLIP weights once, ~500 MB, then works offline for ranking)

## Optional settings (environment variables)
| Variable | Default | Meaning |
|---|---|---|
| FLOWSEARCH_HOTKEY | `<ctrl>+<alt>+f` | global hotkey |
| FLOWSEARCH_MODEL | `llama3.2:3b` | any model you have pulled in Ollama |
| OPENVERSE_CLIENT_ID / OPENVERSE_CLIENT_SECRET | unset | free Openverse key -> bigger catalogue |

## Platform notes
- **Windows**: works as-is (uses pywin32 to paste a real image and to return focus to your document).
- **macOS**: allow Terminal under System Settings > Privacy > Accessibility and Input Monitoring.
- **Linux**: X11 only, `sudo apt install xclip python3-tk`.