"""Pluggable document-format parsers (markup family → text + segments + edges).

Importing the package registers the built-in parsers. Add a format = a module that
calls ``register(name, parser)``.
"""

from . import (  # noqa: F401  (register on import)
    akoma_ntoso,
    bwb,
    eisb_html,
    eisb_xml,
    eurlex_html,
    dila_xml,
    formex,
    frl_html,
    gii_xml,
    hklm_xml,
    lawmaker_html,
    ldml_de,
    legifrance_json,
    lims_xml,
    nz_pco_xml,
    rii_xml,
    rtf,
)
from .base import ParsedDoc, available, parse, register

__all__ = ["ParsedDoc", "available", "parse", "register"]
