"""
flowsearch.py - the desktop app.

  1. You select a sentence in Word / Google Docs / PowerPoint / anything.
  2. Press the hotkey (default Ctrl+Alt+F).
  3. A small popup shows the best-matching pictures.
  4. Click one      -> it is pasted straight into your document (no tab switching).
     Ctrl+click     -> opens the page the photo came from.
     Right-click    -> copies a ready-made credit line (for CC-BY attribution).
     Esc            -> closes the popup.
"""
import os
import platform
import queue
import threading
import time
import tkinter as tk
import webbrowser

import pyperclip
from PIL import ImageOps, ImageTk
from pynput import keyboard

import flowsearch_core as core

HOTKEY = os.getenv("FLOWSEARCH_HOTKEY", "<ctrl>+<alt>+f")
N_SHOWN = 6
THUMB = 170
BG, FG, MUTED = "#1f1f1f", "#f2f2f2", "#9a9a9a"
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


def foreground_window():
    """Remember which window you were typing in (Windows only)."""
    if SYSTEM == "Windows":
        import win32gui
        return win32gui.GetForegroundWindow()
    return None


def refocus(hwnd) -> None:
    if SYSTEM == "Windows" and hwnd:
        import win32gui
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass


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
    sentinel = f"__flowsearch_{time.time()}__"
    pyperclip.copy(sentinel)            # lets us detect that Ctrl+C really changed the clipboard
    press_combo("c")
    for _ in range(20):                 # wait up to ~1 second for the target app to respond
        time.sleep(0.05)
        text = pyperclip.paste()
        if text != sentinel:
            return text.strip()
    pyperclip.copy(previous)            # nothing was selected: put the clipboard back
    return ""


# ----------------------------------------------------------------------------
# The pipeline (runs on a worker thread so the UI never freezes)
# ----------------------------------------------------------------------------
def run_pipeline() -> None:
    if not busy.acquire(blocking=False):
        return
    try:
        hwnd = foreground_window()
        sentence = grab_selection()
        if not sentence:
            ui_queue.put(("open", "Select a sentence first", hwnd))
            return
        ui_queue.put(("open", "Thinking...", hwnd))

        query, used_llm = core.build_query(sentence)
        ui_queue.put(("status", f"Searching: {query}" + ("" if used_llm else "  (Ollama off - basic mode)")))

        cands = core.load_thumbs(core.search_images(query))
        if not cands:
            ui_queue.put(("status", f"No pictures found for: {query}"))
            return

        ui_queue.put(("status", "Ranking with CLIP..." if ranker.ready
                      else "CLIP still loading - showing unranked results"))
        cands = ranker.rank(sentence, cands)[:N_SHOWN]
        ui_queue.put(("results", query, cands))
    except Exception as e:
        ui_queue.put(("status", f"Error: {e}"))
    finally:
        busy.release()


# ----------------------------------------------------------------------------
# The popup window
# ----------------------------------------------------------------------------
class Popup:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.win = None
        self.hwnd = None
        self.photos = []   # keep references so Tk does not garbage-collect the images

    def close(self, *_):
        if self.win is not None:
            self.win.destroy()
            self.win = None

    def open(self, message: str, hwnd):
        self.close()
        self.hwnd = hwnd
        w = self.win = tk.Toplevel(self.root)
        w.overrideredirect(True)                 # no title bar
        w.attributes("-topmost", True)
        w.configure(bg=BG, highlightbackground="#444", highlightthickness=1)

        head = tk.Frame(w, bg=BG)
        head.pack(fill="x", padx=10, pady=(8, 4))
        self.status_label = tk.Label(head, text="\U0001F9A5 " + message, bg=BG, fg=FG,
                                     font=("Segoe UI", 10, "bold"), anchor="w")
        self.status_label.pack(side="left")
        tk.Button(head, text="\u2715", command=self.close, bg=BG, fg=MUTED, bd=0,
                  activebackground=BG, activeforeground=FG, cursor="hand2").pack(side="right")

        self.grid_frame = tk.Frame(w, bg=BG)
        self.grid_frame.pack(padx=8)
        tk.Label(w, text="Click: paste   Ctrl+click: open source   Right-click: copy credit   Esc: close",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(pady=(4, 8))

        w.update_idletasks()
        x = min(self.root.winfo_pointerx() + 12, self.root.winfo_screenwidth() - 560)
        y = min(self.root.winfo_pointery() + 12, self.root.winfo_screenheight() - 480)
        w.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        w.bind("<Escape>", self.close)
        w.focus_force()

    def status(self, text: str):
        if self.win is not None:
            self.status_label.config(text="\U0001F9A5 " + text)

    def show_results(self, query: str, cands: list):
        if self.win is None:
            return
        self.status(f"{query}")
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self.photos.clear()
        for i, c in enumerate(cands):
            tile = tk.Frame(self.grid_frame, bg=BG)
            tile.grid(row=i // 3, column=i % 3, padx=4, pady=4)
            photo = ImageTk.PhotoImage(ImageOps.fit(c["img"], (THUMB, THUMB)))
            self.photos.append(photo)
            pic = tk.Label(tile, image=photo, bg=BG, cursor="hand2")
            pic.pack()
            credit = f'{c["creator"][:22]} \u00b7 {c["license"]}'
            caption = tk.Label(tile, text=credit, bg=BG, fg=MUTED, font=("Segoe UI", 7))
            caption.pack()
            for widget in (pic, caption):
                widget.bind("<Button-1>", lambda e, c=c: self.on_click(e, c))
                widget.bind("<Button-3>", lambda e, c=c: self.on_right_click(c))
        self.win.focus_force()
        # Close when you click away - but only start listening after the window has settled.
        self.win.after(400, lambda: self.win and self.win.bind("<FocusOut>", self._maybe_close))

    def _maybe_close(self, _event):
        # Ignore focus moving between our own widgets.
        self.win.after(150, lambda: self.win and self.root.focus_get() is None and self.close())

    # --- user actions -------------------------------------------------------
    def on_click(self, event, c):
        if event.state & 0x4:                       # Ctrl held -> go to the source page
            webbrowser.open(c["page"])
            return
        self.status("Fetching picture...")
        threading.Thread(target=self._paste_worker, args=(c,), daemon=True).start()

    def _paste_worker(self, c):
        try:
            core.copy_image(core.full_image(c))
            self.root.after(0, self._paste_into_document)
        except Exception as e:
            self.root.after(0, lambda: self.status(f"Could not copy: {e}"))

    def _paste_into_document(self):
        self.close()
        refocus(self.hwnd)
        self.root.after(200, lambda: press_combo("v"))   # give the old window time to wake up

    def on_right_click(self, c):
        pyperclip.copy(core.credit_text(c))
        self.status("Credit copied - paste it as a caption")


# ----------------------------------------------------------------------------
def main():
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
                elif kind == "status":
                    popup.status(msg[1])
                elif kind == "results":
                    popup.show_results(msg[1], msg[2])
        except queue.Empty:
            pass
        root.after(80, poll)

    def on_hotkey():
        threading.Thread(target=run_pipeline, daemon=True).start()

    keyboard.GlobalHotKeys({HOTKEY: on_hotkey}).start()
    print(f"FlowSearch running. Select a sentence anywhere and press {HOTKEY}. Ctrl+C here to quit.")
    root.after(80, poll)
    root.mainloop()


if __name__ == "__main__":
    main()