"""Body-safe source-update display facts and private Application lifetime."""
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceUpdateItem:
    category: str
    source_ref: str
    segment_number: int | None
    required: bool


@dataclass(frozen=True)
class SourceUpdateReview:
    preview: object
    items: tuple[SourceUpdateItem, ...]

    @property
    def required_items(self):
        return tuple(item for item in self.items if item.required)


@dataclass
class SourceUpdateCandidate:
    prepared: object
    runtime: object

    def close(self):
        self.prepared.close()


def source_update_review(preview, current, incoming):
    """Rows are labels only; identity and decisions stay with the Project owner."""
    old_docs = {doc.document_id: doc for doc in current.documents}
    new_docs = {doc.document_id: doc for doc in incoming.documents}
    required = set(preview.required_decision_identities)
    items = []
    for category in ('unchanged', 'source_changed', 'new', 'removed', 'ambiguous', 'unresolved'):
        for identity in getattr(preview, category + '_identities'):
            docs = old_docs if category in ('removed', 'ambiguous', 'unresolved') else new_docs
            doc = docs[identity.document_id]
            number = next((index + 1 for index, slot in enumerate(doc.source_segments)
                           if slot.local_segment_id == identity.local_segment_id), None)
            items.append(SourceUpdateItem(category, doc.source_ref, number, identity in required))
    return SourceUpdateReview(preview, tuple(items))
