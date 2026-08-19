#!/usr/bin/env python3
"""
Extract the ARIADNE <-> Getty AAT crosswalk tables (DAI + ADS PDFs) into
tidy CSV/JSON, resolve missing source URIs against the DAI SKOS thesaurus
and validate ADS source URIs against the FISH RDF/XML thesauri, then emit
SKOS-compliant mapping triples (skos:exactMatch / closeMatch / broadMatch /
narrowMatch / relatedMatch) as Turtle.

Run this on your local machine (needs the PDFs, the DAI ttl, and the FISH
RDF/XML files on disk — see CONFIG below).

    pip install pdfplumber rdflib

--------------------------------------------------------------------------
HONEST CAVEAT: the DAI-ttl lookup and FISH-thesaurus validation code below
was written without access to your actual .ttl / FISH files (I only have
the two PDFs). The PDF-extraction half is tested and known-good; the
thesaurus-linking half is my best-effort against the one example triple
you showed me. Run it, and if label/URI lookups behave unexpectedly,
send me the warnings and a snippet of the file and I'll adjust.
--------------------------------------------------------------------------
"""

import csv
import json
import re
from pathlib import Path

import pdfplumber

try:
    import rdflib
    from rdflib.namespace import SKOS, RDF
except ImportError:
    rdflib = None

# ---------------------------------------------------------------------------
# CONFIG — local paths on your machine
# ---------------------------------------------------------------------------

BASE = Path("/home/mempellaenger/repos/thesaurusscience")

DAI_PDF = BASE / "Ariadne Mappings" / "ARIADNE_DAI_AAT_Mappings.pdf"
ADS_PDF = BASE / "Ariadne Mappings" / "ARIADNE_ADS_AAT_Mappings.pdf"
DAI_TTL = BASE / "DAI" / "export-2020-10-14_10-36.ttl"
FISH_DIR = BASE / "FISH"  # directory containing the FISH RDF/XML thesauri

OUTPUT_DIR = BASE / "Output"

# ---------------------------------------------------------------------------
# Known OCR/typo variants of the "Match" column -> canonical SKOS predicate.
# Add new variants here as you spot them in the warning printout.
# ---------------------------------------------------------------------------

MATCH_TYPE_ALIASES = {
    "exact match": "exactMatch",
    "close match": "closeMatch",
    "broad match": "broadMatch",
    "narrow match": "narrowMatch",
    "related match": "relatedMatch",
    "narrow math": "narrowMatch",   # observed OCR typo
    "broad math": "broadMatch",     # defensive, in case the same typo occurs
    "close mateh": "closeMatch",    # defensive common OCR confusion
    "exaet match": "exactMatch",    # defensive common OCR confusion
    "skos:closematch": "closeMatch",
    "skos:exactmatch": "exactMatch",
    "skos:broadmatch": "broadMatch",
    "skos:narrowmatch": "narrowMatch",
    "skos:relatedmatch": "relatedMatch",
}

# ---------------------------------------------------------------------------
# Known spelling/OCR variants of DAI source labels -> correct spelling.
# These are applied before looking up labels in the DAI SKOS thesaurus.
# Add new variants here as you spot them in the [WARN][DAI label unmatched] output.
# ---------------------------------------------------------------------------

DAI_LABEL_SPELLING_CORRECTIONS = {
    "Geschäshaus": "Geschäftshaus",    # missing 'f'
    "Negride": "Afrikaner",            # incorrect term, replaced by DAI -> correct DAI label
    "Bergügel": "Berg/Hügel",            # missing '/h'
    "Hof/Geöft": "Hof/Gehöft",          # missing 'h'
}

SKOS_NS = "http://www.w3.org/2004/02/skos/core#"

# ---------------------------------------------------------------------------
# PDF extraction (unchanged logic from the earlier version — tested)
# ---------------------------------------------------------------------------

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
    return bool(re.fullmatch(r"[a-z0-9/_.\-]+", text)) or text.isdigit()


def looks_like_source_uri_piece(text):
    return bool(re.search(r"concepts|schemes|gedata|heritagedata", text))


def looks_like_target_uri_piece(text):
    return bool(re.search(r"/aat/|vocab\.getty", text)) or text.isdigit()


def find_section_headers_with_position(page, section_prefix):
    words = page.extract_words()
    headers = []
    i = 0
    while i < len(words):
        w = words[i]
        if re.match(r"^\d+\.$", w["text"]) and i + 1 < len(words) and words[i + 1]["text"] == section_prefix:
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
    fields = FIELDS_WITH_SOURCE_URI if has_source_uri else FIELDS_NO_SOURCE_URI
    raw_rows = []
    current_section = None

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            headers = find_section_headers_with_position(page, section_prefix)
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
            if header_idx < len(headers):
                current_section = headers[-1][1]

    records = []
    for section, cells in raw_rows:
        if len(cells) == len(fields):
            record = {"section": section, **dict(zip(fields, cells))}
            record["target_uri"] = re.sub(r"^[^h]*(?=http)", "", record["target_uri"])
            records.append(record)
            continue

        if not records:
            continue
        prev = records[-1]
        for piece in cells:
            if is_uri_like_fragment(piece):
                target_complete = bool(re.fullmatch(r"http://vocab\.getty\.edu/aat/\d{6,9}", prev["target_uri"]))
                if has_source_uri and looks_like_source_uri_piece(piece):
                    prev["source_uri"] += piece
                elif not looks_like_target_uri_piece(piece):
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
                prev["source_label"] = (prev["source_label"] + " " + piece).strip()

    return records


def validate_uri_shape(records, has_source_uri):
    bad = []
    for r in records:
        if not re.fullmatch(r"http://vocab\.getty\.edu/aat/\d{6,9}", r["target_uri"]):
            bad.append(("target_uri", r))
        if has_source_uri and r.get("source_uri") and not r["source_uri"].startswith("http://purl.org/heritagedata/"):
            bad.append(("source_uri", r))
    return bad


# ---------------------------------------------------------------------------
# DAI thesaurus (SKOS ttl) label -> URI lookup
# ---------------------------------------------------------------------------

def normalize_label(s):
    return re.sub(r"\s+", "", s).lower()


def build_dai_label_index(ttl_path):
    """subject URI indexed by every de-language prefLabel/altLabel, in both
    original and whitespace-collapsed form (to absorb the word-wrap-join
    ambiguity from PDF extraction, e.g. 'Grabungsdokumentati on')."""
    if rdflib is None:
        raise RuntimeError("rdflib is required — pip install rdflib")

    g = rdflib.Graph()
    g.parse(str(ttl_path), format="turtle")

    index = {}       # exact label (case-sensitive) -> uri
    norm_index = {}   # normalized (whitespace-stripped, lowercased) -> uri

    for pred in (SKOS.prefLabel, SKOS.altLabel):
        for s, _, o in g.triples((None, pred, None)):
            if o.language != "de":
                continue
            label = str(o)
            index.setdefault(label, str(s))
            norm_index.setdefault(normalize_label(label), str(s))

    print(f"[DAI ttl] loaded {len(index)} German labels from {ttl_path.name}")
    return index, norm_index


def resolve_dai_source_uris(records, index, norm_index):
    unresolved = 0
    for r in records:
        label = r["source_label"]
        
        # Apply spelling correction if the label is in our known corrections dictionary
        corrected_label = DAI_LABEL_SPELLING_CORRECTIONS.get(label)
        if corrected_label and corrected_label != label:
            print(f"[INFO][DAI spelling corrected] '{label}' -> '{corrected_label}' "
                  f"(section: {r['section']}, target: {r['target_label']})")
            label = corrected_label
        
        uri = index.get(label) or norm_index.get(normalize_label(label))
        if uri:
            r["source_uri"] = uri
        else:
            r["source_uri"] = ""
            unresolved += 1
            print(f"[WARN][DAI label unmatched] '{label}' "
                  f"(section: {r['section']}, target: {r['target_label']}) "
                  f"— no matching skos:prefLabel/altLabel@de in {DAI_TTL.name}")
    print(f"[DAI ttl] resolved {len(records) - unresolved}/{len(records)} source URIs "
          f"({unresolved} unmatched — excluded from SKOS output)")


# ---------------------------------------------------------------------------
# FISH RDF/XML thesauri: validate ADS source URIs exist + labels align
# ---------------------------------------------------------------------------

def load_fish_graph(fish_dir):
    if rdflib is None:
        raise RuntimeError("rdflib is required — pip install rdflib")

    g = rdflib.Graph()
    files = list(fish_dir.glob("*.rdf")) + list(fish_dir.glob("*.xml")) + list(fish_dir.glob("*.owl"))
    if not files:
        print(f"[WARN][FISH] no .rdf/.xml/.owl files found in {fish_dir}")
        return g

    for f in files:
        try:
            g.parse(str(f), format="xml")
            print(f"[FISH] loaded {f.name}")
        except Exception as e:  # noqa: BLE001 — surfacing parse errors as warnings, not fatal
            print(f"[WARN][FISH] failed to parse {f.name}: {e}")

    print(f"[FISH] combined graph: {len(g)} triples from {len(files)} file(s)")
    return g


def validate_ads_against_fish(records, fish_graph):
    if len(fish_graph) == 0:
        print("[WARN][FISH] graph is empty — skipping FISH validation entirely")
        return

    existing_subjects = {str(s) for s in fish_graph.subjects()}
    missing = 0
    label_mismatches = 0
    altlabel_fallbacks = 0

    for r in records:
        uri = r.get("source_uri", "")
        if not uri:
            continue
        uri_ref = rdflib.URIRef(uri)
        if uri not in existing_subjects:
            missing += 1
            print(f"[WARN][FISH URI not found] {uri} (label: '{r['source_label']}', "
                  f"section: {r['section']})")
            continue

        source_label = r["source_label"].strip().lower()
        
        # Check prefLabel
        pref_labels = {
            str(o).strip().lower()
            for _, _, o in fish_graph.triples((uri_ref, SKOS.prefLabel, None))
        }
        
        # Check RDFS.label
        rdfs_labels = {
            str(o).strip().lower()
            for _, _, o in fish_graph.triples((uri_ref, rdflib.RDFS.label, None))
        }
        
        # Check altLabel
        alt_labels = {
            str(o).strip().lower()
            for _, _, o in fish_graph.triples((uri_ref, SKOS.altLabel, None))
        }
        
        all_labels = pref_labels | rdfs_labels | alt_labels
        
        if source_label in pref_labels:
            # Perfect match via prefLabel
            pass
        elif source_label in alt_labels or source_label in rdfs_labels:
            # Match found via altLabel or RDFS.label fallback
            altlabel_fallbacks += 1
            # Get prefLabel for context
            pref_label_text = pref_labels.pop() if pref_labels else "?"
            print(f"[INFO][FISH altLabel fallback] {uri} — mapping uses non-preferred label "
                  f"'{r['source_label']}', prefLabel is '{pref_label_text}'")
        else:
            # No match found
            label_mismatches += 1
            print(f"[WARN][FISH label mismatch] {uri} — mapping says '{r['source_label']}', "
                  f"thesaurus has prefLabel(s): {sorted(pref_labels)}, "
                  f"altLabel(s): {sorted(alt_labels)}")

    print(f"\n[FISH Validation Summary]")
    print(f"  Source URIs not found: {missing}")
    print(f"  True label mismatches: {label_mismatches}")
    print(f"  AltLabel/RDFS_label fallbacks: {altlabel_fallbacks}")
    print(f"  Total records validated: {len([r for r in records if r.get('source_uri', '')])}")


# ---------------------------------------------------------------------------
# Match-type -> SKOS predicate resolution (with typo dict)
# ---------------------------------------------------------------------------

def resolve_predicate(records):
    unresolved_types = set()
    for r in records:
        key = r["match"].strip().lower()
        pred = MATCH_TYPE_ALIASES.get(key)
        if pred is None:
            unresolved_types.add(r["match"])
            r["skos_predicate"] = ""
        else:
            r["skos_predicate"] = pred

    for t in sorted(unresolved_types):
        print(f"[WARN][unrecognized match type] '{t}' — add it to MATCH_TYPE_ALIASES "
              f"in this script once you know the intended predicate. Rows with this "
              f"value are excluded from the SKOS ttl.")


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def write_csv_json(records, out_stub, fieldnames):
    csv_path = out_stub.with_suffix(".csv")
    json_path = out_stub.with_suffix(".json")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in records:
            w.writerow({k: r.get(k, "") for k in fieldnames})
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    return csv_path, json_path


def write_skos_ttl(records, out_path):
    if rdflib is None:
        raise RuntimeError("rdflib is required — pip install rdflib")

    g = rdflib.Graph()
    g.bind("skos", SKOS)

    written, skipped_no_uri, skipped_no_pred = 0, 0, 0
    for r in records:
        source_uri = r.get("source_uri", "")
        pred = r.get("skos_predicate", "")
        if not source_uri:
            skipped_no_uri += 1
            continue
        if not pred:
            skipped_no_pred += 1
            continue
        g.add((
            rdflib.URIRef(source_uri),
            rdflib.URIRef(SKOS_NS + pred),
            rdflib.URIRef(r["target_uri"]),
        ))
        written += 1

    g.serialize(destination=str(out_path), format="turtle")
    print(f"[ttl] {out_path.name}: {written} triples written "
          f"({skipped_no_uri} skipped: no source URI, {skipped_no_pred} skipped: unresolved match type)")
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # --- DAI ---
    dai_records = extract_pdf(DAI_PDF, "DAI", has_source_uri=False)
    dai_index, dai_norm_index = build_dai_label_index(DAI_TTL)
    resolve_dai_source_uris(dai_records, dai_index, dai_norm_index)
    resolve_predicate(dai_records)

    dai_fields = ["section", "source_uri", "source_label", "target_label", "target_uri", "match", "skos_predicate"]
    write_csv_json(dai_records, OUTPUT_DIR / "dai_aat_mappings", dai_fields)
    write_skos_ttl(dai_records, OUTPUT_DIR / "dai_aat_mappings.ttl")

    bad = validate_uri_shape(dai_records, has_source_uri=False)
    print(f"DAI: {len(dai_records)} rows, {len(bad)} target_uri shape failures")

    # --- ADS ---
    ads_records = extract_pdf(ADS_PDF, "ADS", has_source_uri=True)
    resolve_predicate(ads_records)
    fish_graph = load_fish_graph(FISH_DIR)
    validate_ads_against_fish(ads_records, fish_graph)

    ads_fields = ["section", "source_uri", "source_label", "target_label", "target_uri", "match", "skos_predicate"]
    write_csv_json(ads_records, OUTPUT_DIR / "ads_aat_mappings", ads_fields)
    write_skos_ttl(ads_records, OUTPUT_DIR / "ads_aat_mappings.ttl")

    bad = validate_uri_shape(ads_records, has_source_uri=True)
    print(f"ADS: {len(ads_records)} rows, {len(bad)} URI shape failures")

    # --- combined ---
    combined_fields = ["source_file", "section", "source_uri", "source_label", "target_label", "target_uri", "match", "skos_predicate"]
    combined = (
        [{"source_file": DAI_PDF.name, **r} for r in dai_records]
        + [{"source_file": ADS_PDF.name, **r} for r in ads_records]
    )
    write_csv_json(combined, OUTPUT_DIR / "combined_aat_mappings", combined_fields)
    write_skos_ttl(combined, OUTPUT_DIR / "combined_aat_mappings.ttl")

    print(f"\nDone. Outputs written to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
