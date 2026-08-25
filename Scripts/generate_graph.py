#!/usr/bin/env python3
"""
Generate standardized SKOS vocabulary graphs from multiple sources.

Processes DAI, Dariah Vocabs, FISH vocabularies, and Wortnetz Kultur,
generating one standardized Turtle file per vocabulary.

Output: /home/mempellaenger/repos/thesaurusscience/Output/standardized/
"""

import os
from pathlib import Path

from rdflib import Graph, URIRef, Literal
from rdflib.namespace import RDF, RDFS, SKOS, DC, DCTERMS, OWL

# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path("/home/mempellaenger/repos/thesaurusscience")

# Source directories
DAI_DIR = BASE_DIR / "DAI"
DARIAH_DIR = BASE_DIR / "Dariah Vocabs"
FISH_DIR = BASE_DIR / "FISH"
OUTPUT_DIR = BASE_DIR / "Output"
STANDARDIZED_DIR = OUTPUT_DIR / "standardized"

# Mapping files
DAI_AAT_MAPPINGS = OUTPUT_DIR / "dai_aat_mappings.ttl"
ADS_AAT_MAPPINGS = OUTPUT_DIR / "ads_aat_mappings.ttl"

# DAI v1.2 for scheme metadata
DAI_V1_2_FILE = DARIAH_DIR / "DAI_v1.2_Idaiworld.ttl"

# ============================================================
# VOCABULARY DEFINITIONS
# ============================================================

VOCABULARIES = []

# 1. DAI Main - uses export file for concepts, v1.2 for scheme metadata
VOCABULARIES.append({
    "output": "dai_standardized.ttl",
    "sources": [DAI_DIR / "export-2020-10-14_10-36.ttl"],
    "scheme_override": "http://thesauri.dainst.org/scheme",
    "scheme_label_source": DAI_V1_2_FILE,
    "mapping_files": [DAI_AAT_MAPPINGS],
    "type": "dai"
})

# 2. DAI v1.2 (process separately for cross-references)
VOCABULARIES.append({
    "output": "dai_v1.2_idaiworld_standardized.ttl",
    "sources": [DAI_V1_2_FILE],
    "scheme_override": "http://thesauri.dainst.org/scheme",
    "mapping_files": [],
    "type": "dariah"
})

# 3. Dariah Vocabs (TTL files)
dariah_ttl_files = [
    ("backbone_thesaurus", "Backbone Thesaurus.ttl"),
    ("dyas_humanities", "DYAS Humanities Thesaurus .ttl"),
    ("gemet_concepts", "GEMET - Concepts.ttl"),
    ("oeai_cultural_periods", "OeAI Thesaurus - Cultural Time Periods.ttl"),
    ("oeai_materials", "OeAI Thesaurus - Materials.ttl"),
]


# 5. FISH Vocabularies (skip orphanedConcepts)
fish_files = [
    ("fish_archaeological_objects", "FISH_Archaeological_Objects_20260204_Full.rdf"),
    ("fish_archaeological_sciences", "FISH_Archaeological_Sciences_20260409.rdf"),
    ("fish_building_materials", "Building_Materials_20260209.rdf"),
    ("fish_climate_hazards", "FISH_Climate_Hazards_Vocab_20240327.rdf"),
    ("fish_components", "FISH_Components_20260206.rdf"),
    ("fish_evidence", "Evidence_20250417.rdf"),
    ("fish_event_type", "FISH_Event_Type_20260205.rdf"),
    ("fish_heritage_crime", "FISH_Heritage_Crime_20190718.rdf"),
    ("fish_historic_characterization", "FISH_Historic_Characterization_20240701.rdf"),
    ("fish_maritime_cargo", "FISH_MaritimeCargo_20190717.rdf"),
    ("fish_maritime_craft", "FISH_Maritime_craft_20190718.rdf"),
    ("fish_monument_type", "MonumentType_20260819.rdf"),
    ("fish_farmsteads", "Farmsteads_20190718.rdf"),
    ("fish_fixtures_fittings", "Fixtures_Fittings_20220609.rdf"),
    ("fish_he_maritime_object_material", "HE_MaritimeObjectMaterial_20250620.rdf"),
    ("fish_maritime_placename", "Maritime_placename_20220314.rdf"),
]
for name, filename in fish_files:
    VOCABULARIES.append({
        "output": f"{name}_standardized.ttl",
        "sources": [FISH_DIR / filename],
        "scheme_override": None,
        "mapping_files": [ADS_AAT_MAPPINGS],
        "type": "fish"
    })

# 6. Wortnetz Kultur
VOCABULARIES.append({
    "output": "wortnetz_kultur_standardized.ttl",
    "sources": [BASE_DIR / "Wortnetz Kultur.rdf"],
    "scheme_override": "http://lvr.vocnet.org/wnk",
    "mapping_files": [],
    "type": "wortnetz"
})

# Do not process orphanedConcepts - only 3 concepts, skip as discussed

# ============================================================
# UTILITY FUNCTIONS
# ============================================================

def ensure_dir(path):
    """Create directory if it doesn't exist."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path

def load_graph(filepath):
    """Load an RDF file into a Graph."""
    g = Graph()
    try:
        if filepath.suffix.lower() in ['.ttl', '.turtle']:
            g.parse(str(filepath), format='turtle')
        elif filepath.suffix.lower() == '.rdf':
            g.parse(str(filepath), format='xml')
        else:
            g.parse(str(filepath))
        return g
    except Exception as e:
        print(f"  WARNING: Could not load {filepath}: {e}")
        return g

def save_graph(graph, filepath):
    """Save a Graph to a Turtle file."""
    ensure_dir(filepath.parent)
    graph.bind("skos", SKOS)
    graph.bind("rdf", RDF)
    graph.bind("rdfs", RDFS)
    graph.bind("dc", DC)
    graph.bind("dcterms", DCTERMS)
    graph.bind("owl", OWL)
    turtle_str = graph.serialize(format='turtle', encoding='utf-8')
    if isinstance(turtle_str, bytes):
        turtle_str = turtle_str.decode('utf-8')
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(turtle_str)

def get_scheme_info(graph):
    """
    Extract ConceptScheme information from a graph.
    Returns (scheme_uri, scheme_labels_dict)
    """
    scheme_uris = list(graph.subjects(RDF.type, SKOS.ConceptScheme))
    
    if not scheme_uris:
        for s in graph.subjects():
            if 'scheme' in str(s).lower():
                scheme_uris.append(s)
    
    if not scheme_uris:
        return None, {}
    
    scheme_uri = scheme_uris[0]
    labels = {}
    
    for label in graph.objects(scheme_uri, SKOS.prefLabel):
        if hasattr(label, 'language'):
            labels[label.language] = str(label)
    
    for title in graph.objects(scheme_uri, DC.title):
        if hasattr(title, 'language'):
            labels[title.language] = str(title)
    
    for title in graph.objects(scheme_uri, DCTERMS.title):
        if hasattr(title, 'language'):
            labels[title.language] = str(title)
    
    return scheme_uri, labels


# ============================================================
# FILTERING FUNCTIONS
# ============================================================

def filter_graph_languages(graph, keep_langs=['en', 'de']):
    """
    Filter a graph to keep only labels with specified languages.
    Keeps non-language literals and any triples that aren't labels.
    """
    new_graph = Graph()
    keep_langs_set = set(keep_langs)
    
    language_properties = [SKOS.prefLabel, SKOS.altLabel, SKOS.definition, 
                          SKOS.scopeNote, DCTERMS.description]
    
    for s, p, o in graph:
        # For language-specific properties, filter by language
        if p in language_properties and isinstance(o, Literal):
            if not hasattr(o, 'language') or o.language in keep_langs_set:
                new_graph.add((s, p, o))
        else:
            # Keep all other triples
            new_graph.add((s, p, o))
    
    return new_graph


# ============================================================
# SKOS PROCESSING FUNCTIONS
# ============================================================

def ensure_in_scheme(graph, scheme_uri):
    """Ensure all Concepts have skos:inScheme pointing to the scheme URI."""
    scheme_uri = URIRef(scheme_uri) if isinstance(scheme_uri, str) else scheme_uri
    for concept in graph.subjects(RDF.type, SKOS.Concept):
        if not (concept, SKOS.inScheme, scheme_uri) in graph:
            graph.add((concept, SKOS.inScheme, scheme_uri))
    return graph

def normalize_hierarchy(graph):
    """
    Normalize hierarchical relations:
    - Keep skos:broader
    - Convert skos:narrower to skos:broader (inverse)
    - Preserve existing topConceptOf/hasTopConcept
    """
    new_graph = Graph()
    
    for s, p, o in graph:
        if p == SKOS.narrower:
            # Convert to broader on the target
            new_graph.add((o, SKOS.broader, s))
        else:
            new_graph.add((s, p, o))
    
    return new_graph



def merge_definition_and_scope_note(graph):
    """
    For concepts without skos:definition, promote skos:scopeNote to definition.
    """
    for concept in list(graph.subjects(RDF.type, SKOS.Concept)):
        # Check if concept has any definition
        has_definition = any(True for _ in graph.objects(concept, SKOS.definition))
        
        if not has_definition:
            # Get all scope notes
            scope_notes = list(graph.triples((concept, SKOS.scopeNote, None)))
            if scope_notes:
                # Remove scope note triples
                for s, p, o in scope_notes:
                    graph.remove((s, p, o))
                # Add as definitions
                for s, p, o in scope_notes:
                    graph.add((s, SKOS.definition, o))
    
    return graph


# ============================================================
# MAPPING INTEGRATION
# ============================================================

def integrate_mappings(graph, mapping_file):
    """
    Integrate mapping triples from a mapping file into the graph.
    Only adds mappings for concepts that exist in the graph or are referenced.
    """
    if not mapping_file.exists():
        return graph
    
    mapping_graph = load_graph(mapping_file)
    
    # Properties to integrate
    mapping_predicates = [
        SKOS.exactMatch, SKOS.closeMatch, SKOS.broadMatch,
        SKOS.narrowMatch, SKOS.relatedMatch, SKOS.related
    ]
    
    # Integrate all mapping properties
    for s, p, o in mapping_graph:
        if p in mapping_predicates:
            graph.add((s, p, o))
    
    return graph


def get_scheme_labels_from_file(filepath):
    """Extract scheme label from a separate file."""
    if not filepath or not filepath.exists():
        return {}
    g = load_graph(filepath)
    _, labels = get_scheme_info(g)
    return labels


# ============================================================
# MAIN PROCESSING
# ============================================================

def process_vocabulary(vocab_def):
    """Process a single vocabulary definition and save to output."""
    output_file = STANDARDIZED_DIR / vocab_def["output"]
    sources = vocab_def["sources"]
    scheme_override = vocab_def["scheme_override"]
    mapping_files = vocab_def.get("mapping_files", [])
    scheme_label_source = vocab_def.get("scheme_label_source")
    
    print(f"\nProcessing {output_file.name}...")
    
    # Load all source files
    main_graph = Graph()
    for source_file in sources:
        if not source_file.exists():
            print(f"  WARNING: Source file not found: {source_file}")
            continue
        print(f"  Loading {source_file.name}...")
        g = load_graph(source_file)
        main_graph += g
    
    if len(main_graph) == 0:
        print(f"  SKIPPED: No data loaded")
        return
    
    # Get scheme info
    scheme_result = get_scheme_info(main_graph)
    if scheme_result[0] is None:
        scheme_uri = None
        scheme_labels = scheme_result[1]
    else:
        scheme_uri, scheme_labels = scheme_result
    
    # Override scheme URI if specified
    if scheme_override:
        scheme_uri = URIRef(scheme_override)
        # Try to get labels from override source
        if scheme_label_source and Path(scheme_label_source).exists():
            override_labels = get_scheme_labels_from_file(Path(scheme_label_source))
            scheme_labels.update(override_labels)
    
    # Step 1: Filter to en/de languages only
    print(f"  Filtering languages...")
    main_graph = filter_graph_languages(main_graph, keep_langs=['en', 'de'])
    
    # Step 2: Normalize hierarchy (broader only, remove narrower)
    print(f"  Normalizing hierarchy...")
    main_graph = normalize_hierarchy(main_graph)
    
    # Step 3: Merge definition and scopeNote
    print(f"  Merging definitions...")
    main_graph = merge_definition_and_scope_note(main_graph)
    
    # Step 4: Integrate mappings
    for mapping_file in mapping_files:
        if mapping_file.exists():
            print(f"  Integrating mappings from {mapping_file.name}...")
            main_graph = integrate_mappings(main_graph, mapping_file)
    
    # Step 5: Ensure inScheme
    if scheme_uri:
        print(f"  Setting inScheme to {scheme_uri}...")
        main_graph = ensure_in_scheme(main_graph, scheme_uri)
        
        # Add scheme labels if we have them
        if scheme_labels:
            for lang, label in scheme_labels.items():
                if lang in ['en', 'de']:
                    main_graph.add((scheme_uri, SKOS.prefLabel, Literal(label, lang=lang)))
        else:
            # Try to get title from dc:title or similar
            for title in main_graph.objects(scheme_uri, DC.title):
                if hasattr(title, 'language') and title.language in ['en', 'de']:
                    main_graph.add((scheme_uri, SKOS.prefLabel, title))
            for title in main_graph.objects(scheme_uri, DCTERMS.title):
                if hasattr(title, 'language') and title.language in ['en', 'de']:
                    main_graph.add((scheme_uri, SKOS.prefLabel, title))
    else:
        print(f"  WARNING: No ConceptScheme found")
    
    # Step 6: Ensure top concepts are properly linked
    main_graph = ensure_top_concepts(main_graph)
    
    # Save output
    print(f"  Saving to {output_file.name}...")
    save_graph(main_graph, output_file)
    print(f"  DONE: {len(main_graph)} triples")


def main():
    """Main processing function."""
    print("=" * 70)
    print("STANDARDIZING SKOS VOCABULARIES")
    print("=" * 70)
    
    # Create output directory
    ensure_dir(STANDARDIZED_DIR)
    
    # Process all vocabularies
    total_processed = 0
    total_skipped = 0
    
    for vocab_def in VOCABULARIES:
        try:
            process_vocabulary(vocab_def)
            total_processed += 1
        except Exception as e:
            print(f"\nERROR processing {vocab_def.get('output', 'unknown')}: {e}")
            import traceback
            traceback.print_exc()
            total_skipped += 1
    
    print("\n" + "=" * 70)
    print(f"FINISHED")
    print(f"  Processed: {total_processed}")
    print(f"  Skipped: {total_skipped}")
    print(f"  Output: {STANDARDIZED_DIR}")
    print("=" * 70)


if __name__ == "__main__":
    main()
def ensure_top_concepts(graph):
    """
    Ensure top concepts are properly linked to their scheme.
    For concepts with skos:topConceptOf or referenced by hasTopConcept,
    ensure they have skos:inScheme and the relation is preserved.
    """
    # Find all top concepts (via topConceptOf)
    top_concepts = set()
    for s, p, o in graph:
        if p == SKOS.topConceptOf:
            top_concepts.add(s)
    
    # Also find via hasTopConcept
    for scheme_uri in graph.subjects(SKOS.hasTopConcept, None):
        for concept in graph.objects(scheme_uri, SKOS.hasTopConcept):
            top_concepts.add(concept)
    
    return graph
    if not scheme_uris:
        return None, {}
    
    scheme_uri = scheme_uris[0]
    labels = {}
    
    for label in graph.objects(scheme_uri, SKOS.prefLabel):
        if hasattr(label, 'language'):
            labels[label.language] = str(label)
    
    for title in graph.objects(scheme_uri, DC.title):
        if hasattr(title, 'language'):
            labels[title.language] = str(title)
    
    for title in graph.objects(scheme_uri, DCTERMS.title):
        if hasattr(title, 'language'):
            labels[title.language] = str(title)
    
    return scheme_uri, labels
