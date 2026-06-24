"""Multilingual topic vocabularies (§4).

Most sources aren't in English, so the data-protection / FOI vocabularies carry
NL/FR/DE/ES/IT terms alongside English. These are hand-tuned as gaps surface —
deliberately data, not logic. This is the seed set that §4a's rule engine later
re-expresses as editable `literal`/`regex` rules (build step 4).

Each entry is (term, weight): heavy weight = strongly topical, light = supporting.
Matching is case-folded and accent-folded (see ``gate.fold``).
"""

from __future__ import annotations

# Court / source hints that are in-scope by construction (§4): these short-circuit
# the stage-1 gate to KEEP without a vocabulary match. Keyed by lowercased token.
IN_SCOPE_COURTS: frozenset[str] = frozenset(
    {
        "ukftt-grc",  # UK FTT (General Regulatory Chamber) — info-rights/DP tribunal
        "grc",
        "ico",  # UK Information Commissioner
        "dpc",  # Irish Data Protection Commission
        "edpb",  # European Data Protection Board
        "cnil",  # French DPA
    }
)

DATA_PROTECTION: dict[str, float] = {
    # English
    "data protection": 3.0,
    "personal data": 2.5,
    "gdpr": 3.0,
    "data subject": 2.0,
    "data controller": 2.0,
    "data processor": 1.5,
    "right to erasure": 2.0,
    "data minimisation": 2.0,
    "supervisory authority": 1.5,
    "2016/679": 3.0,  # GDPR CELEX number / cite form
    # French
    "donnees a caractere personnel": 3.0,
    "donnees personnelles": 2.5,
    "rgpd": 3.0,
    "protection des donnees": 3.0,
    # German
    "personenbezogene daten": 3.0,
    "datenschutz": 3.0,
    "dsgvo": 3.0,
    # Dutch
    "persoonsgegevens": 3.0,
    "avg": 2.5,  # Algemene verordening gegevensbescherming
    "gegevensbescherming": 3.0,
    # Spanish
    "datos personales": 3.0,
    "proteccion de datos": 3.0,
    # Italian
    "dati personali": 3.0,
    "protezione dei dati": 3.0,
}

FOI: dict[str, float] = {
    # English
    "freedom of information": 3.0,
    "foi": 2.0,
    "right to access": 1.5,
    "public authority": 1.5,
    "environmental information": 2.0,  # EIR
    # French
    "acces aux documents": 2.5,
    "documents administratifs": 2.0,
    # German
    "informationsfreiheit": 3.0,
    # Dutch
    "openbaarheid van bestuur": 3.0,
    "wob": 2.0,
    "woo": 2.0,
}

# tag -> vocabulary
VOCABULARIES: dict[str, dict[str, float]] = {
    "data_protection": DATA_PROTECTION,
    "foi": FOI,
}
