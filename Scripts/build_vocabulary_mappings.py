#!/usr/bin/env python3
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
  * groups mappings by source concept scheme and target concept scheme
  * writes one SSSOM TSV per group
  * records UnspecifiedMatching because the original mapping process is unknown

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

# Fill/adjust these after your manual provider/ConceptScheme checks.
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

# Per-concept-scheme overrides. This is especially useful for Dariah Vocabs.
# Example:
#
# CONCEPT_SCHEME_OVERRIDES = {
#     "Dariah Vocabs": {
#         "https://example.org/scheme/foo": {
#             "mapping_provider": "https://ror.org/....",
#             "label": "Foo Vocabulary",
#         },
#     },
# }
CONCEPT_SCHEME_OVERRIDES: dict[str, dict[str, dict[str, str]]] = {}

# Optional manual provider enrichment. This can be folder-wide or file-specific.
# Keys are folder names. Use "__default__" for a folder-wide value and a
# relative source-file path for a file-specific override. A concept-scheme
# override above takes precedence over these defaults.
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
# Namespace-root -> conceptScheme lookup for well-known EXTERNAL targets
# (AAT, Wikidata, GND today). Unlike our own vocab dumps, these concepts
# never carry a local skos:inScheme triple, so subject_scheme/object_scheme
# would otherwise always come out "unknown" for matches into them — even
# though the concept scheme is perfectly well known from the URI alone.
#
# Extend this as you curate: key = the namespace root exactly as
# namespace_candidate() would cut it (last "/" or "#" kept), value =
#   "prefix":     CURIE prefix registered globally in every output file's
#                 curie_map (so subject_id/object_id compact too, not just
#                 subject_source/object_source)
#   "scheme_uri": the conceptScheme identifier to write as subject_source/
#                 object_source
#   "label":      human-readable name, used in mapping_set_title/filenames
#
# AAT concepts declare skos:inScheme aat: themselves, so its own namespace
# IS its conceptScheme. Wikidata items and GND authority records aren't
# natively skos:Concept/skos:ConceptScheme at all, so scheme_uri below is
# each vocabulary's BARTOC registration instead — the same identifier
# coli-conc/Cocoda itself uses as fromScheme/toScheme for them. Double-check
# these two against how your Cocoda instance actually has Wikidata/GND
# registered before publishing; swap them for a different URI if it differs.
NAMESPACE_SCHEME_MAP: dict[str, dict[str, str]] = {
    "http://vocab.getty.edu/aat/": {
        "prefix": "aat",
        "scheme_uri": "http://vocab.getty.edu/aat/",
        "label": "Getty Art & Architecture Thesaurus (AAT)",
    },
    "http://www.wikidata.org/entity/": {
        "prefix": "wikidata",
        "scheme_uri": "http://bartoc.org/en/node/1940",
        "label": "Wikidata",
    },
    "https://d-nb.info/gnd/": {
        "prefix": "gnd",
        "scheme_uri": "http://bartoc.org/en/node/430",
        "label": "Gemeinsame Normdatei (GND)",
        # KNOWN GAP: some older/third-party dumps use "http://d-nb.info/gnd/"
        # (no "s"). A single CURIE prefix can only carry one namespace, so an
        # http:// GND URI won't match this entry or get CURIE-compacted —
        # it'll just fall through as unknown/unresolved instead of silently
        # mis-grouping. If that shows up a lot in your unknown_target namespace
        # report, normalize those URIs to https:// during extraction instead
        # of adding a second "gnd" entry here.
    },
}

# Registered so subject_source/object_source values that are themselves
# BARTOC scheme URIs (Wikidata/GND above) compact into readable CURIEs too,
# e.g. "bartoc:en/node/1940" instead of the bare URI.
BARTOC_PREFIX = {"bartoc": "http://bartoc.org/"}


def resolve_scheme_via_namespace(uri: str) -> str | None:
    """Longest-prefix match of `uri` against NAMESPACE_SCHEME_MAP's namespace
    roots — same algorithm as to_curie(), just resolving to a conceptScheme
    URI instead of a CURIE. Returns None if no registered root matches."""
    best_root = ""
    best_entry = None
    for root, entry in NAMESPACE_SCHEME_MAP.items():
        if uri.startswith(root) and len(root) > len(best_root):
            best_root = root
            best_entry = entry
    return best_entry["scheme_uri"] if best_entry else None


def scheme_uri_label(scheme_uri: str) -> str | None:
    """Friendly label for a resolved scheme_uri, for nicer filenames/titles
    than a raw slugified URI — looks up NAMESPACE_SCHEME_MAP by scheme_uri
    (not namespace root, since callers only have the resolved scheme here)."""
    for entry in NAMESPACE_SCHEME_MAP.values():
        if entry["scheme_uri"] == scheme_uri:
            return entry["prefix"]
    return None


# Cross-folder tally of namespace roots seen on a subject/object URI that
# was STILL unresolved after the NAMESPACE_SCHEME_MAP fallback above — i.e.
# candidates for you to curate into NAMESPACE_SCHEME_MAP next. Written out
# by main() once all folders are processed (Mappings/unknown_target/
# _namespace_roots_to_curate.tsv), not per-folder, so the frequencies reflect
# the whole corpus.
UNRESOLVED_NAMESPACE_COUNTS: dict[str, int] = defaultdict(int)


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


def mapping_set_filename(folder_name: str, subject_scheme: str | None,
                         object_scheme: str | None) -> str:
    source_part = slugify(scheme_uri_label(subject_scheme) or subject_scheme or "unknown_source_scheme")
    target_part = slugify(scheme_uri_label(object_scheme) or object_scheme or "unknown_target_scheme")
    return f"{slugify(folder_name)}__{source_part}__{target_part}.sssom.tsv"


def mapping_set_id(filename: str, rel_dir: str = "") -> str:
    # The canonical mapping-set identifier is the TSV URL, not the later RDF
    # serialization. This follows the SSSOM model/examples. rel_dir must match
    # write_sssom's actual output path (e.g. "unknown_target") so the published
    # URL isn't a dead link.
    sub = f"{rel_dir}/" if rel_dir else ""
    return (
        "https://raw.githubusercontent.com/LasseMempel/thesaurusscience/"
        f"main/Mappings/{sub}{filename}"
    )


def namespace_candidate(uri: str) -> str:
    """
    Heuristic namespace candidate for REPORTING ONLY.

    This intentionally never becomes subject_source/object_source automatically.
    It is used only to show which URI stems occur in the corpus for later
    namespace/concept-scheme harvesting.
    """
    if "#" in uri:
        base = uri.rsplit("#", 1)[0] + "#"
        return base
    if "/" in uri:
        return uri.rsplit("/", 1)[0] + "/"
    return uri


def provider_for(folder_name: str, subject_scheme: str | None, source_file: Path | None = None) -> tuple[str | None, str]:
    config = VOCABULARIES[folder_name]
    override = CONCEPT_SCHEME_OVERRIDES.get(folder_name, {}).get(
        subject_scheme or "",
        {},
    )

    provider = override.get("mapping_provider")
    if provider is None:
        provider = config.get("mapping_provider")

    file_overrides = MAPPING_PROVIDER_OVERRIDES.get(folder_name, {})
    if source_file is not None:
        try:
            rel = str(source_file.relative_to(config["path"]))
        except ValueError:
            rel = str(source_file)
        provider = file_overrides.get(rel, provider)
    provider = file_overrides.get("__default__", provider)

    label = override.get("label", config.get("label", folder_name))
    return provider, label


def build_prefix_map(folder_name: str) -> dict[str, str]:
    config = VOCABULARIES[folder_name]
    prefix_map = {
        "skos": str(SKOS._NS),
        "semapv": "https://w3id.org/semapv/vocab/",
        "orcid": "https://orcid.org/",
        "github": "https://github.com/",
        **BARTOC_PREFIX,
    }
    # Global, well-known target vocabularies (AAT/Wikidata/GND today, see
    # NAMESPACE_SCHEME_MAP) get their CURIE prefix registered in every
    # output file, not just the folder's own vocabulary — so subject_id/
    # object_id compact regardless of which folder's dump the match came from.
    for root, entry in NAMESPACE_SCHEME_MAP.items():
        prefix_map[entry["prefix"]] = root
    namespace = config.get("source_namespace")
    if namespace:
        prefix_map[config["prefix"]] = namespace
    return prefix_map


def write_sssom(
    folder_name: str,
    subject_scheme: str | None,
    object_scheme: str | None,
    rows: list[dict],
    source_file: Path,
    graph: Graph,
) -> Path:
    # Cocoda-facing routing: a resolved object_scheme (explicit inScheme, or
    # a NAMESPACE_SCHEME_MAP guess for AAT/Wikidata/GND-style external
    # targets) keeps the file in OUTPUT_DIR as before. An unresolved one
    # goes into unknown_target/ instead of cluttering the main folder.
    rel_dir = "" if object_scheme else "unknown_target"
    out_dir = OUTPUT_DIR / rel_dir if rel_dir else OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    filename = mapping_set_filename(folder_name, subject_scheme, object_scheme)
    output = out_dir / filename
    set_id = mapping_set_id(filename, rel_dir)

    provider, vocab_label = provider_for(folder_name, subject_scheme)

    prefix_map = build_prefix_map(folder_name)

    description = (
        f"SKOS mappings extracted from a local RDF dump of {vocab_label}"
    )
    if subject_scheme:
        description += f" (concept scheme: {subject_scheme})"
    if object_scheme:
        description += f", targeting concept scheme {object_scheme}"
    description += (
        ". The original procedure by which the mappings were established is "
        "unknown; mapping_justification is therefore "
        "semapv:UnspecifiedMatching."
    )

    subject_source = subject_scheme
    object_source = object_scheme

    with output.open("w", encoding="utf-8", newline="") as fh:
        fh.write("#curie_map:\n")
        for prefix, namespace in prefix_map.items():
            fh.write(f"#  {prefix}: {namespace}\n")

        fh.write(f"#mapping_set_id: {set_id}\n")
        fh.write(
            "#mapping_set_title: "
            f"{vocab_label} SKOS mappings extracted from local dump\n"
        )
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
            fh.write(
                "#subject_source: "
                f"{to_curie(subject_source, prefix_map)}\n"
            )
            version = get_scheme_version(graph, subject_source)
            if version:
                fh.write(f"#subject_source_version: {version}\n")

        if object_source:
            fh.write(
                "#object_source: "
                f"{to_curie(object_source, prefix_map)}\n"
            )
            version = get_scheme_version(graph, object_source)
            if version:
                fh.write(f"#object_source_version: {version}\n")

        try:
            relative_source = source_file.relative_to(VOCABULARIES[folder_name]["path"])
        except ValueError:
            relative_source = source_file

        fh.write(
            "#comment: Extracted from local RDF file "
            f"{relative_source}; source graph contains SKOS mapping "
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
            # Internal extraction fields (subject_uri, object_uri, provider)
            # are deliberately not part of the SSSOM mapping table. Select
            # only the declared TSV columns so csv.DictWriter cannot leak
            # implementation details into the output.
            writer.writerow({field: row.get(field, "") for field in fields})

    return output


def process_folder(folder_name: str, config: dict) -> int:
    folder = config["path"]
    files = find_rdf_files(folder)

    print(f"\n=== {folder_name} ===")
    print(f"RDF/XML + Turtle files: {len(files)}")

    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    report = {
        "files": 0,
        "files_with_mappings": 0,
        "mappings": 0,
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
        "providers_missing": 0,
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

        prefix_map = build_prefix_map(folder_name)
        provider, _ = provider_for(folder_name, file_default_scheme, source_file)

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
            elif default_ok:
                report["file_default_scheme_used"] += 1
                subject_scheme = file_default_scheme
            else:
                report["subjects_without_inScheme"] += 1
                subject_scheme = None

            explicit_object_schemes = sorted(
                str(o) for o in graph.objects(obj, SKOS.inScheme)
            )
            object_scheme = explicit_object_schemes[0] if explicit_object_schemes else None

            # Fallback for well-known EXTERNAL targets (AAT/Wikidata/GND):
            # these never carry a local skos:inScheme, so without this the
            # scheme would always come out unknown. Explicit inScheme (above)
            # always wins; this only fires when nothing else resolved it.
            # Applied to both sides — e.g. useful if some dump matches into
            # another external vocabulary you later add to NAMESPACE_SCHEME_MAP.
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

            # IMPORTANT: no namespace inference is used for scheme assignment
            # beyond the curated NAMESPACE_SCHEME_MAP fallback above. We
            # collect namespace candidates for URIs that are STILL unresolved
            # after that fallback into a global, cross-folder tally that
            # main() writes out as a curation list at the end — the roots
            # that show up there are your next candidates to add to
            # NAMESPACE_SCHEME_MAP.
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

            key = (
                subject_scheme or "",
                object_scheme or "",
                str(source_file),
            )

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
                "provider": provider,
            })

    total = 0

    for (subject_scheme, object_scheme, source_file_str), rows in grouped.items():
        unique = {}
        for row in rows:
            key = (row["subject_uri"], row["predicate_id"], row["object_uri"])
            unique[key] = row
        rows = list(unique.values())

        source_file = Path(source_file_str)
        try:
            source_graph = parse_file(source_file)
        except Exception:
            source_graph = Graph()

        output = write_sssom(
            folder_name,
            subject_scheme or None,
            object_scheme or None,
            rows,
            source_file,
            source_graph,
        )

        provider, _ = provider_for(
            folder_name, subject_scheme or None, source_file
        )
        if not provider:
            report["providers_missing"] += 1
            print(
                f"[WARN] {output.name}: no mapping_provider configured for "
                f"source scheme {subject_scheme or '[unknown]'}"
            )

        print(f"[SSSOM] {output.relative_to(BASE)}: {len(rows)} mappings")
        total += len(rows)

    report["mappings"] = total
    print(f"\n--- {folder_name} report ---")
    print(f"Files scanned:                 {report['files']}")
    print(f"Files containing mappings:     {report['files_with_mappings']}")
    print(f"Mappings written:              {report['mappings']}")
    print(f"Subjects with explicit inScheme: {report['subjects_with_explicit_inScheme']}")
    print(f"Subjects using file-level scheme: {report['file_default_scheme_used']}")
    print(f"Subjects without source scheme: {report['subjects_without_inScheme']}")
    print(f"Subject scheme resolved via NAMESPACE_SCHEME_MAP guess: {report['subject_scheme_from_namespace']}")
    print(f"Object scheme resolved via NAMESPACE_SCHEME_MAP guess: {report['object_scheme_from_namespace']}")
    print(f"Mappings with unknown source scheme: {report['unknown_source_scheme']}")
    print(f"Mappings with unknown target scheme: {report['unknown_target_scheme']}")
    print(f"Missing subject labels:         {report['labels_missing_subject']}")
    print(f"Missing object labels:          {report['labels_missing_object']}")
    print(f"Output sets without provider:   {report['providers_missing']}")
    print("Namespaces encountered (URI heuristic only; NOT used as schemes):")
    for namespace, count in sorted(
        report["namespaces"].items(), key=lambda x: (-x[1], x[0])
    )[:50]:
        print(f"  {count:6d}  {namespace}")
    return total


def write_unresolved_namespace_report() -> Path | None:
    """Writes the cross-folder curation list: namespace roots seen on a
    subject/object URI whose concept scheme is still unknown after the
    NAMESPACE_SCHEME_MAP fallback, sorted by frequency. Add the most
    frequent ones to NAMESPACE_SCHEME_MAP, then re-run."""
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

    grand_total = 0
    for folder_name, config in VOCABULARIES.items():
        if not config["path"].exists():
            print(f"[WARN] Folder does not exist: {config['path']}")
            continue
        grand_total += process_folder(folder_name, config)

    write_unresolved_namespace_report()

    print(f"\nDone. Total mapping assertions written: {grand_total}")
    print(f"SSSOM output directory: {OUTPUT_DIR}")
    print(f"Files with an unresolved target scheme: {OUTPUT_DIR / 'unknown_target'}")
    print("Scheme assignment policy: explicit skos:inScheme > safe single-scheme file default "
          "> curated NAMESPACE_SCHEME_MAP guess (AAT/Wikidata/GND et al.) > unknown")
    print("URI namespaces in reports are heuristic candidates only and are never used as schemes "
          "unless curated into NAMESPACE_SCHEME_MAP.")


if __name__ == "__main__":
    main()