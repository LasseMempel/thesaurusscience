DAI
- zwei Vokabulare, eins mit Dariah Mappings, eins neuer
- skos:exactMatches auf sich selbst beim moderneren

Wortnetz Kultur Mappings zu GND, Wikidata, FISH und Getty AAT!
- exact, close ,related, broad und narrow Matches

Dariah Vocabs 
- interne Mappings? Mappings zu backbone Thesaurus?

Fish Vocabs: Keine Mappings
- downgeloaded/extrahiert

Archäologisches Museum Hamburg - etliche Vokabulare
- Mappings zu Wortnetz Kultur

DBPedia?
- wird gemappt

Bamberger Vokabular für historische Literatur

Wikidata Sparql Construct?
- cultural artifact (Q1791627) 
- Art & Architecture Thesaurus ID
- GND ID
- FISH Archaeological Objects Thesaurus ID (mehr?)
- Iconclass notation
- Getty Thesaurus of Geographic Names ID
- Cultural Objects Names Authority ID

AAT? 
- aktuell down, XML dump

Cocoda
- Mappings Restaurierungsthesaurus extrahieren

Ariadne Mappings:
- http://legacy.ariadne-infrastructure.eu/resources-2/aat/mappings-to-aat/
- aus CSV extrahiert
- DAI mappings invertiert :-D 



Finto.fi Weitere englische Konzepte mit Mappings...
Pactols?!

Vorgehen:
- Download Fish Vocabularies von
    - https://heritage-standards.org.uk/fish-vocabularies/
    - https://www.heritagedata.org/blog/vocabularies-provided/

- Bezug Aktueller DAI World Thesaurus über Kontakte (RDF Download erfordert Mitarbeiter Login)

- Download Backbone Thesaurus und verknüpfte Vokabulare über https://vocabs.dariah.eu/, SKOSMOS Instanz von Austrian Centre for Digital Humanities (ADCD)

- Download Wortnetz Kultur über https://wnk-viewer.lvr.de/about

- Download Pactols via https://github.com/frantiq/PACTOLS/tree/master/pactols-latest-version/

- Download Ariadne Mappings via http://legacy.ariadne-infrastructure.eu/resources-2/aat/mappings-to-aat/
- Gevibecodetes Script übersetzt Tabellen PDF in ttl Mappings, dabei Validierung ob Concept vorhanden ist und Label übereinstimmt. Altlabel Fallback weil häufig nicht das PrefLabel verwendet wurde...


