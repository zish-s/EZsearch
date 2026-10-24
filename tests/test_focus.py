"""
Tests for the focus capture / restore logic - the code that decides whether
the picture lands in the right document or vanishes.

These run against a real window on Windows, so they exercise the actual Win32
calls rather than a mock of them.
"""
from __future__ import annotations

import platform

import pytest

import ezsearch_focus

pytestmark = pytest.mark.skipif(platform.system() != "Windows",
                                reason="the Win32 focus logic is Windows specific")


@pytest.fixture(scope="module")
def window():
    """A toplevel with a focusable child, like Word has.

    Tk cannot reliably create a second root after the first one is destroyed,
    so the whole module shares one.
    """
    import tkinter as tk
    root = tk.Tk()
    root.geometry("200x100+100+100")
    top = tk.Toplevel(root)
    top.geometry("200x100+300+300")
    entry = tk.Entry(top)
    entry.pack()
    entry.focus_force()
    root.update()
    yield root, top, entry
    try:
        root.destroy()
    except Exception:
        pass


def test_capture_finds_the_foreground_window(window):
    focus = ezsearch_focus.capture()
    assert isinstance(focus, ezsearch_focus.Focus)
    assert focus.top_level != 0, "we should have found a foreground window"


def test_capture_maps_the_focused_child_control(monkeypatch):
    """The child half of a Focus snapshot comes from GetGUIThreadInfo's hwndFocus.

    This is scripted rather than read from the live desktop on purpose: the
    foreground window belongs to whatever the test runner is sitting in, so a
    real read would test the tester, not EZsearch. The live path - finding the
    foreground window itself - is covered by the test above; the restore tests
    below exercise the real Win32 calls against the Tk window.
    """
    calls = {"hwnd": None}

    def fake_thread_id(hwnd, ptr):
        calls["hwnd"] = hwnd
        return 77

    def fake_thread_info(thread_id, ref):
        assert thread_id == 77
        info = ref._obj
        info.hwndFocus = 0xBEEF
        return 1

    monkeypatch.setattr(ezsearch_focus.user32, "GetWindowThreadProcessId", fake_thread_id)
    monkeypatch.setattr(ezsearch_focus.user32, "GetGUIThreadInfo", fake_thread_info)

    focus = ezsearch_focus._focus_from_hwnd(1234)
    assert calls["hwnd"] == 1234
    assert focus.top_level == 1234
    assert focus.child == 0xBEEF, "the caret's child control must survive into Focus"


def test_restore_runs_the_action(window):
    focus = ezsearch_focus.capture()
    called = []
    ezsearch_focus.restore(focus, lambda: called.append(True))
    assert called == [True]


def test_restore_with_no_focus_still_runs_the_action():
    """A missing focus must not stop the paste from happening."""
    called = []
    ezsearch_focus.restore(ezsearch_focus.Focus(), lambda: called.append(True))
    assert called == [True]


def test_restore_does_not_raise(window):
    focus = ezsearch_focus.capture()
    assert ezsearch_focus.restore(focus, lambda: None) in (True, False)


def test_focus_truthiness():
    assert not ezsearch_focus.Focus()
    assert ezsearch_focus.Focus(top_level=123)
    assert ezsearch_focus.Focus(app_name="Microsoft Word")
