"""Long-term memory retrieval helpers."""

from __future__ import annotations

from typing import List, Optional

from ..contracts.memory_models import LongTermMemoryQuery, RetrievalRecord
from .longterm import LongTermMemory


def retrieve_from_longterm(memory: LongTermMemory, query: LongTermMemoryQuery) -> List[RetrievalRecord]:
    """Retrieve reusable records from long-term memory patterns."""
    items = memory.find_matching_patterns(query.signal, query.tags)
    results: List[RetrievalRecord] = []
    for item in items[: query.limit]:
        results.append(
            RetrievalRecord(
                signal=item.signal,
                action=item.action,
                confidence=item.confidence,
                evidence_refs=item.evidence_refs,
            )
        )
    return results
