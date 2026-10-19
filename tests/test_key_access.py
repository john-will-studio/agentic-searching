"""Who else can reach the signing key (issue #2): checked and reported, restricted only when asked."""

import os

import pytest

from lucid_warrant.key_access import _others, _rights_allow_write, problems, restrict

posix_only = pytest.mark.skipif(os.name == "nt", reason="file modes; Windows uses access lists")


@posix_only
def test_loose_permissions_are_reported_and_can_be_restricted(paths, key):
    paths.keys.chmod(0o777)
    paths.private_key.chmod(0o644)
    paths.public_key.chmod(0o644)  # readable by everyone: fine for a public key
    found = problems(paths)
    assert any("The keys folder can be changed" in p for p in found)
    assert any(paths.private_key.name in p and "read" in p for p in found)
    assert not any(paths.public_key.name in p for p in found), "a readable public key isn't a problem"
    restrict(paths)
    assert problems(paths) == []
    assert oct(paths.keys.stat().st_mode & 0o777) == "0o700" and oct(paths.private_key.stat().st_mode & 0o777) == "0o600"


@posix_only
def test_a_new_key_needs_no_warning(paths, key):
    assert problems(paths) == []


def test_any_account_besides_you_system_and_administrators_is_reported(monkeypatch):
    """Issue #13 and round 10 #1/#12: anyone else allowed in is named (resolution stubbed: this tests the parser);
    no access list means everyone; conditional entries aren't treated as harmless; delete-child counts as write;
    owner rights are exempt only when you own the item."""
    from lucid_warrant import key_access

    monkeypatch.setattr(key_access, "_account_name", lambda sid: {"BG": "Guests", "AU": "Authenticated Users"}.get(sid, sid))
    me = "S-1-5-21-1-2-3-1001"
    sddl = (f"O:{me}D:AI(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;FA;;;{me})(A;OICI;FR;;;BG)"
            "(A;OICI;0x1200a9;;;S-1-5-21-1-2-3-1002)(A;OICIIO;GA;;;CO)(A;;FA;;;OW)")
    assert _others(sddl, me, write_only=False) == ["Guests", "S-1-5-21-1-2-3-1002"]
    assert _others(sddl, me, write_only=True) == [], "read-only entries don't count as writers"
    assert _others("D:(A;;0x1301bf;;;AU)", me, write_only=True) == ["Authenticated Users"]
    assert _others("D:NO_ACCESS_CONTROL", me, write_only=False) == ["everyone (there is no access list)"]
    assert _others("O:BA", me, write_only=False) == ["everyone (there is no access list)"]
    assert _others("D:(XA;;FR;;;BG;(Member_of {SID(S-1-1-0)}))", me, write_only=False) == [
        "Guests (under conditions this check can't evaluate)"]
    assert _others("D:(A;;0x40;;;BG)", me, write_only=True) == ["Guests"], "delete-child is a write"
    assert _others("O:S-1-5-21-9D:(A;;FA;;;OW)", me, write_only=True) == ["S-1-5-21-9"], "someone else owns it"
    assert _rights_allow_write("FA") and not _rights_allow_write("FR") and _rights_allow_write("0x40000000")
    assert _rights_allow_write("DT")


@posix_only
def test_atomic_write_respects_a_restrictive_umask(tmp_path):
    """Round 10 #2: files aren't widened beyond the user's umask."""
    from lucid_warrant.signing import atomic_write

    old = os.umask(0o077)
    try:
        atomic_write(tmp_path / "x.json", b"sensitive")
    finally:
        os.umask(old)
    assert oct((tmp_path / "x.json").stat().st_mode & 0o777) == "0o600"


def test_another_owner_can_change_even_an_empty_access_list(monkeypatch):
    from lucid_warrant import key_access

    monkeypatch.setattr(key_access, "_account_name", lambda sid: sid)
    me, other = "S-1-5-21-1001", "S-1-5-21-1002"
    for write_only in (False, True):
        assert _others(f"O:{other}D:", me, write_only=write_only) == [other + " (owner can change permissions)"]
        assert _others(f"O:{me}D:", me, write_only=write_only) == []
    assert _others(f"O:{other}D:(A;;RC;;;OW)", me, write_only=True) == []
    assert _others(f"O:{other}D:(A;IO;RC;;;OW)", me, write_only=True) == [other + " (owner can change permissions)"]


windows_only = pytest.mark.skipif(os.name != "nt", reason="real Windows access lists")


@windows_only
def test_windows_check_and_restrict_with_real_access_lists(paths, key):
    """The Windows path itself, not parsed SDDL: OpenProcessToken failed with error 6 (the pseudo-handle -1 sent as a
    32-bit int), so every check said "Couldn't check…" and Restrict access raised. Guests are reported, and Restrict
    access leaves only you, SYSTEM and Administrators."""
    import subprocess

    from lucid_warrant.key_access import _current_user_sid

    assert _current_user_sid().startswith("S-1-5-")
    restrict(paths)  # pytest's temporary folders inherit whatever %TEMP% grants
    for folder in (paths.keys, paths.data):
        if folder.exists():
            subprocess.run(["icacls", str(folder), "/inheritance:r", "/grant:r", f"*{_current_user_sid()}:(OI)(CI)F",
                            "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"], check=True, capture_output=True)
    assert problems(paths) == []
    subprocess.run(["icacls", str(paths.private_key), "/grant", "*S-1-5-32-546:R"], check=True, capture_output=True)
    found = problems(paths)
    assert any(paths.private_key.name in p and "read" in p for p in found), found
    restrict(paths)
    assert not any(paths.private_key.name in p for p in problems(paths))
