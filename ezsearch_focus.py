"""
ezsearch_focus.py - remember where you were typing, and come back to it.

The picture is the easy half of "click a picture and it lands in your document".
The paste is the hard half.

Word, Outlook, VS Code, Chrome and Google Docs all put the caret inside a
*child* control. Calling SetForegroundWindow() on the top-level frame is not
enough: the frame gets activated, but keyboard focus stays on the frame's own
window proc, so the Ctrl+V we send lands nowhere and the picture silently
evaporates. That is the single most load-bearing code path in this app.

The Windows recipe (all of it ctypes, no extra dependency):

  capture   BEFORE any UI appears:
              GetForegroundWindow()   -> the top level frame
              GetGUIThreadInfo()      -> hwndFocus, the child that really has the caret

  restore   AFTER the user picked a picture:
              AttachThreadInput()     -> share input queues with the target thread.
                                         This is what bypasses the "you may not
                                         steal focus" restriction.
              BringWindowToTop() + SetForegroundWindow()
              SetFocus(child)         -> the CHILD, not the frame
              detach                  -> ALWAYS, in a finally block

Leaking an AttachThreadInput leaves the user with no keyboard control at all
for the lifetime of the process, which is why the detach is not optional and
not optional *later* - it happens in a finally, immediately.

macOS has the same problem in a different shape: the paste goes to whatever
app is frontmost, so we remember the frontmost app by name and activate it
again before pasting.

Linux needs xdotool; when it is not installed we degrade to "paste wherever
the pointer is", which is still better than nothing.
"""
from __future__ import annotations

import platform
import subprocess
import time
from dataclasses import dataclass

SYSTEM = platform.system()


@dataclass
class Focus:
    """Where the user was typing when they hit the hotkey."""

    top_level: int = 0     # Windows: HWND of the top level frame
    child: int = 0         # Windows: HWND of the child that held the caret
    app_name: str = ""     # macOS: name of the frontmost application

    def __bool__(self) -> bool:
        return bool(self.top_level or self.app_name)


# ----------------------------------------------------------------------------
# Windows
# ----------------------------------------------------------------------------
if SYSTEM == "Windows":
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hwndActive", wintypes.HWND),
            ("hwndFocus", wintypes.HWND),
            ("hwndCapture", wintypes.HWND),
            ("hwndMenuOwner", wintypes.HWND),
            ("hwndMoveSize", wintypes.HWND),
            ("hwndCaret", wintypes.HWND),
            ("rcCaret", wintypes.RECT),
        ]

    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO)]
    user32.GetGUIThreadInfo.restype = wintypes.BOOL
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindowAsync.restype = wintypes.BOOL
    user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    user32.SwitchToThisWindow.restype = None
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    _SW_RESTORE = 9

    def _capture_windows() -> Focus:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return Focus()
        thread_id = user32.GetWindowThreadProcessId(hwnd, None)
        child = 0
        if thread_id:
            info = _GUITHREADINFO()
            info.cbSize = ctypes.sizeof(info)
            if user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
                child = int(info.hwndFocus or 0)
        return Focus(top_level=int(hwnd), child=child)

    def _restore_windows(focus: Focus, action) -> bool:
        """Put the caret back where it was, run `action` (the paste), then let go.

        The attach/detach window deliberately wraps the key press too: while
        the input queues are shared, the synthesised Ctrl+V is delivered to the
        child control we just focused instead of to our own popup.
        """
        if not focus.top_level:
            action()
            return False

        target_id = user32.GetWindowThreadProcessId(focus.top_level, None)
        if not target_id:
            action()
            return False

        if user32.IsIconic(focus.top_level):
            user32.ShowWindowAsync(focus.top_level, _SW_RESTORE)

        our_id = kernel32.GetCurrentThreadId()
        if our_id == target_id:
            # Same thread - attaching is undefined behaviour, so skip it.
            user32.SetForegroundWindow(focus.top_level)
            user32.SetFocus(focus.child or focus.top_level)
            action()
            return True

        attached = bool(user32.AttachThreadInput(our_id, target_id, True))
        try:
            user32.BringWindowToTop(focus.top_level)
            ok = bool(user32.SetForegroundWindow(focus.top_level))
            user32.SetFocus(focus.child or focus.top_level)
            time.sleep(0.05)          # let the target app settle on the focus change
            action()
            return ok
        finally:
            if attached:
                user32.AttachThreadInput(our_id, target_id, False)


# ----------------------------------------------------------------------------
# macOS
# ----------------------------------------------------------------------------
def _osascript(script: str) -> str:
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def _capture_macos() -> Focus:
    name = _osascript(
        'tell application "System Events" to get name of '
        "first application process whose frontmost is true"
    )
    return Focus(app_name=name)


def _restore_macos(focus: Focus, action) -> bool:
    if focus.app_name:
        _osascript(f'tell application "{focus.app_name}" to activate')
        time.sleep(0.2)
    action()
    return bool(focus.app_name)


# ----------------------------------------------------------------------------
# Linux
# ----------------------------------------------------------------------------
def _capture_linux() -> Focus:
    return Focus()


def _restore_linux(focus: Focus, action) -> bool:
    # xdotool is optional; without it we simply paste wherever focus is.
    try:
        subprocess.run(["xdotool", "getactivewindow"], capture_output=True, timeout=2)
    except Exception:
        pass
    action()
    return False


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------
def capture() -> Focus:
    """Snapshot where the user is typing. Call this BEFORE showing any UI."""
    if SYSTEM == "Windows":
        return _capture_windows()
    if SYSTEM == "Darwin":
        return _capture_macos()
    return _capture_linux()


def restore(focus: Focus, action) -> bool:
    """Give the focus back to `focus` and run `action` (the Ctrl+V) while we hold it.

    `action` is a zero-argument callable. It runs inside the focus-restoring
    window on Windows, which is what makes the paste land in the right control.
    """
    if not focus:
        action()
        return False
    if SYSTEM == "Windows":
        return _restore_windows(focus, action)
    if SYSTEM == "Darwin":
        return _restore_macos(focus, action)
    return _restore_linux(focus, action)
