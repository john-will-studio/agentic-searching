"""What a search returned: a profile and a hit term × country pivot for people, the full results for the terminal."""

import codecs
import os
import json
import time

import pytest

from lucid_warrant.results import describe_lines, export, load_results, pivot_table, profile, save_results

WARRANT = {"terms": [{"term_id": "t-a", "literal": "PROJECT_CEDAR"}, {"term_id": "t-b", "literal": "SUPPLIER_NORTH"}]}
ROWS = [
    {"record_id": "r1", "title": "Kickoff", "timestamp": "2026-09-01T10:00:00Z", "country": "US",
     "snippet": "PROJECT_CEDAR kickoff with SUPPLIER_NORTH"},
    {"record_id": "r2", "title": "Kickoff", "timestamp": "2026-10-05T10:00:00Z", "country": "GB",
     "snippet": "PROJECT_CEDAR budget"},
    {"record_id": "r3", "title": "Audit", "timestamp": "2026-10-09T10:00:00Z", "snippet": "SUPPLIER_NORTH audit"},
]


def test_profile_describes_fields_and_pivots_terms_by_country():
    prof = profile(ROWS, ["t-a", "t-b"], WARRANT)
    lines = dict(describe_lines(prof))
    assert lines["Records"] == "3" and lines["Dates"] == "1 Sep 2026 to 9 Oct 2026"
    assert lines["Countries"] == "2: US (1), GB (1)" and lines["Titles"] == "2 different; most common \u201cKickoff\u201d (2)"
    assert lines["Gaps"] == "1 without a country", "only what's missing is mentioned"
    headers, rows = pivot_table(prof)
    table = {r[0]: dict(zip(headers[1:], r[1:])) for r in rows}
    assert table["PROJECT_CEDAR"] == {"US": "1", "GB": "1", "(none)": "0", "Total": "2"}, "names, not internal IDs"
    assert table["SUPPLIER_NORTH"]["(none)"] == "1" and table["All records"]["Total"] == "3"


def test_nothing_unusual_means_no_gaps_line():
    rows = [{**r, "country": r.get("country", "US")} for r in ROWS]
    assert "Gaps" not in dict(describe_lines(profile(rows, ["t-a"], WARRANT)))


def test_profile_handles_many_records_and_countries_quickly():
    rows = [{"record_id": f"r{i}", "country": f"C{i % 30}", "timestamp": "2026-10-01T00:00:00Z",
             "title": "t", "snippet": "PROJECT_CEDAR" if i % 2 else "other"} for i in range(100_000)]
    started = time.monotonic()
    prof = profile(rows, ["t-a"], WARRANT)
    assert time.monotonic() - started < 10
    assert len(prof["pivot"]["columns"]) == 13 and prof["pivot"]["columns"][-1] == "Other", "top 12 countries + Other"
    assert prof["pivot"]["rows"][0]["total"] == 50_000 and len(json.dumps(prof)) < 5_000, "a profile stays small"


def test_results_are_saved_and_exported(paths, tmp_path):
    save_results(paths, "run-20261017-000000-abcdef", ROWS)
    assert [r["record_id"] for r in load_results(paths, "run-20261017-000000-abcdef")] == ["r1", "r2", "r3"]
    assert export(paths, "run-20261017-000000-abcdef", "jsonl", tmp_path / "x.jsonl") == 3
    assert export(paths, "run-20261017-000000-abcdef", "csv", tmp_path / "x.csv") == 3
    raw = (tmp_path / "x.csv").read_bytes()
    assert raw.decode("utf-8").splitlines()[0].startswith("record_id,title,timestamp,country,snippet")
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="pyarrow"):
            export(paths, "run-20261017-000000-abcdef", "parquet", tmp_path / "x.parquet")
    else:
        assert export(paths, "run-20261017-000000-abcdef", "parquet", tmp_path / "x.parquet") == 3


def test_export_never_overwrites_its_source_or_existing_files(paths, tmp_path):
    """Codex round 7 #10: --out pointing at the saved results used to truncate them."""
    from lucid_warrant.results import ExportError, results_path

    run_id = "run-20261017-000000-abcdef"
    save_results(paths, run_id, ROWS)
    with pytest.raises(ExportError, match="inside the warrant directory"):
        export(paths, run_id, "jsonl", results_path(paths, run_id))
    assert len(list(load_results(paths, run_id))) == 3
    (tmp_path / "x.csv").write_text("keep me")
    with pytest.raises(ExportError, match="already exists"):
        export(paths, run_id, "csv", tmp_path / "x.csv")
    assert (tmp_path / "x.csv").read_text() == "keep me"
    assert export(paths, run_id, "csv", tmp_path / "x.csv", overwrite=True) == 3


def test_counts_use_full_text_and_never_join_fields():
    """Codex round 7 #11: a term past the 400-character snippet is counted; a phrase can't span title and text."""
    from lucid_warrant.backend import FULL_TEXT
    from lucid_warrant.results import pivot_caption, without_full_text

    long_text = "unrelated words " * 40 + "PROJECT_CEDAR appears late"
    rows = [{"record_id": "r1", "title": "Minutes", "country": "US", "snippet": long_text[:400], FULL_TEXT: long_text},
            {"record_id": "r2", "title": "NORTH", "country": "US", "snippet": "SUPPLY chain", FULL_TEXT: "SUPPLY chain"}]
    warrant = {"terms": [{"term_id": "t-a", "literal": "PROJECT_CEDAR"}, {"term_id": "t-n", "literal": "NORTH SUPPLY"}]}
    prof = profile(rows, ["t-a", "t-n"], warrant)
    totals = {row["term_id"]: row["total"] for row in prof["pivot"]["rows"]}
    assert totals == {"t-a": 1, "t-n": 0}
    assert prof["counted_in"] == "full text" and "excerpts" not in pivot_caption(prof)
    assert all(FULL_TEXT not in r for r in without_full_text(rows)), "the full text is never saved or returned"
    excerpt_only = profile([{k: v for k, v in r.items() if k != FULL_TEXT} for r in rows], ["t-a"], warrant)
    assert "a zero doesn't prove it's absent" in pivot_caption(excerpt_only)


def test_round8_export_ignores_a_planted_partial_link(paths, tmp_path):
    """Codex round 8 A5: a link at the old predictable temporary name can't truncate the source; values are exact."""
    import os

    from lucid_warrant.results import results_path

    run_id = "run-20261018-000000-abcdef"
    save_results(paths, run_id, [{**ROWS[0], "title": "=1+1", "note": "-74.0"}])
    source = results_path(paths, run_id)
    try:
        os.symlink(source, tmp_path / ".x.jsonl.partial")
    except OSError:  # Windows without Developer Mode can't make symlinks (WinError 1314); a hard link is as dangerous
        os.link(source, tmp_path / ".x.jsonl.partial")
    assert export(paths, run_id, "jsonl", tmp_path / "x.jsonl") == 1
    assert len(list(load_results(paths, run_id))) == 1, "the source is untouched"
    export(paths, run_id, "csv", tmp_path / "x.csv")
    text = (tmp_path / "x.csv").read_text(encoding="utf-8")
    assert ",=1+1," in text and ",-74.0" in text, "exact values"
    assert not list(tmp_path.glob(".lucid-export-*")), "no temporary files left behind"

def test_round9_export_without_hard_links_never_clobbers(paths, tmp_path, monkeypatch):
    """Codex round 9 #1."""
    import os

    from lucid_warrant import results
    from lucid_warrant.results import ExportError

    run_id = "run-20261018-000000-abcdef"
    save_results(paths, run_id, [{**ROWS[0], "=1+1": "a"}])
    monkeypatch.setattr(os, "link", lambda *_a: (_ for _ in ()).throw(OSError("no hard links here")))
    target = tmp_path / "x.jsonl"
    real_open = open

    def other_writer_first(path, mode="r", *a, **k):
        if str(path) == str(target) and "x" in mode:
            target.write_text("OTHER WRITER")  # appears just before the exclusive create
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr("builtins.open", other_writer_first)
    with pytest.raises(ExportError, match="already exists"):
        export(paths, run_id, "jsonl", target)
    monkeypatch.setattr("builtins.open", real_open)
    assert target.read_text() == "OTHER WRITER"
    assert results  # module used


def test_replacing_a_file_open_elsewhere_says_so(paths, tmp_path, monkeypatch):
    """On Windows a file open in another program can't be replaced. --force then failed with a raw PermissionError naming the
    hidden temporary file; it now says what's wrong, and nothing is left behind."""
    import os

    from lucid_warrant.results import ExportError

    run_id = "run-20261017-000000-abcdef"
    save_results(paths, run_id, ROWS)
    out = tmp_path / "exports" / "results.csv"
    out.parent.mkdir()
    out.write_text("open elsewhere")

    def locked(_src, _dst):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(ExportError, match="open in another program or read-only"):
        export(paths, run_id, "csv", out, overwrite=True)
    assert out.read_text() == "open elsewhere" and [p.name for p in out.parent.iterdir()] == ["results.csv"]


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to replace an open file")
def test_replacing_an_open_file_on_windows(paths, tmp_path):
    from lucid_warrant.results import ExportError

    run_id = "run-20261017-000000-abcdef"
    save_results(paths, run_id, ROWS)
    out = tmp_path / "results.csv"
    out.write_text("open elsewhere")
    with open(out):  # open without sharing delete access
        with pytest.raises(ExportError, match="open in another program"):
            export(paths, run_id, "csv", out, overwrite=True)
    assert out.read_text() == "open elsewhere"
