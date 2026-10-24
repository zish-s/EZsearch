"""
ezsearch_ui.py - the popup window.

A small borderless panel that appears next to the cursor:

    [ status line .................................... x ]
    [ tile ] [ tile ] [ tile ] [ tile ]
    [ tile ] [ tile ] [ tile ] [ tile ]
    [ hints ............................................ ]

Mouse:
    click          paste the picture into your document
    ctrl+click     open the page the picture came from
    right-click    copy a ready-made credit line

Keyboard:
    arrows         move the selection
    enter          paste the selected picture
    1-9            paste the n-th picture
    o              open the source page of the selection
    c              copy the credit line of the selection
    esc            close
"""
from __future__ import annotations

import threading
import tkinter as tk
import webbrowser

from PIL import ImageOps, ImageTk

import ezsearch_core as core
import ezsearch_focus

BG, FG, MUTED = "#1f1f1f", "#f2f2f2", "#9a9a9a"
ACCENT, GOOD, WARN, BAD = "#4c9aff", "#4caf50", "#e0a33e", "#e05252"
THUMB = 170
COLS = 4


class Popup:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.win: tk.Toplevel | None = None
        self.focus = ezsearch_focus.Focus()
        self.photos: list[ImageTk.PhotoImage] = []   # keep refs so Tk does not GC them
        self.candidates: list[dict] = []
        self.selected = 0
        self.tiles: list[tk.Frame] = []

    # -- lifecycle ----------------------------------------------------------
    def close(self, *_):
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.win = None
            self.photos.clear()
            self.tiles.clear()
            self.candidates = []

    def open(self, message: str, focus: ezsearch_focus.Focus | None = None):
        self.close()
        if focus is not None:
            self.focus = focus
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

        tk.Label(
            w,
            text="Click: paste   Ctrl+click: open source   Right-click: copy credit   "
                 "Arrows+Enter: pick   m: show more   Esc: close",
            bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(pady=(4, 8))

        w.update_idletasks()
        x = min(self.root.winfo_pointerx() + 12, self.root.winfo_screenwidth() - 760)
        y = min(self.root.winfo_pointery() + 12, self.root.winfo_screenheight() - 520)
        w.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        w.bind("<Escape>", self.close)
        w.focus_force()

    def status(self, text: str):
        if self.win is not None:
            self.status_label.config(text="\U0001F9A5 " + text)

    # -- results ------------------------------------------------------------
    PAGE = 8

    def show_results(self, query: str, cands: list[dict]):
        """Show the first page; "show more" reveals the rest of the pool."""
        self.query = query
        self.all_results = cands
        self.showing = min(self.PAGE, len(cands))
        self._render()

    def show_more(self):
        self.showing = min(self.showing + self.PAGE, len(self.all_results))
        self._render()
        return "break"

    def _render(self):
        cands = self.all_results[:self.showing]
        self.candidates = cands
        self.selected = 0
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self.photos.clear()
        self.tiles.clear()

        for i, c in enumerate(cands):
            tile = tk.Frame(self.grid_frame, bg=BG, highlightbackground="#555",
                            highlightthickness=1)
            tile.grid(row=i // COLS, column=i % COLS, padx=4, pady=4)
            self.tiles.append(tile)

            photo = ImageTk.PhotoImage(ImageOps.fit(c["img"], (THUMB, THUMB)))
            self.photos.append(photo)
            pic = tk.Label(tile, image=photo, bg=BG, cursor="hand2")
            pic.pack()

            credit = f'{c["creator"][:20]} \u00b7 {c["license"][:16]}'
            caption = tk.Label(tile, text=credit, bg=BG, fg=MUTED, font=("Segoe UI", 7))
            caption.pack()

            bar = self._score_bar(tile, c.get("score", 0.0))
            bar.pack(fill="x", padx=6, pady=(1, 4))

            for widget in (pic, caption, bar):
                widget.bind("<Button-1>", lambda e, c=c: self._choose(c, ctrl=bool(e.state & 0x4)))
                widget.bind("<Button-3>", lambda e, c=c: self.copy_credit_for(c))
                widget.bind("<Enter>", lambda e, i=i: self.set_selected(i))

        if len(self.all_results) > self.showing:
            remaining = len(self.all_results) - self.showing
            tk.Button(self.grid_frame, text=f"+ show more ({remaining} more)",
                      command=self.show_more, bg="#2b2b2b", fg=FG, bd=0, cursor="hand2",
                      font=("Segoe UI", 9, "bold"), activebackground=BG,
                      activeforeground=FG).grid(
                row=len(cands) // COLS + 1, columnspan=COLS, sticky="ew", padx=4,
                pady=(4, 2))

        self._bind_keys()
        self.set_selected(0)

        # Warn when nothing really matched - better than silently showing noise.
        best = cands[0].get("score", 0.0) if cands else 0.0
        if best < core.MIN_SCORE:
            self.status(f"{self.query}  \u2014  no strong match (best {best:.2f})")
        else:
            self.status(f"{self.query}  \u2014  best match {best:.2f}")

        self.win.focus_force()
        # Close when the user clicks away - but only start listening once the
        # window has settled, so our own focus_force does not trigger it.
        self.win.after(400, lambda: self.win and self.win.bind("<FocusOut>", self._maybe_close))

    def _score_bar(self, parent, score: float) -> tk.Frame:
        """A thin bar showing how well the picture matched the sentence."""
        holder = tk.Frame(parent, bg="#2b2b2b", width=158, height=3)
        holder.pack_propagate(False)
        fraction = (score - 0.12) / 0.20
        fraction = max(0.05, min(1.0, fraction))
        colour = GOOD if score >= 0.24 else (WARN if score >= core.MIN_SCORE else BAD)
        tk.Frame(holder, bg=colour, height=3, width=max(2, int(158 * fraction))).pack(
            side="left", fill="y")
        return holder

    # -- keyboard -----------------------------------------------------------
    def _bind_keys(self):
        w = self.win
        if w is None:
            return
        w.bind("<Left>", lambda e: self.move(-1))
        w.bind("<Right>", lambda e: self.move(1))
        w.bind("<Up>", lambda e: self.move(-COLS))
        w.bind("<Down>", lambda e: self.move(COLS))
        w.bind("<Return>", lambda e: self.paste_selected())
        w.bind("<KP_Enter>", lambda e: self.paste_selected())
        w.bind("<space>", lambda e: self.paste_selected())
        for n in range(1, 10):
            w.bind(str(n), lambda e, n=n: self.paste_index(n - 1))
        w.bind("o", lambda e: self.open_source())
        w.bind("c", lambda e: self.copy_credit())
        w.bind("m", lambda e: self.show_more())

    def move(self, delta: int):
        if not self.candidates:
            return "break"
        self.set_selected((self.selected + delta) % len(self.candidates))
        return "break"

    def set_selected(self, index: int):
        if not self.tiles:
            return
        self.selected = max(0, min(index, len(self.tiles) - 1))
        for i, tile in enumerate(self.tiles):
            chosen = i == self.selected
            tile.configure(highlightbackground=ACCENT if chosen else "#555",
                           highlightthickness=2 if chosen else 1)

    # -- user actions -------------------------------------------------------
    def _choose(self, c: dict, ctrl: bool = False):
        if ctrl:
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
        # Let the popup actually disappear before we take the focus back, then
        # restore and paste. The focus restore has to happen on the Tk thread,
        # and the paste has to happen while the target app holds the focus -
        # see ezsearch_focus.
        self.root.after(120, self._restore_and_paste)

    def _restore_and_paste(self):
        ezsearch_focus.restore(self.focus, self._send_paste)

    def _send_paste(self):
        # Prefer Word's own automation: if Word is ignoring synthesised keys
        # (it can), Selection.Paste() still lands the picture at the caret.
        if core.word_selection_paste():
            return
        from pynput.keyboard import Controller, Key
        kb = Controller()
        mod = Key.cmd if core.SYSTEM == "Darwin" else Key.ctrl
        with kb.pressed(mod):
            kb.press("v")
            kb.release("v")

    def copy_credit_for(self, c):
        import pyperclip
        pyperclip.copy(core.credit_text(c))
        self.status("Credit copied - paste it as a caption")

    def paste_selected(self):
        if 0 <= self.selected < len(self.candidates):
            self._choose(self.candidates[self.selected])
        return "break"

    def paste_index(self, index: int):
        if 0 <= index < len(self.candidates):
            self._choose(self.candidates[index])
        return "break"

    def open_source(self):
        if 0 <= self.selected < len(self.candidates):
            webbrowser.open(self.candidates[self.selected]["page"])
        return "break"

    def copy_credit(self):
        if 0 <= self.selected < len(self.candidates):
            self.copy_credit_for(self.candidates[self.selected])
        return "break"

    # -- focus handling -----------------------------------------------------
    def _maybe_close(self, _event):
        if self.win is None:
            return
        def check():
            if self.win is None:
                return
            if self.win.focus_get() is None:
                self.close()
        self.win.after(150, check)
