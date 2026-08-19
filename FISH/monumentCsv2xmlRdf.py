#!/usr/bin/env python3
"""
Convert FISH ThesaurusTerms.csv to SKOS RDF/XML vocabulary.

Generates a SKOS vocabulary from:
- /home/mempellaenger/repos/thesaurusscience/FISH/MonumentTypeV29/ThesaurusTerms.csv
- /home/mempellaenger/repos/thesaurusscience/FISH/MonumentTypeV29/ThesaurusTermUses.csv (for hierarchy)

Output: MonumentType_20260819.rdf matching other FISH vocabulary files
"""

import csv
from datetime import datetime
from pathlib import Path
import rdflib
from rdflib import URIRef, Literal

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path("/home/mempellaenger/repos/thesaurusscience/FISH/MonumentTypeV29")
INPUT_CSV = BASE_DIR / "ThesaurusTerms.csv"
HIERARCHY_CSV = BASE_DIR / "ThesaurusTermUses.csv"
PREFERENCES_CSV = BASE_DIR / "ThesaurusTermPreferences.csv"
OUTPUT_RDF = Path("/home/mempellaenger/repos/thesaurusscience/FISH/MonumentType_20260819.rdf")

# Namespace definitions
SCHEME_URI = "http://purl.org/heritagedata/schemes/eh_tmt2"
CONCEPT_BASE_URI = "http://purl.org/heritagedata/schemes/eh_tmt2/concepts/"

# Metadata values
METADATA = {
    "license": "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
    "attribution_url": "https://historicengland.org.uk/",
    "attribution_name": "Historic England",
    "publisher": "https://historicengland.org.uk/",
    "spatial": [
        "http://dbpedia.org/resource/England",
        "http://vocab.getty.edu/tgn/7002445",
    ],
    "issued_date": datetime.now().strftime("%Y-%m-%d"),
    "issued_datetime": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
}


def read_terms_csv(filepath):
    """Read ThesaurusTerms.csv and return list of dicts with THE_TE_UID, TERM, SCOPE_NOTE, STATUS."""
    terms = []
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            terms.append({
                "THE_TE_UID": row["THE_TE_UID"].strip(),
                "TERM": row["TERM"].strip(),
                "SCOPE_NOTE": row["SCOPE_NOTE"].strip(),
                "STATUS": row["STATUS"].strip(),
            })
    return terms


def read_preferences_csv(filepath):
    """Read ThesaurusTermPreferences.csv and build non-preferred -> preferred mapping."""
    non_preferred_to_preferred = {}
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            uid1 = row["THE_TE_UID_1"].strip()
            uid2 = row["THE_TE_UID_2"].strip()
            if uid1 not in non_preferred_to_preferred:
                non_preferred_to_preferred[uid1] = []
            non_preferred_to_preferred[uid1].append(uid2)
    return non_preferred_to_preferred


def read_hierarchy_csv(filepath):
    """Read ThesaurusTermUses.csv and build hierarchy mappings."""
    term_uses = {}
    uid_to_term = {}
    
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            th_t_u_uid = row["TH_T_U_UID"].strip()
            term = row["TERM"].strip()
            broad_term_uid = row["BROAD_TERM_U_UID"].strip() if row["BROAD_TERM_U_UID"] else ""
            
            uid_to_term[th_t_u_uid] = term
            
            if term not in term_uses:
                term_uses[term] = []
            term_uses[term].append((th_t_u_uid, broad_term_uid))
    
    term_to_broader = {}
    for term, uses in term_uses.items():
        broader_terms = set()
        for th_t_u_uid, broad_term_uid in uses:
            if broad_term_uid:
                broader_term = uid_to_term.get(broad_term_uid, "")
                if broader_term:
                    broader_terms.add(broader_term)
        if broader_terms:
            term_to_broader[term] = broader_terms
    
    return term_to_broader, term_uses, uid_to_term


def build_termid_mapping(terms):
    """Build mapping from TERM to THE_TE_UID and from UID to TERM."""
    term_to_uid = {}
    uid_to_term = {}
    for term_info in terms:
        uid = term_info["THE_TE_UID"]
        term_name = term_info["TERM"]
        term_to_uid[term_name] = uid
        uid_to_term[uid] = term_name
    return term_to_uid, uid_to_term


def main():
    print("Reading input files...")
    
    all_terms = read_terms_csv(INPUT_CSV)
    print(f"  Loaded {len(all_terms)} terms from {INPUT_CSV}")
    
    term_to_broader, term_uses, uid_to_term_from_uses = read_hierarchy_csv(HIERARCHY_CSV)
    print(f"  Loaded hierarchy from {HIERARCHY_CSV}")
    print(f"  Found {len(term_to_broader)} terms with broader relationships")
    
    non_preferred_to_preferred = read_preferences_csv(PREFERENCES_CSV)
    print(f"  Loaded preferences from {PREFERENCES_CSV}")
    print(f"  Found {len(non_preferred_to_preferred)} non-preferred term mappings")
    
    term_to_uid, uid_to_term = build_termid_mapping(all_terms)
    
    # Separate terms into preferred and non-preferred
    preferred_terms = [t for t in all_terms if t["STATUS"] == "P"]
    non_preferred_terms = [t for t in all_terms if t["STATUS"] == "N"]
    print(f"  Preferred terms: {len(preferred_terms)}, Non-preferred terms: {len(non_preferred_terms)}")
    
    # Build mappings for preferred terms only
    pref_term_to_uid = {t["TERM"]: t["THE_TE_UID"] for t in preferred_terms}
    pref_uid_to_term = {t["THE_TE_UID"]: t["TERM"] for t in preferred_terms}
    
    # Build concept URI cache
    pref_concept_uris = {t["THE_TE_UID"]: URIRef(CONCEPT_BASE_URI + t["THE_TE_UID"]) 
                         for t in preferred_terms}
    
    g = rdflib.Graph()
    
    SKOS = rdflib.Namespace("http://www.w3.org/2004/02/skos/core#")
    DC = rdflib.Namespace("http://purl.org/dc/terms/")
    CC = rdflib.Namespace("http://creativecommons.org/ns#")
    
    g.bind("skos", SKOS)
    g.bind("dc", DC)
    g.bind("cc", CC)
    
    scheme = URIRef(SCHEME_URI)
    
    print("Creating ConceptScheme...")
    
    g.add((scheme, rdflib.RDF.type, SKOS.ConceptScheme))
    g.add((scheme, DC.title, Literal("FISH Thesaurus of Monument Types", lang="en")))
    g.add((scheme, DC.description, Literal("Terminology relating to the built and buried heritage of the British Isles and used for recording sites, monuments, buildings and structures.", lang="en")))
    g.add((scheme, DC.identifier, URIRef(SCHEME_URI)))
    g.add((scheme, DC.issued, Literal(METADATA["issued_date"], datatype=rdflib.XSD.date)))
    g.add((scheme, CC.license, URIRef(METADATA["license"])))
    g.add((scheme, CC.attributionURL, URIRef(METADATA["attribution_url"])))
    g.add((scheme, CC.attributionName, Literal(METADATA["attribution_name"], lang="en")))
    g.add((scheme, DC.publisher, URIRef(METADATA["publisher"])))
    for spatial in METADATA["spatial"]:
        g.add((scheme, DC.spatial, URIRef(spatial)))
    
    # Find top-level concepts among preferred terms only
    top_concepts = set()
    for term, uses in term_uses.items():
        for th_t_u_uid, broad_term_uid in uses:
            if not broad_term_uid:
                if term in pref_term_to_uid:
                    top_concepts.add(term)
    
    for top_term in top_concepts:
        top_uid = pref_term_to_uid[top_term]
        g.add((scheme, SKOS.hasTopConcept, pref_concept_uris[top_uid]))
    
    print(f"  Added {len(top_concepts)} top-level concepts to scheme")
    
    print("Creating Concepts...")
    
    # First pass: create all preferred term concepts
    concepts_created = 0
    alt_labels_added = 0
    
    # Create concept URIs and basic info for preferred terms
    for term_info in preferred_terms:
        uid = term_info["THE_TE_UID"]
        term_name = term_info["TERM"]
        scope_note = term_info["SCOPE_NOTE"]
        
        concept = pref_concept_uris[uid]
        
        g.add((concept, rdflib.RDF.type, SKOS.Concept))
        g.add((concept, SKOS.inScheme, scheme))
        g.add((concept, SKOS.prefLabel, Literal(term_name, lang="en")))
        
        if scope_note and scope_note != "-":
            g.add((concept, SKOS.scopeNote, Literal(scope_note, lang="en")))
        
        # Add metadata
        g.add((concept, DC.identifier, URIRef(CONCEPT_BASE_URI + uid)))
        g.add((concept, DC.issued, Literal(METADATA["issued_datetime"], datatype=rdflib.XSD.dateTime)))
        g.add((concept, CC.license, URIRef(METADATA["license"])))
        g.add((concept, CC.attributionURL, URIRef(METADATA["attribution_url"])))
        g.add((concept, CC.attributionName, Literal(METADATA["attribution_name"], lang="en")))
        g.add((concept, DC.publisher, URIRef(METADATA["publisher"])))
        for spatial in METADATA["spatial"]:
            g.add((concept, DC.spatial, URIRef(spatial)))
        
        concepts_created += 1
    
    print(f"  Created {concepts_created} preferred-term concepts")
    
    # Second pass: add broader relationships for preferred terms
    print("Adding hierarchy (broader) relationships...")
    broader_count = 0
    for term_info in preferred_terms:
        uid = term_info["THE_TE_UID"]
        term_name = term_info["TERM"]
        concept = pref_concept_uris[uid]
        
        if term_name in term_to_broader:
            for broader_term in term_to_broader[term_name]:
                if broader_term in pref_term_to_uid:
                    broader_uid = pref_term_to_uid[broader_term]
                    g.add((concept, SKOS.broader, pref_concept_uris[broader_uid]))
                    broader_count += 1
    print(f"  Added {broader_count} broader relationships")
    
    # Third pass: add altLabels from non-preferred terms
    print("Adding altLabels from non-preferred terms...")
    for term_info in non_preferred_terms:
        non_pref_uid = term_info["THE_TE_UID"]
        non_pref_term = term_info["TERM"]
        
        # Find which preferred term(s) this non-preferred term maps to
        if non_pref_uid in non_preferred_to_preferred:
            for pref_uid in non_preferred_to_preferred[non_pref_uid]:
                # Check if the preferred concept exists
                if pref_uid in pref_concept_uris:
                    g.add((pref_concept_uris[pref_uid], SKOS.altLabel, Literal(non_pref_term, lang="en")))
                    alt_labels_added += 1
    print(f"  Added {alt_labels_added} altLabels")
    
    print(f"Serializing to {OUTPUT_RDF}...")
    xml = g.serialize(format="pretty-xml", encoding="utf-8")
    
    with open(OUTPUT_RDF, "wb") as f:
        f.write(xml)
    
    print(f"Done! Output written to {OUTPUT_RDF}")
    print(f"  Total triples: {len(g)}")


if __name__ == "__main__":
    main()
