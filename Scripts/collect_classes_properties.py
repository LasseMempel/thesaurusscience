#!/usr/bin/env python3
"""
RDF Class and Property Collector

Reads RDF/XML and Turtle files from specified folders and collects
all distinctive classes and properties using SPARQL queries.
Each vocabulary file is evaluated separately.
"""

import logging
from pathlib import Path
from rdflib import Graph

# Silence rdflib warnings (e.g., malformed datetime values)
logging.getLogger('rdflib').setLevel(logging.ERROR)

# =============================================================================
# CONFIGURATION - Folders to process (non-recursive, direct files only)
# =============================================================================
FOLDERS = [
    "/home/mempellaenger/repos/thesaurusscience/DAI",
    "/home/mempellaenger/repos/thesaurusscience/Dariah Vocabs",
    "/home/mempellaenger/repos/thesaurusscience/FISH",
    "/home/mempellaenger/repos/thesaurusscience/Wortnetz",
]

SUPPORTED_EXTENSIONS = {'.ttl', '.rdf', '.xml'}

# Filter results (set to True to filter, False to show all)
FILTER_SKOS_ONLY = True
SKOS_NAMESPACE = ["http://www.w3.org/2004/02/skos/core#", "http://www.w3.org/2008/05/skos-xl#"]

# =============================================================================
# SPARQL QUERIES
# =============================================================================

CLASSES_QUERY = """
SELECT DISTINCT ?cls WHERE {
    ?s a ?cls .
}
ORDER BY ?cls
"""

PROPS_QUERY = """
SELECT DISTINCT ?prop WHERE {
    ?s ?prop ?o .
}
ORDER BY ?prop
"""

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def process_single_file(filepath, filter_skos=False):
    """
    Process a single RDF file and return its classes and properties.
    
    Args:
        filepath: Path to the RDF file
        filter_skos: If True, filter results to only SKOS namespace
        
    Returns:
        Tuple of (classes_list, props_list, triple_count, error_message)
        If error occurs, classes_list and props_list will be empty lists,
        triple_count will be 0, and error_message will contain the error.
    """
    fmt = 'turtle' if filepath.suffix.lower() == '.ttl' else 'xml'
    
    g = Graph()
    try:
        g.parse(filepath, format=fmt)
    except Exception as e:
        return ([], [], 0, str(e))
    
    # Get all DISTINCT classes (objects of rdf:type)
    classes = list(g.query(CLASSES_QUERY))
    
    # Get all DISTINCT properties (predicates)
    props = list(g.query(PROPS_QUERY))
    
    # Filter for SKOS namespace if requested
    if filter_skos:
        classes = [c for c in classes if any(str(c.cls).startswith(ns) for ns in SKOS_NAMESPACE)]
        props = [p for p in props if any(str(p.prop).startswith(ns) for ns in SKOS_NAMESPACE)]
    
    return (classes, props, len(g), None)


def discover_files():
    """
    Discover all RDF files in configured folders.
    
    Returns:
        List of Path objects for all discovered RDF files
    """
    files = []
    for folder in FOLDERS:
        folder_path = Path(folder)
        if not folder_path.exists():
            print(f"⚠️  Warning: Folder does not exist: {folder}")
            continue
        for item in folder_path.iterdir():
            if item.is_file() and item.suffix.lower() in SUPPORTED_EXTENSIONS:
                files.append(item)
    return files


def print_file_results(filepath, classes, props, triple_count):
    """
    Print the results for a single file in a formatted way.
    
    Args:
        filepath: Path to the file
        classes: List of class URIs
        props: List of property URIs
        triple_count: Number of triples in the file
    """
    print(f"\n{'─' * 70}")
    print(f"FILE: {filepath.name}")
    print(f"Path: {filepath}")
    print(f"Triples: {triple_count:,}")
    print(f"{'─' * 70}")
    
    # Print classes
    print(f"\n  CLASSES ({len(classes)}):")
    if classes:
        for row in classes:
            print(f"    {row.cls}")
    else:
        print("    (none found)")
    
    # Print properties
    print(f"\n  PROPERTIES ({len(props)}):")
    if props:
        for row in props:
            print(f"    {row.prop}")
    else:
        print("    (none found)")


def print_summary(all_results):
    """
    Print a summary of all processed files.
    
    Args:
        all_results: List of tuples (filepath, classes, props, triple_count, error)
    """
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    
    total_triples = 0
    total_classes = 0
    total_props = 0
    successful_files = 0
    failed_files = 0
    
    for filepath, classes, props, triple_count, error in all_results:
        if error:
            failed_files += 1
        else:
            successful_files += 1
            total_triples += triple_count
            total_classes += len(classes)
            total_props += len(props)
    
    print(f"\n  Files processed successfully: {successful_files}")
    print(f"  Files with errors: {failed_files}")
    print(f"  Total triples: {total_triples:,}")
    print(f"  Total classes (sum across files): {total_classes}")
    print(f"  Total properties (sum across files): {total_props}")
    print("\n" + "=" * 70)
    print("Done!")
    print("=" * 70)


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 70)
    print("RDF Class and Property Collector")
    print("(Each vocabulary file evaluated separately)")
    print("=" * 70)
    
    # Show filter status
    if FILTER_SKOS_ONLY:
        print(f"\nFilter: SKOS namespace only ({SKOS_NAMESPACE})")
    
    # 1. Discover all files from folders (non-recursive)
    print("\nDiscovering RDF files...")
    files = discover_files()
    
    if not files:
        print("No RDF files found!")
        return
    
    print(f"Found {len(files)} file(s):")
    for f in sorted(files):
        print(f"  - {f.name}")
    
    # 2. Process each file separately
    print("\n" + "=" * 70)
    print("PROCESSING FILES")
    print("=" * 70)
    
    all_results = []
    for filepath in sorted(files):
        classes, props, triple_count, error = process_single_file(filepath, filter_skos=FILTER_SKOS_ONLY)
        
        if error:
            print(f"\n⚠️  ERROR processing {filepath.name}: {error}")
            all_results.append((filepath, [], [], 0, error))
        else:
            print_file_results(filepath, classes, props, triple_count)
            all_results.append((filepath, classes, props, triple_count, None))
    
    # 3. Print summary
    print_summary(all_results)


if __name__ == "__main__":
    main()