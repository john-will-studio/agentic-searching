"""Desktop work rectangles in Tk coordinates, without optional Python dependencies.

Windows uses MonitorFromWindow / GetMonitorInfoW.rcWork (including side taskbars).
macOS calls NSScreen.screens, frame and visibleFrame through the system Objective-C
runtime. Cocoa points and Tk coordinates share units; flip Y about the primary
screen's top, not the selected screen's height. NSRect uses objc_msgSend_stret on
Intel and objc_msgSend on arm64. See Apple's NSScreen visibleFrame documentation:
https://developer.apple.com/documentation/appkit/nsscreen/visibleframe
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-getmonitorinfow

If native queries fail, or on other window systems, Tk's virtual-root rectangle
is the fallback. Tk exposes no portable taskbar/work-area API: this fallback
cannot promise to exclude desktop panels. Never infer work area from wm maxsize.
"""
from __future__ import annotations

import ctypes as C
import platform
import re
import sys


def _windows_work_area(owner):
    from ctypes import wintypes as W

    class MonitorInfo(C.Structure):
        _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", W.DWORD)]

    user = C.WinDLL("user32", use_last_error=True)
    user.GetAncestor.argtypes, user.GetAncestor.restype = [W.HWND, W.UINT], W.HWND
    user.MonitorFromWindow.argtypes, user.MonitorFromWindow.restype = [W.HWND, W.DWORD], W.HANDLE
    user.GetMonitorInfoW.argtypes, user.GetMonitorInfoW.restype = [W.HANDLE, C.POINTER(MonitorInfo)], W.BOOL
    hwnd = user.GetAncestor(owner.winfo_id(), 2) or owner.winfo_id()  # GA_ROOT: Tk's wrapper window
    info = MonitorInfo(cbSize=C.sizeof(MonitorInfo))
    monitor = user.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    if not user.GetMonitorInfoW(monitor, C.byref(info)):
        raise C.WinError(C.get_last_error())
    r = info.rcWork
    return r.left, r.top, r.right, r.bottom


def _mac_work_area(owner):
    class Point(C.Structure):
        _fields_ = [("x", C.c_double), ("y", C.c_double)]

    class Size(C.Structure):
        _fields_ = [("width", C.c_double), ("height", C.c_double)]

    class Rect(C.Structure):
        _fields_ = [("origin", Point), ("size", Size)]

    C.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
    objc = C.CDLL("/usr/lib/libobjc.A.dylib")
    objc.objc_getClass.argtypes, objc.objc_getClass.restype = [C.c_char_p], C.c_void_p
    objc.sel_registerName.argtypes, objc.sel_registerName.restype = [C.c_char_p], C.c_void_p

    def send(obj, name, result=C.c_void_p, args=(), types=()):
        fn = C.CFUNCTYPE(result, C.c_void_p, C.c_void_p, *types)(("objc_msgSend", objc))
        return fn(obj, objc.sel_registerName(name.encode()), *args)

    def rect(obj, name):
        if platform.machine() == "x86_64":
            result = Rect()
            fn = C.CFUNCTYPE(None, C.POINTER(Rect), C.c_void_p, C.c_void_p)(("objc_msgSend_stret", objc))
            fn(C.byref(result), obj, objc.sel_registerName(name.encode()))
            return result
        return send(obj, name, Rect)

    screens = send(objc.objc_getClass(b"NSScreen"), "screens")
    count = send(screens, "count", C.c_ulong)
    if not count:
        raise OSError("NSScreen returned no screens")
    objects = [send(screens, "objectAtIndex:", args=(i,), types=(C.c_ulong,)) for i in range(count)]
    primary = rect(objects[0], "frame")
    ceiling = primary.origin.y + primary.size.height

    def bounds(r):
        return (round(r.origin.x), round(ceiling - r.origin.y - r.size.height),
                round(r.origin.x + r.size.width), round(ceiling - r.origin.y))

    x, y = owner.winfo_rootx(), owner.winfo_rooty()
    w, h = owner.winfo_width(), owner.winfo_height()

    def score(obj):
        left, top, right, bottom = bounds(rect(obj, "frame"))
        overlap = max(0, min(x + w, right) - max(x, left)) * max(0, min(y + h, bottom) - max(y, top))
        distance = max(left - x, 0, x - right) ** 2 + max(top - y, 0, y - bottom) ** 2
        return overlap, -distance

    return bounds(rect(max(objects, key=score), "visibleFrame"))


def work_area(widget):
    owner = widget.winfo_toplevel()
    try:
        if sys.platform == "win32":
            return _windows_work_area(owner)
        if sys.platform == "darwin":
            return _mac_work_area(owner)
    except (OSError, AttributeError, ValueError):
        pass
    x, y = owner.winfo_vrootx(), owner.winfo_vrooty()
    return x, y, x + owner.winfo_vrootwidth(), y + owner.winfo_vrootheight()


def frame_insets(window):
    """Left, top, right, bottom decoration pixels; conservative before first map."""
    if sys.platform == "win32":
        from ctypes import wintypes as W

        class WindowInfo(C.Structure):
            _fields_ = [("cbSize", W.DWORD), ("rcWindow", W.RECT), ("rcClient", W.RECT),
                        ("dwStyle", W.DWORD), ("dwExStyle", W.DWORD), ("dwWindowStatus", W.DWORD),
                        ("cxWindowBorders", W.UINT), ("cyWindowBorders", W.UINT),
                        ("atomWindowType", W.WORD), ("wCreatorVersion", W.WORD)]

        user = C.WinDLL("user32", use_last_error=True)
        user.GetAncestor.argtypes, user.GetAncestor.restype = [W.HWND, W.UINT], W.HWND
        user.GetWindowInfo.argtypes, user.GetWindowInfo.restype = [W.HWND, C.POINTER(WindowInfo)], W.BOOL
        hwnd = user.GetAncestor(window.winfo_id(), 2) or window.winfo_id()
        info = WindowInfo(cbSize=C.sizeof(WindowInfo))
        if user.GetWindowInfo(hwnd, C.byref(info)) and info.dwStyle & 0x00C00000:  # WS_CAPTION
            # Both rectangles are screen coordinates, even before mapping. Do not
            # subtract Tk's still-unmapped 1x1 client size or assume 96-DPI chrome.
            r, client = info.rcWindow, info.rcClient
            return client.left - r.left, client.top - r.top, r.right - client.right, r.bottom - client.bottom
        # Tk may not have created its decorated wrapper yet. Reserve a full
        # resizable frame at this window's DPI; fixed-size dialogs need no more.
        user.GetSystemMetrics.argtypes, user.GetSystemMetrics.restype = [C.c_int], C.c_int
        metric = user.GetSystemMetrics
        if hasattr(user, "GetSystemMetricsForDpi") and hasattr(user, "GetDpiForWindow"):
            user.GetDpiForWindow.argtypes, user.GetDpiForWindow.restype = [W.HWND], W.UINT
            user.GetSystemMetricsForDpi.argtypes, user.GetSystemMetricsForDpi.restype = [C.c_int, W.UINT], C.c_int
            dpi = user.GetDpiForWindow(hwnd) or 96
            metric = lambda index: user.GetSystemMetricsForDpi(index, dpi)
        x, y = metric(32) + metric(92), metric(33) + metric(92)
        return x, y + metric(4), x, y
    match = re.search(r"([+-]\d+)([+-]\d+)$", window.geometry())
    if window.winfo_ismapped() and match:
        top = window.winfo_rooty() - int(match[2])
        if 0 <= top < 80:
            return 0, top, 0, 0
    return 0, 32, 0, 0
