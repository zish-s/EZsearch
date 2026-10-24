"""
ezsearch.py - the desktop app.

  1. You select a sentence in Word / Google Docs / PowerPoint / anything.
  2. Press the hotkey (default Ctrl+Alt+F).
  3. A small popup shows the best-matching pictures.
  4. Click one      -> it is pasted straight into your document (no tab switching).
     Ctrl+click     -> opens the page the photo came from.
     Right-click    -> copies a ready-made credit line (for CC-BY attribution).
     Esc            -> closes the popup.

Headless mode (no GUI, useful for scripting, testing and benchmarks):

  python ezsearch.py --search "A lighthouse against the storm"
  python ezsearch.py --doctor
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import sys
import threading
import time

import pyperclip
from pynput import keyboard

import ezsearch_core as core
import ezsearch_focus
from ezsearch_ui import Popup

HOTKEY = os.getenv("EZSEARCH_HOTKEY", "<ctrl>+<alt>+f")

SYSTEM = platform.system()

kb = keyboard.Controller()
ranker = core.Ranker()
ui_queue: "queue.Queue[tuple]" = queue.Queue()   # worker thread -> Tk main thread
busy = threading.Lock()


# ----------------------------------------------------------------------------
# Talking to the other app: copy the selection, paste the picture
# ----------------------------------------------------------------------------
def press_combo(letter: str) -> None:
    mod = keyboard.Key.cmd if SYSTEM == "Darwin" else keyboard.Key.ctrl
    with kb.pressed(mod):
        kb.press(letter)
        kb.release(letter)


def selection_from_word() -> str:
    """Ask Word for the selection directly.

    Word sometimes ignores a synthesised Ctrl+C (no error, the clipboard just
    does not change). COM automation gets the answer straight from the source.
    """
    if SYSTEM != "Windows":
        return ""
    try:
        import pythoncom
        pythoncom.CoInitialize()     # COM is per-thread: the pipeline runs on
    except Exception:                # its own, so this must not be skipped
        return ""
    try:
        import win32com.client
        word = win32com.client.GetActiveObject("Word.Application")
        text = word.ActiveWindow.Selection.Text or ""
        return str(text).replace("\r", " ").replace("\x07", " ").strip()
    except Exception:
        return ""
    finally:
        pythoncom.CoUninitialize()


def grab_selection() -> str:
    # The hotkey's own Ctrl/Alt may still be held down; release them so we send a clean Ctrl+C.
    for k in (keyboard.Key.ctrl, keyboard.Key.alt, keyboard.Key.shift):
        try:
            kb.release(k)
        except Exception:
            pass
    time.sleep(0.05)
    try:
        previous = pyperclip.paste()
    except Exception:
        previous = ""
    sentinel = f"__ezsearch_{time.time()}__"
    pyperclip.copy(sentinel)            # lets us detect that Ctrl+C really changed the clipboard
    press_combo("c")
    for _ in range(20):                 # wait up to ~1 second for the target app to respond
        time.sleep(0.05)
        text = pyperclip.paste()
        if text != sentinel:
            return text.strip()
    pyperclip.copy(previous)            # nothing was selected: put the clipboard back
    if fallback := selection_from_word():
        return fallback                 # Word sometimes ignores a synthetic Ctrl+C
    print("EZsearch: selection capture failed - "
          "the clipboard never left the sentinel and Word gave no selection.")
    return ""


# ----------------------------------------------------------------------------
# The pipeline (runs on a worker thread so the UI never freezes)
# ----------------------------------------------------------------------------
def run_pipeline() -> None:
    if not busy.acquire(blocking=False):
        return
    focus = None
    try:
        # If a popup from the previous run is still open it owns the focus:
        # dismiss it and wait, so capture sees the user's document, not us.
        closed = threading.Event()
        ui_queue.put(("close", closed))
        closed.wait(0.6)
        # Remember where the user was typing *before* any popup steals focus.
        focus = ezsearch_focus.capture()
        sentence = grab_selection()
        if not sentence:
            ui_queue.put(("open", "Select a sentence first", focus))
            return
        ui_queue.put(("open", "Thinking...", focus))

        query, used_llm = core.build_query(sentence)
        if used_llm:
            ui_queue.put(("status", f"Searching: {query}"))
        elif core.ollama_available():
            ui_queue.put(("status", f"Searching: {query}  (Ollama too slow - basic mode)"))
        else:
            ui_queue.put(("status", f"Searching: {query}  (Ollama off - basic mode)"))

        # A chatty query can match nothing at all, so we shorten it until the
        # sources answer, and show the query that actually worked.
        used_query, cands = core.resolve_query(query)
        if used_query != query:
            ui_queue.put(("status", f"Searching: {used_query}"))
        if not cands:
            ui_queue.put(("status", f"No pictures found for: {used_query}"))
            return

        if not ranker.ready:
            # CLIP loads in the background; give it a moment rather than
            # showing the user an unranked grid.
            ui_queue.put(("status", "Waiting for CLIP..."))
            ranker.wait_ready(5.0)
        ui_queue.put(("status", "Ranking with CLIP..." if ranker.ready
                      else "CLIP still loading - showing unranked results"))
        # Download and embed in one pass: each picture is embedded the moment it
        # lands, instead of waiting for the whole grid to download first.
        cands = core.rank_candidates(ranker, sentence, used_query, cands, limit=core.N_SHOWN)
        if not cands:
            ui_queue.put(("status", f"No pictures could be loaded for: {used_query}"))
            return
        ui_queue.put(("results", used_query, cands))
    except Exception as e:
        # An error before the popup opened used to be invisible and look like
        # "the hotkey did nothing" - now it shows a popup and a console line.
        print(f"EZsearch error: {e}")
        ui_queue.put(("open", f"Error: {e}", focus))
    finally:
        busy.release()


# ----------------------------------------------------------------------------
# Headless mode
# ----------------------------------------------------------------------------
def headless_search(sentence: str, as_json: bool = False, limit: int = 8) -> int:
    """Run the whole pipeline without any GUI. Used by the tests and benchmarks."""
    ranker.wait_ready(60.0)
    query, used_llm = core.build_query(sentence)
    used_query, cands = core.resolve_query(query)
    if not cands:
        if as_json:
            print(json.dumps({"sentence": sentence, "query": query, "results": []}))
        else:
            print(f"query: {query}\nno pictures found")
        return 1

    cands = core.rank_candidates(ranker, sentence, used_query, cands, limit=limit)

    if as_json:
        print(json.dumps({
            "sentence": sentence,
            "query": used_query,
            "used_llm": used_llm,
            "results": [
                {
                    "title": c["title"], "creator": c["creator"], "license": c["license"],
                    "source": c["source"], "score": round(c.get("score", 0.0), 4),
                    "page": c["page"],
                }
                for c in cands
            ],
        }, indent=2))
        return 0

    print(f'sentence : "{sentence}"')
    print(f"query    : {used_query}" + ("" if used_llm else "   (fallback - Ollama off)"))
    print()
    for i, c in enumerate(cands, 1):
        print(f'{i:2}. {c.get("score", 0):.3f}  {c["title"][:48]}')
        print(f'         {c["creator"][:30]} | {c["license"]} | {c["source"]}')
        print(f'         {c["page"]}')
    return 0


def doctor() -> int:
    """Check that everything the app needs is actually in place."""
    print("EZsearch doctor")
    print("=" * 60)
    print(f"python           : {sys.version.split()[0]} ({platform.system()} {platform.machine()})")
    print(f"cache directory  : {core.CACHE_DIR}")
    print(f"search candidates: {core.N_CANDIDATES}")
    print(f"minimum CLIP score: {core.MIN_SCORE}")
    print()

    print(f"Ollama endpoint  : {core.OLLAMA_URL}")
    if core.ollama_available():
        print(f"  model          : {core.LLM_MODEL} is pulled and ready")
    else:
        try:
            import requests
            requests.get(core.OLLAMA_TAGS, timeout=2)
            print(f"  model          : {core.LLM_MODEL} is NOT pulled "
                  f"(run: ollama pull {core.LLM_MODEL})")
        except Exception:
            print("  Ollama         : not running (start it, or the app falls back "
                  "to keyword search)")

    print()
    if ranker.ready:
        print("CLIP ranker      : loaded")
    elif ranker.error:
        print(f"CLIP ranker      : failed to load -> {ranker.error}")
    else:
        ranker.wait_ready(30.0)
        if ranker.ready:
            print("CLIP ranker      : loaded")
        elif ranker.error:
            print(f"CLIP ranker      : failed to load -> {ranker.error}")
        else:
            print("CLIP ranker      : still loading (normal on a cold start - "
                  "the first search waits for it)")

    print()
    try:
        core.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        probe = core.CACHE_DIR / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
        print("cache writable   : yes")
    except Exception as e:
        print(f"cache writable   : no -> {e}")

    try:
        import pyperclip as _pc
        _pc.copy("ezsearch")
        ok = _pc.paste() == "ezsearch"
        print(f"clipboard        : {'yes' if ok else 'no'}")
    except Exception as e:
        print(f"clipboard        : no -> {e}")

    print()
    if os.getenv("EZSEARCH_OPENVERSE", "1") == "0":
        print("Openverse        : disabled (EZSEARCH_OPENVERSE=0)")
    elif os.getenv("OPENVERSE_CLIENT_ID") and os.getenv("OPENVERSE_CLIENT_SECRET"):
        print("Openverse        : enabled (key set)")
    else:
        print("Openverse        : enabled (anonymous quota; a free key raises the limit)")
    return 0


# ----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        prog="ezsearch",
        description="Select a sentence, press a hotkey, click a picture. It lands in your document.")
    parser.add_argument("--search", metavar="SENTENCE",
                        help="run the pipeline headless and print the ranked results")
    parser.add_argument("--json", action="store_true", help="with --search, print JSON")
    parser.add_argument("--limit", type=int, default=8, help="with --search, how many results")
    parser.add_argument("--doctor", action="store_true", help="check that everything is in place")
    args = parser.parse_args()

    if args.doctor:
        return doctor()
    if args.search:
        return headless_search(args.search, as_json=args.json, limit=args.limit)

    import tkinter as tk
    root = tk.Tk()
    root.withdraw()                      # the app has no main window, only the popup
    popup = Popup(root)

    def poll():
        try:
            while True:
                msg = ui_queue.get_nowait()
                kind = msg[0]
                if kind == "open":
                    popup.open(msg[1], msg[2])
                elif kind == "close":
                    popup.close()
                    if msg[1] is not None:
                        msg[1].set()
                elif kind == "status":
                    popup.status(msg[1])
                elif kind == "results":
                    popup.show_results(msg[1], msg[2])
        except queue.Empty:
            pass
        root.after(80, poll)

    def on_hotkey():
        threading.Thread(target=run_pipeline, daemon=True).start()

    try:
        keyboard.GlobalHotKeys({HOTKEY: on_hotkey}).start()
    except Exception as e:
        print(f"Could not register the hotkey {HOTKEY}: {e}")
        return 1

    llm = "ready" if core.ollama_available() else "not running (keyword fallback)"
    print(f"EZsearch running. Select a sentence anywhere and press {HOTKEY}.")
    print(f"  Ollama  : {llm}")
    print(f"  CLIP    : {'loaded' if ranker.ready else 'loading in the background'}")
    print("  Ctrl+C here to quit.")
    core.warm_up()          # load the model now so the first search is not slow
    root.after(80, poll)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
