"""Read-only source-update display facts; Project retains matching and apply authority."""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re

_CATEGORIES = ('unchanged', 'source_changed', 'new', 'removed', 'ambiguous', 'unresolved')
_MAX_DIFF_CHARACTERS = 20000
_MAX_DIFF_TOKENS = 1024
_WORDS = re.compile(r'[\u3400-\u9fff]|[^\W_]+(?:[\'’][^\W_]+)*|\s+|.', re.DOTALL)


@dataclass(frozen=True)
class SourceUpdateText:
    source_ref: str
    segment_number: int
    source: str
    target: str


@dataclass(frozen=True)
class SourceUpdateItem:
    category: str
    source_ref: str
    segment_number: int | None
    required: bool
    old: SourceUpdateText | None = None
    new: SourceUpdateText | None = None


@dataclass(frozen=True)
class SourceUpdateBlock:
    item_indices: tuple[int, ...]
    old: tuple[SourceUpdateText, ...]
    new: tuple[SourceUpdateText, ...]


@dataclass(frozen=True)
class SourceUpdateReview:
    preview: object
    items: tuple[SourceUpdateItem, ...]
    blocks: tuple[SourceUpdateBlock, ...] = ()
    templates_identical: bool = False

    @property
    def required_items(self):
        return tuple(item for item in self.items if item.required)


@dataclass(frozen=True)
class SourceUpdateWordDiff:
    old: tuple[tuple[str, bool], ...]
    new: tuple[tuple[str, bool], ...]
    highlighted: bool


@dataclass
class SourceUpdateCandidate:
    prepared: object
    runtime: object

    def close(self):
        self.prepared.close()


def source_update_word_diff(old, new):
    """Bounded, presentation-only token diff, called for the visible block only."""
    plain = SourceUpdateWordDiff(((old, False),), ((new, False),), False)
    if len(old) + len(new) > _MAX_DIFF_CHARACTERS:
        return plain
    old_words, new_words = _WORDS.findall(old), _WORDS.findall(new)
    if len(old_words) + len(new_words) > _MAX_DIFF_TOKENS:
        return plain
    old_parts, new_parts = [], []
    for tag, a, b, c, d in SequenceMatcher(None, old_words, new_words, autojunk=False).get_opcodes():
        if a != b:
            old_parts.append((''.join(old_words[a:b]), tag != 'equal'))
        if c != d:
            new_parts.append((''.join(new_words[c:d]), tag != 'equal'))
    return SourceUpdateWordDiff(tuple(old_parts), tuple(new_parts), True)


def _text_index(doc):
    targets = {entry.local_segment_id: entry.target for entry in doc.editing_overlay}
    return {slot.local_segment_id: SourceUpdateText(doc.source_ref, number, slot.source,
                targets.get(slot.local_segment_id, ''))
            for number, slot in enumerate(doc.source_segments, 1)}


def _document_blocks(old, new, indices, items):
    """Linear anchor walk; reordered anchors conservatively use one file block.

    Anchors are owner-classified shared identities, never text or row similarity.
    A block is a display interval only, including multiple old/new paragraphs.
    """
    blocks = []

    def append(keys):
        selected = tuple(sorted({indices[key] for key in keys
                                 if key in indices and items[indices[key]].category != 'unchanged'}))
        if selected:
            blocks.append(SourceUpdateBlock(selected,
                tuple(sorted((items[i].old for i in selected if items[i].old is not None),
                             key=lambda text: text.segment_number)),
                tuple(sorted((items[i].new for i in selected if items[i].new is not None),
                             key=lambda text: text.segment_number))))

    old_keys = tuple(key for key in old if key in indices and items[indices[key]].old is not None)
    new_keys = tuple(key for key in new if key in indices and items[indices[key]].new is not None)
    new_positions = {key: position for position, key in enumerate(new_keys)}
    anchors = [key for key in old_keys if key in new_positions and key in indices
               and items[indices[key]].category in ('unchanged', 'source_changed')]
    if any(new_positions[a] >= new_positions[b] for a, b in zip(anchors, anchors[1:])):
        append(indices)
        return blocks
    old_positions = {key: position for position, key in enumerate(old_keys)}
    old_start = new_start = 0
    for key in anchors:
        old_end, new_end = old_positions[key], new_positions[key]
        append((*old_keys[old_start:old_end], *new_keys[new_start:new_end]))
        if items[indices[key]].category == 'source_changed':
            append((key,))
        old_start, new_start = old_end + 1, new_end + 1
    append((*old_keys[old_start:], *new_keys[new_start:]))
    return blocks


def source_update_review(preview, current, incoming):
    """Preserve owner category/decision order while projecting separate display blocks."""
    old_docs = {doc.document_id: doc for doc in current.documents}
    new_docs = {doc.document_id: doc for doc in incoming.documents}
    old_text = {key: _text_index(doc) for key, doc in old_docs.items()}
    new_text = {key: _text_index(doc) for key, doc in new_docs.items()}
    required = set(preview.required_decision_identities)
    indices = {key: {} for key in old_docs.keys() | new_docs.keys()}
    items = []
    for category in _CATEGORIES:
        for identity in getattr(preview, category + '_identities'):
            doc_id, key = identity.document_id, identity.local_segment_id
            old = old_text.get(doc_id, {}).get(key) if category != 'new' else None
            new = new_text.get(doc_id, {}).get(key) if category in ('unchanged', 'source_changed', 'new') else None
            chosen = old if category in ('removed', 'ambiguous', 'unresolved') else new
            indices[doc_id][key] = len(items)
            items.append(SourceUpdateItem(category, chosen.source_ref,
                chosen.segment_number, identity in required, old, new))
    blocks = []
    # Source update retains Document identity, including explicit path renames.
    for doc_id in dict.fromkeys((*old_docs, *new_docs)):
        blocks.extend(_document_blocks(old_text.get(doc_id, {}), new_text.get(doc_id, {}),
                                       indices[doc_id], items))
    identical = tuple((doc.document_id, doc.source_ref, doc.source_snapshot_digest)
                      for doc in current.documents) == tuple(
                          (doc.document_id, doc.source_ref, doc.source_snapshot_digest)
                          for doc in incoming.documents)
    return SourceUpdateReview(preview, tuple(items), tuple(blocks), identical)
