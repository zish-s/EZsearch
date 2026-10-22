"""
scripts/verify_pasteback.py - prove that the paste-back actually works.

This is the part of EZsearch that is easiest to get wrong and hardest to
notice when it is wrong: Word, Outlook and VS Code put the caret in a child
control, so activating the top-level window is not enough - the Ctrl+V has to
land on the child.

The test starts a separate process with a native Win32 window and a focused
EDIT control inside it (the stand-in for "your document"), makes this process's
window the foreground (the stand-in for "the popup"), and then checks not only
that the focus comes back, but that text pasted with a synthetic Ctrl+V ends up
*inside the edit control*.

    python scripts/verify_pasteback.py

Needs an interactive desktop session: Windows will not let a background service
steal the foreground, and that is the whole point of the thing being tested.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ezsearch_focus  # noqa: E402

user32 = ezsearch_focus.user32

WM_GETTEXT = 0x000D
MARKER = "EZsearch paste check"


def foreground() -> int:
    return int(user32.GetForegroundWindow())


def focused_child() -> int:
    hwnd = foreground()
    if not hwnd:
        return 0
    thread_id = user32.GetWindowThreadProcessId(hwnd, None)
    if not thread_id:
        return 0
    info = ezsearch_focus._GUITHREADINFO()
    info.cbSize = ctypes.sizeof(info)
    if user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
        return int(info.hwndFocus or 0)
    return 0


def force_foreground(hwnd: int) -> bool:
    """Bring a window to the front.

    Plain SetForegroundWindow is refused when our process has not received the
    last input event, which is why the real fix uses AttachThreadInput - we
    need it here too, just to set the scene up. Two extra tricks make this
    reliable in a normal desktop session:

      * drop the foreground-lock timeout to zero, and
      * tap Alt, which counts as "this process got the last input event".
    """
    # SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001, SPIF_SENDCHANGE = 0x2
    try:
        user32.SystemParametersInfoW(0x2001, 0, None, 0x2)
    except Exception:
        pass
    for flags in (0, 2):                     # ALT down, ALT up
        try:
            user32.keybd_event(0x12, 0, flags, 0)
        except Exception:
            pass

    target = user32.GetWindowThreadProcessId(hwnd, None)
    ours = ezsearch_focus.kernel32.GetCurrentThreadId()
    attached = bool(user32.AttachThreadInput(ours, target, True)) if target else False
    try:
        user32.ShowWindowAsync(hwnd, 9)          # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(ours, target, False)
    try:
        user32.SwitchToThisWindow(hwnd, True)
    except Exception:
        pass
    time.sleep(0.4)
    return foreground() == hwnd


def read_text(hwnd: int) -> str:
    user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, ctypes.c_void_p]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    buf = ctypes.create_unicode_buffer(512)
    user32.SendMessageW(hwnd, WM_GETTEXT, 512, ctypes.byref(buf))
    return buf.value


def send_ctrl_v() -> None:
    """The same synthetic keystroke the app sends, with a little settling time.

    pynput (which the app uses) sends through SendInput; here we use keybd_event
    directly so the test has one less moving part, but the path through Windows
    is the same.
    """
    user32.keybd_event(0x11, 0, 0, 0)        # Ctrl down
    time.sleep(0.05)
    user32.keybd_event(0x56, 0, 0, 0)        # V down
    time.sleep(0.05)
    user32.keybd_event(0x56, 0, 2, 0)        # V up
    time.sleep(0.05)
    user32.keybd_event(0x11, 0, 2, 0)        # Ctrl up


def main() -> int:
    if ezsearch_focus.SYSTEM != "Windows":
        print("This check is Windows specific.")
        return 0

    import pyperclip

    helper = Path(__file__).with_name("_fake_document.py")
    process = subprocess.Popen([sys.executable, str(helper)],
                               stdout=subprocess.PIPE, text=True)
    try:
        line = process.stdout.readline().strip()
        if not line:
            print("the fake document never started")
            return 1
        document_hwnd, edit_hwnd = (int(x, 0) for x in line.split())
        print(f"fake document window : {document_hwnd}")
        print(f"its focused edit     : {edit_hwnd}")

        import tkinter as tk
        root = tk.Tk()
        root.title("EZsearch popup")
        root.geometry("360x240+700+420")
        tk.Label(root, text="popup", font=("Segoe UI", 20)).pack(pady=40)
        root.update()
        popup_hwnd = int(root.wm_frame(), 16)     # Tk reports the frame as hex

        print("\n1. the document comes to the front")
        if not force_foreground(document_hwnd):
            print("   could not bring the document to the front")
            return 1
        print(f"   foreground   : {foreground()}")
        print(f"   focused child: {focused_child()}")

        print("\n2. EZsearch captures the focus (before the popup opens)")
        focus = ezsearch_focus.capture()
        print(f"   captured top level: {focus.top_level}")
        print(f"   captured child    : {focus.child}")
        ok = True
        if focus.top_level != document_hwnd:
            print("   FAIL: captured the wrong window")
            ok = False
        if focus.child != edit_hwnd:
            print("   FAIL: captured the wrong child control")
            ok = False

        print("\n3. the popup steals the focus")
        force_foreground(popup_hwnd)
        print(f"   foreground : {foreground()} (the popup, {popup_hwnd})")
        if foreground() != popup_hwnd:
            print("   FAIL: could not put the popup in front, so the restore was not tested")
            ok = False

        print("\n4. EZsearch restores the focus and pastes")
        pyperclip.copy(MARKER)
        user32.SendMessageW(edit_hwnd, 0x00B1, 0, -1)    # select all, paste replaces it

        seen: dict[str, int] = {}

        def paste_here() -> None:
            # Record what has focus at the moment the paste is sent. Checking
            # later is unreliable: an unrelated app can steal the foreground in
            # the meantime, and that is not EZsearch's fault.
            time.sleep(0.15)                 # let the control take the focus
            seen["window"] = foreground()
            seen["child"] = focused_child()
            send_ctrl_v()

        ezsearch_focus.restore(focus, paste_here)
        time.sleep(0.6)                      # let the target control process the keys

        now_window = seen.get("window", 0)
        now_child = seen.get("child", 0)
        text = read_text(edit_hwnd)
        print(f"   focus at paste time : {now_window} (should be {document_hwnd})")
        print(f"   caret at paste time : {now_child} (should be {edit_hwnd})")
        print(f"   foreground after    : {foreground()}")
        print(f"   text in edit        : {text!r} (should be {MARKER!r})")

        if now_window != document_hwnd:
            print("   FAIL: the document window did not come back to the front")
            ok = False
        if now_child != edit_hwnd:
            print("   FAIL: the caret did not go back to the text field")
            ok = False
        if text != MARKER:
            print("   FAIL: the paste did not land in the edit control")
            ok = False

        root.destroy()
        print()
        print("PASS - the focus and the paste land in the exact control you were typing in."
              if ok else "FAIL - the paste would land in the wrong place.")
        return 0 if ok else 1
    finally:
        process.terminate()


if __name__ == "__main__":
    sys.exit(main())
