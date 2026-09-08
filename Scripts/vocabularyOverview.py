import rdflib
from rdflib import RDF
from rdflib.namespace import SKOS
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

basePath = "/home/lasse/repos/thesaurusscience/"

SKOS_NS = str(SKOS)


def read_graph(file_path):
    """Parse an .rdf (XML) or .ttl (Turtle) file into an rdflib Graph."""
    ext = os.path.splitext(file_path)[1].lower()
    g = rdflib.Graph()
    try:
        if ext == ".rdf":
            g.parse(file_path, format="xml")
        elif ext == ".ttl":
            g.parse(file_path, format="turtle")
        else:
            print(f"  Unsupported file extension: {ext}")
            return None
    except Exception as e:
        print(f"  Failed to parse {file_path}: {e}")
        return None
    return g


def count_skos_usage(g):
    """
    Walk every triple and tally SKOS usage:
      - if the predicate itself is a SKOS property (skos:prefLabel, skos:broader, ...),
        count that property.
      - if the predicate is rdf:type and the object is a SKOS class
        (skos:Concept, skos:ConceptScheme, ...), count that class.
    Returns a Counter mapping term URI -> occurrence count.
    """
    counter = Counter()
    for s, p, o in g:
        if p == RDF.type and str(o).startswith(SKOS_NS):
            counter[str(o)] += 1
        elif str(p).startswith(SKOS_NS):
            counter[str(p)] += 1
    return counter


def find_vocab_files(folder_path):
    files = glob.glob(os.path.join(folder_path, "*.rdf"))
    files.extend(glob.glob(os.path.join(folder_path, "*.ttl")))
    return files


def main():
    grand_total = Counter()

    for folder_name in folders:
        folder_path = os.path.join(basePath, folder_name)
        files = find_vocab_files(folder_path)
        print(f"\n=== {folder_name} ({len(files)} files) ===")

        folder_total = Counter()
        for file_path in files:
            graph = read_graph(file_path)
            if graph is None:
                continue

            usage = count_skos_usage(graph)
            folder_total.update(usage)

            total_hits = sum(usage.values())
            print(f"  {os.path.basename(file_path)}: "
                  f"{total_hits} SKOS occurrences, {len(usage)} distinct terms")

        if folder_total:
            print(f"  -- {folder_name} totals --")
            for term, count in folder_total.most_common():
                print(f"    {term}: {count}")

        grand_total.update(folder_total)

    print("\n=== Grand total across all folders ===")
    for term, count in grand_total.most_common():
        print(f"{term}: {count}")


if __name__ == "__main__":
    main()