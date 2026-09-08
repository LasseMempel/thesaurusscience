#!/usr/bin/env python3
"""
Normalize SKOS vocabularies from multiple sources into a consistent format.

This script reads TTL and RDF XML files from specified directories, normalizes
the SKOS vocabularies, and outputs them as clean Turtle files.

Normalization rules:
- Concept Schemes: title or prefLabel, hasTopConcept
- Concepts: prefLabel (all en/de variants), altLabel, definition, scopeNote, 
            broader (flip narrower), inScheme, topConceptOf
- Mappings: exactMatch, closeMatch, relatedMatch, related, broadMatch, narrowMatch
- Labels: Only English (@en) or German (@de)

Inference rules:
- Infer topConceptOf from hasTopConcept (if scheme hasTopConcept X, add X topConceptOf scheme)
- Infer hasTopConcept from topConceptOf
- Ensure all concepts have inScheme
- Warn if concept has neither broader nor topConceptOf
"""

import os
import sys
import logging
import warnings
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any
from rdflib import Graph, Namespace, URIRef, Literal, BNode
from rdflib.namespace import RDF, RDFS, SKOS, DCTERMS, DC, XSD

# Suppress rdflib warnings about invalid dateTime formats and other parsing issues
logging.getLogger('rdflib').setLevel(logging.ERROR)
warnings.filterwarnings('ignore', category=RuntimeWarning, module='rdflib')
warnings.filterwarnings('ignore', message='.*isoformat.*')

# Define namespaces
SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")
DCTERMS = Namespace("http://purl.org/dc/terms/")
DC = Namespace("http://purl.org/dc/elements/1.1/")
RDFS = Namespace("http://www.w3.org/2000/01/rdf-schema#")

# Allowed languages for labels
ALLOWED_LANGS = {"en", "de"}  # Only English and German

# Input directories
INPUT_DIRS = [
    Path("/home/mempellaenger/repos/thesaurusscience/Dariah Vocabs"),
    Path("/home/mempellaenger/repos/thesaurusscience/FISH"),
    Path("/home/mempellaenger/repos/thesaurusscience/Wortnetz"),
]

# Output directory
OUTPUT_DIR = Path("/home/mempellaenger/repos/thesaurusscience/Output/Vocabularies")

# Mapping properties to preserve
MAPPING_PROPERTIES = [
    SKOS.exactMatch,
    SKOS.closeMatch,
    SKOS.relatedMatch,
    SKOS.related,
    SKOS.broadMatch,
    SKOS.narrowMatch,
]


def is_valid_uri(uri: Any) -> bool:
    """Check if a URI is valid for serialization."""
    if isinstance(uri, URIRef):
        uri_str = str(uri)
        # Check for spaces or other invalid characters in URI
        if ' ' in uri_str or uri_str.startswith('file:///'):
            return False
        # Basic URI pattern check
        try:
            URIRef(uri_str)
            return True
        except:
            return False
    return True


def is_valid_literal(literal: Literal) -> bool:
    """Check if a literal is valid (no malformed dateTime)."""
    if isinstance(literal, Literal):
        datatype = literal.datatype
        if datatype and 'dateTime' in str(datatype):
            # Check for malformed dateTime (double timezone)
            if re.search(r'\+\d{2}:\d{2}\+\d{2}:\d{2}', str(literal)):
                return False
    return True


def is_allowed_language(literal: Literal) -> bool:
    """Check if a literal has an allowed language (en or de)."""
    if isinstance(literal, Literal):
        lang = literal.language
        return lang in ALLOWED_LANGS
    return True


def filter_labels_by_language(graph: Graph, subject: URIRef, predicate: URIRef) -> List[Literal]:
    """Get all objects for a subject/predicate, filtering to allowed languages."""
    labels = []
    for obj in graph.objects(subject, predicate):
        if isinstance(obj, Literal):
            if is_allowed_language(obj) and is_valid_literal(obj):
                labels.append(obj)
        else:
            labels.append(obj)
    return labels


def extract_concept_schemes(graph: Graph) -> Dict[URIRef, dict]:
    """Extract all Concept Schemes from the graph."""
    schemes = {}
    
    for scheme in graph.subjects(RDF.type, SKOS.ConceptScheme):
        scheme_data = {
            "uri": scheme,
            "title": None,
            "prefLabel": [],
            "hasTopConcept": [],
            "other_properties": [],
        }
        
        # Get title (dc:title or dct:title) - filtered by language
        for title_pred in [DCTERMS.title, DC.title]:
            titles = filter_labels_by_language(graph, scheme, title_pred)
            for title in titles:
                scheme_data["title"] = title
                break
            if scheme_data["title"]:
                break
        
        # Get skos:prefLabel - keep ALL en/de variants
        for label in filter_labels_by_language(graph, scheme, SKOS.prefLabel):
            scheme_data["prefLabel"].append(label)
        
        # Get hasTopConcept
        for top_concept in graph.objects(scheme, SKOS.hasTopConcept):
            if is_valid_uri(top_concept):
                scheme_data["hasTopConcept"].append(top_concept)
        
        # Store other relevant properties
        for pred, obj in graph.predicate_objects(scheme):
            if pred not in [RDF.type, DCTERMS.title, DC.title, SKOS.prefLabel, SKOS.hasTopConcept]:
                if isinstance(obj, Literal) and is_allowed_language(obj) and is_valid_literal(obj):
                    scheme_data["other_properties"].append((pred, obj))
                elif isinstance(obj, URIRef) and is_valid_uri(obj):
                    scheme_data["other_properties"].append((pred, obj))
        
        schemes[scheme] = scheme_data
    
    return schemes


def extract_concepts(graph: Graph, concept_scheme: Optional[URIRef] = None) -> Dict[URIRef, dict]:
    """Extract all Concepts from the graph, optionally filtered by concept scheme."""
    concepts = {}
    
    for concept in graph.subjects(RDF.type, SKOS.Concept):
        if concept_scheme:
            in_scheme = False
            for scheme in graph.objects(concept, SKOS.inScheme):
                if scheme == concept_scheme:
                    in_scheme = True
                    break
            if not in_scheme:
                for scheme in graph.objects(concept, SKOS.topConceptOf):
                    if scheme == concept_scheme:
                        in_scheme = True
                        break
            if not in_scheme:
                continue
        
        concept_data = {
            "uri": concept,
            "prefLabel": [],
            "altLabel": [],
            "definition": [],
            "scopeNote": [],
            "broader": [],
            "narrower": [],
            "inScheme": [],
            "topConceptOf": [],
            "mappings": [],
        }
        
        # Get prefLabel - keep ALL en/de variants
        for label in filter_labels_by_language(graph, concept, SKOS.prefLabel):
            concept_data["prefLabel"].append(label)
        
        # Get altLabel - keep ALL en/de variants
        for label in filter_labels_by_language(graph, concept, SKOS.altLabel):
            concept_data["altLabel"].append(label)
        
        # Get definition - keep ALL en/de variants
        for defin in filter_labels_by_language(graph, concept, SKOS.definition):
            concept_data["definition"].append(defin)
        
        # Get scopeNote - keep ALL en/de variants
        for note in filter_labels_by_language(graph, concept, SKOS.scopeNote):
            concept_data["scopeNote"].append(note)
        
        # Get broader relations
        for broader in graph.objects(concept, SKOS.broader):
            if is_valid_uri(broader):
                concept_data["broader"].append(broader)
        
        # Get narrower relations (will be flipped)
        for narrower in graph.objects(concept, SKOS.narrower):
            if is_valid_uri(narrower):
                concept_data["narrower"].append(narrower)
        
        # Get inScheme
        for scheme in graph.objects(concept, SKOS.inScheme):
            if is_valid_uri(scheme):
                concept_data["inScheme"].append(scheme)
        
        # Get topConceptOf
        for scheme in graph.objects(concept, SKOS.topConceptOf):
            if is_valid_uri(scheme):
                concept_data["topConceptOf"].append(scheme)
        
        # Get mapping properties
        for mapping_pred in MAPPING_PROPERTIES:
            for target in graph.objects(concept, mapping_pred):
                if is_valid_uri(target):
                    concept_data["mappings"].append((mapping_pred, target))
        
        concepts[concept] = concept_data
    
    return concepts


def infer_topconcept_relations(concept_schemes: Dict[URIRef, dict], concepts: Dict[URIRef, dict]) -> None:
    """Infer topConceptOf from hasTopConcept and vice versa."""
    for scheme_uri, scheme_data in concept_schemes.items():
        for top_concept_uri in scheme_data["hasTopConcept"]:
            if top_concept_uri in concepts:
                if scheme_uri not in concepts[top_concept_uri]["topConceptOf"]:
                    concepts[top_concept_uri]["topConceptOf"].append(scheme_uri)
    
    for concept_uri, concept_data in concepts.items():
        for scheme_uri in concept_data["topConceptOf"]:
            if scheme_uri in concept_schemes:
                if concept_uri not in concept_schemes[scheme_uri]["hasTopConcept"]:
                    concept_schemes[scheme_uri]["hasTopConcept"].append(concept_uri)


def ensure_inscheme(concept_schemes: Dict[URIRef, dict], concepts: Dict[URIRef, dict]) -> None:
    """Ensure all concepts have inScheme property."""
    for concept_uri, concept_data in concepts.items():
        if not concept_data["inScheme"]:
            for scheme_uri in concept_data["topConceptOf"]:
                if scheme_uri in concept_schemes:
                    concept_data["inScheme"].append(scheme_uri)
            
            if not concept_data["inScheme"]:
                for scheme_uri, scheme_data in concept_schemes.items():
                    if concept_uri in scheme_data["hasTopConcept"]:
                        concept_data["inScheme"].append(scheme_uri)


def flip_narrower_relations(concepts: Dict[URIRef, dict]) -> None:
    """Flip narrower relations to broader relations."""
    for concept_uri, concept_data in concepts.items():
        for narrower_uri in concept_data["narrower"]:
            if narrower_uri in concepts:
                if concept_uri not in concepts[narrower_uri]["broader"]:
                    concepts[narrower_uri]["broader"].append(concept_uri)


def validate_concepts(concepts: Dict[URIRef, dict], source_file: str) -> List[str]:
    """Validate that each concept has either broader or topConceptOf."""
    warnings = []
    for concept_uri, concept_data in concepts.items():
        has_broader = len(concept_data["broader"]) > 0
        has_topconceptof = len(concept_data["topConceptOf"]) > 0
        
        if not has_broader and not has_topconceptof:
            pref_label = concept_data["prefLabel"][0] if concept_data["prefLabel"] else str(concept_uri)
            warnings.append(
                f"[{source_file}] Concept {concept_uri} ({pref_label}) has neither broader nor topConceptOf"
            )
    
    return warnings

        

def create_normalized_graph(
    concept_schemes: Dict[URIRef, dict],
    concepts: Dict[URIRef, dict],
    source_name: str
) -> Graph:
    """Create a normalized RDF graph from extracted data."""
    graph = Graph()
    
    # Bind common namespaces
    graph.bind("skos", SKOS)
    graph.bind("dct", DCTERMS)
    graph.bind("dc", DC)
    graph.bind("rdfs", RDFS)
    
    # Add concept schemes
    for scheme_uri, scheme_data in concept_schemes.items():
        if not is_valid_uri(scheme_uri):
            continue
        graph.add((scheme_uri, RDF.type, SKOS.ConceptScheme))
        
        if scheme_data["title"]:
            graph.add((scheme_uri, DCTERMS.title, scheme_data["title"]))
        
        for label in scheme_data["prefLabel"]:
            graph.add((scheme_uri, SKOS.prefLabel, label))
        
        for top_concept in scheme_data["hasTopConcept"]:
            if is_valid_uri(top_concept):
                graph.add((scheme_uri, SKOS.hasTopConcept, top_concept))
        
        for pred, obj in scheme_data["other_properties"]:
            if isinstance(obj, Literal) and is_valid_literal(obj):
                graph.add((scheme_uri, pred, obj))
            elif isinstance(obj, URIRef) and is_valid_uri(obj):
                graph.add((scheme_uri, pred, obj))
    
    # Add concepts
    for concept_uri, concept_data in concepts.items():
        if not is_valid_uri(concept_uri):
            continue
        graph.add((concept_uri, RDF.type, SKOS.Concept))
        
        for label in concept_data["prefLabel"]:
            graph.add((concept_uri, SKOS.prefLabel, label))
        
        for label in concept_data["altLabel"]:
            graph.add((concept_uri, SKOS.altLabel, label))
        
        for defin in concept_data["definition"]:
            graph.add((concept_uri, SKOS.definition, defin))
        
        for note in concept_data["scopeNote"]:
            graph.add((concept_uri, SKOS.scopeNote, note))
        
        for broader in concept_data["broader"]:
            if is_valid_uri(broader):
                graph.add((concept_uri, SKOS.broader, broader))
        
        for scheme in concept_data["inScheme"]:
            if is_valid_uri(scheme):
                graph.add((concept_uri, SKOS.inScheme, scheme))
        
        for scheme in concept_data["topConceptOf"]:
            if is_valid_uri(scheme):
                graph.add((concept_uri, SKOS.topConceptOf, scheme))
        
        for mapping_pred, target in concept_data["mappings"]:
            if is_valid_uri(target):
                graph.add((concept_uri, mapping_pred, target))
    
    return graph


def process_file(file_path: Path, output_dir: Path) -> Optional[Path]:
    """Process a single TTL or RDF file and return the output path."""
    print(f"\nProcessing: {file_path}")
    
    file_ext = file_path.suffix.lower()
    if file_ext == ".ttl":
        rdf_format = "turtle"
    elif file_ext in [".rdf", ".xml"]:
        rdf_format = "xml"
    else:
        print(f"  Skipping unknown format: {file_ext}")
        return None
    
    try:
        graph = Graph()
        graph.parse(str(file_path), format=rdf_format)
    except Exception as e:
        print(f"  Error parsing file: {e}")
        return None
    
    concept_schemes = extract_concept_schemes(graph)
    if not concept_schemes:
        print(f"  No concept schemes found, skipping")
        return None
    
    print(f"  Found {len(concept_schemes)} concept scheme(s)")
    
    all_concepts = {}
    for scheme_uri in concept_schemes.keys():
        concepts = extract_concepts(graph, scheme_uri)
        all_concepts.update(concepts)
    
    if not all_concepts:
        all_concepts = extract_concepts(graph)
    
    print(f"  Found {len(all_concepts)} concept(s)")
    
    infer_topconcept_relations(concept_schemes, all_concepts)
    ensure_inscheme(concept_schemes, all_concepts)
    flip_narrower_relations(all_concepts)
    
    warnings = validate_concepts(all_concepts, file_path.name)
    for warning in warnings:
        print(f"  WARNING: {warning}")
    
    source_name = file_path.stem
    normalized_graph = create_normalized_graph(concept_schemes, all_concepts, source_name)
    
    output_filename = f"{source_name}_normalized.ttl"
    output_path = output_dir / output_filename
    
    try:
        turtle_content = normalized_graph.serialize(format="turtle")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(turtle_content)
        print(f"  Output written to: {output_path}")
        return output_path
    except Exception as e:
        print(f"  Error writing output: {e}")
        print(f"  Skipping file due to serialization issues (invalid URIs or data)")
        return None


def find_vocab_files(directories: List[Path]) -> List[Path]:
    """Find all TTL and RDF files in the given directories."""
    vocab_files = []
    
    for directory in directories:
        if not directory.exists():
            print(f"Warning: Directory does not exist: {directory}")
            continue
        
        for file_path in directory.rglob("*"):
            if file_path.is_file():
                ext = file_path.suffix.lower()
                if ext in [".ttl", ".rdf", ".xml"]:
                    if "__pycache__" in str(file_path):
                        continue
                    if "orphanedConcepts" in str(file_path):
                        continue
                    vocab_files.append(file_path)
    
    return vocab_files


def main():
    """Main entry point."""
    print("=" * 70)
    print("SKOS Vocabulary Normalizer")
    print("=" * 70)
    print("\nNormalization rules:")
    print("  - Labels: Only English (@en) and German (@de)")
    print("  - Keep all en/de prefLabel variants per concept")
    print("  - Flip narrower relations to broader")
    print("  - Infer topConceptOf from hasTopConcept and vice versa")
    print("  - Ensure all concepts have inScheme")
    print("  - Preserve mappings: exactMatch, closeMatch, relatedMatch,")
    print("                       related, broadMatch, narrowMatch")
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    print("\n" + "=" * 70)
    print("Searching for vocabulary files...")
    vocab_files = find_vocab_files(INPUT_DIRS)
    print(f"Found {len(vocab_files)} vocabulary file(s)")
    
    if not vocab_files:
        print("No vocabulary files found. Exiting.")
        sys.exit(1)
    
    print("\n" + "=" * 70)
    print("Processing files...")
    print("=" * 70)
    successful = 0
    failed = 0
    
    for file_path in vocab_files:
        result = process_file(file_path, OUTPUT_DIR)
        if result:
            successful += 1
        else:
            failed += 1
    
    print("\n" + "=" * 70)
    print("Summary")
    print("=" * 70)
    print(f"Files processed successfully: {successful}")
    print(f"Files failed/skipped: {failed}")
    print(f"Output directory: {OUTPUT_DIR}")
    
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
