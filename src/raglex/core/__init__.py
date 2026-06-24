"""Core, jurisdiction-agnostic abstractions shared by every adapter and stage."""

from .adapter import Adapter, BaseAdapter
from .errors import (
    AdapterError,
    FetchError,
    RaglexError,
    RateLimitException,
)
from .models import (
    AddedBy,
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Stub,
    TypedRelation,
    UpstreamStatus,
    sha256_bytes,
)

__all__ = [
    "Adapter",
    "BaseAdapter",
    "AdapterError",
    "FetchError",
    "RaglexError",
    "RateLimitException",
    "AddedBy",
    "DocType",
    "ExtractedVia",
    "Record",
    "RelationshipType",
    "ResolutionStatus",
    "Stub",
    "TypedRelation",
    "UpstreamStatus",
    "sha256_bytes",
]
