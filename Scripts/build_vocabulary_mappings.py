"""
Extract SKOS mapping assertions from local vocabulary dumps and write SSSOM TSVs.

Configured folders:
  DAI
  Dariah Vocabs
  FISH
  Pactols
  Wortnetz

The script:
  * recursively finds RDF/XML (.rdf/.xml/.owl) and Turtle (.ttl) files
  * loads them with rdflib
  * extracts SKOS exact/close/broad/narrow/related matches
  * gets subject/object prefLabels when available
  * resolves each match's subject/object concept scheme (explicit
    skos:inScheme > safe single-scheme file default > curated
    SCHEME_REGISTRY guess from the concept URI > unknown)
  * groups mappings GLOBALLY by (subject scheme, object scheme) — across
    ALL folders and files, not per folder/file — and writes one SSSOM TSV
    per group
  * records UnspecifiedMatching because the original mapping process is unknown

Filenames and scheme direction:
  Output files are named "<subjectSchemeSlug>_<objectSchemeSlug>.sssom.tsv"
  (slugs come from SCHEME_REGISTRY, consistent with build_aat_mappings.py's
  ARIADNE->AAT filenames: ads_eh_tbm, ads_eh_com, ads_mda_obj, ads_eh_tmc,
  ads_eh_tmt2, dai, pactols_lieux, pactols_sujets). There is no folder name
  in the filename anymore: a folder's own vocabulary can legitimately show
  up as either the subject OR the object of a given dump's assertions (some
  matches are stored dumpedVocab->target, some the other way round), and
  since SCHEME_REGISTRY resolves provider/label/slug by the scheme itself —
  not by which folder produced the row — a reversed-direction batch of
  mappings gets its own correctly-named, correctly-attributed file (e.g.
  "aat_dai.sssom.tsv" alongside the usual "dai_aat.sssom.tsv") instead of
  being mislabeled as if it were the DAI folder's own vocabulary again.
  A concept scheme with no SCHEME_REGISTRY entry yet (e.g. an
  as-of-yet-uncurated Dariah/Wortnetz sub-vocabulary) still works, just
  falls back to a slugified full URI until you add it to the registry.

Important provenance distinction:
  * creator_id = Lasse Mempel-Länger, because you created this SSSOM representation
  * mapping_provider = optional manual enrichment; omitted when the origin of
    the mapping assertion is unknown
  * publication_date = date this generated SSSOM artifact is created
  * mapping_date is omitted unless a reliable assertion date exists in the source
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
import re
from collections import defaultdict
from pathlib import Path

# rdflib may encounter malformed typed literals in source dumps (e.g.
# duplicated timezone offsets). This extraction only needs graph structure,
# so suppress rdflib's lexical conversion traceback noise.
logging.getLogger("rdflib.term").setLevel(logging.ERROR)
logging.getLogger("rdflib").setLevel(logging.ERROR)

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, SKOS


BASE = Path("/home/mempellaenger/repos/thesaurusscience")
OUTPUT_DIR = BASE / "Mappings"

CREATOR_ID = "orcid:0009-0001-5183-1635"
CREATOR_LABEL = "Lasse Mempel-Länger"
PUBLICATION_DATE = dt.date.today().isoformat()
LICENSE = "https://creativecommons.org/licenses/by/4.0/"

GITHUB_TOOL_URL = (
    "https://github.com/LasseMempel/thesaurusscience/"
    "blob/main/Scripts/build_vocabulary_mappings.py"
)

# Folder scan config. Now that output grouping/naming is scheme-based (see
# SCHEME_REGISTRY below) rather than folder-based, these entries mainly
# drive (a) which folder to scan and with which default concept-id CURIE
# prefix, and (b) the FALLBACK label/provider used only for a concept scheme
# that ISN'T yet in SCHEME_REGISTRY (e.g. an uncurated Dariah/Wortnetz
# sub-vocabulary) — see resolve_scheme_meta().
VOCABULARIES = {
    "DAI": {
        "path": BASE / "DAI",
        "prefix": "dai",
        "source_namespace": "http://thesauri.dainst.org/",
        "mapping_provider": None,
        "label": "German Archaeological Institute (DAI)",
    },
    "Dariah Vocabs": {
        "path": BASE / "Dariah Vocabs",
        "prefix": "dariah",
        "source_namespace": None,
        "mapping_provider": None,
        "label": "DARIAH-DE vocabularies",
    },
    "FISH": {
        "path": BASE / "FISH",
        "prefix": "fish",
        "source_namespace": "http://purl.org/heritagedata/",
        "mapping_provider": None,  # verify responsible institution
        "label": "FISH",
    },
    "Pactols": {
        "path": BASE / "Pactols",
        "prefix": "pactols",
        "source_namespace": "https://ark.frantiq.fr/ark:/26678/",
        "mapping_provider": "https://ror.org/04andmq85",
        "label": "PACTOLS",
    },
    "Wortnetz": {
        "path": BASE / "Wortnetz",
        "prefix": "wortnetz",
        "source_namespace": None,
        "mapping_provider": None,  # verify exact responsible body / ROR
        "label": "Wortnetz",
    },
}

# Manual fallback enrichment for a concept scheme NOT (yet) in
# SCHEME_REGISTRY, keyed by folder name then scheme URI. Once a scheme is
# curated into SCHEME_REGISTRY it takes priority over this and folder
# scoping stops mattering (see resolve_scheme_meta()). Still the right place
# for e.g. Dariah Vocabs' several sub-vocabularies until each gets its own
# SCHEME_REGISTRY entry.
#
# Example:
# CONCEPT_SCHEME_OVERRIDES = {
#     "Dariah Vocabs": {
#         "https://example.org/scheme/foo": {
#             "mapping_provider": "https://ror.org/....",
#             "label": "Foo Vocabulary",
#         },
#     },
# }
CONCEPT_SCHEME_OVERRIDES: dict[str, dict[str, dict[str, str]]] = {}

# Optional manual provider enrichment, keyed by folder then a relative
# source-file path ("__default__" for folder-wide). This is file-specific
# and therefore orthogonal to SCHEME_REGISTRY/CONCEPT_SCHEME_OVERRIDES: it
# wins over both when set, since a specific file is the most specific thing
# you can override. Also orthogonal to scheme-based GLOBAL grouping — if two
# files with different overrides end up in the very same (subject_scheme,
# object_scheme) group, write_sssom() picks one and prints a warning.
#
# Example:
# MAPPING_PROVIDER_OVERRIDES = {
#     "FISH": {
#         "__default__": "https://ror.org/....",
#     },
#     "Dariah Vocabs": {
#         "some/file.ttl": "https://ror.org/....",
#     },
# }
MAPPING_PROVIDER_OVERRIDES: dict[str, dict[str, str]] = {}

MAPPING_PREDICATES = {
    SKOS.exactMatch: "exactMatch",
    SKOS.closeMatch: "closeMatch",
    SKOS.broadMatch: "broadMatch",
    SKOS.narrowMatch: "narrowMatch",
    SKOS.relatedMatch: "relatedMatch",
}

RDF_EXTENSIONS = {".rdf", ".xml", ".owl", ".ttl"}

# ---------------------------------------------------------------------------
# SCHEME_REGISTRY: single source of truth for every concept scheme this
# script (and build_aat_mappings.py) knows about — ours (DAI, the five FISH
# schemes, the two Pactols schemes) and external targets (AAT, Wikidata,
# GND). Slugs match build_aat_mappings.py's ARIADNE->AAT filenames 1:1
# (ads_eh_tbm, ads_eh_com, ads_mda_obj, ads_eh_tmc, ads_eh_tmt2, dai,
# pactols_lieux, pactols_sujets) so files from both scripts read the same way.
#
# Resolution here is keyed by scheme_uri, never by which folder produced a
# row — that's what makes reversed-direction mappings (a dump asserting
# externalConcept -> ourConcept instead of the usual ourConcept -> external)
# come out with the right filename, label and provider automatically: the
# scheme on each side is looked up on its own merits.
#
# Fields per entry:
#   scheme_uri:     the conceptScheme identifier for subject_source/
#                   object_source
#   label:          human-readable name (mapping_set_title, description)
#   provider:       mapping_provider URI (ROR etc.), or None if unknown
#   concept_root:   namespace root — if a concept URI startswith this, it
#                   belongs to this scheme. Omit when concept URIs don't
#                   reveal scheme membership at all (Pactols).
#   concept_marker: substring anywhere in a concept URI implying this scheme
#                   (used for FISH, whose scheme lives in the path but isn't
#                   a clean startswith prefix); tried after concept_root.
#
# Extend this directly as you curate Mappings/unknown_target/
# _namespace_roots_to_curate.tsv, or add entries for Dariah/Wortnetz
# sub-vocabularies once you know their scheme URIs — anything not in here
# still works via the folder-level CONCEPT_SCHEME_OVERRIDES/VOCABULARIES
# fallback, just without a pretty filename slug until you add it here.
#
# Wikidata/GND aren't natively skos:Concept/skos:ConceptScheme at all, so
# their scheme_uri below is each vocabulary's BARTOC registration instead —
# the identifier coli-conc/Cocoda itself uses as fromScheme/toScheme for
# them. Double-check these two against how your Cocoda instance actually
# has them registered before publishing.
SCHEME_REGISTRY: dict[str, dict[str, str | None]] = {
    "aat": {
        "scheme_uri": "http://vocab.getty.edu/aat/",
        "label": "Getty Art & Architecture Thesaurus (AAT)",
        "provider": None,
        "concept_root": "http://vocab.getty.edu/aat/",
    },
    "wikidata": {
        "scheme_uri": "http://bartoc.org/en/node/1940",
        "label": "Wikidata",
        "provider": None,
        "concept_root": "http://www.wikidata.org/entity/",
    },
    "gnd": {
        "scheme_uri": "http://bartoc.org/en/node/430",
        "label": "Gemeinsame Normdatei (GND)",
        "provider": None,
        # KNOWN GAP: some older/third-party dumps use "http://d-nb.info/gnd/"
        # (no "s"). A single registry entry -> single concept_root, so an
        # http:// GND URI won't match this and will fall through to unknown/
        # unresolved (logged in _namespace_roots_to_curate.tsv) rather than
        # silently mis-grouping. Normalize those URIs to https:// during
        # extraction if that shows up a lot, rather than adding a second
        # "gnd" entry (one CURIE prefix can only carry one namespace).
        "concept_root": "https://d-nb.info/gnd/",
    },
    "dai": {
        "scheme_uri": "http://thesauri.dainst.org/scheme",
        "label": "German Archaeological Institute (DAI)",
        "provider": None,
        "concept_root": "http://thesauri.dainst.org/",
    },
    "ads_eh_tbm": {
        "scheme_uri": "http://purl.org/heritagedata/schemes/eh_tbm",
        "label": "ADS Building Materials",
        "provider": None,
        "concept_marker": "eh_tbm",
    },
    "ads_eh_com": {
        "scheme_uri": "http://purl.org/heritagedata/schemes/eh_com",
        "label": "ADS Components",
        "provider": None,
        "concept_marker": "eh_com",
    },
    "ads_mda_obj": {
        "scheme_uri": "http://purl.org/heritagedata/schemes/mda_obj",
        "label": "ADS FISH Objects",
        "provider": None,
        "concept_marker": "mda_obj",
    },
    "ads_eh_tmc": {
        "scheme_uri": "http://purl.org/heritagedata/schemes/eh_tmc",
        "label": "ADS Maritime Craft",
        "provider": None,
        "concept_marker": "eh_tmc",
    },
    "ads_eh_tmt2": {
        "scheme_uri": "http://purl.org/heritagedata/schemes/eh_tmt2",
        "label": "ADS Monuments",
        "provider": None,
        "concept_marker": "eh_tmt2",
    },
    "ads_eh_period": {
        "scheme_uri": "http://purl.org/heritagedata/schemes/eh_period",
        "label": "Historic England Periods",
        "provider": None,
        "concept_marker": "eh_period",
    },
    "ads_hes_scapa": {
        "scheme_uri": "http://purl.org/heritagedata/schemes/scapa",
        "label": "Scottish Archaeological Periods",
        "provider": None,
        "concept_marker": "scapa",
    },
    "pactols_lieux": {
        "scheme_uri": "https://ark.frantiq.fr/ark:/26678/th17",
        "label": "PACTOLS Lieux",
        "provider": "https://ror.org/04andmq85",
        # No concept_root/concept_marker: Pactols concept URIs don't reveal
        # which sub-scheme they belong to. See resolve_pactols_scheme_from_file().
    },
    "pactols_sujets": {
        "scheme_uri": "https://ark.frantiq.fr/ark:/26678/TH_1",
        "label": "PACTOLS Sujets",
        "provider": "https://ror.org/04andmq85",
    },
    "backbone": {
        "scheme_uri": "https://vocabs.dariah.eu/bbt/ConceptScheme/Backbone_Thesaurus",
        "label": "Backbone Thesaurus",
        "provider": None,
        "concept_root": "https://vocabs.dariah.eu/bbt/Concept/",
    },
    "wnk": {
        "scheme_uri": "http://lvr.vocnet.org/wnk",
        "label": "Wortnetz Kultur",
        "provider": None,
        "concept_root": "http://lvr.vocnet.org/wnk/",
    },
    "defc": {
        "scheme_uri": "https://vocabs.acdh.oeaw.ac.at/defcthesaurus/DefcSchema",
        "label": "DEFC Thesaurus",
        "provider": None,
        "concept_root": "https://vocabs.acdh.oeaw.ac.at/defcthesaurus/",
    },
    "OeAI_ctp": {
        "scheme_uri": "https://vocabs.acdh.oeaw.ac.at/oeai-cultural-periods/",
        "label": "OeAI Thesaurus - Cultural Time Periods",
        "provider": None,
        "concept_root": "https://vocabs.acdh.oeaw.ac.at/oeai-cultural-periods/",
    },
    "OeAI_m": {
        "scheme_uri": "https://vocabs.acdh.oeaw.ac.at/oeai-materials/",
        "label": "OeAI Thesaurus - Materials",
        "provider": None,
        "concept_root": "https://vocabs.acdh.oeaw.ac.at/oeai-materials/",
    },
    "gemet": {
        "scheme_uri": "http://www.eionet.europa.eu/gemet/gemetThesaurus",
        "label": "GEMET, the GEneral Multilingual Environmental Thesaurus",
        "provider": None,
        "concept_root": "http://www.eionet.europa.eu/gemet/concept/"
    },
    "dyas": {
        "scheme_uri": "https://humanitiesthesaurus.academyofathens.gr",
        "label": "DYAS Humanities Thesaurus",
        "provider": None,
        "concept_root": "https://humanitiesthesaurus.academyofathens.gr/dyas-resource/Concept/"
    },
}

# Registered globally so subject_source/object_source values that are
# themselves BARTOC scheme URIs (Wikidata/GND above) compact into readable
# CURIEs too, e.g. "bartoc:en/node/1940" instead of the bare URI.
BARTOC_PREFIX = {"bartoc": "http://bartoc.org/"}

# Tracks, at scan time, which folder a given scheme_uri's OWN concepts were
# actually found in (via explicit inScheme or a safe file-level default) —
# used as a fallback hint for resolve_scheme_meta() when a scheme isn't in
# SCHEME_REGISTRY yet. Not consulted at all for registered schemes, so it
# never causes the direction-mislabeling problem SCHEME_REGISTRY fixes.
SCHEME_ORIGIN_FOLDER: dict[str, str] = {}

# Cross-run tally of namespace roots seen on a subject/object URI that was
# STILL unresolved after every fallback below — candidates for you to
# curate into SCHEME_REGISTRY next. Written out once at the end of main()
# (Mappings/unknown_target/_namespace_roots_to_curate.tsv).
UNRESOLVED_NAMESPACE_COUNTS: dict[str, int] = defaultdict(int)


def resolve_scheme_via_namespace(uri: str) -> str | None:
    """Resolve `uri`'s concept scheme from SCHEME_REGISTRY: longest-prefix
    match against concept_root entries first (same algorithm as to_curie()),
    then substring match against concept_marker entries (for FISH-style
    URIs where the scheme isn't a clean prefix). Returns None if nothing
    registered matches."""
    best_root = ""
    best_scheme = None
    for entry in SCHEME_REGISTRY.values():
        root = entry.get("concept_root")
        if root and uri.startswith(root) and len(root) > len(best_root):
            best_root = root
            best_scheme = entry["scheme_uri"]
    if best_scheme:
        return best_scheme

    for entry in SCHEME_REGISTRY.values():
        marker = entry.get("concept_marker")
        if marker and marker in uri:
            return entry["scheme_uri"]

    return None


def resolve_pactols_scheme_from_file(source_file_name: str) -> str | None:
    """Pactols concept URIs don't reveal Lieux vs Sujets themselves, so this
    classifies by which dump file we're currently parsing (e.g.
    'Pactols_Lieux_th17_*.rdf' / 'Pactols_Sujets_TH_1_*.rdf') — same
    convention as build_aat_mappings.py. ASSUMPTION based on that naming
    convention; adjust the patterns below if your real filenames differ.
    Only resolves concepts that are actually described within a Pactols-
    folder file being scanned; a Pactols URI appearing only as a match
    target inside another folder's dump won't be caught by this."""
    name = source_file_name.lower()
    if "lieux" in name or "th17" in name:
        return SCHEME_REGISTRY["pactols_lieux"]["scheme_uri"]
    if "sujets" in name or "th_1" in name or "th1" in name:
        return SCHEME_REGISTRY["pactols_sujets"]["scheme_uri"]
    return None


def scheme_uri_label(scheme_uri: str | None) -> str | None:
    """SCHEME_REGISTRY slug for a resolved scheme_uri (used for filenames),
    or None if it isn't registered yet — callers fall back to slugifying
    the raw URI in that case."""
    for slug, entry in SCHEME_REGISTRY.items():
        if entry["scheme_uri"] == scheme_uri:
            return slug
    return None


def resolve_scheme_meta(scheme_uri: str | None, folder_name: str | None = None) -> tuple[str | None, str | None]:
    """Returns (provider, label) for scheme_uri. SCHEME_REGISTRY is checked
    first and wins regardless of folder — this is what makes provider/label
    correct even when a scheme turns up as the OBJECT of a mapping instead
    of the (usual) subject. Falls back to the folder-scoped
    CONCEPT_SCHEME_OVERRIDES/VOCABULARIES default for schemes not yet
    registered, using folder_name as a hint (pass SCHEME_ORIGIN_FOLDER.get(
    scheme_uri), i.e. only meaningful when that folder is actually where
    this scheme's own concepts were found)."""
    for entry in SCHEME_REGISTRY.values():
        if entry["scheme_uri"] == scheme_uri:
            return entry.get("provider"), entry["label"]

    if folder_name and folder_name in VOCABULARIES:
        config = VOCABULARIES[folder_name]
        override = CONCEPT_SCHEME_OVERRIDES.get(folder_name, {}).get(scheme_uri or "", {})
        provider = override.get("mapping_provider", config.get("mapping_provider"))
        label = override.get("label", config.get("label", folder_name))
        return provider, label

    return None, None


def file_provider_override(folder_name: str, source_file: Path) -> str | None:
    """File-specific mapping_provider override (MAPPING_PROVIDER_OVERRIDES),
    if any — the most specific override available, applied on top of
    whatever resolve_scheme_meta() would otherwise return."""
    config = VOCABULARIES[folder_name]
    file_overrides = MAPPING_PROVIDER_OVERRIDES.get(folder_name, {})
    try:
        rel = str(source_file.relative_to(config["path"]))
    except ValueError:
        rel = str(source_file)
    return file_overrides.get(rel) or file_overrides.get("__default__")


def slugify(value: str, max_len: int = 90) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_").lower()
    return (value or "mapping_set")[:max_len]


def to_curie(iri: str, prefix_map: dict[str, str]) -> str:
    """Longest-prefix CURIE compaction; leaves unknown IRIs untouched."""
    best_prefix = None
    best_namespace = ""
    for prefix, namespace in prefix_map.items():
        if namespace and iri.startswith(namespace) and len(namespace) > len(best_namespace):
            best_prefix = prefix
            best_namespace = namespace

    if best_prefix is None:
        return iri
    return f"{best_prefix}:{iri[len(best_namespace):]}"


def find_rdf_files(folder: Path) -> list[Path]:
    return sorted(
        p for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in RDF_EXTENSIONS
    )


def parse_file(path: Path) -> Graph:
    graph = Graph()
    fmt = "turtle" if path.suffix.lower() == ".ttl" else "xml"
    graph.parse(path, format=fmt)
    return graph


def best_label(graph: Graph, uri: URIRef) -> str:
    """Prefer prefLabel, then altLabel, then rdfs:label."""
    candidates: list[tuple[int, str]] = []

    predicates = (
        (SKOS.prefLabel, 0),
        (SKOS.altLabel, 1),
        (URIRef("http://www.w3.org/2000/01/rdf-schema#label"), 2),
    )

    for predicate, base_rank in predicates:
        for value in graph.objects(uri, predicate):
            if not isinstance(value, Literal):
                continue
            lang = (value.language or "").lower()
            lang_rank = {"en": 0, "de": 1, "fr": 2, "": 3}.get(lang, 4)
            candidates.append((base_rank * 10 + lang_rank, str(value)))

    if not candidates:
        return ""

    candidates.sort(key=lambda x: (x[0], x[1].casefold()))
    return candidates[0][1]


def infer_scheme(graph: Graph, uri: URIRef) -> str | None:
    schemes = sorted(str(o) for o in graph.objects(uri, SKOS.inScheme))
    if schemes:
        return schemes[0]

    graph_schemes = sorted(
        str(s) for s in graph.subjects(RDF.type, SKOS.ConceptScheme)
    )
    if len(graph_schemes) == 1:
        return graph_schemes[0]

    return None


def get_scheme_version(graph: Graph, scheme_uri: str | None) -> str | None:
    if not scheme_uri:
        return None

    scheme = URIRef(scheme_uri)
    predicates = (
        URIRef("http://purl.org/dc/terms/hasVersion"),
        URIRef("http://purl.org/dc/terms/issued"),
        URIRef("http://purl.org/dc/terms/modified"),
        URIRef("http://www.w3.org/2002/07/owl#versionIRI"),
        URIRef("http://www.w3.org/2002/07/owl#versionInfo"),
    )

    values = []
    for predicate in predicates:
        values.extend(str(o) for o in graph.objects(scheme, predicate))

    return sorted(set(values))[0] if values else None


def analyze_declared_schemes(graph: Graph, mapping_subjects: set[URIRef]) -> tuple[str | None, bool, list[str]]:
    """
    Decide whether a file-level ConceptScheme is safe to use as the default
    source scheme.

    Rules:
      * exactly one skos:ConceptScheme must exist; and
      * every mapping subject must either have inScheme = that scheme or no
        conflicting inScheme information.

    If any subject explicitly belongs to another scheme, the file-level scheme
    is NOT used as the default. Explicit inScheme always wins later.
    """
    schemes = sorted(str(s) for s in graph.subjects(RDF.type, SKOS.ConceptScheme))
    print(f"[SCHEME CHECK] ConceptSchemes declared in file: {len(schemes)}")
    for scheme in schemes:
        print(f"    - {scheme}")

    if len(schemes) != 1:
        print("[SCHEME CHECK] Result: cannot use a file-level default source scheme")
        return None, False, schemes

    candidate = schemes[0]
    conflicts = []
    for subject in mapping_subjects:
        explicit = {str(o) for o in graph.objects(subject, SKOS.inScheme)}
        if explicit and candidate not in explicit:
            conflicts.append((str(subject), sorted(explicit)))

    if conflicts:
        print("[SCHEME CHECK] Result: REJECTED as file-level default; explicit inScheme conflicts found")
        for subject, explicit in conflicts[:20]:
            print(f"    [ERROR][SCHEME COLLISION] {subject} -> {explicit}; overrides file-level scheme {candidate}")
        if len(conflicts) > 20:
            print(f"    ... {len(conflicts) - 20} more conflicts")
        return None, False, schemes

    print(f"[SCHEME CHECK] Result: ACCEPTED; all mapping subjects are compatible with {candidate}")
    return candidate, True, schemes


def query_mappings(graph: Graph):
    query = """
    SELECT DISTINCT
        ?subject ?predicate ?object
        ?subjectScheme ?objectScheme
    WHERE {
        VALUES ?predicate {
            skos:exactMatch
            skos:closeMatch
            skos:broadMatch
            skos:narrowMatch
            skos:relatedMatch
        }

        ?subject ?predicate ?object .

        OPTIONAL { ?subject skos:inScheme ?subjectScheme . }
        OPTIONAL { ?object skos:inScheme ?objectScheme . }
    }
    """

    return graph.query(query, initNs={"skos": str(SKOS._NS)})


def mapping_set_filename(subject_scheme: str | None, object_scheme: str | None) -> str:
    source_part = slugify(scheme_uri_label(subject_scheme) or subject_scheme or "unknown_source_scheme")
    target_part = slugify(scheme_uri_label(object_scheme) or object_scheme or "unknown_target_scheme")
    return f"{source_part}_{target_part}.sssom.tsv"


def mapping_set_id(filename: str, rel_dir: str = "") -> str:
    # The canonical mapping-set identifier is the TSV URL, not the later RDF
    # serialization. This follows the SSSOM model/examples. rel_dir must
    # match write_sssom's actual output path (e.g. "unknown_target") so the
    # published URL isn't a dead link.
    sub = f"{rel_dir}/" if rel_dir else ""
    return (
        "https://raw.githubusercontent.com/LasseMempel/thesaurusscience/"
        f"main/Mappings/{sub}{filename}"
    )


def namespace_candidate(uri: str) -> str:
    """
    Heuristic namespace candidate for REPORTING ONLY.

    This intentionally never becomes subject_source/object_source automatically
    beyond the curated SCHEME_REGISTRY fallback. It is used to show which URI
    stems occur in the corpus for later scheme harvesting/curation.
    """
    if "#" in uri:
        base = uri.rsplit("#", 1)[0] + "#"
        return base
    if "/" in uri:
        return uri.rsplit("/", 1)[0] + "/"
    return uri


def build_global_prefix_map() -> dict[str, str]:
    """One prefix map for the whole run — output grouping/naming no longer
    varies per folder, so there's no reason for the curie_map to either."""
    prefix_map = {
        "skos": str(SKOS._NS),
        "semapv": "https://w3id.org/semapv/vocab/",
        "orcid": "https://orcid.org/",
        "github": "https://github.com/",
        **BARTOC_PREFIX,
    }
    # SCHEME_REGISTRY entries with a concept_root compact subject_id/
    # object_id for that scheme's own concepts (e.g. "wikidata:Q123").
    for slug, entry in SCHEME_REGISTRY.items():
        root = entry.get("concept_root")
        if root:
            prefix_map[slug] = root
    # Each folder's own default concept namespace, for vocabularies (or
    # portions of them) not yet broken out with their own SCHEME_REGISTRY
    # entry (Dariah, Wortnetz, generic FISH/Pactols concept ids).
    for config in VOCABULARIES.values():
        namespace = config.get("source_namespace")
        if namespace:
            prefix_map.setdefault(config["prefix"], namespace)
    return prefix_map


def write_sssom(
    subject_scheme: str | None,
    object_scheme: str | None,
    rows: list[dict],
    contributors: list[dict],
    prefix_map: dict[str, str],
) -> Path:
    # Cocoda-facing routing: a resolved object_scheme (explicit inScheme, or
    # a SCHEME_REGISTRY guess) keeps the file in OUTPUT_DIR as before. An
    # unresolved one goes into unknown_target/ instead of cluttering the
    # main folder.
    rel_dir = "" if object_scheme else "unknown_target"
    out_dir = OUTPUT_DIR / rel_dir if rel_dir else OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    filename = mapping_set_filename(subject_scheme, object_scheme)
    output = out_dir / filename
    set_id = mapping_set_id(filename, rel_dir)

    subject_provider, subject_label = resolve_scheme_meta(
        subject_scheme, folder_name=SCHEME_ORIGIN_FOLDER.get(subject_scheme or "")
    )
    _, object_label = resolve_scheme_meta(
        object_scheme, folder_name=SCHEME_ORIGIN_FOLDER.get(object_scheme or "")
    )

    # A row-level MAPPING_PROVIDER_OVERRIDES hit (file-specific) wins over
    # the scheme-level provider above, since it's the most specific setting
    # available. If rows in this group came from files with different
    # overrides, take the first (sorted, for determinism) and warn — this
    # can only happen if two files with different overrides both resolve to
    # the same (subject_scheme, object_scheme) pair.
    row_overrides = {r["provider_override"] for r in rows if r.get("provider_override")}
    provider = subject_provider
    if row_overrides:
        provider = sorted(row_overrides)[0]
        if len(row_overrides) > 1:
            print(f"[WARN] {filename}: {len(row_overrides)} conflicting file-specific "
                  f"provider overrides in this group, using {provider}")
    if not provider:
        print(f"[WARN] {filename}: no mapping_provider configured for "
              f"subject scheme {subject_scheme or '[unknown]'}")

    description = (
        "SKOS mappings extracted from local RDF dump(s) of "
        f"{subject_label or subject_scheme or '[unknown source]'}"
    )
    if subject_scheme:
        description += f" (concept scheme: {subject_scheme})"
    if object_scheme:
        description += f", targeting {object_label or object_scheme} (concept scheme: {object_scheme})"
    description += (
        ". The original procedure by which the mappings were established is "
        "unknown; mapping_justification is therefore "
        "semapv:UnspecifiedMatching."
    )

    mapping_set_title = (
        f"{subject_label or subject_scheme or 'Unknown source'} \u2192 "
        f"{object_label or object_scheme or 'unknown target'} SKOS mappings"
    )

    subject_source = subject_scheme
    object_source = object_scheme

    with output.open("w", encoding="utf-8", newline="") as fh:
        fh.write("#curie_map:\n")
        for prefix, namespace in prefix_map.items():
            fh.write(f"#  {prefix}: {namespace}\n")

        fh.write(f"#mapping_set_id: {set_id}\n")
        fh.write(f"#mapping_set_title: {mapping_set_title}\n")
        fh.write(f"#mapping_set_description: {description}\n")
        fh.write(f"#license: {LICENSE}\n")
        fh.write(f"#publication_date: {PUBLICATION_DATE}\n")
        fh.write(f"#creator_id: {CREATOR_ID}\n")
        fh.write(f"#creator_label: {CREATOR_LABEL}\n")

        if provider:
            fh.write(f"#mapping_provider: {provider}\n")

        fh.write("#mapping_tool: extract_skos_mappings_to_sssom.py\n")
        fh.write(f"#mapping_tool_id: {to_curie(GITHUB_TOOL_URL, prefix_map)}\n")

        if subject_source:
            fh.write(f"#subject_source: {to_curie(subject_source, prefix_map)}\n")
            version = None
            for c in contributors:
                version = get_scheme_version(c["graph"], subject_source)
                if version:
                    break
            if version:
                fh.write(f"#subject_source_version: {version}\n")

        if object_source:
            fh.write(f"#object_source: {to_curie(object_source, prefix_map)}\n")
            version = None
            for c in contributors:
                version = get_scheme_version(c["graph"], object_source)
                if version:
                    break
            if version:
                fh.write(f"#object_source_version: {version}\n")

        listed = [
            f"{c['folder']}/{c['source_file'].relative_to(VOCABULARIES[c['folder']]['path'])}"
            for c in contributors
        ]
        if len(listed) > 5:
            listed_str = ", ".join(listed[:5]) + f", and {len(listed) - 5} more"
        else:
            listed_str = ", ".join(listed)

        fh.write(
            "#comment: Extracted from local RDF file(s) "
            f"{listed_str}; source graph(s) contain SKOS mapping "
            "assertions without information about the original mapping process "
            "or a mapping-specific source label.\n"
        )

        fields = [
            "subject_id",
            "subject_label",
            "predicate_id",
            "object_id",
            "object_label",
            "mapping_justification",
            "comment",
        ]
        writer = csv.DictWriter(
            fh,
            fieldnames=fields,
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()

        for row in sorted(
            rows,
            key=lambda r: (
                r["subject_id"],
                r["predicate_id"],
                r["object_id"],
            ),
        ):
            # Internal extraction fields (subject_uri, object_uri,
            # provider_override) are deliberately not part of the SSSOM
            # mapping table. Select only the declared TSV columns so
            # csv.DictWriter cannot leak implementation details into the output.
            writer.writerow({field: row.get(field, "") for field in fields})

    return output


def process_folder(
    folder_name: str,
    config: dict,
    grouped: dict[tuple[str, str], list[dict]],
    contributors: dict[tuple[str, str], dict[tuple[str, str], dict]],
    prefix_map: dict[str, str],
) -> dict:
    folder = config["path"]
    files = find_rdf_files(folder)

    print(f"\n=== {folder_name} ===")
    print(f"RDF/XML + Turtle files: {len(files)}")

    report = {
        "files": 0,
        "files_with_mappings": 0,
        "labels_missing_subject": 0,
        "labels_missing_object": 0,
        "subjects_without_inScheme": 0,
        "subjects_with_explicit_inScheme": 0,
        "file_default_scheme_used": 0,
        "subject_scheme_from_namespace": 0,
        "object_scheme_from_namespace": 0,
        "unknown_source_scheme": 0,
        "unknown_target_scheme": 0,
        "namespaces": defaultdict(int),
    }

    for source_file in files:
        report["files"] += 1
        try:
            graph = parse_file(source_file)
        except Exception as exc:
            print(f"[WARN] Could not parse {source_file}: {exc}")
            continue

        results = list(query_mappings(graph))
        if not results:
            continue

        report["files_with_mappings"] += 1
        print(
            f"[LOAD] {source_file.relative_to(folder)}: "
            f"{len(graph)} triples, {len(results)} SKOS mappings"
        )

        # Build mapping subject set first, because the single-ConceptScheme
        # safety check has to inspect every mapping subject in the file.
        subjects = {
            r[0] for r in results
            if isinstance(r[0], URIRef)
        }
        file_default_scheme, default_ok, declared_schemes = analyze_declared_schemes(
            graph, subjects
        )

        row_provider_override = file_provider_override(folder_name, source_file)

        for subject, predicate, obj, _, _ in results:
            if not isinstance(subject, URIRef) or not isinstance(obj, URIRef):
                continue

            explicit_subject_schemes = sorted(
                str(o) for o in graph.objects(subject, SKOS.inScheme)
            )

            if explicit_subject_schemes:
                report["subjects_with_explicit_inScheme"] += 1
                if file_default_scheme and any(s != file_default_scheme for s in explicit_subject_schemes):
                    print(
                        f"[ERROR][SCHEME COLLISION] {subject}: explicit inScheme "
                        f"{explicit_subject_schemes} overrides file-level candidate "
                        f"{file_default_scheme}"
                    )
                subject_scheme = explicit_subject_schemes[0]
                SCHEME_ORIGIN_FOLDER.setdefault(subject_scheme, folder_name)
            elif default_ok:
                report["file_default_scheme_used"] += 1
                subject_scheme = file_default_scheme
                if subject_scheme:
                    SCHEME_ORIGIN_FOLDER.setdefault(subject_scheme, folder_name)
            else:
                report["subjects_without_inScheme"] += 1
                subject_scheme = None

            explicit_object_schemes = sorted(
                str(o) for o in graph.objects(obj, SKOS.inScheme)
            )
            object_scheme = explicit_object_schemes[0] if explicit_object_schemes else None
            if object_scheme:
                SCHEME_ORIGIN_FOLDER.setdefault(object_scheme, folder_name)

            # Fallback for schemes curated into SCHEME_REGISTRY (AAT/
            # Wikidata/GND/DAI/FISH sub-schemes): these don't always carry a
            # local skos:inScheme, so without this the scheme would come out
            # unknown. Explicit inScheme (above) always wins.
            if not subject_scheme:
                guessed = resolve_scheme_via_namespace(str(subject))
                if guessed:
                    subject_scheme = guessed
                    report["subject_scheme_from_namespace"] += 1
            if not object_scheme:
                guessed = resolve_scheme_via_namespace(str(obj))
                if guessed:
                    object_scheme = guessed
                    report["object_scheme_from_namespace"] += 1

            # Pactols-only fallback: concept URIs don't reveal Lieux vs
            # Sujets, so use the current file's name instead (see
            # resolve_pactols_scheme_from_file's docstring for the caveat
            # about a Pactols URI showing up only as a match target in
            # another folder's dump).
            if folder_name == "Pactols":
                if not subject_scheme:
                    subject_scheme = resolve_pactols_scheme_from_file(source_file.name)
                    if subject_scheme:
                        report["subject_scheme_from_namespace"] += 1
                        SCHEME_ORIGIN_FOLDER.setdefault(subject_scheme, folder_name)
                if not object_scheme:
                    object_scheme = resolve_pactols_scheme_from_file(source_file.name)
                    if object_scheme:
                        report["object_scheme_from_namespace"] += 1
                        SCHEME_ORIGIN_FOLDER.setdefault(object_scheme, folder_name)

            # IMPORTANT: no namespace inference is used for scheme assignment
            # beyond the curated fallbacks above. We collect namespace
            # candidates for URIs STILL unresolved after those into a
            # cross-run tally that main() writes out as a curation list.
            for uri, scheme in ((str(subject), subject_scheme), (str(obj), object_scheme)):
                namespace = namespace_candidate(uri)
                report["namespaces"][namespace] += 1
                if not scheme:
                    UNRESOLVED_NAMESPACE_COUNTS[namespace] += 1

            subject_label = best_label(graph, subject)
            object_label = best_label(graph, obj)
            if not subject_label:
                report["labels_missing_subject"] += 1
            if not object_label:
                report["labels_missing_object"] += 1

            if not subject_scheme:
                report["unknown_source_scheme"] += 1
            if not object_scheme:
                report["unknown_target_scheme"] += 1

            key = (subject_scheme or "", object_scheme or "")

            grouped[key].append({
                "subject_uri": str(subject),
                "object_uri": str(obj),
                "subject_id": to_curie(str(subject), prefix_map),
                "subject_label": subject_label,
                "predicate_id": f"skos:{MAPPING_PREDICATES[predicate]}",
                "object_id": to_curie(str(obj), prefix_map),
                "object_label": object_label,
                "mapping_justification": "semapv:UnspecifiedMatching",
                "comment": "",
                "provider_override": row_provider_override,
            })
            contributors[key][(folder_name, str(source_file))] = {
                "folder": folder_name,
                "source_file": source_file,
                "graph": graph,
            }

    return report


def write_unresolved_namespace_report() -> Path | None:
    """Writes the curation list: namespace roots seen on a subject/object
    URI whose concept scheme is still unknown after every fallback, sorted
    by frequency. Add the most frequent ones to SCHEME_REGISTRY, then
    re-run."""
    if not UNRESOLVED_NAMESPACE_COUNTS:
        return None

    target_dir = OUTPUT_DIR / "unknown_target"
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / "_namespace_roots_to_curate.tsv"

    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(["count", "namespace_root"])
        for namespace, count in sorted(
            UNRESOLVED_NAMESPACE_COUNTS.items(), key=lambda x: (-x[1], x[0])
        ):
            writer.writerow([count, namespace])

    print(f"\n[curation] {len(UNRESOLVED_NAMESPACE_COUNTS)} unresolved namespace root(s) "
          f"written to {out_path.relative_to(BASE)}")
    return out_path


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    prefix_map = build_global_prefix_map()

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    contributors: dict[tuple[str, str], dict[tuple[str, str], dict]] = defaultdict(dict)
    reports: dict[str, dict] = {}

    for folder_name, config in VOCABULARIES.items():
        if not config["path"].exists():
            print(f"[WARN] Folder does not exist: {config['path']}")
            continue
        reports[folder_name] = process_folder(folder_name, config, grouped, contributors, prefix_map)

    grand_total = 0
    for group_key, rows in grouped.items():
        subject_scheme, object_scheme = group_key
        unique = {}
        for row in rows:
            row_key = (row["subject_uri"], row["predicate_id"], row["object_uri"])
            unique[row_key] = row
        rows = list(unique.values())

        output = write_sssom(
            subject_scheme or None,
            object_scheme or None,
            rows,
            list(contributors[group_key].values()),
            prefix_map,
        )
        print(f"[SSSOM] {output.relative_to(BASE)}: {len(rows)} mappings")
        grand_total += len(rows)

    write_unresolved_namespace_report()

    print("\n=== Per-folder scan report ===")
    for folder_name, report in reports.items():
        print(f"\n--- {folder_name} ---")
        print(f"Files scanned:                 {report['files']}")
        print(f"Files containing mappings:     {report['files_with_mappings']}")
        print(f"Subjects with explicit inScheme: {report['subjects_with_explicit_inScheme']}")
        print(f"Subjects using file-level scheme: {report['file_default_scheme_used']}")
        print(f"Subjects without source scheme: {report['subjects_without_inScheme']}")
        print(f"Subject scheme resolved via registry/filename fallback: {report['subject_scheme_from_namespace']}")
        print(f"Object scheme resolved via registry/filename fallback: {report['object_scheme_from_namespace']}")
        print(f"Mappings with unknown source scheme: {report['unknown_source_scheme']}")
        print(f"Mappings with unknown target scheme: {report['unknown_target_scheme']}")
        print(f"Missing subject labels:         {report['labels_missing_subject']}")
        print(f"Missing object labels:          {report['labels_missing_object']}")
        print("Namespaces encountered (URI heuristic only; NOT used as schemes "
              "unless curated into SCHEME_REGISTRY):")
        for namespace, count in sorted(
            report["namespaces"].items(), key=lambda x: (-x[1], x[0])
        )[:50]:
            print(f"  {count:6d}  {namespace}")

    print(f"\nDone. Total mapping assertions written: {grand_total}")
    print(f"Mapping sets written: {len(grouped)}")
    print(f"SSSOM output directory: {OUTPUT_DIR}")
    print(f"Files with an unresolved target scheme: {OUTPUT_DIR / 'unknown_target'}")
    print("Scheme assignment policy: explicit skos:inScheme > safe single-scheme file "
          "default > curated SCHEME_REGISTRY guess (concept_root/concept_marker) > "
          "Pactols filename fallback > unknown")
    print("Files are grouped and named GLOBALLY by concept-scheme pair "
          "(<subjectSchemeSlug>_<objectSchemeSlug>.sssom.tsv) across all folders — a "
          "folder's own vocabulary can end up on either side depending on which way a "
          "given dump actually asserts the match; check mapping_set_description if unsure "
          "which side is which for a given file.")


if __name__ == "__main__":
    main()