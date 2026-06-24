"""Entity resolution (§5b) — citation strings → graph nodes."""

from .matchers import Candidate, first_candidate
from .resolver import ResolveStats, Resolver, string_hash

__all__ = ["Candidate", "first_candidate", "ResolveStats", "Resolver", "string_hash"]
