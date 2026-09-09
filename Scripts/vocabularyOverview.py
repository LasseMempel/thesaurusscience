import rdflib
from rdflib import RDF, URIRef
from rdflib.namespace import SKOS
import glob
import os
import logging
from collections import Counter, defaultdict

# Some source files contain malformed xsd:dateTime literals (e.g. a doubled
# timezone offset like "+02:00+01:00"). rdflib logs a full traceback for each
# one via logging.warning but otherwise recovers fine, so just raise this
# logger's level to stop the noise.
logging.getLogger("rdflib.term").setLevel(logging.ERROR)

folders = [
    "DAI",
    "FISH",
    "Dariah Vocabs",
    "Pactols",
    "Wortnetz",
]

basePath = "/home/mempellaenger/repos/thesaurusscience" # "/home/lasse/repos/thesaurusscience/"

# Known namespaces, used both for the "is this SKOS-family?" check and to
# render short curie-style labels (skos:prefLabel) instead of full URIs.
PREFIXES = {
    str(SKOS): "skos",
    "http://www.w3.org/2008/05/skos-xl#": "skosxl",
    "http://purl.org/iso25964/skos-thes#": "iso-thes",
    str(RDF): "rdf",
}

# The classic cross-vocabulary SKOS mapping relations. Pulled out into their
# own artificial "Mapping" bucket regardless of which class they attach to,
# since that's usually the thing you actually want to eyeball for validation.
MAPPING_PROPERTIES = {
    str(SKOS.exactMatch),
    str(SKOS.closeMatch),
    str(SKOS.broadMatch),
    str(SKOS.narrowMatch),
    str(SKOS.relatedMatch),
    str(SKOS.mappingRelation),
}

MAPPING_LABEL = "Mapping (cross-vocabulary links)"
UNCLASSIFIED_LABEL = "Unclassified (no rdf:type asserted on subject)"


def get_namespace(uri):
    """Split a URI into its namespace part: everything up to and including
    the last '#', or failing that, the last '/'."""
    uri = str(uri)
    if "#" in uri:
        return uri.rsplit("#", 1)[0] + "#"
    return uri.rsplit("/", 1)[0] + "/"


def is_skos_family(namespace):
    """Match core SKOS plus any extension whose namespace mentions 'skos'
    (skos-xl, iso-thes's skos-thes, and anything similar in the future)."""
    return "skos" in namespace.lower()


def curie(uri):
    """Render known namespaces as prefix:local, otherwise return the URI as-is."""
    uri = str(uri)
    ns = get_namespace(uri)
    prefix = PREFIXES.get(ns)
    if prefix:
        return f"{prefix}:{uri[len(ns):]}"
    return uri


def read_graph(file_path):
    """Parse an .rdf (XML) or .ttl (Turtle) file into an rdflib Graph."""
    ext = os.path.splitext(file_path)[1].lower()
    g = rdflib.Graph()
    if ext == ".rdf":
        g.parse(file_path, format="xml")
    elif ext == ".ttl":
        g.parse(file_path, format="turtle")
    else:
        raise ValueError(f"Unsupported file extension: {ext}")
    return g


def analyze_graph(g):
    """
    Two passes over the graph:
      1) figure out which SKOS-family class(es) each subject is asserted to be
      2) tally SKOS-family property usage, grouped by the subject's class(es),
         with mapping properties pulled into their own bucket instead

    Returns:
      class_instance_counts: Counter[class_uri] -> number of rdf:type triples
      class_property_counts: dict[class_uri or bucket label] -> Counter[property_uri]
      namespaces: set of every namespace seen on any URIRef s/p/o in this graph
    """
    subject_classes = defaultdict(set)
    class_instance_counts = Counter()
    namespaces = set()

    for s, p, o in g:
        if isinstance(s, URIRef):
            namespaces.add(get_namespace(s))
        namespaces.add(get_namespace(p))
        if isinstance(o, URIRef):
            namespaces.add(get_namespace(o))

        if p == RDF.type and isinstance(o, URIRef) and is_skos_family(get_namespace(o)):
            subject_classes[s].add(str(o))
            class_instance_counts[str(o)] += 1

    class_property_counts = defaultdict(Counter)

    for s, p, o in g:
        if p == RDF.type or not is_skos_family(get_namespace(p)):
            continue

        pstr = str(p)
        if pstr in MAPPING_PROPERTIES:
            class_property_counts[MAPPING_LABEL][pstr] += 1
            continue

        classes = subject_classes.get(s)
        if classes:
            for cls in classes:
                class_property_counts[cls][pstr] += 1
        else:
            class_property_counts[UNCLASSIFIED_LABEL][pstr] += 1

    return class_instance_counts, class_property_counts, namespaces


def merge_class_property_counts(target, source):
    for cls, counter in source.items():
        target[cls].update(counter)


def print_grouped(class_instance_counts, class_property_counts, indent="  "):
    # Real SKOS-family classes first, alphabetically; artificial buckets last.
    real_classes = sorted(
        c for c in class_property_counts if c not in (MAPPING_LABEL, UNCLASSIFIED_LABEL)
    )
    ordering = real_classes
    if MAPPING_LABEL in class_property_counts:
        ordering.append(MAPPING_LABEL)
    if UNCLASSIFIED_LABEL in class_property_counts:
        ordering.append(UNCLASSIFIED_LABEL)

    for cls in ordering:
        if cls in (MAPPING_LABEL, UNCLASSIFIED_LABEL):
            header = cls
        else:
            instances = class_instance_counts.get(cls, 0)
            header = f"class {curie(cls)} ({instances} instances)"
        print(f"{indent}{header}")
        for prop, count in class_property_counts[cls].most_common():
            print(f"{indent}  - {curie(prop)}: {count}")


def find_vocab_files(folder_path):
    files = glob.glob(os.path.join(folder_path, "*.rdf"))
    files.extend(glob.glob(os.path.join(folder_path, "*.ttl")))
    return files


def main():
    grand_class_instances = Counter()
    grand_class_properties = defaultdict(Counter)
    all_namespaces = set()

    for folder_name in folders:
        folder_path = os.path.join(basePath, folder_name)
        files = find_vocab_files(folder_path)
        print(f"\n=== {folder_name} ({len(files)} files) ===")

        folder_class_instances = Counter()
        folder_class_properties = defaultdict(Counter)

        for file_path in files:
            graph = read_graph(file_path)
            if graph is None:
                raise RuntimeError(f"Failed to read graph from {file_path}")

            class_instances, class_properties, namespaces = analyze_graph(graph)
            folder_class_instances.update(class_instances)
            merge_class_property_counts(folder_class_properties, class_properties)
            all_namespaces.update(namespaces)

            print(f"\n  --- {os.path.basename(file_path)} ---")
            print_grouped(class_instances, class_properties, indent="    ")

        if len(files) > 1:
            print(f"\n  === {folder_name} totals ===")
            print_grouped(folder_class_instances, folder_class_properties, indent="    ")

        grand_class_instances.update(folder_class_instances)
        merge_class_property_counts(grand_class_properties, folder_class_properties)

    print("\n\n=== Grand total across all folders ===")
    print_grouped(grand_class_instances, grand_class_properties)

    """
    print("\n=== All namespaces used across all vocabularies ===")
    for ns in sorted(all_namespaces):
        print(ns)
    """

main()