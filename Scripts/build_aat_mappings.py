#!/usr/bin/env python3
"""
Extract the ARIADNE <-> Getty AAT crosswalk tables (DAI + ADS + INRAP PDFs)
into tidy CSV/JSON, resolve missing source URIs against the DAI SKOS
thesaurus, validate ADS source URIs against the FISH RDF/XML thesauri and
INRAP source URIs against the PACTOLS SKOS RDF/XML thesauri, then emit both
SKOS-compliant mapping triples (Turtle) AND SSSOM TSV+YAML files — one per
source (dai_aat.sssom.tsv / ads_aat.sssom.tsv / inrap_aat.sssom.tsv) — which
are the intended master/published artifact per our mapping-model discussion
(SSSOM Option A). The CSV/JSON/TTL outputs are kept as a secondary/legacy
view for now.

Run this on your local machine (needs the PDFs, the DAI ttl, the FISH
RDF/XML files, and the Pactols RDF/XML files on disk — see CONFIG below).

    pip install pdfplumber rdflib
    pip install sssom   # optional, but recommended: `sssom validate <file>.sssom.tsv`

"""

import csv
import json
import logging
import re
import warnings
from pathlib import Path

import pdfplumber

# rdflib logs (with full traceback via exc_info=True) every time it hits a
# malformed xsd:date literal (e.g. "2023-10" with no day) or a URIRef it
# considers ill-formed (e.g. a MediaWiki "[[File:...]]" value that ended up
# in a source field and got resolved as a relative URI). Real issues in the
# Pactols/FISH source data, but irrelevant to the AAT mapping extraction and
# validation this script does — we never touch those date/URI values — so
# silence them here rather than let them bury the [WARN]/[INFO] lines that
# actually matter.
logging.getLogger("rdflib").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message=".*does not look like a valid URI.*")

try:
    import rdflib
    from rdflib.namespace import SKOS, RDF
except ImportError:
    rdflib = None

# ---------------------------------------------------------------------------
# CONFIG — local paths on your machine
# ---------------------------------------------------------------------------

BASE = Path("/home/lasse/repos/thesaurusscience")  

DAI_PDF = BASE / "Ariadne Mappings" / "ARIADNE_DAI_AAT_Mappings.pdf"
ADS_PDF = BASE / "Ariadne Mappings" / "ARIADNE_ADS_AAT_Mappings.pdf"
INRAP_PDF = BASE / "Ariadne Mappings" / "ARIADNE_INRAP_AAT_Mappings.pdf"
DAI_TTL = BASE / "DAI" / "export-2020-10-14_10-36.ttl"
FISH_DIR = BASE / "FISH"  # directory containing the FISH RDF/XML thesauri
PACTOLS_DIR = BASE / "Pactols"  # directory containing the Pactols SKOS RDF/XML files
                                # (Pactols_Lieux_th17_*.rdf, Pactols_Sujets_TH_1_*.rdf)

OUTPUT_DIR = BASE / "Output"       # legacy CSV/JSON/TTL views — kept for now, not the source of truth
SSSOM_DIR = BASE / "Mappings"       # new: SSSOM TSV+YAML files, one per (fromScheme, toScheme) source

GITHUB_TOOL_URL = "https://github.com/LasseMempel/thesaurusscience/blob/main/Scripts/build_aat_mappings.py"

# CREATOR_ID: tested against `sssom validate` — this slot specifically
# REQUIRES a proper CURIE (a prefix declared in the file's curie_map), not a
# bare URL. A bare "https://lod-am.net" fails validation even though it's a
# perfectly good absolute URI; only e.g. "orcid:0000-0000-0000-0000" (with
# "orcid" declared below) passes. If you don't have an ORCID, mint your own
# prefix instead — e.g. add "lodam: https://lod-am.net/" to each prefix map
# below and set CREATOR_ID = "lodam:lasse".
CREATOR_ID = "orcid:0009-0001-5183-1635"

# LICENSE / MAPPING_PROVIDER, by contrast, validated fine as bare absolute
# URIs in testing — no CURIE required for these.
LICENSE = "https://creativecommons.org/licenses/by/4.0/"
                                               # "https://creativecommons.org/publicdomain/zero/1.0/" (CC0)
PUBLICATION_DATE = "2026-09-10"               # date you actually publish/commit the SSSOM files — update per release

# Prefix maps: only INRAP/Pactols and ADS/FISH are grounded in real data seen
# this conversation. DAI's namespace was never confirmed against your actual
# DAI_TTL file — TODO placeholder until you check dai_records[i]["source_uri"]
# after resolve_dai_source_uris() runs and tell me the real namespace.
# "orcid" is declared everywhere in case you fill in CREATOR_ID with one.
PACTOLS_PREFIX_MAP = {
    "pactols": "https://ark.frantiq.fr/ark:/26678/",
    "aat": "http://vocab.getty.edu/aat/",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "orcid": "https://orcid.org/",
}
FISH_PREFIX_MAP = {
    "fish": "http://purl.org/heritagedata/",
    "aat": "http://vocab.getty.edu/aat/",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "orcid": "https://orcid.org/",
}
DAI_PREFIX_MAP = {
    "dai": "http://thesauri.dainst.org/",
    "aat": "http://vocab.getty.edu/aat/",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "orcid": "https://orcid.org/",
}

INRAP_SSSOM_META = {
    "mapping_set_id": "https://raw.githubusercontent.com/LasseMempel/thesaurusscience/main/Mappings/inrap_aat.sssom.tsv",
    "mapping_set_title": "INRAP PACTOLS \u2192 Getty AAT crosswalk (ARIADNE)",
    "mapping_set_description": "PACTOLS-thesaurus-to-AAT concept crosswalk compiled by INRAP for the "
                                "ARIADNE project, extracted from the project's published PDF table.",
    "license": LICENSE,
    "mapping_date": "2016-10-10",          # ARIADNE_INRAP_AAT_Mappings.pdf CreationDate (pdfinfo)
    "publication_date": PUBLICATION_DATE,
    "creator_id": CREATOR_ID,
    "creator_label": "Lasse Mempel",
    "mapping_provider": "https://ror.org/04andmq85",
    "mapping_tool": "build_aat_mappings.py (PDF table extraction)",
    "mapping_tool_id": GITHUB_TOOL_URL,
    "subject_source": "https://ark.frantiq.fr/ark:/26678/TH_1",     # Pactols Sujets scheme URI (seen in your example RDF)
    "object_source": "http://vocab.getty.edu/aat/",
    "see_also": "http://legacy.ariadne-infrastructure.eu/wp-content/uploads/2019/01/ARIADNE_INRAP_AAT_Mappings.pdf",
    "comment": "Original table compiled by Achille Felicetti (per PDF author metadata) for ARIADNE. "
               "2 of 1634 rows needed hand correction for a PDF word-wrap extraction artifact "
               "(see INRAP_LABEL_SPELLING_CORRECTIONS) before this export was generated.",
}

ADS_SSSOM_META = {
    "mapping_set_id": "https://raw.githubusercontent.com/LasseMempel/thesaurusscience/main/Mappings/ads_aat.sssom.tsv",
    "mapping_set_title": "ADS FISH \u2192 Getty AAT crosswalk (ARIADNE)",
    "mapping_set_description": "FISH-thesaurus-to-AAT concept crosswalk compiled by the Archaeology Data "
                                "Service (ADS) for the ARIADNE project, extracted from the project's "
                                "published PDF table.",
    "license": LICENSE,
    "mapping_date": "2019-01-29",
    "publication_date": PUBLICATION_DATE,
    "creator_id": CREATOR_ID,
    "creator_label": "Lasse Mempel",
    "mapping_provider": "https://ror.org/04w1khd64",
    "mapping_tool": "build_aat_mappings.py (PDF table extraction)",
    "mapping_tool_id": GITHUB_TOOL_URL,
    "subject_source": "http://purl.org/heritagedata/",              # FISH thesaurus scheme root
    "object_source": "http://vocab.getty.edu/aat/",
    "see_also": "http://legacy.ariadne-infrastructure.eu/wp-content/uploads/2019/01/ARIADNE_ADS_AAT_Mappings.pdf",
    "comment": "",
}

DAI_SSSOM_META = {
    "mapping_set_id": "https://raw.githubusercontent.com/LasseMempel/thesaurusscience/main/Mappings/dai_aat.sssom.tsv",
    "mapping_set_title": "DAI thesaurus \u2192 Getty AAT crosswalk (ARIADNE)",
    "mapping_set_description": "DAI-thesaurus-to-AAT concept crosswalk compiled by the German "
                                "Archaeological Institute (DAI) for the ARIADNE project. Source URIs "
                                "are resolved against your local DAI ttl export, not read from the PDF "
                                "directly (the PDF table has no source_uri column for DAI).",
    "license": LICENSE,
    "mapping_date": "2019-01-29",
    "publication_date": PUBLICATION_DATE,
    "creator_id": CREATOR_ID,
    "creator_label": "Lasse Mempel",
    "mapping_provider": "https://ror.org/041qv0h25",
    "mapping_tool": "build_aat_mappings.py (PDF table extraction + DAI ttl label resolution)",
    "mapping_tool_id": GITHUB_TOOL_URL,
    "subject_source": "http://thesauri.dainst.org/",
    "object_source": "http://vocab.getty.edu/aat/",
    "see_also": "http://legacy.ariadne-infrastructure.eu/wp-content/uploads/2019/01/ARIADNE_DAI_AAT_Mappings.pdf",
    "comment": "",
}

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

# ---------------------------------------------------------------------------
# Known spelling/extraction-artifact variants of INRAP (PACTOLS) source
# labels -> correct spelling. Same purpose as DAI_LABEL_SPELLING_CORRECTIONS
# above, but keyed on French labels. Add entries here as you spot them in
# the [WARN][PACTOLS label mismatch] output.
# ---------------------------------------------------------------------------

INRAP_LABEL_SPELLING_CORRECTIONS = {
    # Confirmed against the real PACTOLS validation run: these two rows had
    # their word-wrapped second half misrouted into target_uri during PDF
    # extraction (2 of 1634 rows). The corrected labels are PACTOLS's own
    # prefLabel(fr) for pcrtYPpqbYK8AK / pcrtb1ZjhINTB9 respectively.
    "inscription de": "inscription de fondation",
    "monnaie gallo-": "monnaie gallo-celtique",
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
    # "ark:"/"frantiq" added for the INRAP/PACTOLS source URIs
    # (https://ark.frantiq.fr/ark:/26678/...), which otherwise get a stray
    # space inserted when a single table cell wraps an ARK id across two
    # lines (e.g. "/ark:/26678/pcrtu6\nMrQiPdBO").
    looks_like_url_fragment = ("http" in raw) or bool(re.search(r"/aat/|concepts/|schemes/|ark:|frantiq", raw))
    if looks_like_url_fragment:
        return re.sub(r"\s+", "", raw)
    return re.sub(r"\s+", " ", raw).strip()


def is_uri_like_fragment(text):
    # Case-insensitive + ":" added: PACTOLS ARK ids are mixed-case base62
    # (e.g. "MrQiPdBO") and the "ark:" path segment needs the colon.
    return bool(re.fullmatch(r"[A-Za-z0-9/_.:\-]+", text)) or text.isdigit()


def looks_like_source_uri_piece(text):
    return bool(re.search(r"concepts|schemes|gedata|heritagedata|ark:|frantiq", text))


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


def validate_uri_shape(records, has_source_uri, source_uri_prefix="http://purl.org/heritagedata/"):
    bad = []
    for r in records:
        if not re.fullmatch(r"http://vocab\.getty\.edu/aat/\d{6,9}", r["target_uri"]):
            bad.append(("target_uri", r))
        if has_source_uri and r.get("source_uri") and not r["source_uri"].startswith(source_uri_prefix):
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
    files = (
        list(fish_dir.glob("*.rdf")) 
        + list(fish_dir.glob("*.xml")) 
        + list(fish_dir.glob("*.owl"))
        + list(fish_dir.glob("*.ttl"))
    )
    
    # Also load orphaned concepts from subdirectory
    orphaned_dir = fish_dir / "orphanedConcepts"
    if orphaned_dir.exists():
        orphaned_files = list(orphaned_dir.glob("*.ttl"))
        files.extend(orphaned_files)
    
    if not files:
        print(f"[WARN][FISH] no .rdf/.xml/.owl/.ttl files found in {fish_dir}")
        return g

    for f in files:
        try:
            # Use turtle format for .ttl files, xml for others
            parse_format = "turtle" if f.suffix.lower() == ".ttl" else "xml"
            g.parse(str(f), format=parse_format)
            
            # Special message for orphaned concepts
            if "orphanedConcepts" in str(f):
                print(f"[FISH] loaded {f.relative_to(fish_dir)} (orphaned concept)")
            else:
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
# PACTOLS (INRAP) SKOS RDF/XML thesauri: validate INRAP source URIs exist
# + French labels align. Same shape as the FISH validation above, adapted
# for: (a) RDF/XML instead of mixed formats, (b) multilingual prefLabel/
# altLabel where we care specifically about the fr labels the PDF quotes,
# and (c) http/https scheme drift between the PDF ("http://ark.frantiq.fr/...")
# and the RDF ("https://ark.frantiq.fr/...").
#
# HONEST CAVEAT: written against the one example triple you pasted, not
# against the actual .rdf files (I don't have them). If URIs/labels don't
# line up, send me a [WARN] snippet and I'll adjust.
# ---------------------------------------------------------------------------

def _normalize_scheme(uri):
    """Strip the scheme so http/https variants of the same ARK URI compare equal."""
    return re.sub(r"^https?://", "", uri)


def load_pactols_graph(pactols_dir):
    if rdflib is None:
        raise RuntimeError("rdflib is required — pip install rdflib")

    g = rdflib.Graph()
    files = sorted(pactols_dir.glob("*.rdf")) + sorted(pactols_dir.glob("*.xml"))

    if not files:
        print(f"[WARN][PACTOLS] no .rdf/.xml files found in {pactols_dir}")
        return g

    for f in files:
        try:
            g.parse(str(f), format="xml")
            print(f"[PACTOLS] loaded {f.name}")
        except Exception as e:  # noqa: BLE001 — surfacing parse errors as warnings, not fatal
            print(f"[WARN][PACTOLS] failed to parse {f.name}: {e}")

    print(f"[PACTOLS] combined graph: {len(g)} triples from {len(files)} file(s)")
    return g


def validate_inrap_against_pactols(records, pactols_graph, label_corrections=None):
    if len(pactols_graph) == 0:
        print("[WARN][PACTOLS] graph is empty — skipping PACTOLS validation entirely")
        return

    label_corrections = label_corrections or {}

    # index every subject by its scheme-normalized form, so an "http://" PDF
    # URI matches an "https://" RDF rdf:about
    existing_by_norm = {}
    for s in pactols_graph.subjects():
        existing_by_norm.setdefault(_normalize_scheme(str(s)), str(s))

    missing = 0
    label_mismatches = 0
    non_fr_fallbacks = 0
    corrected = 0

    for r in records:
        uri = r.get("source_uri", "")
        if not uri:
            continue

        real_uri = existing_by_norm.get(_normalize_scheme(uri))
        if real_uri is None:
            missing += 1
            print(f"[WARN][PACTOLS URI not found] {uri} (label: '{r['source_label']}', "
                  f"section: {r['section']})")
            continue
        uri_ref = rdflib.URIRef(real_uri)

        label = r["source_label"]
        corrected_label = label_corrections.get(label)
        if corrected_label and corrected_label != label:
            print(f"[INFO][PACTOLS spelling corrected] '{label}' -> '{corrected_label}' "
                  f"(target: {r['target_label']})")
            label = corrected_label
            corrected += 1
        source_label = label.strip().lower()

        fr_pref = {
            str(o).strip().lower()
            for _, _, o in pactols_graph.triples((uri_ref, SKOS.prefLabel, None))
            if o.language == "fr"
        }
        fr_alt = {
            str(o).strip().lower()
            for _, _, o in pactols_graph.triples((uri_ref, SKOS.altLabel, None))
            if o.language == "fr"
        }
        any_lang_labels = {
            str(o).strip().lower()
            for _, _, o in pactols_graph.triples((uri_ref, SKOS.prefLabel, None))
        } | {
            str(o).strip().lower()
            for _, _, o in pactols_graph.triples((uri_ref, SKOS.altLabel, None))
        }

        if source_label in fr_pref:
            pass  # perfect match via French prefLabel
        elif source_label in fr_alt:
            print(f"[INFO][PACTOLS altLabel fallback] {real_uri} — mapping uses non-preferred "
                  f"fr label '{r['source_label']}', prefLabel(fr) is {sorted(fr_pref) or '?'}")
        elif source_label in any_lang_labels:
            non_fr_fallbacks += 1
            print(f"[INFO][PACTOLS non-fr label] {real_uri} — '{r['source_label']}' matched a "
                  f"label in another language; no fr prefLabel/altLabel matched")
        else:
            label_mismatches += 1
            print(f"[WARN][PACTOLS label mismatch] {real_uri} — mapping says '{r['source_label']}', "
                  f"thesaurus has fr prefLabel(s): {sorted(fr_pref)}, fr altLabel(s): {sorted(fr_alt)}")

    print(f"\n[PACTOLS Validation Summary]")
    print(f"  Source URIs not found: {missing}")
    print(f"  True label mismatches: {label_mismatches}")
    print(f"  Non-fr label fallbacks: {non_fr_fallbacks}")
    print(f"  Labels auto-corrected via INRAP_LABEL_SPELLING_CORRECTIONS: {corrected}")
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


def _scheme_variants(uri):
    """Same http/https drift as the PACTOLS ark.frantiq.fr URIs elsewhere in this
    script (see _normalize_scheme) — the PDF says http://, the source RDF and
    our own prefix maps say https://. Try both when CURIE-matching."""
    if uri.startswith("http://"):
        yield uri
        yield "https://" + uri[len("http://"):]
    elif uri.startswith("https://"):
        yield uri
        yield "http://" + uri[len("https://"):]
    else:
        yield uri


def to_curie(uri, prefix_map):
    """Longest-prefix match against prefix_map (scheme-drift tolerant); returns
    the URI unchanged if nothing matches."""
    best_prefix, best_ns, best_variant = None, "", None
    for variant in _scheme_variants(uri):
        for prefix, ns in prefix_map.items():
            if variant.startswith(ns) and len(ns) > len(best_ns):
                best_prefix, best_ns, best_variant = prefix, ns, variant
    return f"{best_prefix}:{best_variant[len(best_ns):]}" if best_prefix else uri


# Order matters for header readability only (curie_map, then the rest of the
# recognised MappingSet-level slots, in roughly the order the SSSOM spec
# groups them: identity -> provenance -> people/tools -> vocab sources -> free text).
SSSOM_HEADER_SLOT_ORDER = [
    "mapping_set_id", "mapping_set_title", "mapping_set_version", "mapping_set_description",
    "license", "mapping_date", "publication_date",
    "creator_id", "creator_label", "mapping_provider",
    "mapping_tool", "mapping_tool_id",
    "subject_source", "object_source",
    "see_also", "comment",
]


def write_sssom_tsv(records, out_path, mapping_set_meta, prefix_map,
                     row_mapping_justification="semapv:UnspecifiedMatching",
                     subject_field="source_uri", subject_label_field="source_label",
                     object_field="target_uri", object_label_field="target_label"):
    """
    Write records as an SSSOM TSV+YAML file (the "#"-prefixed YAML header block
    followed by a tab-separated mapping table — this is literally what
    "SSSOM/TSV" means, see https://mapping-commons.github.io/sssom/).

    mapping_set_meta: dict of MappingSet-level slot values (mapping_set_id,
        license, creator_id, ... — see SSSOM_HEADER_SLOT_ORDER). Only slots
        with a non-empty value are written.
    prefix_map: dict CURIE-prefix -> namespace URI, written as curie_map and
        used to compact subject_id/object_id into CURIEs.
    row_mapping_justification: SEMAPV CURIE applied to every row in this file
        (propagated from the mapping-set level — none of our current tracks
        need a different justification per row within the same source file).

    This intentionally does NOT depend on the `sssom` package to WRITE the
    file — it's a small, fully-controlled format, and keeping the generator
    dependency-free means this script still only needs pdfplumber + rdflib.
    Use the `sssom` CLI/library downstream to validate, convert to RDF, merge,
    or otherwise operate on the file this produces (`pip install sssom`;
    `sssom validate out.sssom.tsv`).
    """
    rows = []
    skipped_no_subject = skipped_no_pred = skipped_bad_object_uri = 0
    for r in records:
        subject_uri = r.get(subject_field, "")
        pred = r.get("skos_predicate", "")
        object_uri = r.get(object_field, "")
        if not subject_uri:
            skipped_no_subject += 1
            continue
        if not pred:
            skipped_no_pred += 1
            continue
        if not re.fullmatch(r"http://vocab\.getty\.edu/aat/\d{6,9}", object_uri):
            # known PDF word-wrap extraction artifacts (see validate_uri_shape /
            # the [WARN][bad target_uri shape] printout) — don't publish a
            # mapping to a garbled AAT URI; fix the source row and re-run instead.
            skipped_bad_object_uri += 1
            continue
        rows.append({
            "subject_id": to_curie(subject_uri, prefix_map),
            "predicate_id": f"skos:{pred}",
            "object_id": to_curie(object_uri, prefix_map),
            "mapping_justification": row_mapping_justification,
            "subject_label": r.get(subject_label_field, ""),
            "object_label": r.get(object_label_field, ""),
            "comment": r.get("sssom_comment", ""),
        })

    with out_path.open("w", encoding="utf-8", newline="") as f:
        f.write("#curie_map:\n")
        for prefix, ns in prefix_map.items():
            f.write(f"#  {prefix}: {ns}\n")
        for slot in SSSOM_HEADER_SLOT_ORDER:
            value = mapping_set_meta.get(slot)
            if value:
                f.write(f"#{slot}: {value}\n")

        tsv_fields = ["subject_id", "predicate_id", "object_id", "mapping_justification",
                      "subject_label", "object_label", "comment"]
        writer = csv.DictWriter(f, fieldnames=tsv_fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in tsv_fields})

    print(f"[sssom] {out_path.name}: {len(rows)} mappings written "
          f"({skipped_no_subject} skipped: no subject URI, {skipped_no_pred} skipped: unresolved match type, "
          f"{skipped_bad_object_uri} skipped: malformed object_uri — fix and re-run)")
    return out_path


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
    SSSOM_DIR.mkdir(parents=True, exist_ok=True)

    # --- DAI ---
    dai_records = extract_pdf(DAI_PDF, "DAI", has_source_uri=False)
    dai_index, dai_norm_index = build_dai_label_index(DAI_TTL)
    resolve_dai_source_uris(dai_records, dai_index, dai_norm_index)
    resolve_predicate(dai_records)

    dai_fields = ["section", "source_uri", "source_label", "target_label", "target_uri", "match", "skos_predicate"]
    write_csv_json(dai_records, OUTPUT_DIR / "dai_aat_mappings", dai_fields)
    write_skos_ttl(dai_records, OUTPUT_DIR / "dai_aat_mappings.ttl")
    write_sssom_tsv(dai_records, SSSOM_DIR / "dai_aat.sssom.tsv", DAI_SSSOM_META, DAI_PREFIX_MAP)

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
    write_sssom_tsv(ads_records, SSSOM_DIR / "ads_aat.sssom.tsv", ADS_SSSOM_META, FISH_PREFIX_MAP)

    bad = validate_uri_shape(ads_records, has_source_uri=True, source_uri_prefix="http://purl.org/heritagedata/")
    print(f"ADS: {len(ads_records)} rows, {len(bad)} URI shape failures")

    # --- INRAP (PACTOLS) ---
    inrap_records = extract_pdf(INRAP_PDF, "PACTOLS", has_source_uri=True)
    for r in inrap_records:
        # this PDF has a single un-numbered "PACTOLS" heading (no "1. PACTOLS"
        # style section markers like DAI/ADS), so the header-position finder
        # never fires and section stays None — fill it in directly.
        r["section"] = r["section"] or "PACTOLS"
    # apply corrections to the records themselves (not just inside the Pactols
    # validator's comparison) so CSV/TTL/SSSOM outputs all carry the fixed label
    for r in inrap_records:
        fix = INRAP_LABEL_SPELLING_CORRECTIONS.get(r["source_label"])
        if fix:
            r["sssom_comment"] = (f"source_label corrected from '{r['source_label']}' to '{fix}' "
                                   f"(PDF word-wrap extraction artifact)")
            r["source_label"] = fix

    resolve_predicate(inrap_records)
    pactols_graph = load_pactols_graph(PACTOLS_DIR)
    validate_inrap_against_pactols(inrap_records, pactols_graph, INRAP_LABEL_SPELLING_CORRECTIONS)

    inrap_fields = ["section", "source_uri", "source_label", "target_label", "target_uri", "match", "skos_predicate"]
    write_csv_json(inrap_records, OUTPUT_DIR / "inrap_aat_mappings", inrap_fields)
    write_skos_ttl(inrap_records, OUTPUT_DIR / "inrap_aat_mappings.ttl")
    write_sssom_tsv(inrap_records, SSSOM_DIR / "inrap_aat.sssom.tsv", INRAP_SSSOM_META, PACTOLS_PREFIX_MAP)

    bad = validate_uri_shape(inrap_records, has_source_uri=True, source_uri_prefix="http://ark.frantiq.fr/ark:/26678/")
    print(f"INRAP: {len(inrap_records)} rows, {len(bad)} URI shape failures")
    for _, r in bad:
        print(f"  [WARN][bad target_uri shape] '{r['source_label']}' -> target_uri={r['target_uri']!r} "
              f"(fix by hand, then re-run)")

    # --- combined ---
    combined_fields = ["source_file", "section", "source_uri", "source_label", "target_label", "target_uri", "match", "skos_predicate"]
    combined = (
        [{"source_file": DAI_PDF.name, **r} for r in dai_records]
        + [{"source_file": ADS_PDF.name, **r} for r in ads_records]
        + [{"source_file": INRAP_PDF.name, **r} for r in inrap_records]
    )
    write_csv_json(combined, OUTPUT_DIR / "combined_aat_mappings", combined_fields)
    write_skos_ttl(combined, OUTPUT_DIR / "combined_aat_mappings.ttl")

    print(f"\nDone. Outputs written to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()