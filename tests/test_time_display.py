"""How shown times name their zone. Windows names zones in full ("Eastern Daylight Time"), which widened every time
column and the status bar; those are shown as a UTC offset, while short names (macOS's "EDT") are kept."""

from datetime import datetime, timedelta, timezone, tzinfo

from lucid_warrant.ui import widgets


class Named(tzinfo):
    def __init__(self, name, hours, minutes=0):
        self.name, self.offset = name, timedelta(hours=hours, minutes=minutes)

    def utcoffset(self, _dt):
        return self.offset

    def tzname(self, _dt):
        return self.name

    def dst(self, _dt):
        return timedelta(0)


def at(zone):
    return datetime(2026, 10, 18, 22, 3, tzinfo=timezone.utc).astimezone(zone)


def test_long_windows_zone_names_become_a_utc_offset():
    assert widgets.zone_label(at(Named("Eastern Daylight Time", -4))) == "UTC-04:00"
    assert widgets.zone_label(at(Named("India Standard Time", 5, 30))) == "UTC+05:30"
    assert widgets.zone_label(at(Named("W. Europe Standard Time", 1))) == "UTC+01:00"


def test_short_zone_names_and_utc_are_kept():
    assert widgets.zone_label(at(Named("EDT", -4))) == "EDT"
    assert widgets.zone_label(at(timezone.utc)) == "UTC"


def test_local_time_uses_the_short_label(monkeypatch):
    monkeypatch.setitem(widgets.TIME_ZONE, "value", "local")
    monkeypatch.setattr(widgets, "shown_time", lambda moment: moment.astimezone(Named("Eastern Daylight Time", -4)))
    assert widgets.local_time("2026-10-18T22:03:00Z") == "2026-10-18 18:03 UTC-04:00"
