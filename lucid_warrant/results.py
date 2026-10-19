"""What a search returned: a compact profile for people, and the full results on disk for the terminal.

A search can return hundreds of thousands of records, so the app and the printed warrant show a profile instead
of a list: how many came back, the date range, a describe/info view of each field, and a pivot table of the hit
terms against countries. The full results are saved per run (gzip-compressed JSON lines) and can be exported
with `lucid-warrant results RUN_ID --format parquet|csv|jsonl` for investigation.

The full results contain the records' text. They stay in the warrant directory (data/state/results/); keep it
under the same access controls and retention rules as the search service's data.
"""

from __future__ import annotations

import gzip
import json
from collections import Counter
from pathlib import Path

from .config import Paths
from .util import check_id

FIELDS = ("record_id", "title", "timestamp", "country", "snippet")
MAX_COUNTRY_COLUMNS = 12


def results_path(paths: Paths, run_id: str) -> Path:
    return paths.state / "results" / f"{check_id(run_id, 'run')}.jsonl.gz"


def without_full_text(results: list) -> list:
    """Results as saved and handed back: the backend's complete text (used only for counting) removed."""
    from .backend import FULL_TEXT

    return [{k: v for k, v in r.items() if k != FULL_TEXT} if isinstance(r, dict) else r for r in results]


def save_results(paths: Paths, run_id: str, results: list) -> Path:
    import os
    import tempfile

    from .util import replace_file

    path = results_path(paths, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as raw, gzip.open(raw, "wt", encoding="utf-8") as fh:  # both closed before replacing
            for r in results:
                fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        replace_file(tmp, path)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise
    return path


def load_results(paths: Paths, run_id: str):
    """Yield the saved results of a run, one dict at a time (so a large run needn't fit in memory)."""
    with gzip.open(results_path(paths, run_id), "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def profile(results: list, term_ids: list[str], warrant: dict) -> dict:
    """A describe/info view of the results and a hit term × country pivot (record counts)."""
    from .backend import _tokens

    rows = [r for r in results if isinstance(r, dict)]
    fields = {}
    for name in FIELDS:
        values = [r[name] for r in rows if r.get(name) not in (None, "")]
        info = {"present": len(values)}
        if name != "snippet":
            counts = Counter(str(v) for v in values)
            info["distinct"] = len(counts)
            if name in ("title", "country"):
                info["top"] = [[v[:80], n] for v, n in counts.most_common(5)]
        if name == "timestamp" and values:
            info["min"], info["max"] = min(map(str, values)), max(map(str, values))
        fields[name] = info

    def country_of(r):
        return str(r.get("country") or "(none)")

    totals = Counter(country_of(r) for r in rows)
    columns = [c for c, _ in totals.most_common(MAX_COUNTRY_COLUMNS)]
    other = len(totals) > len(columns)

    def bucket(r):
        c = country_of(r)
        return c if c in columns else "Other"

    from .backend import FULL_TEXT

    literals = {t["term_id"]: t["literal"] for t in warrant.get("terms", [])}
    # Each field on its own (a phrase never runs from the title into the text), and the complete text where the
    # backend supplied it rather than the shortened snippet.
    full = all(FULL_TEXT in r for r in rows)
    texts = [([_tokens(str(r.get("title", ""))), _tokens(str(r.get(FULL_TEXT, r.get("snippet", ""))))], bucket(r))
             for r in rows]

    def contains(tokens, phrase):
        return any(tokens[i:i + len(phrase)] == phrase for i in range(len(tokens) - len(phrase) + 1))

    from .syntax import pattern_matches

    patterns = {t["term_id"] for t in warrant.get("terms", []) if t.get("match") == "pattern"}
    pivot_rows = []
    for term_id in dict.fromkeys(term_ids):
        if term_id in patterns:  # a pattern (PROJECT*) matches single words
            pattern = literals.get(term_id, "")
            matched = lambda field_tokens: any(pattern_matches(pattern, w) for w in field_tokens)  # noqa: E731
        else:
            phrase = _tokens(literals.get(term_id, ""))
            matched = lambda field_tokens, phrase=phrase: bool(phrase) and contains(field_tokens, phrase)  # noqa: E731
        counts = Counter()
        for fields_tokens, col in texts:
            if any(matched(tokens) for tokens in fields_tokens):
                counts[col] += 1
        pivot_rows.append({"term_id": term_id, "literal": literals.get(term_id, term_id), "counts": dict(counts),
                           "total": sum(counts.values()), **({"match": "pattern"} if term_id in patterns else {})})
    all_counts = Counter(bucket(r) for r in rows)
    return {"count": len(rows), "fields": fields, "counted_in": "full text" if full or not rows else "excerpts",
            "pivot": {"columns": columns + (["Other"] if other else []), "rows": pivot_rows,
                      "all": {"counts": dict(all_counts), "total": len(rows)}}}


def _date(stamp: str) -> str:
    from datetime import datetime

    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).strftime("%-d %b %Y" if not _windows() else "%#d %b %Y")
    except ValueError:
        return stamp[:10]


def _windows() -> bool:
    import os

    return os.name == "nt"


def describe_lines(prof: dict) -> list[tuple[str, str]]:
    """The profile as a person would want it read: what came back, when, from where, and what's missing."""
    f = {name: dict(info) for name, info in prof.get("fields", {}).items()}
    for info in f.values():  # profiles saved before "top" became a list held a single [value, count] pair
        top = info.get("top")
        if top and not isinstance(top[0], list):
            info["top"] = [top]
    total = prof.get("count", 0)
    out = [("Records", f"{total:,}")]
    if not total:
        return out
    ts = f.get("timestamp", {})
    if ts.get("min"):
        first, last = _date(ts["min"]), _date(ts["max"])
        out.append(("Dates", first if first == last else f"{first} to {last}"))
    countries = f.get("country", {})
    if countries.get("distinct"):
        top = ", ".join(f"{c} ({n:,})" for c, n in countries.get("top", []))
        more = countries["distinct"] - len(countries.get("top", []))
        out.append(("Countries", f"{countries['distinct']:,}: {top}" + (f", and {more:,} more" if more > 0 else "")))
    titles = f.get("title", {})
    if titles.get("distinct"):
        top = titles.get("top") or []
        if titles["distinct"] == titles["present"]:
            out.append(("Titles", "all different"))
        elif top:
            out.append(("Titles", f"{titles['distinct']:,} different; most common \u201c{top[0][0]}\u201d ({top[0][1]:,})"))
    missing = [f"{total - info['present']:,} without {what}"
               for name, what in (("timestamp", "a date"), ("country", "a country"), ("title", "a title"),
                                  ("snippet", "any text"))
               if (info := f.get(name)) and info["present"] < total]
    ids = f.get("record_id", {})
    if ids and ids.get("distinct", total) < ids.get("present", total):
        missing.append(f"{ids['present'] - ids['distinct']:,} repeated record IDs")
    if missing:
        out.append(("Gaps", "; ".join(missing)))
    return out


def pivot_caption(prof: dict, hit: str = "hit term") -> str:
    """What the pivot counts, honestly: excerpts can miss a term further into a record."""
    caption = f"Records containing each {hit.lower()}, by country"
    if prof.get("counted_in") == "excerpts":
        caption += (" (counted in titles and the returned excerpts only, so a record can contain a term further in; "
                    "a zero doesn't prove it's absent)")
    return caption


def pivot_table(prof: dict, hit_label: str = "Hit term") -> tuple[list[str], list[list[str]]]:
    """(headers, rows): hit terms (and all records) against countries, record counts, with totals."""
    pivot = prof.get("pivot") or {}
    columns = pivot.get("columns", [])
    headers = [hit_label] + columns + ["Total"]
    rows = []
    for row in pivot.get("rows", []):
        rows.append([row["literal"] + (" (pattern)" if row.get("match") == "pattern" else "")] + [f"{row['counts'].get(c, 0):,}" for c in columns]
                    + [f"{row['total']:,}"])
    every = pivot.get("all", {})
    rows.append(["All records"] + [f"{every.get('counts', {}).get(c, 0):,}" for c in columns] + [f"{every.get('total', 0):,}"])
    return headers, rows


class ExportError(ValueError):
    """The export can't be written where asked."""


def _check_destination(paths: Paths, out: Path, overwrite: bool) -> Path:
    target = Path(out).expanduser().resolve()
    for inside in (paths.data, paths.keys):
        try:
            target.relative_to(Path(inside).resolve())
        except ValueError:
            continue
        raise ExportError(f"{target} is inside the warrant directory; export somewhere else (the warrant directory "
                          "holds the signed records and the saved results themselves)")
    if target.exists() and not overwrite:
        raise ExportError(f"{target} already exists; choose another name, or pass --force to replace it")
    return target


def export(paths: Paths, run_id: str, fmt: str, out: Path, *, overwrite: bool = False) -> int:
    """Write a run's saved results as jsonl, csv or parquet. Returns the number of records.

    Never inside the warrant directory and never over an existing file unless asked. The records are read first,
    then written to a new, randomly named file in the destination folder (created exclusively, so it can't be a
    link to anything), which then takes the destination's name: a failed export leaves nothing half-written, and
    nothing else is ever opened for writing. Every format keeps the exact values."""
    import csv
    import os
    import tempfile

    if fmt not in ("jsonl", "csv", "parquet"):
        raise ValueError(f"unknown format {fmt!r}")
    target = _check_destination(paths, out, overwrite)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = list(load_results(paths, run_id))  # everything read before anything is written

    def flat(r):
        return {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in r.items()}

    fd, tmp_name = tempfile.mkstemp(prefix=".lucid-export-", suffix=".partial", dir=target.parent)
    tmp = Path(tmp_name)
    try:
        if fmt == "jsonl":
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        elif fmt == "csv":
            columns = list(dict.fromkeys([*FIELDS, *(k for r in rows for k in r)]))
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(columns)
                for r in rows:
                    cells = flat(r)
                    writer.writerow([cells.get(c, "") for c in columns])
        else:
            try:
                import pyarrow as pa
                import pyarrow.parquet as pq
            except ImportError as exc:
                os.close(fd)
                raise RuntimeError("Parquet needs pyarrow (pip install pyarrow); or export --format csv / jsonl") from exc
            with os.fdopen(fd, "wb") as fh:
                pq.write_table(pa.Table.from_pylist([flat(r) for r in rows]), fh)
        if overwrite:
            try:
                os.replace(tmp, target)
            except PermissionError as exc:  # Windows: the file is open in another program, or read-only
                raise ExportError(f"{target} can't be replaced: it's open in another program or read-only. "
                                  "Close it and try again, or choose another name") from exc
        else:
            try:
                os.link(tmp, target)  # fails if the destination appeared in the meantime: never clobbered
            except FileExistsError as exc:
                raise ExportError(f"{target} already exists; choose another name, or pass --force to replace it") from exc
            except OSError:  # no hard links on this file system (e.g. exFAT/FAT): create the destination exclusively
                import shutil

                try:
                    with open(target, "xb") as out, open(tmp, "rb") as src:  # "x": fails if it exists, never clobbers
                        shutil.copyfileobj(src, out)
                except FileExistsError as exc:
                    raise ExportError(f"{target} already exists; choose another name, or pass --force to replace "
                                      "it") from exc
    finally:
        if tmp.exists():
            tmp.unlink()
    return len(rows)
