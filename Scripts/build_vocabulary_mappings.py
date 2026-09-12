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
  * creator_id = Lasse Mempel, because you created this SSSOM representation
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


BASE = Path("/home/lasse/repos/thesaurusscience")
OUTPUT_DIR = BASE / "Mappings"

CREATOR_ID = "orcid:0009-0001-5183-1635"
CREATOR_LABEL = "Lasse Mempel"
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
    source_part = slugify(subject_scheme or "unknown_source_scheme")
    target_part = slugify(object_scheme or "unknown_target_scheme")
    return f"{slugify(folder_name)}__{source_part}__{target_part}.sssom.tsv"


def mapping_set_id(filename: str) -> str:
    # The canonical mapping-set identifier is the TSV URL, not the later RDF
    # serialization. This follows the SSSOM model/examples.
    return (
        "https://raw.githubusercontent.com/LasseMempel/thesaurusscience/"
        f"main/Mappings/auto/{filename}"
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
        "aat": "http://vocab.getty.edu/aat/",
    }
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
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    filename = mapping_set_filename(folder_name, subject_scheme, object_scheme)
    output = OUTPUT_DIR / filename
    set_id = mapping_set_id(filename)

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
        fh.write(f"#mapping_tool_id: {GITHUB_TOOL_URL}\n")

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

            # IMPORTANT: no namespace inference is used for scheme assignment.
            # We merely collect URI namespace candidates for the final report so
            # that they can be harvested/curated later.
            for uri in (str(subject), str(obj)):
                namespace = namespace_candidate(uri)
                report["namespaces"][namespace] += 1

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


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    grand_total = 0
    for folder_name, config in VOCABULARIES.items():
        if not config["path"].exists():
            print(f"[WARN] Folder does not exist: {config['path']}")
            continue
        grand_total += process_folder(folder_name, config)

    print(f"\nDone. Total mapping assertions written: {grand_total}")
    print(f"SSSOM output directory: {OUTPUT_DIR}")
    print("Scheme assignment policy: explicit skos:inScheme > safe single-scheme file default > unknown")
    print("URI namespaces in reports are heuristic candidates only and are never used as schemes.")


if __name__ == "__main__":
    main()
