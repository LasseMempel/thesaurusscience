#!/usr/bin/env python3
"""
Harvest Wikidata <-> {AAT, FISH x3, PACTOLS, GND} identifier crosswalks via
SPARQL against the public Wikidata Query Service, and write them out as
SSSOM TSV+YAML files, one per target vocabulary.

    pip install requests rdflib

Run:
    python3 build_wikidata_mappings.py             # AAT/FISHx3/PACTOLS, then GND
    python3 build_wikidata_mappings.py --gnd-only  # just the two-pass GND step
                                                    # (re-scans Mappings/ first)

--------------------------------------------------------------------------
HONEST CAVEAT: I cannot reach query.wikidata.org from this sandbox (it isn't
in my allowed network egress list), so the SPARQL queries below have NOT
been run against the live endpoint. What I *could* and did verify locally:
  - every property/item ID used (P1014, P10674, P14370, P14369, P4212, P227,
    P4390, and the P4390 value QIDs Q39893184/Q39893449/Q39894604/
    Q39894595/Q39893967) against current Wikidata property/item pages.
  - every SPARQL query string parses as syntactically valid SPARQL 1.1
    (checked with rdflib's own parser, not just eyeballed).
  - all the pure-Python logic (CURIE expansion/compaction, the existing-
    mapping-file scanner, dedup/merge, the SSSOM writer) against realistic
    mock WDQS JSON responses and your real generated inrap_aat.sssom.tsv.
Run it, and if a query errors out or times out against the real endpoint,
send me the error and I'll adjust — WDQS's actual behavior (timeouts,
result truncation) can't be predicted with certainty from here.
--------------------------------------------------------------------------

Two-pass design for GND specifically (per your instruction: GND is too broad
to harvest wholesale, so we only want it for entities already known to be
archaeology-relevant):
  Pass 1: harvest AAT/FISH x3/PACTOLS crosswalks directly (below) — this
          already anchors most of the "relevant" Wikidata items.
  Pass 2: scan every *.sssom.tsv already under Mappings/ (both this script's
          own pass-1 output AND your existing ARIADNE + vocabulary-internal
          tsvs) for any Wikidata QID or GND URI appearing as subject_id or
          object_id, then run two more Wikidata queries — forward (QID ->
          GND) and reverse (known GND id -> QID) — merged into one
          wikidata_gnd.sssom.tsv.
"""

from __future__ import annotations

import csv
import logging
import re
import sys
import time
import warnings
from collections import defaultdict
from pathlib import Path

import requests
import rdflib

# The local Pactols dumps (load_pactols_scheme_index) contain malformed
# xsd:date literals (e.g. "2023-10" with no day) and at least one bogus
# wikitext-style "URI" (a stray [[File:...]] image link). rdflib logs a full
# traceback for the former and warnings.warn()s for the latter — neither is
# fatal, both are just noise from the source data, not from this script.
# Same suppression as build_vocabulary_mappings.py's DAI/FISH/Pactols parsing.
logging.getLogger("rdflib.term").setLevel(logging.ERROR)
logging.getLogger("rdflib").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message=".*does not look like a valid URI.*")


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

MAPPINGS_DIR = Path("/home/mempellaenger/repos/thesaurusscience/Mappings")

# Local Pactols RDF/XML dumps — same folder build_vocabulary_mappings.py
# scans (its VOCABULARIES["Pactols"]["path"]), used here to resolve each
# harvested ARK id to Lieux vs Sujets (see load_pactols_scheme_index()).
# ASSUMPTION: adjust if your actual layout differs — it's the one constant
# doing that work.
PACTOLS_DIR = MAPPINGS_DIR.parent / "Pactols"

WDQS_ENDPOINT = "https://query.wikidata.org/sparql"
# Wikidata expects a descriptive User-Agent identifying the tool + a contact —
# generic/absent UAs get rate-limited. Put a real contact here before running.
USER_AGENT = "LEIZA-ThesaurusCrosswalk/0.1 (https://lod-am.net; mailto:TODO@example.org)"

LANGS = ["en", "de", "fr"]     # label languages, in priority order
VALUES_BATCH_SIZE = 150        # QIDs/GND-ids per VALUES-batched query — lowered from 300:
                                # your GND anchor set turned out to be 70k+ QIDs (much bigger
                                # than estimated), so the forward-lookup pass alone was ~234
                                # sequential requests; smaller batches = smaller, faster,
                                # less-likely-to-502 individual queries, at the cost of more
                                # round trips.
PAGE_SIZE = 5000               # LIMIT/OFFSET page size for the per-property harvest — lowered
                                # from 20000: the PACTOLS run hit 2 corrupted/truncated JSON
                                # responses on its single ~18.5k-row page before succeeding on
                                # retry. Smaller pages = smaller individual responses, less
                                # exposed to a mid-stream cutoff.
REQUEST_TIMEOUT = 90            # seconds
POLITE_DELAY = 2.0              # seconds between requests — raised from 1.0, given the real
                                 # request counts turned out much higher than expected

# Filled in with the same values already established in build_aat_mappings.py
# / build_vocabulary_mappings.py (was "TODO:..." here before — this script
# hadn't been run for real yet).
CREATOR_ID = "orcid:0009-0001-5183-1635"
CREATOR_LABEL = "Lasse Mempel-Länger"
LICENSE = "https://creativecommons.org/licenses/by/4.0/"
MAPPING_PROVIDER = "https://www.wikidata.org/"

GITHUB_TOOL_URL = "https://github.com/LasseMempel/thesaurusscience/blob/main/Scripts/build_wikidata_mappings.py"

WD_ENTITY_NS = "http://www.wikidata.org/entity/"
WD_STATEMENT_NS = "http://www.wikidata.org/entity/statement/"

# ---------------------------------------------------------------------------
# Scheme identifiers + filename slugs — kept IDENTICAL to SCHEME_REGISTRY in
# build_vocabulary_mappings.py (and to build_aat_mappings.py's slugs) so all
# three scripts' output reads the same way. Wikidata/GND aren't natively
# skos:Concept/skos:ConceptScheme, so their "scheme" here is each
# vocabulary's BARTOC registration (same choice made in the other two
# scripts) — deliberately distinct from their CONCEPT namespace (WD_ENTITY_NS
# / GND_NAMESPACE below), which is what actually compacts subject_id/
# object_id CURIEs. Double-check these two against how your Cocoda instance
# actually has Wikidata/GND registered before publishing.
WIKIDATA_SCHEME_URI = "http://bartoc.org/en/node/1940"
GND_SCHEME_URI = "http://bartoc.org/en/node/430"

# Pactols scheme URIs — identical to SCHEME_REGISTRY's pactols_lieux/
# pactols_sujets entries in build_vocabulary_mappings.py.
PACTOLS_SCHEME_URIS = {
    "pactols_lieux": "https://ark.frantiq.fr/ark:/26678/th17",
    "pactols_sujets": "https://ark.frantiq.fr/ark:/26678/TH_1",
}

# One SSSOM mapping set per external vocabulary. All treated as skos:exactMatch
# by default per your call — overridden per-row only when a P4390 qualifier
# says otherwise (see P4390_TO_SKOS below).
PROPERTY_CONFIG = {
    "aat": {
        "pid": "P1014",
        "slug": "aat",
        "prefix": "aat",
        "namespace": "http://vocab.getty.edu/aat/",
        "title": "Wikidata to Getty AAT",
        "object_source": "http://vocab.getty.edu/aat/",
    },
    "fish_objects": {
        "pid": "P10674",
        "slug": "ads_mda_obj",
        "prefix": "fishobj",
        "namespace": "http://purl.org/heritagedata/schemes/mda_obj/concepts/",
        "title": "Wikidata to FISH Archaeological Objects Thesaurus",
        # FIX: this was pointing at the concept-container path
        # (".../concepts/"), not the scheme itself — object_source should be
        # the same scheme URI used everywhere else for this scheme (matches
        # SCHEME_REGISTRY's "ads_mda_obj" in build_vocabulary_mappings.py).
        # The concept-container path stays in "namespace" below, which is
        # what actually compacts object_id CURIEs.
        "object_source": "http://purl.org/heritagedata/schemes/mda_obj",
    },
    "fish_evidence": {
        "pid": "P14370",
        # UNCONFIRMED: "eh_evd" was never in the list of 5 FISH schemes you
        # originally gave me (eh_tbm, eh_com, mda_obj, eh_tmc, eh_tmt2) — this
        # entry predates that list. I'm assuming it follows the same
        # "schemes/<code>" URL pattern as the other five, but haven't
        # verified this scheme URI actually resolves/exists. Check it (and
        # add it to build_vocabulary_mappings.py's SCHEME_REGISTRY too if
        # confirmed) before relying on this file.
        "slug": "ads_eh_evd",
        "prefix": "fishevd",
        "namespace": "http://purl.org/heritagedata/schemes/eh_evd/concepts/",
        "title": "Wikidata to FISH Evidence Thesaurus",
        "object_source": "http://purl.org/heritagedata/schemes/eh_evd",
    },
    "fish_monument_types": {
        "pid": "P14369",
        "slug": "ads_eh_tmt2",
        "prefix": "fishtmt",
        "namespace": "http://purl.org/heritagedata/schemes/eh_tmt2/concepts/",
        "title": "Wikidata to FISH Monument Types Thesaurus",
        # Same FIX as fish_objects above.
        "object_source": "http://purl.org/heritagedata/schemes/eh_tmt2",
    },
    "pactols": {
        "pid": "P4212",
        "prefix": "pactols",
        "namespace": "https://ark.frantiq.fr/ark:/26678/",
        "title": "Wikidata to PACTOLS",
        # No "slug"/"object_source" here on purpose: PACTOLS covers >1 scheme
        # (Sujets/TH_1, Lieux/th17, ...) merged under one ARK namespace —
        # P4212 doesn't distinguish which. Handled specially by
        # build_and_write_pactols_mapping(), which resolves each harvested id
        # against the local Pactols RDF dumps (PACTOLS_DIR) instead — see
        # load_pactols_scheme_index().
    },
}

GND_PID = "P227"
GND_PREFIX = "gnd"
GND_NAMESPACES = (
    "https://d-nb.info/gnd/",
    "http://d-nb.info/gnd/",
)

# P4390 (mapping relation type) qualifier values -> SKOS predicate local name.
# Verified against the property's actual one-of constraint on Wikidata.
P4390_TO_SKOS = {
    "Q39893449": "exactMatch",
    "Q39893184": "closeMatch",
    "Q39894604": "relatedMatch",
    "Q39894595": "broadMatch",
    "Q39893967": "narrowMatch",
}

BASE_PREFIX_MAP = {
    "wd": WD_ENTITY_NS,
    "wds": WD_STATEMENT_NS,
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "semapv": "https://w3id.org/semapv/vocab/",
    "github": "https://github.com/",
    # Compacts subject_source/object_source values that are themselves
    # BARTOC scheme URIs (WIKIDATA_SCHEME_URI/GND_SCHEME_URI above), e.g.
    # "bartoc:en/node/1940" instead of the bare URI.
    "bartoc": "http://bartoc.org/",
}

# MappingSet-level slots CURIE-compacted at write time — same fix already
# applied in build_aat_mappings.py/build_vocabulary_mappings.py after we hit
# this with `sssom validate` on the ARIADNE files.
SSSOM_CURIE_SLOTS = {"mapping_tool_id", "subject_source", "object_source"}

SSSOM_HEADER_SLOT_ORDER = [
    "mapping_set_id", "mapping_set_title", "mapping_set_description",
    "license", "mapping_date", "publication_date",
    "creator_id", "creator_label", "mapping_provider",
    "mapping_tool", "mapping_tool_id",
    "subject_source", "object_source",
    "see_also", "comment",
]


# ---------------------------------------------------------------------------
# SPARQL plumbing
# ---------------------------------------------------------------------------

def run_sparql_select(query, retries=3):
    """POST a SELECT query to WDQS, return the list of result-row bindings
    (raw SPARQL-JSON). Retries with backoff on transient errors/timeouts —
    including a JSON-decode failure, which in practice (see build log from
    the PACTOLS run) means a truncated/corrupted response body from a large
    query, not necessarily a raised timeout; treated the same way here."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                WDQS_ENDPOINT,
                data={"query": query, "format": "json"},
                headers={"Accept": "application/sparql-results+json", "User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()["results"]["bindings"]
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] SPARQL request failed (attempt {attempt}/{retries}): {e}")
            if attempt == retries:
                raise
            # 502s in particular mean WDQS's own proxy gave up on a slow
            # backend query — back off harder than for an ordinary hiccup.
            backoff = POLITE_DELAY * attempt * (4 if "502" in str(e) else 2)
            time.sleep(backoff)
    return []


def _chunks(seq, size):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def harvest_property(pid):
    """SELECT every (item, id, statement, mappingRelation?) for a direct-ID
    property, paginated via LIMIT/OFFSET so this stays safe regardless of how
    large the property turns out to be (unknown from here — see caveat)."""
    rows = []
    offset = 0
    while True:
        query = f"""
        PREFIX p: <http://www.wikidata.org/prop/>
        PREFIX ps: <http://www.wikidata.org/prop/statement/>
        PREFIX pq: <http://www.wikidata.org/prop/qualifier/>
        SELECT ?item ?id ?statement ?mappingRelation WHERE {{
          ?item p:{pid} ?statement .
          ?statement ps:{pid} ?id .
          OPTIONAL {{ ?statement pq:P4390 ?mappingRelation . }}
        }}
        ORDER BY ?item
        LIMIT {PAGE_SIZE} OFFSET {offset}
        """
        page = run_sparql_select(query)
        if not page:
            break
        rows.extend(page)
        print(f"  [{pid}] fetched {len(page)} rows (offset {offset})")
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(POLITE_DELAY)
    return rows


def fetch_labels(qids, langs=None):
    """qids: bare QIDs ('Q123', not full URIs). Returns {qid: best_label}."""
    langs = langs or LANGS
    best = {}
    for batch in _chunks(sorted(set(qids)), VALUES_BATCH_SIZE):
        if not batch:
            continue
        values = " ".join(f"wd:{q}" for q in batch)
        lang_filter = ", ".join(f'"{l}"' for l in langs)
        query = f"""
        PREFIX wd: <{WD_ENTITY_NS}>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT ?item ?label WHERE {{
          VALUES ?item {{ {values} }}
          ?item rdfs:label ?label .
          FILTER(LANG(?label) IN ({lang_filter}))
        }}
        """
        for row in run_sparql_select(query):
            qid = row["item"]["value"].rsplit("/", 1)[-1]
            lang = row["label"].get("xml:lang", "")
            val = row["label"]["value"]
            rank = langs.index(lang) if lang in langs else len(langs)
            if qid not in best or rank < best[qid][0]:
                best[qid] = (rank, val)
        time.sleep(POLITE_DELAY)
    return {qid: val for qid, (_, val) in best.items()}


# ---------------------------------------------------------------------------
# CURIE helpers + SSSOM writer (self-contained copy — see note at bottom of
# file about consolidating this with the other two scripts' near-identical
# versions into one shared module)
# ---------------------------------------------------------------------------

def mapping_set_id(filename, rel_dir=""):
    sub = f"{rel_dir}/" if rel_dir else ""
    return (
        "https://raw.githubusercontent.com/LasseMempel/thesaurusscience/"
        f"main/Mappings/{sub}{filename}"
    )


def to_curie(uri, prefix_map):
    best_prefix, best_ns = None, ""
    for prefix, ns in prefix_map.items():
        if ns and uri.startswith(ns) and len(ns) > len(best_ns):
            best_prefix, best_ns = prefix, ns
    return f"{best_prefix}:{uri[len(best_ns):]}" if best_prefix else uri


def write_sssom_tsv(rows, out_path, meta, prefix_map):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        f.write("#curie_map:\n")
        for prefix, ns in prefix_map.items():
            f.write(f"#  {prefix}: {ns}\n")
        for slot in SSSOM_HEADER_SLOT_ORDER:
            value = meta.get(slot)
            if value:
                if slot in SSSOM_CURIE_SLOTS:
                    value = to_curie(value, prefix_map)
                f.write(f"#{slot}: {value}\n")

        fields = ["subject_id", "predicate_id", "object_id", "mapping_justification",
                  "subject_label", "object_label", "mapping_source", "comment"]
        writer = csv.DictWriter(f, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})
    print(f"[sssom] {out_path.name}: {len(rows)} mappings written")


# ---------------------------------------------------------------------------
# Track 1: AAT / FISH x3 / PACTOLS
# ---------------------------------------------------------------------------

def build_and_write_property_mapping(name, cfg):
    print(f"\n=== {cfg['title']} ({cfg['pid']}) ===")
    raw_rows = harvest_property(cfg["pid"])
    print(f"  {len(raw_rows)} statements harvested")
    if not raw_rows:
        return []

    qids = {row["item"]["value"].rsplit("/", 1)[-1] for row in raw_rows}
    labels = fetch_labels(qids)

    prefix_map = dict(BASE_PREFIX_MAP)
    prefix_map[cfg["prefix"]] = cfg["namespace"]

    out_rows = []
    for row in raw_rows:
        item_uri = row["item"]["value"]
        qid = item_uri.rsplit("/", 1)[-1]
        ext_id = row["id"]["value"]
        statement_uri = row["statement"]["value"]
        mapping_relation_qid = (row.get("mappingRelation", {}).get("value", "") or "").rsplit("/", 1)[-1] or None
        predicate = P4390_TO_SKOS.get(mapping_relation_qid, "exactMatch")

        out_rows.append({
            "subject_id": to_curie(item_uri, prefix_map),
            "predicate_id": f"skos:{predicate}",
            "object_id": to_curie(cfg["namespace"] + ext_id, prefix_map),
            "mapping_justification": "semapv:UnspecifiedMatching",
            "subject_label": labels.get(qid, ""),
            # object_label intentionally blank: the target vocabulary isn't
            # loaded here, and per SSSOM this slot is optional — no data is
            # lost by leaving it empty, see build_vocabulary_mappings.py
            # discussion.
            "object_label": "",
            "mapping_source": statement_uri,
            "comment": "",
        })

    # Every remaining PROPERTY_CONFIG entry (not "pactols", which is handled
    # separately by build_and_write_pactols_mapping) has a confirmed slug.
    slug = cfg["slug"]
    filename = f"wikidata_{slug}.sssom.tsv"

    meta = {
        "mapping_set_id": mapping_set_id(filename),
        "mapping_set_title": cfg["title"],
        "mapping_set_description": (
            f"Wikidata items carrying a {cfg['pid']} identifier, harvested via SPARQL "
            f"from {WDQS_ENDPOINT}."
        ),
        "license": LICENSE,
        "creator_id": CREATOR_ID,
        "creator_label": CREATOR_LABEL,
        "mapping_provider": MAPPING_PROVIDER,
        "mapping_tool": "build_wikidata_mappings.py",
        "mapping_tool_id": GITHUB_TOOL_URL,
        "subject_source": WIKIDATA_SCHEME_URI,
        "object_source": cfg["object_source"],
        "comment": (
            "predicate_id defaults to skos:exactMatch (per project decision); overridden "
            "per-row only when the Wikidata statement carries a mapping relation type "
            "(P4390) qualifier. mapping_source is the specific Wikidata statement URI."
        ),
    }

    out_path = MAPPINGS_DIR / filename
    write_sssom_tsv(out_rows, out_path, meta, prefix_map)
    return out_rows


# ---------------------------------------------------------------------------
# Track: PACTOLS (split Lieux/Sujets via local RDF dumps)
# ---------------------------------------------------------------------------

def detect_pactols_scheme_from_filename(filename: str) -> str | None:
    """Classifies a local Pactols dump file by name (e.g.
    'Pactols_Lieux_th17_*.rdf' / 'Pactols_Sujets_TH_1_*.rdf') — identical
    convention to build_aat_mappings.py/build_vocabulary_mappings.py.
    ASSUMPTION based on that naming convention; adjust the patterns below if
    your real filenames differ."""
    name = filename.lower()
    if "lieux" in name or "th17" in name:
        return "pactols_lieux"
    if "sujets" in name or "th_1" in name or "th1" in name:
        return "pactols_sujets"
    return None


def load_pactols_scheme_index(pactols_dir: Path) -> dict[str, str]:
    """Mirrors build_aat_mappings.py's load_pactols_graph(): parses every
    local Pactols dump file and remembers, for every concept URI (subject)
    found in it, which scheme that FILE represents (Pactols concept URIs
    don't reveal scheme themselves — see PACTOLS_SCHEME_URIS). Returns
    {concept_uri: scheme_uri}."""
    index: dict[str, str] = {}
    files = sorted(pactols_dir.glob("*.rdf")) + sorted(pactols_dir.glob("*.xml"))

    if not files:
        print(f"[WARN][PACTOLS] no .rdf/.xml files found in {pactols_dir} — "
              "every id will end up unresolved (unknown_target/wikidata_pactols.sssom.tsv)")
        return index

    for f in files:
        scheme_key = detect_pactols_scheme_from_filename(f.name)
        if scheme_key is None:
            print(f"[WARN][PACTOLS] can't tell Lieux vs Sujets from filename '{f.name}' "
                  "— concepts from this file won't be indexed")
            continue
        scheme_uri = PACTOLS_SCHEME_URIS[scheme_key]
        try:
            g = rdflib.Graph()
            g.parse(str(f), format="xml")
            for s in g.subjects():
                index.setdefault(str(s), scheme_uri)
            print(f"[PACTOLS] indexed {f.name}: {len(g)} triples, scheme={scheme_key}")
        except Exception as e:  # noqa: BLE001 — surfacing parse errors as warnings, not fatal
            print(f"[WARN][PACTOLS] failed to parse {f.name}: {e}")

    print(f"[PACTOLS] scheme index: {len(index)} concept URI(s) from {len(files)} file(s)")
    return index


def build_and_write_pactols_mapping(cfg: dict) -> list[dict]:
    print(f"\n=== {cfg['title']} ({cfg['pid']}) ===")
    raw_rows = harvest_property(cfg["pid"])
    print(f"  {len(raw_rows)} statements harvested")
    if not raw_rows:
        return []

    qids = {row["item"]["value"].rsplit("/", 1)[-1] for row in raw_rows}
    labels = fetch_labels(qids)

    prefix_map = dict(BASE_PREFIX_MAP)
    prefix_map[cfg["prefix"]] = cfg["namespace"]
    prefix_map.update(PACTOLS_SCHEME_URIS)  # pactols_lieux:/pactols_sujets: for subject/object_source

    scheme_index = load_pactols_scheme_index(PACTOLS_DIR)

    # Group by resolved scheme_uri; None = not found in either local dump.
    groups: dict[str | None, list[dict]] = defaultdict(list)
    unresolved_ids: set[str] = set()

    for row in raw_rows:
        item_uri = row["item"]["value"]
        qid = item_uri.rsplit("/", 1)[-1]
        ext_id = row["id"]["value"]
        object_uri = cfg["namespace"] + ext_id
        statement_uri = row["statement"]["value"]
        mapping_relation_qid = (row.get("mappingRelation", {}).get("value", "") or "").rsplit("/", 1)[-1] or None
        predicate = P4390_TO_SKOS.get(mapping_relation_qid, "exactMatch")

        scheme_uri = scheme_index.get(object_uri)
        if scheme_uri is None:
            unresolved_ids.add(ext_id)

        groups[scheme_uri].append({
            "subject_id": to_curie(item_uri, prefix_map),
            "predicate_id": f"skos:{predicate}",
            "object_id": to_curie(object_uri, prefix_map),
            "mapping_justification": "semapv:UnspecifiedMatching",
            "subject_label": labels.get(qid, ""),
            "object_label": "",
            "mapping_source": statement_uri,
            "comment": "",
        })

    if unresolved_ids:
        print(f"[WARN][PACTOLS] {len(unresolved_ids)} ARK id(s) not found in the local dump "
              "index (folder missing, id genuinely absent from both dumps, or from a file that "
              "didn't classify) — written to unknown_target/wikidata_pactols.sssom.tsv. "
              "Example id(s): " + ", ".join(sorted(unresolved_ids)[:5]))

    scheme_uri_to_slug = {v: k for k, v in PACTOLS_SCHEME_URIS.items()}
    all_rows: list[dict] = []

    for scheme_uri, rows in groups.items():
        slug = scheme_uri_to_slug.get(scheme_uri)
        rel_dir = "" if slug else "unknown_target"
        out_dir = MAPPINGS_DIR / rel_dir if rel_dir else MAPPINGS_DIR
        filename = f"wikidata_{slug or 'pactols'}.sssom.tsv"

        meta = {
            "mapping_set_id": mapping_set_id(filename, rel_dir),
            "mapping_set_title": cfg["title"] + (f" ({slug})" if slug else " (scheme unresolved)"),
            "mapping_set_description": (
                f"Wikidata items carrying a {cfg['pid']} identifier, harvested via SPARQL "
                f"from {WDQS_ENDPOINT}"
                + (f", restricted to ids found in the local Pactols {slug} dump."
                   if slug else
                   ", for ids not found in either local Pactols dump (Lieux/Sujets) — "
                   "scheme could not be determined from PACTOLS_DIR.")
            ),
            "license": LICENSE,
            "creator_id": CREATOR_ID,
            "creator_label": CREATOR_LABEL,
            "mapping_provider": MAPPING_PROVIDER,
            "mapping_tool": "build_wikidata_mappings.py",
            "mapping_tool_id": GITHUB_TOOL_URL,
            "subject_source": WIKIDATA_SCHEME_URI,
            "object_source": scheme_uri,
            "comment": (
                "predicate_id defaults to skos:exactMatch (per project decision); overridden "
                "per-row only when the Wikidata statement carries a mapping relation type "
                "(P4390) qualifier. mapping_source is the specific Wikidata statement URI. "
                "Lieux/Sujets scheme determined by looking up each id against local Pactols "
                "RDF dumps (load_pactols_scheme_index(), PACTOLS_DIR) — not from Wikidata "
                "itself, since P4212 doesn't distinguish the two schemes."
            ),
        }
        out_path = out_dir / filename
        write_sssom_tsv(rows, out_path, meta, prefix_map)
        all_rows.extend(rows)

    return all_rows


# ---------------------------------------------------------------------------
# Track: GND (two-pass, restricted to archaeology-relevant entities)
# ---------------------------------------------------------------------------

def _parse_curie_map_header(path):
    """Read just the '#'-prefixed YAML header block of an SSSOM TSV and
    return its curie_map as a plain dict. Deliberately tolerant: any file
    written by our own write_sssom_tsv() (this script or the other two in
    the family) has the same shape, but we don't hard-depend on exact
    whitespace beyond what we ourselves emit."""
    prefix_map = {}
    in_curie_map = False
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.startswith("#"):
                break
            body = line[1:].rstrip("\n")
            if body.strip() == "curie_map:":
                in_curie_map = True
                continue
            if in_curie_map:
                m = re.match(r"^\s{2}(\S+):\s*(\S+)\s*$", body)
                if m:
                    prefix_map[m.group(1)] = m.group(2)
                    continue
                in_curie_map = False
    return prefix_map


def _expand_curie(value, prefix_map):
    if ":" not in value:
        return value
    prefix, _, local = value.partition(":")
    ns = prefix_map.get(prefix)
    return f"{ns}{local}" if ns else value


def scan_existing_mappings_for_qids_and_gnd(mappings_dir):
    """Parse every *.sssom.tsv under mappings_dir (recursively — this covers
    this script's own pass-1 output AND your existing ARIADNE + vocabulary-
    internal tsvs wherever they live under Mappings/), expand every
    subject_id/object_id CURIE using THAT FILE'S OWN curie_map, and collect
    every distinct Wikidata QID and GND id seen on either side."""
    qids, gnd_ids = set(), set()
    files = sorted(mappings_dir.rglob("*.sssom.tsv"))
    print(f"\n=== scanning {len(files)} existing SSSOM files for QIDs/GND ids ===")
    for path in files:
        prefix_map = _parse_curie_map_header(path)
        with path.open(encoding="utf-8") as f:
            body_lines = [line for line in f if not line.startswith("#")]
        if not body_lines:
            continue
        for row in csv.DictReader(body_lines, delimiter="\t"):
            for field in ("subject_id", "object_id"):
                val = row.get(field, "")
                if not val:
                    continue
                uri = _expand_curie(val, prefix_map)
                if uri.startswith(WD_ENTITY_NS):
                    qids.add(uri[len(WD_ENTITY_NS):])
                elif any(uri.startswith(ns) for ns in GND_NAMESPACES):
                    for ns in GND_NAMESPACES:
                        if uri.startswith(ns):
                            gnd_ids.add(uri[len(ns):])
                            break
    print(f"  found {len(qids)} distinct Wikidata QIDs, {len(gnd_ids)} distinct GND ids")
    return qids, gnd_ids


def harvest_gnd_forward(qids):
    """QID -> GND id, restricted to the given QID pool via VALUES."""
    rows = []
    for batch in _chunks(sorted(qids), VALUES_BATCH_SIZE):
        values = " ".join(f"wd:{q}" for q in batch)
        query = f"""
        PREFIX wd: <{WD_ENTITY_NS}>
        PREFIX p: <http://www.wikidata.org/prop/>
        PREFIX ps: <http://www.wikidata.org/prop/statement/>
        PREFIX pq: <http://www.wikidata.org/prop/qualifier/>
        SELECT ?item ?id ?statement ?mappingRelation WHERE {{
          VALUES ?item {{ {values} }}
          ?item p:{GND_PID} ?statement .
          ?statement ps:{GND_PID} ?id .
          OPTIONAL {{ ?statement pq:P4390 ?mappingRelation . }}
        }}
        """
        rows.extend(run_sparql_select(query))
        time.sleep(POLITE_DELAY)
    return rows


def harvest_gnd_reverse(gnd_ids):
    """Known GND id -> QID. P227 values are STRING literals, not URIs, so
    VALUES here binds against the literal string, unlike every other query
    in this script."""
    rows = []
    for batch in _chunks(sorted(gnd_ids), VALUES_BATCH_SIZE):
        values = " ".join(f'"{g}"' for g in batch)
        query = f"""
        PREFIX p: <http://www.wikidata.org/prop/>
        PREFIX ps: <http://www.wikidata.org/prop/statement/>
        PREFIX pq: <http://www.wikidata.org/prop/qualifier/>
        SELECT ?item ?id ?statement ?mappingRelation WHERE {{
          VALUES ?id {{ {values} }}
          ?item p:{GND_PID} ?statement .
          ?statement ps:{GND_PID} ?id .
          OPTIONAL {{ ?statement pq:P4390 ?mappingRelation . }}
        }}
        """
        rows.extend(run_sparql_select(query))
        time.sleep(POLITE_DELAY)
    return rows


def build_and_write_gnd_mapping():
    print("\n=== Wikidata to GND (two-pass, restricted to archaeology-relevant entities) ===")
    qids, gnd_ids = scan_existing_mappings_for_qids_and_gnd(MAPPINGS_DIR)
    if not qids and not gnd_ids:
        print("  nothing to anchor on yet — run the AAT/FISH/PACTOLS harvests "
              "(and/or the ARIADNE + vocabulary-internal scripts) first.")
        return []

    forward = harvest_gnd_forward(qids) if qids else []
    reverse = harvest_gnd_reverse(gnd_ids) if gnd_ids else []
    print(f"  forward (QID->GND): {len(forward)} statements")
    print(f"  reverse (GND->QID): {len(reverse)} statements")

    merged = {}
    for row in forward + reverse:
        qid = row["item"]["value"].rsplit("/", 1)[-1]
        gnd_id = row["id"]["value"]
        merged[(qid, gnd_id)] = row

    all_qids = {row["item"]["value"].rsplit("/", 1)[-1] for row in merged.values()}
    labels = fetch_labels(all_qids)

    prefix_map = dict(BASE_PREFIX_MAP)
    prefix_map[GND_PREFIX] = GND_NAMESPACE

    out_rows = []
    for (qid, gnd_id), row in merged.items():
        mapping_relation_qid = (row.get("mappingRelation", {}).get("value", "") or "").rsplit("/", 1)[-1] or None
        predicate = P4390_TO_SKOS.get(mapping_relation_qid, "exactMatch")
        out_rows.append({
            "subject_id": f"wd:{qid}",
            "predicate_id": f"skos:{predicate}",
            "object_id": f"{GND_PREFIX}:{gnd_id}",
            "mapping_justification": "semapv:UnspecifiedMatching",
            "subject_label": labels.get(qid, ""),
            "object_label": "",
            "mapping_source": row["statement"]["value"],
            "comment": "",
        })

    filename = "wikidata_gnd.sssom.tsv"
    meta = {
        "mapping_set_id": mapping_set_id(filename),
        "mapping_set_title": "Wikidata to GND (archaeology-relevant subset)",
        "mapping_set_description": (
            "Wikidata<->GND (P227) crosswalk, restricted to Wikidata items and GND "
            "records already known to be archaeology-relevant via the AAT/FISH/PACTOLS "
            "crosswalks and the ARIADNE + vocabulary-internal mapping sets — deliberately "
            "NOT a wholesale P227 harvest, which would pull in GND's entire non-"
            "archaeological scope. See scan_existing_mappings_for_qids_and_gnd()."
        ),
        "license": LICENSE,
        "creator_id": CREATOR_ID,
        "creator_label": CREATOR_LABEL,
        "mapping_provider": MAPPING_PROVIDER,
        "mapping_tool": "build_wikidata_mappings.py",
        "mapping_tool_id": GITHUB_TOOL_URL,
        "subject_source": WIKIDATA_SCHEME_URI,
        "object_source": GND_SCHEME_URI,
        "comment": (
            "predicate_id defaults to skos:exactMatch; overridden per-row when the "
            "Wikidata statement carries a mapping relation type (P4390) qualifier."
        ),
    }
    write_sssom_tsv(out_rows, MAPPINGS_DIR / filename, meta, prefix_map)
    return out_rows


def main():
    MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)

    if "--gnd-only" in sys.argv:
        build_and_write_gnd_mapping()
        return

    total = 0
    for name, cfg in PROPERTY_CONFIG.items():
        if name == "pactols":
            rows = build_and_write_pactols_mapping(cfg)
        else:
            rows = build_and_write_property_mapping(name, cfg)
        total += len(rows)
        time.sleep(POLITE_DELAY)

    build_and_write_gnd_mapping()

    print(f"\nDone. {total} AAT/FISH/PACTOLS mappings written, plus GND, to {MAPPINGS_DIR}")


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# HOUSEKEEPING NOTE (not code): this is now the third script with its own
# copy of to_curie()/write_sssom_tsv(). Worth pulling these + the SSSOM
# metadata-slot constants into one shared module (e.g. sssom_common.py) that
# all three scripts import, once this one is confirmed working — would also
# be the natural place to fix the mapping_tool_id/subject_source/object_source
# CURIE-vs-bare-URI issue you hit converting ads_aat.sssom.tsv, in one spot
# rather than three.
# ---------------------------------------------------------------------------