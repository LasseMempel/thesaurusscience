from SPARQLWrapper import SPARQLWrapper, JSON

endpoint = "https://api.linkeddata.cultureelerfgoed.nl/datasets/thesauri/archeologischbasisregister/sparql"

# ============================================================
# SPARQL Queries
# ============================================================

# Query to retrieve all skos:Concept instances with their labels
concepts_query = """
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?concept ?prefLabel ?altLabel ?definition
WHERE {
  ?concept a skos:Concept .
  OPTIONAL { ?concept skos:prefLabel ?prefLabel }
  OPTIONAL { ?concept skos:altLabel ?altLabel }
  OPTIONAL { ?concept skos:definition ?definition }
}
LIMIT 100
"""

extended_concepts_query = """
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?concept ?prefLabel ?altLabel ?definition ?p ?o
WHERE {
  ?concept a skos:Concept .
  OPTIONAL { ?concept skos:prefLabel ?prefLabel }
  OPTIONAL { ?concept skos:altLabel ?altLabel }
  OPTIONAL { ?concept skos:definition ?definition }
  ?concept ?p ?o .
}
LIMIT 20
"""

concept_schemes_query = """
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?cs
WHERE {
  ?cs a skos:ConceptScheme .
}
LIMIT 100
"""

# Query to retrieve all SKOS mapping properties (exactMatch, closeMatch, etc.)
mapping_query = """
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?concept ?property ?match
WHERE {
  ?concept ?property ?match .
  VALUES ?property {
    skos:exactMatch
    skos:closeMatch
    skos:relatedMatch
    skos:broadMatch
    skos:narrowMatch
  }
}
LIMIT 100
"""

# Query to retrieve SKOS mappings to/from Getty AAT
getty_mapping_query = """
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?concept ?property ?match
WHERE {
  ?concept ?property ?match .
  VALUES ?property {
    skos:exactMatch
    skos:closeMatch
    skos:relatedMatch
    skos:broadMatch
    skos:narrowMatch
  }
  FILTER(
    regex(STR(?concept), "vocab.getty.edu/aat") ||
    regex(STR(?match), "vocab.getty.edu/aat")
  )
}
LIMIT 100
"""

# Query to retrieve triples with URIs containing 'ariadne' namespaces
ariadne_namespace_query = """
SELECT ?s ?p ?o
WHERE {
  ?s ?p ?o .
  FILTER(
    CONTAINS(STR(?s), "knaw.nl/dccd-terms/") ||
    CONTAINS(STR(?p), "knaw.nl/dccd-terms/") ||
    CONTAINS(STR(?o), "knaw.nl/dccd-terms/") ||
    CONTAINS(STR(?s), "dendro.dans.knaw.nl/") ||
    CONTAINS(STR(?p), "dendro.dans.knaw.nl/") ||
    CONTAINS(STR(?o), "dendro.dans.knaw.nl/") ||
    CONTAINS(STR(?s), "rnaproject.org") ||
    CONTAINS(STR(?p), "rnaproject.org") ||
    CONTAINS(STR(?o), "rnaproject.org")
  )
}
"""

# ============================================================
# Functions
# ============================================================

def execute_query(query_string, endpoint_url=endpoint):
    """
    Execute a SPARQL query against the given endpoint.
    
    Args:
        query_string: The SPARQL query to execute
        endpoint_url: The SPARQL endpoint URL
    
    Returns:
        Dictionary with query results, or None on error
    """
    sparql = SPARQLWrapper(endpoint_url)
    sparql.setQuery(query_string)
    sparql.setReturnFormat(JSON)
    
    try:
        return sparql.query().convert()
    except Exception as e:
        print(f"Error executing SPARQL query: {e}")
        return None


def print_results(results, title="Query Results"):
    """
    Generic function to print SPARQL query results.
    
    Args:
        results: The query results in JSON format
        title: A title to display for the results
    """
    if results is None or "results" not in results or "bindings" not in results["results"]:
        print("No results to display.")
        return
    
    bindings = results["results"]["bindings"]
    if not bindings:
        print(f"{title}: No results found.")
        return
    
    print(f"\n{title}:")
    print("=" * 80)
    
    # Get all possible variable names from the first result
    variable_names = list(bindings[0].keys())
    
    count = 0
    for result in bindings:
        count += 1
        print(f"\nResult #{count}:")
        for var_name in variable_names:
            value = result.get(var_name, {}).get("value", "")
            if value:
                print(f"  {var_name}: {value}")
        print("-" * 80)
    
    print(f"\nTotal results: {len(bindings)}")


# ============================================================
# Main Execution
# ============================================================

# Uncomment the query you want to run, or add more queries to the list
queries_to_run = [
    #("SKOS Concepts", concepts_query),
    #("Extended SKOS Concepts", extended_concepts_query),
    ("SKOS Concept Schemes", concept_schemes_query),
    #("SKOS Mapping Properties", mapping_query),
    #("Getty AAT Mappings", getty_mapping_query),
    #("Ariadne Namespace Triples", ariadne_namespace_query),
]

# Execute and print results for each query
for title, query in queries_to_run:
    print(f"\n{'=' * 80}")
    print(f"Executing: {title}")
    print('=' * 80)
    results = execute_query(query)
    print_results(results, title)