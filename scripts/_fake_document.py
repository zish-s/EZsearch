"""
A stand-in for "the document you were typing in".

A separate OS process with a top level window and a real Win32 EDIT control
inside it - the same shape as Word, Outlook or VS Code, where the caret lives in
a child control rather than on the frame. Unlike a Tk entry, WM_GETTEXT works
on it, so the verification script can prove that the pasted text actually
landed in the control, not just that some window came to the front.

Prints the two window handles (hex) on stdout and then sits there until killed.

Used by scripts/verify_pasteback.py.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

WM_DESTROY = 0x0002
WM_SETFOCUS = 0x0007
WM_SETFONT = 0x0030
EM_SETSEL = 0x00B1
SW_SHOW = 5

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_void_p, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
]
user32.DefWindowProcW.restype = ctypes.c_void_p
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.RegisterClassW.restype = wt.ATOM
user32.SendMessageW.restype = ctypes.c_void_p
user32.SetFocus.argtypes = [wt.HWND]
user32.SetForegroundWindow.argtypes = [wt.HWND]

edit_hwnd: int = 0


@WNDPROC
def wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_SETFOCUS and edit_hwnd:
        user32.SetFocus(edit_hwnd)
        return 0
    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


def main() -> int:
    global edit_hwnd

    hinst = kernel32.GetModuleHandleW(None)
    cls = WNDCLASSW()
    cls.lpfnWndProc = wndproc
    cls.hInstance = hinst
    cls.hbrBackground = user32.GetSysColorBrush(5)       # COLOR_WINDOW
    cls.lpszClassName = "EZsearchFakeDocument"
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    if not user32.RegisterClassW(ctypes.byref(cls)):
        print("could not register the window class", file=sys.stderr)
        return 1

    hwnd = user32.CreateWindowExW(
        0, cls.lpszClassName, "fake document", 0x00CF0000,
        140, 140, 520, 200, None, None, hinst, None)
    if not hwnd:
        print("could not create the window", file=sys.stderr)
        return 1

    style = (0x40000000      # WS_CHILD
             | 0x10000000    # WS_VISIBLE
             | 0x00800000    # WS_BORDER
             | 0x00200000    # WS_VSCROLL
             | 0x0004        # ES_MULTILINE
             | 0x0040)       # ES_AUTOVSCROLL
    edit_hwnd = int(user32.CreateWindowExW(
        0, "EDIT", "The old lighthouse stood against a violent storm.",
        style, 12, 12, 480, 140, hwnd, None, hinst, None))
    if not edit_hwnd:
        print("could not create the edit control", file=sys.stderr)
        return 1

    gdi32.GetStockObject.restype = wt.HGDIOBJ
    user32.SendMessageW(edit_hwnd, WM_SETFONT, gdi32.GetStockObject(17), 1)

    user32.ShowWindow(hwnd, SW_SHOW)
    user32.UpdateWindow(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.SetFocus(edit_hwnd)
    user32.SendMessageW(edit_hwnd, EM_SETSEL, 0, -1)     # select all, so a paste replaces

    print(hex(hwnd), hex(edit_hwnd), flush=True)

    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
