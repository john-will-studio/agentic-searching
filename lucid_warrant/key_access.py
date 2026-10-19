"""Who else on this computer can reach the signing key: check and warn, and restrict on request.

The private key is encrypted with the passphrase, but another account that can read it can try passphrases offline,
and one that can write the folder can replace the public key (so warrants signed with another key would verify).
Lucid Warrant never changes permissions silently: it says what it found, and offers Restrict access.

- macOS / Linux: file modes. Restrict sets the keys folder to 700 and its files to 600.
- Windows: the folder's access list (DACL), read with the Win32 security API (never by running icacls, which
  endpoint tools flag). Restrict replaces it with: you, SYSTEM and Administrators (kept for backup and recovery
  tools), not inherited. Drives formatted exFAT or FAT can't hold permissions at all: the only fix is to move the
  warrant directory to an NTFS drive, such as your Documents folder.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from .config import Paths

# Who may reach the keys (everyone else is reported): the signed-in user, SYSTEM and Administrators (kept for
# enterprise backup and recovery tools). SDDL abbreviations and SIDs.
ALLOWED = {"SY", "S-1-5-18", "BA", "S-1-5-32-544"}
NAMES = {"WD": "Everyone", "S-1-1-0": "Everyone", "AU": "Authenticated Users", "S-1-5-11": "Authenticated Users",
         "BU": "Users", "S-1-5-32-545": "Users", "BG": "Guests", "S-1-5-32-546": "Guests", "IU": "Interactive users",
         "S-1-5-4": "Interactive users", "NU": "Network logon users", "AN": "Anonymous", "S-1-5-7": "Anonymous"}
# Access-right abbreviations and mask bits that allow writing (replacing the public key, or planting files).
WRITE_RIGHTS = {"FA", "GA", "GW", "FW", "WD", "WO", "DC", "SD", "CC", "WP", "DT"}
# GENERIC_ALL, GENERIC_WRITE, FILE_WRITE_DATA, FILE_APPEND_DATA, FILE_DELETE_CHILD, DELETE, WRITE_DAC, WRITE_OWNER
WRITE_MASK = 0x10000000 | 0x40000000 | 0x2 | 0x4 | 0x40 | 0x10000 | 0x40000 | 0x80000


def problems(paths: Paths) -> list[str]:
    """What's exposed, in plain sentences; empty when only you (and the system) can reach the keys."""
    keys = Path(paths.keys)
    if not keys.exists():
        return []
    try:
        return _windows_problems(paths) if os.name == "nt" else _posix_problems(paths)
    except Exception as exc:  # noqa: BLE001 - a check that can't run is reported, not hidden
        return [f"Couldn't check who can reach the signing key ({exc})."]


def _posix_problems(paths: Paths) -> list[str]:
    """What matters: others reading the encrypted private key, or writing where they could swap the public key
    or plant files (the keys folder, the public key, the data folder). A readable public key is fine."""
    out = []
    checks = [(Path(paths.keys), stat.S_IWGRP | stat.S_IWOTH, "The keys folder", "changed"),
              (Path(paths.private_key), stat.S_IRWXG | stat.S_IRWXO, Path(paths.private_key).name, "read"),
              (Path(paths.public_key), stat.S_IWGRP | stat.S_IWOTH, Path(paths.public_key).name, "replaced"),
              (Path(paths.data), stat.S_IWGRP | stat.S_IWOTH, "The data folder", "changed")]
    for path, bits, name, verb in checks:
        if not path.exists():
            continue
        mode = path.stat().st_mode
        if mode & bits:
            who = "other accounts on this computer" if mode & bits & 0o007 else "your group"
            out.append(f"{name} can be {verb} by {who} (permissions {stat.filemode(mode)}).")
    return out


def restrict(paths: Paths) -> None:
    """Limit the keys folder to you (and, on Windows, SYSTEM and Administrators), and stop others changing the
    data folder: the same things problems() reports."""
    keys = Path(paths.keys)
    if os.name == "nt":
        _windows_restrict(keys)
        if Path(paths.data).exists():
            _windows_restrict(Path(paths.data), children=False)  # inherited by what's inside
        return
    keys.chmod(0o700)
    for path in keys.iterdir():
        path.chmod(0o600)
    data = Path(paths.data)
    if data.exists():
        data.chmod(data.stat().st_mode & ~(stat.S_IWGRP | stat.S_IWOTH))


# --- Windows -------------------------------------------------------------------------------------------------------

def _windows_problems(paths: Paths) -> list[str]:
    """Per path, what matters: anyone else reading the private key; anyone else able to change the keys folder,
    the public key or the data folder. (Reading the public key is fine: it's public.)"""
    keys = Path(paths.keys)
    fs = _volume_filesystem(keys)
    if fs and fs.upper() in ("FAT", "FAT32", "EXFAT"):
        return [f"The drive holding your keys ({keys.drive or keys.anchor}, {fs}) can't restrict access: any account on "
                "this computer can read the key or replace the public key. Move the warrant directory to an NTFS "
                "drive, such as your Documents folder."]
    me = _current_user_sid()
    checks = [(keys, True, "The keys folder", "changed"), (Path(paths.private_key), False, Path(paths.private_key).name, "read"),
              (Path(paths.public_key), True, Path(paths.public_key).name, "replaced"),
              (Path(paths.data), True, "The data folder", "changed")]
    out = []
    for path, write_only, name, verb in checks:
        if not path.exists():
            continue
        others = _others(_sddl(path), me, write_only=write_only)
        if others:
            out.append(f"{name} can be {verb} by {', '.join(others)}.")
    return out


def _rights_allow_write(rights: str) -> bool:
    if rights.lower().startswith("0x"):
        try:
            return bool(int(rights, 16) & WRITE_MASK)
        except ValueError:
            return True
    return any(rights[i:i + 2] in WRITE_RIGHTS for i in range(0, len(rights), 2))


def _others(sddl: str, me: str, *, write_only: bool) -> list[str]:
    """Accounts other than you, SYSTEM and Administrators that an SDDL access list allows in (or allows to write,
    with write_only), by name. Fails safe: no access list at all means everyone; entries whose effect depends on
    conditions this check can't evaluate are reported as such, never treated as harmless."""
    sddl = sddl or ""
    owner = (re.search(r"O:([^:()]+?)(?=[GDS]:|$)", sddl) or [None, ""])[1]
    dacl = (re.search(r"D:(.*?)(?=S:\(|$)", sddl) or [None, None])[1]
    if dacl is None or dacl.startswith("NO_ACCESS_CONTROL"):
        return ["everyone (there is no access list)"]
    entries = re.findall(r"\(([A-Z]+);([^;]*);([^;]*);[^;]*;[^;]*;([^;)]*)", dacl)
    found = []
    # Windows grants the owner implicit WRITE_DAC unless an effective Owner Rights ACE overrides it.
    # An otherwise empty/restrictive DACL is not safe when an untrusted account owns the file.
    owner_limited = any(sid in ("OW", "S-1-3-4") and "IO" not in flags for _, flags, _, sid in entries)
    if owner and owner not in ALLOWED and owner != me and not owner_limited:
        found.append(_account_name(owner) + " (owner can change permissions)")
    for ace_type, flags, rights, sid in entries:
        if ace_type in ("D", "OD", "XD") or "IO" in flags:
            continue  # deny entries and inherit-only entries don't grant access to this item
        if sid in ("SY", "S-1-5-18", "BA", "S-1-5-32-544") or sid == me:
            continue
        if sid in ("OW", "S-1-3-4") and owner in (me, "SY", "S-1-5-18", "BA", "S-1-5-32-544"):
            continue  # owner rights, and you (or the system) own it
        if write_only and not _rights_allow_write(rights):
            continue
        name = _account_name(owner if sid in ("OW", "S-1-3-4") else sid)
        if ace_type not in ("A", "OA"):
            name += " (under conditions this check can't evaluate)"
        if name not in found:
            found.append(name)
    return found


def _account_name(sid: str) -> str:
    if sid in NAMES:
        return NAMES[sid]
    if os.name != "nt" or not sid.startswith("S-"):
        return sid
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.windll.advapi32
    psid = ctypes.c_void_p()
    if not advapi.ConvertStringSidToSidW(ctypes.c_wchar_p(sid), ctypes.byref(psid)):
        return sid
    try:
        name, domain = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        n, d, use = wintypes.DWORD(256), wintypes.DWORD(256), wintypes.DWORD()
        if advapi.LookupAccountSidW(None, psid, name, ctypes.byref(n), domain, ctypes.byref(d), ctypes.byref(use)):
            return f"{domain.value}\\{name.value}" if domain.value else name.value
        return f"an unknown account ({sid})"
    finally:
        ctypes.windll.kernel32.LocalFree(psid)


def _volume_filesystem(path: Path) -> str:
    import ctypes

    root = str(Path(path).resolve().anchor)
    name = ctypes.create_unicode_buffer(64)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(ctypes.c_wchar_p(root), None, 0, None, None, None, name, 64)
    return name.value if ok else ""


def _sddl(path: Path) -> str:
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.windll.advapi32
    sd = ctypes.c_void_p()
    DACL = 0x4 | 0x1  # DACL_SECURITY_INFORMATION | OWNER_SECURITY_INFORMATION (owner rights depend on the owner)
    rc = advapi.GetNamedSecurityInfoW(ctypes.c_wchar_p(str(path)), 1, DACL, None, None, None, None, ctypes.byref(sd))
    if rc != 0:
        raise OSError(rc, "GetNamedSecurityInfoW failed")
    try:
        text = wintypes.LPWSTR()
        if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(sd, 1, DACL, ctypes.byref(text), None):
            raise OSError(ctypes.GetLastError(), "ConvertSecurityDescriptorToStringSecurityDescriptorW failed")
        value = text.value
        ctypes.windll.kernel32.LocalFree(text)
        return value
    finally:
        ctypes.windll.kernel32.LocalFree(sd)


def _current_user_sid() -> str:
    import ctypes
    from ctypes import wintypes

    advapi, kernel = ctypes.windll.advapi32, ctypes.windll.kernel32
    token = wintypes.HANDLE()
    # GetCurrentProcess returns the pseudo-handle -1. Passed back as a plain int, ctypes sends a 32-bit value
    # (0x00000000FFFFFFFF on 64-bit Windows), which isn't a handle: OpenProcessToken failed with error 6 and the
    # whole key access check and Restrict access failed. Wrap it as a pointer-sized HANDLE.
    process = wintypes.HANDLE(kernel.GetCurrentProcess())
    if not advapi.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        raise OSError(ctypes.GetLastError(), "OpenProcessToken failed")
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        buf = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)):
            raise OSError(ctypes.GetLastError(), "GetTokenInformation failed")
        sid = ctypes.c_void_p.from_buffer(buf).value  # TOKEN_USER.User.Sid
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(ctypes.c_void_p(sid), ctypes.byref(text)):
            raise OSError(ctypes.GetLastError(), "ConvertSidToStringSidW failed")
        value = text.value
        kernel.LocalFree(text)
        return value
    finally:
        kernel.CloseHandle(token)


def _windows_restrict(keys: Path, children: bool = True) -> None:
    import ctypes
    from ctypes import wintypes

    advapi, kernel = ctypes.windll.advapi32, ctypes.windll.kernel32
    user = _current_user_sid()
    for path, inherit in [(keys, "OICI"), *([(p, "") for p in keys.iterdir()] if children else [])]:
        # Protected (P): not inherited from the parent. Full access for you, SYSTEM and Administrators only.
        sddl = f"D:P(A;{inherit};FA;;;{user})(A;{inherit};FA;;;SY)(A;{inherit};FA;;;BA)"
        sd = ctypes.c_void_p()
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(ctypes.c_wchar_p(sddl), 1,
                                                                            ctypes.byref(sd), None):
            raise OSError(ctypes.GetLastError(), "ConvertStringSecurityDescriptorToSecurityDescriptorW failed")
        try:
            present, dacl, defaulted = ctypes.c_int(), ctypes.c_void_p(), ctypes.c_int()
            if not advapi.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
                raise OSError(ctypes.GetLastError(), "GetSecurityDescriptorDacl failed")
            # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION, as a DWORD: 0x80000004 doesn't fit the
            # signed int ctypes assumes for a bare Python int ("OverflowError: int too long to convert").
            flags = wintypes.DWORD(0x4 | 0x80000000)
            rc = advapi.SetNamedSecurityInfoW(ctypes.c_wchar_p(str(path)), 1, flags, None, None, dacl, None)
            if rc != 0:
                raise OSError(rc, f"SetNamedSecurityInfoW failed for {path}")
        finally:
            kernel.LocalFree(sd)
