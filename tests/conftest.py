from pathlib import Path

import pytest

from lucid_warrant import signing
from lucid_warrant.config import SEED_DIR, Paths
from lucid_warrant.library import sign_seed_library
from lucid_warrant.signing import generate_keypair, load_private_key
from lucid_warrant.rbac import get_rbac
from lucid_warrant.warrant import build_warrant, save_warrant

PASSPHRASE = "correct horse battery"
# Tests freeze the clock, so they use a copy of the practice records with fixed dates.
FIXED_CORPUS = Path(__file__).parent / "fixtures" / "mock_corpus_fixed.json"


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    """Lowest accepted scrypt cost, so the suite stays fast. Production uses 2**17."""
    monkeypatch.setattr(signing, "SCRYPT_N", signing.MIN_SCRYPT_N)


@pytest.fixture
def paths(tmp_path):
    p = Paths(keys=tmp_path / "keys", data=tmp_path / "data", seed=SEED_DIR, corpus_file=FIXED_CORPUS)
    generate_keypair(p, PASSPHRASE)
    return p


@pytest.fixture
def key(paths):
    return load_private_key(paths, PASSPHRASE)


@pytest.fixture
def library(paths, key):
    return sign_seed_library(paths, key)


TERMS = [
    {"term_id": "t-project", "literal": "PROJECT_CEDAR", "source": "script", "provenance": "intake"},
    {"term_id": "t-supplier", "literal": "SUPPLIER_NORTH", "source": "script", "provenance": "intake"},
    {"term_id": "t-alias", "literal": "NORTH_SUPPLY_CO", "source": "script", "provenance": "vendor master"},
    {"term_id": "t-test", "literal": "TEST_DATA", "source": "human", "provenance": "exclusion"},
]


@pytest.fixture
def warrant(paths, key, library):
    doc = build_warrant(
        parameter_1="0012",
        parameter_2=["ATTR_1000", "ATTR_1200"],
        purpose="Find records linking PROJECT_CEDAR and SUPPLIER_NORTH for case DEMO-42.",
        terms=TERMS,
        ttl_minutes=60,
        max_rows=3,
        rbac=get_rbac(paths),
    )
    save_warrant(paths, doc, key)
    return doc


# --- GUI smoke tests run one per process (Tk allows one interpreter per process on macOS) ---

import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402


def pytest_collection_modifyitems(config, items):
    if os.environ.get("LW_GUI_CHILD") == "1":
        return
    for item in items:
        if item.get_closest_marker("gui"):
            item.runtest = (lambda it=item: _run_isolated(it))


def _run_isolated(item):
    env = {**os.environ, "LW_GUI_CHILD": "1"}
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", item.nodeid],
                            cwd=str(item.config.rootpath), env=env, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        pytest.fail(f"GUI test failed in its own process (exit {result.returncode}):\n"
                    + result.stdout[-6000:] + result.stderr[-3000:], pytrace=False)


def sign_org_file(paths, text, path=None, passphrase=PASSPHRASE):
    """Write an organization file and sign its exact bytes with the test key, as the analyst would. Signing the raw
    text (rather than canonical JSON) lets tests check how malformed-but-signed files are handled."""
    import base64
    import json as _json

    from lucid_warrant.signing import atomic_write, canonical_bytes, key_id, sig_path

    path = path or paths.data / "organization.json"
    data = text if isinstance(text, bytes) else (text if isinstance(text, str) else _json.dumps(text)).encode("utf-8")
    key = load_private_key(paths, passphrase)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, data)
    atomic_write(sig_path(path), canonical_bytes({"alg": "ed25519", "key_id": key_id(key.public_key()),
                                                  "signature": base64.b64encode(key.sign(data)).decode("ascii")}))
    return path
