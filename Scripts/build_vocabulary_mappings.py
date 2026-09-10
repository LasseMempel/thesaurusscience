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
  * mapping_provider = configured institution/project or, where appropriate,
    the vocabulary/source itself
  * publication_date = date this generated SSSOM artifact is created
  * mapping_date is omitted unless a reliable assertion date exists in the source
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from collections import defaultdict
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, SKOS


BASE = Path("/home/lasse/repos/thesaurusscience")
OUTPUT_DIR = BASE / "Mappings" / "auto"

CREATOR_ID = "orcid:0009-0001-5183-1635"
CREATOR_LABEL = "Lasse Mempel"
PUBLICATION_DATE = dt.date.today().isoformat()
LICENSE = "https://creativecommons.org/licenses/by/4.0/"

GITHUB_TOOL_URL = (
    "https://github.com/LasseMempel/thesaurusscience/"
    "blob/main/Scripts/extract_skos_mappings_to_sssom.py"
)

# Fill/adjust these after your manual provider/Con­ceptScheme checks.
VOCABULARIES = {
    "DAI": {
        "path": BASE / "DAI",
        "prefix": "dai",
        "source_namespace": "http://thesauri.dainst.org/",
        "mapping_provider": "https://ror.org/041qv0h25",
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


def provider_for(folder_name: str, subject_scheme: str | None) -> tuple[str | None, str]:
    config = VOCABULARIES[folder_name]
    override = CONCEPT_SCHEME_OVERRIDES.get(folder_name, {}).get(
        subject_scheme or "", {}
    )
    provider = override.get("mapping_provider", config.get("mapping_provider"))
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
            writer.writerow(row)

    return output


def process_folder(folder_name: str, config: dict) -> int:
    folder = config["path"]
    files = find_rdf_files(folder)

    print(f"\n=== {folder_name} ===")
    print(f"RDF/XML + Turtle files: {len(files)}")

    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)

    for source_file in files:
        try:
            graph = parse_file(source_file)
        except Exception as exc:
            print(f"[WARN] Could not parse {source_file}: {exc}")
            continue

        results = list(query_mappings(graph))
        if not results:
            continue

        print(
            f"[LOAD] {source_file.relative_to(folder)}: "
            f"{len(graph)} triples, {len(results)} SKOS mappings"
        )

        prefix_map = build_prefix_map(folder_name)

        for result in results:
            subject = result.subject
            predicate = result.predicate
            obj = result.object

            if not isinstance(subject, URIRef) or not isinstance(obj, URIRef):
                continue

            subject_scheme = (
                str(result.subjectScheme)
                if result.subjectScheme
                else infer_scheme(graph, subject)
            )
            object_scheme = (
                str(result.objectScheme)
                if result.objectScheme
                else infer_scheme(graph, obj)
            )

            provider, _ = provider_for(folder_name, subject_scheme)

            key = (
                subject_scheme or "",
                object_scheme or "",
                str(source_file),
            )

            grouped[key].append(
                {
                    "subject_id": to_curie(str(subject), prefix_map),
                    "subject_label": best_label(graph, subject),
                    "predicate_id": f"skos:{MAPPING_PREDICATES[predicate]}",
                    "object_id": to_curie(str(obj), prefix_map),
                    "object_label": best_label(graph, obj),
                    "mapping_justification": "semapv:UnspecifiedMatching",
                    "comment": "",
                }
            )

            if not subject_scheme:
                print(
                    f"[WARN] No skos:inScheme for subject {subject} "
                    f"in {source_file.name}"
                )

            if not object_scheme:
                print(
                    f"[WARN] No skos:inScheme for object {obj} "
                    f"in {source_file.name}"
                )

    total = 0

    for (subject_scheme, object_scheme, source_file_str), rows in grouped.items():
        # Remove duplicate identical assertions.
        unique = {}
        for row in rows:
            key = (
                row["subject_id"],
                row["predicate_id"],
                row["object_id"],
            )
            unique[key] = row
        rows = list(unique.values())

        source_file = Path(source_file_str)

        # Recover one graph for scheme version metadata.
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

        provider, _ = provider_for(folder_name, subject_scheme or None)
        if not provider:
            print(
                f"[WARN] {output.name}: no mapping_provider configured for "
                f"subject scheme {subject_scheme or '[unknown]'}"
            )

        print(f"[SSSOM] {output.relative_to(BASE)}: {len(rows)} mappings")
        total += len(rows)

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


if __name__ == "__main__":
    main()
