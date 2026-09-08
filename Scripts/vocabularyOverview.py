import rdflib
from rdflib import RDF, URIRef
import glob
import os
import logging
from collections import Counter

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
    Walk every triple once and return:
      - skos_counter: Counter of SKOS-family term URI -> occurrence count
        (predicate usage, or rdf:type where the class is SKOS-family)
      - namespaces: set of every namespace seen on any URIRef subject,
        predicate, or object in this graph (not just SKOS-family ones)
    """
    skos_counter = Counter()
    namespaces = set()

    for s, p, o in g:
        if isinstance(s, URIRef):
            namespaces.add(get_namespace(s))
        namespaces.add(get_namespace(p))
        if isinstance(o, URIRef):
            namespaces.add(get_namespace(o))

        if p == RDF.type and isinstance(o, URIRef) and is_skos_family(get_namespace(o)):
            skos_counter[str(o)] += 1
        elif is_skos_family(get_namespace(p)):
            skos_counter[str(p)] += 1

    return skos_counter, namespaces


def find_vocab_files(folder_path):
    files = glob.glob(os.path.join(folder_path, "*.rdf"))
    files.extend(glob.glob(os.path.join(folder_path, "*.ttl")))
    return files


def main():
    grand_total = Counter()
    all_namespaces = set()

    for folder_name in folders:
        folder_path = os.path.join(basePath, folder_name)
        files = find_vocab_files(folder_path)
        print(f"\n=== {folder_name} ({len(files)} files) ===")

        folder_total = Counter()

        for file_path in files:
            graph = read_graph(file_path)
            if graph is None:
                raise RuntimeError(f"Failed to read graph from {file_path}")

            usage, namespaces = analyze_graph(graph)
            folder_total.update(usage)
            all_namespaces.update(namespaces)

            print(f"  {os.path.basename(file_path)}:")
            for term, count in usage.most_common():
                print(f"    {term}: {count}")
            print()

        if folder_total and len(files) > 1:
            print(f"  -- {folder_name} totals --")
            for term, count in folder_total.most_common():
                print(f"    {term}: {count}")

        grand_total.update(folder_total)

    print("\n=== Grand total across all folders ===")
    for term, count in grand_total.most_common():
        print(f"{term}: {count}")

    """
    print("\n=== All namespaces used across all vocabularies ===")
    for ns in sorted(all_namespaces):
        print(ns)
    """


if __name__ == "__main__":
    main()