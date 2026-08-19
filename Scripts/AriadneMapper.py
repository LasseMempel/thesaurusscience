#!/usr/bin/env python3
"""
Extract the ARIADNE <-> Getty AAT crosswalk tables from the DAI and ADS
mapping PDFs into tidy CSV/JSON.

Why this isn't a one-liner
---------------------------
pdfplumber's extract_tables() does a good job overall, but two artifacts
show up in these particular PDFs:

1. Long cell values (especially source URIs and getty AAT URIs) wrap
   across *lines within a cell* -> joined back with no separator for
   URL-like text, single space for label text.
2. Rows that straddle a *page break* get split into two separate
   "rows" by pdfplumber: the tail end of the row (usually the rest of
   a URI) reappears as an orphan 1-2 cell row at the top of the next
   page. These have to be re-attached to the previous real record.

The script below handles both, tags each row with its source section
(e.g. "1. DAI Books"), and writes out a combined CSV + JSON per PDF,
plus one merged file with a `source_file` column.
"""

import csv
import json
import re
from pathlib import Path

import pdfplumber

HEADER_TOKENS = {
    "Source URI", "Source Label", "Target Label", "Target URI", "Match",
    "SourceURI", "SourceLabel", "TargetLabel", "TargetURI",
}

FIELDS_WITH_SOURCE_URI = ["source_uri", "source_label", "target_label", "target_uri", "match"]
FIELDS_NO_SOURCE_URI = ["source_label", "target_label", "target_uri", "match"]


def is_header_row(row):
    non_empty = [c.strip() for c in row if c not in (None, "") and c.strip()]
    if not non_empty:
        return True
    return all(
        any(tok == c or tok.replace(" ", "") == c.replace(" ", "") for tok in HEADER_TOKENS)
        for c in non_empty
    )


def clean_cell(raw):
    """Join line-wraps inside a single cell. URL-ish text -> no separator,
    ordinary label text -> single space."""
    if raw is None:
        return ""
    raw = raw.strip()
    if not raw:
        return ""
    looks_like_url_fragment = ("http" in raw) or bool(re.search(r"/aat/|concepts/|schemes/", raw))
    if looks_like_url_fragment:
        return re.sub(r"\s+", "", raw)
    return re.sub(r"\s+", " ", raw).strip()


def is_uri_like_fragment(text):
    """Orphan continuation piece that looks like part of a URL (lowercase
    letters/digits/slashes/underscores/dots only, or pure digits)."""
    return bool(re.fullmatch(r"[a-z0-9/_.\-]+", text)) or text.isdigit()


def looks_like_source_uri_piece(text):
    return bool(re.search(r"concepts|schemes|gedata|heritagedata", text))


def looks_like_target_uri_piece(text):
    return bool(re.search(r"/aat/|vocab\.getty", text)) or text.isdigit()


def find_section_headers_with_position(page, section_prefix):
    """Section headers ('1. DAI Books' etc.) as (top_y, text), using word
    positions so headers occurring mid-page are ordered correctly relative
    to the tables around them."""
    words = page.extract_words()
    headers = []
    i = 0
    while i < len(words):
        w = words[i]
        if re.match(r"^\d+\.$", w["text"]) and i + 1 < len(words) and words[i + 1]["text"] == section_prefix:
            # gather the rest of the heading line (same 'top')
            line_words = [w]
            j = i + 1
            while j < len(words) and abs(words[j]["top"] - w["top"]) < 2:
                line_words.append(words[j])
                j += 1
            headers.append((w["top"], " ".join(x["text"] for x in line_words)))
            i = j
        else:
            i += 1
    return headers


def extract_pdf(path, section_prefix, has_source_uri):
    """Returns a list of dicts: section, source_uri(optional), source_label,
    target_label, target_uri, match."""
    fields = FIELDS_WITH_SOURCE_URI if has_source_uri else FIELDS_NO_SOURCE_URI

    raw_rows = []  # (section, [cleaned non-empty cells in column order])
    current_section = None

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            headers = find_section_headers_with_position(page, section_prefix)  # [(top, text), ...] sorted by position

            # Interleave: walk each table's rows in order, advancing
            # current_section past any header whose top is above the row.
            header_idx = 0
            for table in page.find_tables():
                for row_obj, row in zip(table.rows, table.extract()):
                    row_top = row_obj.bbox[1]
                    while header_idx < len(headers) and headers[header_idx][0] < row_top:
                        current_section = headers[header_idx][1]
                        header_idx += 1
                    if is_header_row(row):
                        continue
                    cells = [clean_cell(c) for c in row]
                    cells = [c for c in cells if c != ""]
                    if not cells:
                        continue
                    raw_rows.append((current_section, cells))
            # catch any header at the very bottom of the page (no rows after it)
            if header_idx < len(headers):
                current_section = headers[-1][1]

    # Merge orphan continuation rows (len < len(fields)) into the
    # preceding full record.
    records = []
    for section, cells in raw_rows:
        if len(cells) == len(fields):
            record = {"section": section, **dict(zip(fields, cells))}
            # occasional stray punctuation glyph before the URL (source PDF artifact)
            record["target_uri"] = re.sub(r"^[^h]*(?=http)", "", record["target_uri"])
            records.append(record)
            continue

        # Orphan fragment row: figure out which field(s) each piece continues.
        if not records:
            continue  # nothing to attach to (shouldn't happen)
        prev = records[-1]
        for piece in cells:
            if is_uri_like_fragment(piece):
                target_complete = bool(re.fullmatch(r"http://vocab\.getty\.edu/aat/\d{6,9}", prev["target_uri"]))
                if has_source_uri and looks_like_source_uri_piece(piece):
                    prev["source_uri"] += piece
                elif not looks_like_target_uri_piece(piece):
                    # ambiguous (e.g. bare digits) -> attach to whichever
                    # URI field isn't already a complete, valid URI
                    if not target_complete:
                        prev["target_uri"] += piece
                    elif has_source_uri:
                        prev["source_uri"] += piece
                    else:
                        prev["target_uri"] += piece
                elif target_complete and has_source_uri:
                    prev["source_uri"] += piece
                else:
                    prev["target_uri"] += piece
            else:
                # word-like continuation -> most often wraps the source label
                prev["source_label"] = (prev["source_label"] + " " + piece).strip()

    return records


def validate(records, has_source_uri):
    bad = []
    for r in records:
        if not re.fullmatch(r"http://vocab\.getty\.edu/aat/\d{6,9}", r["target_uri"]):
            bad.append(("target_uri", r))
        if has_source_uri and not r["source_uri"].startswith("http://purl.org/heritagedata/"):
            bad.append(("source_uri", r))
    return bad


def write_outputs(records, out_stub, fieldnames):
    csv_path = Path(f"{out_stub}.csv")
    json_path = Path(f"{out_stub}.json")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["section"] + fieldnames)
        w.writeheader()
        for r in records:
            w.writerow(r)
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    return csv_path, json_path


def main():
    jobs = [
        ("/home/mempellaenger/repos/thesaurusscience/Ariadne Mappings/ARIADNE_DAI_AAT_Mappings.pdf", "DAI", False, "dai_aat_mappings"),
        ("/home/mempellaenger/repos/thesaurusscience/Ariadne Mappings/ARIADNE_ADS_AAT_Mappings.pdf", "ADS", True, "ads_aat_mappings"),
    ]

    all_records = []
    for path, prefix, has_uri, stub in jobs:
        records = extract_pdf(path, prefix, has_uri)
        fieldnames = FIELDS_WITH_SOURCE_URI if has_uri else FIELDS_NO_SOURCE_URI
        csv_path, json_path = write_outputs(records, f"/home/mempellaenger/repos/thesaurusscience/Output/{stub}", fieldnames)
        bad = validate(records, has_uri)
        print(f"{prefix}: {len(records)} rows -> {csv_path.name}, {json_path.name} "
              f"({len(bad)} rows failed URI sanity check)")
        for kind, r in bad[:15]:
            print(f"   [{kind}] {r}")
        for r in records:
            all_records.append({"source_file": Path(path).name, **{k: r.get(k, "") for k in ["section"] + FIELDS_WITH_SOURCE_URI}})

    with open("/home/mempellaenger/repos/thesaurusscience/Output/combined_aat_mappings.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["source_file", "section"] + FIELDS_WITH_SOURCE_URI)
        w.writeheader()
        w.writerows(all_records)
    print(f"combined: {len(all_records)} rows -> combined_aat_mappings.csv")


if __name__ == "__main__":
    main()