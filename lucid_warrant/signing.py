"""Ed25519 signing for warrants, one-time approvals and the template library.

The private key is stored encrypted with the user's passphrase (scrypt +
AES-256-GCM) and decrypted only for the moment of signing. Verification needs
only the public key, so the executor never handles the passphrase.

Each signed document is written as canonical JSON bytes (`name.json`) with a
detached signature (`name.json.sig`). Verification checks the exact bytes on
disk before parsing them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .config import Paths

MIN_PASSPHRASE_LENGTH = 8
# The original product name, kept on purpose: it is part of every key file and the encryption's associated
# data, so changing it would make existing keys unreadable.
KEY_FORMAT = "lucene-warrant-key-v1"
# OWASP-recommended scrypt cost: ~128 MB and a fraction of a second per guess.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**17, 8, 1
MIN_SCRYPT_N = 2**14


class SignatureError(Exception):
    """A signed file is missing, altered, or signed by a different key."""


class BadPassphrase(Exception):
    """The passphrase does not decrypt the private key."""


def canonical_bytes(obj) -> bytes:
    return (json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write a new, randomly named file beside `path` (created exclusively), then put it in place, retrying
    briefly if Windows reports the destination locked by a scanner. A failed write leaves `path` as it was."""
    import secrets

    from .util import replace_file

    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        # A random name, created exclusively with the wanted mode: the operating system applies the user's umask
        # itself, so a restrictive umask (077) keeps its effect, and nothing is widened afterwards.
        tmp = str(path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp"))
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), mode)
            break
        except FileExistsError:
            continue
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        replace_file(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def sig_path(path: Path) -> Path:
    return path.with_name(path.name + ".sig")


def keys_exist(paths: Paths) -> bool:
    return paths.private_key.exists() and paths.public_key.exists()


def key_id(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def generate_keypair(paths: Paths, passphrase: str) -> str:
    """Create the signing key pair. Returns the key ID."""
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise ValueError(f"passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters")
    if paths.private_key.exists() or paths.public_key.exists():
        raise FileExistsError(f"a signing key already exists in {paths.keys}")
    paths.keys.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.keys, 0o700)
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    salt, nonce = os.urandom(16), os.urandom(12)
    params = {"n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P}
    sealed = AESGCM(_derive(passphrase, salt, params)).encrypt(nonce, raw, KEY_FORMAT.encode())
    envelope = {
        "format": KEY_FORMAT,
        "kdf": {"name": "scrypt", **params, "salt": _b64(salt)},
        "cipher": {"name": "aes-256-gcm", "nonce": _b64(nonce)},
        "ciphertext": _b64(sealed),
    }
    atomic_write(paths.private_key, canonical_bytes(envelope), mode=0o600)
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    atomic_write(paths.public_key, public)
    return key_id(key.public_key())


def load_private_key(paths: Paths, passphrase: str) -> Ed25519PrivateKey:
    try:
        envelope = json.loads(paths.private_key.read_text("utf-8"))
        kdf, cipher = envelope["kdf"], envelope["cipher"]
        if envelope["format"] != KEY_FORMAT or kdf["name"] != "scrypt" or cipher["name"] != "aes-256-gcm":
            raise SignatureError("unsupported key file format")
        if kdf["n"] < MIN_SCRYPT_N:
            raise SignatureError("key file uses a weak scrypt cost; refusing to use it")
        aes_key = _derive(passphrase, base64.b64decode(kdf["salt"]), kdf)
        raw = AESGCM(aes_key).decrypt(
            base64.b64decode(cipher["nonce"]), base64.b64decode(envelope["ciphertext"]), KEY_FORMAT.encode()
        )
    except InvalidTag as exc:
        raise BadPassphrase("passphrase does not unlock the signing key") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise SignatureError(f"key file {paths.private_key} is malformed") from exc
    key = Ed25519PrivateKey.from_private_bytes(raw)
    # A swapped key file must not sign documents the executor would reject later.
    if key_id(key.public_key()) != key_id(load_public_key(paths)):
        raise SignatureError("private key does not match the public key the executor trusts")
    return key


def _derive(passphrase: str, salt: bytes, params: dict) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=params["n"], r=params["r"], p=params["p"])
    return kdf.derive(passphrase.encode("utf-8"))


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def load_public_key(paths: Paths) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(paths.public_key.read_bytes())
    except FileNotFoundError as exc:
        raise SignatureError(f"no signing key yet in {paths.keys}: the analyst hasn't finished setting up this project "
                             "(open it in the Lucid Warrant GUI, or run lucid-warrant init)") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise SignatureError("public key is not an Ed25519 key")
    return key


def write_signed(path: Path, obj, private_key: Ed25519PrivateKey) -> None:
    data = canonical_bytes(obj)
    signature = {
        "alg": "ed25519",
        "key_id": key_id(private_key.public_key()),
        "signature": base64.b64encode(private_key.sign(data)).decode("ascii"),
    }
    # Document first, then signature: a crash in between leaves a mismatch,
    # which fails verification rather than passing it.
    atomic_write(path, data)
    atomic_write(sig_path(path), canonical_bytes(signature))


def read_signed(path: Path, public_key: Ed25519PublicKey) -> dict:
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise SignatureError(f"{path.name}: file or signature is missing") from exc
    verify_bytes(path, data, public_key)
    return json.loads(data)


def verify_bytes(path: Path, data: bytes, public_key: Ed25519PublicKey) -> None:
    """Check `data` (bytes already read from `path`) against path's signature file. Callers then use these same
    bytes, so the file can't be swapped between the check and its use."""
    try:
        signature = json.loads(sig_path(path).read_text("utf-8"))
    except FileNotFoundError as exc:
        raise SignatureError(f"{path.name}: file or signature is missing") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SignatureError(f"{path.name}: signature file is malformed") from exc
    if not isinstance(signature, dict) or signature.get("alg") != "ed25519":
        raise SignatureError(f"{path.name}: unsupported signature format")
    if signature.get("key_id") != key_id(public_key):
        raise SignatureError(f"{path.name}: signed by a different key")
    try:
        public_key.verify(base64.b64decode(signature["signature"], validate=True), data)
    except (InvalidSignature, KeyError, TypeError, ValueError) as exc:
        raise SignatureError(f"{path.name}: signature does not match contents") from exc
