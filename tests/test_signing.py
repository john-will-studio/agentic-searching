import json
import os

import pytest

from lucid_warrant.config import SEED_DIR, Paths
from lucid_warrant.signing import (
    BadPassphrase,
    SignatureError,
    generate_keypair,
    load_private_key,
    load_public_key,
    read_signed,
    sig_path,
    write_signed,
)
from tests.conftest import PASSPHRASE


def test_roundtrip(paths, key, tmp_path):
    target = tmp_path / "doc.json"
    write_signed(target, {"parameter_1": "0012"}, key)
    assert read_signed(target, load_public_key(paths)) == {"parameter_1": "0012"}


def test_any_byte_change_fails(paths, key, tmp_path):
    target = tmp_path / "doc.json"
    write_signed(target, {"parameter_1": "0012"}, key)
    target.write_text(target.read_text().replace("0012", "0123"))
    with pytest.raises(SignatureError, match="does not match"):
        read_signed(target, load_public_key(paths))


def test_missing_signature_fails(paths, key, tmp_path):
    target = tmp_path / "doc.json"
    write_signed(target, {"a": 1}, key)
    sig_path(target).unlink()
    with pytest.raises(SignatureError, match="missing"):
        read_signed(target, load_public_key(paths))


def test_other_key_rejected(paths, key, tmp_path):
    other = Paths(keys=tmp_path / "other", data=tmp_path / "d2", seed=SEED_DIR)
    generate_keypair(other, "another passphrase")
    target = tmp_path / "doc.json"
    write_signed(target, {"a": 1}, load_private_key(other, "another passphrase"))
    with pytest.raises(SignatureError, match="different key"):
        read_signed(target, load_public_key(paths))
    # Forging the key_id doesn't help: the signature itself won't verify.
    write_signed(tmp_path / "mine.json", {"a": 1}, key)
    sig = json.loads(sig_path(target).read_text())
    sig["key_id"] = json.loads(sig_path(tmp_path / "mine.json").read_text())["key_id"]
    sig_path(target).write_text(json.dumps(sig))
    with pytest.raises(SignatureError, match="does not match"):
        read_signed(target, load_public_key(paths))


def test_wrong_passphrase(paths):
    with pytest.raises(BadPassphrase):
        load_private_key(paths, "wrong passphrase")
    assert load_private_key(paths, PASSPHRASE)


def test_private_key_is_encrypted_and_private(paths):
    envelope = json.loads(paths.private_key.read_text())
    assert envelope["kdf"]["name"] == "scrypt" and envelope["kdf"]["n"] >= 2**14
    if os.name != "nt":  # Windows has access lists, not modes: lucid_warrant.key_access checks and restricts those
        assert paths.private_key.stat().st_mode & 0o077 == 0


def test_weakened_key_file_refused(paths):
    envelope = json.loads(paths.private_key.read_text())
    envelope["kdf"]["n"] = 2**10
    paths.private_key.write_text(json.dumps(envelope))
    with pytest.raises(SignatureError, match="weak"):
        load_private_key(paths, PASSPHRASE)


def test_swapped_private_key_refused(paths, tmp_path):
    other = Paths(keys=tmp_path / "other", data=tmp_path / "d2", seed=SEED_DIR)
    generate_keypair(other, PASSPHRASE)
    paths.private_key.write_bytes(other.private_key.read_bytes())
    with pytest.raises(SignatureError, match="does not match the public key"):
        load_private_key(paths, PASSPHRASE)


def test_short_passphrase_and_overwrite_refused(paths, tmp_path):
    with pytest.raises(ValueError):
        generate_keypair(Paths(keys=tmp_path / "k2", data=tmp_path / "d2"), "short")
    with pytest.raises(FileExistsError):
        generate_keypair(paths, "a long enough passphrase")
