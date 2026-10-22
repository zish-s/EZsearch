"""
scripts/screenshot.py - render the popup with real results and save a PNG.

Used for the README and the DEV post. It runs the real pipeline, so what you
see in the picture is what the app actually shows.

    python scripts/screenshot.py --sentence "A lighthouse against a storm" \
                                 --out docs/screenshot.png
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ezsearch_core as core            # noqa: E402
from ezsearch_ui import Popup            # noqa: E402


def become_dpi_aware() -> None:
    """Make Tk and ImageGrab agree on what a pixel is.

    On a 125% display Windows reports 1536x864 to a DPI-unaware process but
    ImageGrab captures the real 1920x1080 screen. Without this, the crop is
    offset by 25% and the screenshot is half desktop, half popup.
    """
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)     # per monitor
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main() -> int:
    become_dpi_aware()
    parser = argparse.ArgumentParser()
    parser.add_argument("--sentence", default="A lighthouse stood against a violent storm.")
    parser.add_argument("--out", default="docs/screenshot.png")
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()

    print("Building the query...")
    query, used_llm = core.build_query(args.sentence)
    print(f"  query: {query}" + ("" if used_llm else "  (fallback - Ollama off)"))

    print("Searching...")
    used_query, candidates = core.resolve_query(query, core.N_CANDIDATES)
    if used_query != query:
        print(f"  shortened to: {used_query}")
    print(f"  {len(candidates)} candidates")

    print("Loading CLIP...")
    ranker = core.Ranker()
    deadline = time.time() + 300
    while not ranker.ready and time.time() < deadline:
        time.sleep(1)
    if not ranker.ready:
        print(f"  CLIP failed to load: {ranker.error}")
        return 1

    print("Downloading and scoring...")
    candidates = core.rank_candidates(ranker, args.sentence, used_query, candidates,
                                       limit=args.limit)

    import tkinter as tk
    from PIL import ImageGrab

    root = tk.Tk()
    root.withdraw()
    popup = Popup(root)
    popup.open("Thinking...", None)
    popup.show_results(used_query, candidates)
    root.update()
    root.update_idletasks()

    # Park the panel in the top-left so the whole thing is on screen (the app
    # itself opens next to the cursor, which may be a pixel from the edge).
    win = popup.win
    win.geometry("+40+40")
    root.update()
    time.sleep(1.5)          # let the window settle and the images paint
    root.update()

    x, y = win.winfo_rootx(), win.winfo_rooty()
    w, h = win.winfo_width(), win.winfo_height()
    image = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)
    print(f"saved {out} ({w}x{h})")
    popup.close()
    root.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
