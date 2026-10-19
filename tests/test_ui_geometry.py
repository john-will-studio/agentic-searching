"""Native work-area API contracts; widget placement is covered in GUI smoke tests."""
import ctypes
from unittest.mock import Mock

from lucid_warrant.ui import geometry


def test_windows_work_rectangle_keeps_monitor_origin_and_all_reserved_edges(monkeypatch):
    # Secondary monitor left of primary, taskbar on left, reserved top/right/bottom.
    hwnd, monitor = 0x123456789, 0x987654321
    user = Mock()
    user.GetAncestor = Mock(return_value=hwnd)
    user.MonitorFromWindow = Mock(return_value=monitor)

    def info(handle, pointer):
        assert handle == monitor
        record = pointer._obj
        assert record.cbSize == ctypes.sizeof(record)
        record.rcMonitor.left, record.rcMonitor.top = -1920, -200
        record.rcMonitor.right, record.rcMonitor.bottom = 0, 880
        record.rcWork.left, record.rcWork.top = -1870, -170
        record.rcWork.right, record.rcWork.bottom = -20, 830
        return 1

    user.GetMonitorInfoW = Mock(side_effect=info)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: user, raising=False)
    owner = Mock()
    owner.winfo_id.return_value = 123
    assert geometry._windows_work_area(owner) == (-1870, -170, -20, 830)
    assert ctypes.sizeof(user.MonitorFromWindow.restype) == ctypes.sizeof(ctypes.c_void_p)
    assert ctypes.sizeof(user.GetAncestor.restype) == ctypes.sizeof(ctypes.c_void_p)
    user.MonitorFromWindow.assert_called_once_with(hwnd, 2)


def test_fallback_uses_virtual_root_not_configurable_maximum(monkeypatch):
    monkeypatch.setattr(geometry.sys, "platform", "other")
    owner = Mock()
    owner.winfo_toplevel.return_value = owner
    owner.winfo_vrootx.return_value, owner.winfo_vrooty.return_value = -1600, -50
    owner.winfo_vrootwidth.return_value, owner.winfo_vrootheight.return_value = 3520, 1130
    assert geometry.work_area(owner) == (-1600, -50, 1920, 1080)
    owner.wm_maxsize.assert_not_called()


def test_windows_unmapped_dialog_uses_native_frame_at_current_dpi(monkeypatch):
    user = Mock()
    user.GetAncestor = Mock(return_value=0x123456789)

    def info(_handle, pointer):
        record = pointer._obj
        record.dwStyle = 0x00C00000
        record.rcWindow.left, record.rcWindow.top = -900, 30
        record.rcWindow.right, record.rcWindow.bottom = -100, 630
        record.rcClient.left, record.rcClient.top = -888, 78
        record.rcClient.right, record.rcClient.bottom = -112, 618
        return 1

    user.GetWindowInfo = Mock(side_effect=info)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: user, raising=False)
    monkeypatch.setattr(geometry.sys, "platform", "win32")
    window = Mock()
    window.winfo_ismapped.return_value = False
    window.winfo_width.return_value = window.winfo_height.return_value = 1
    assert geometry.frame_insets(window) == (12, 48, 12, 12)
