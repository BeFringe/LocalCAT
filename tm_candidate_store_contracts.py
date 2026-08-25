"""Neutral candidate storage contracts shared by TM algorithms and stores.

This leaf module deliberately contains no SQLite, coordinator, retrieval,
engine, application, or Qt authority.  Historical ``SQLiteCandidate*`` names
remain canonical so existing imports can re-export the exact same objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NoReturn, Protocol, SupportsIndex, TypeVar, runtime_checkable


CANDIDATE_INDEX_VERSION = "candidate-index-v1"
CANDIDATE_PROOF_BLOCK_VERSION_V1 = "candidate-proof-block-v1"
CANDIDATE_PROOF_BLOCK_SIZE = 256


class SQLiteStoreSchemaError(RuntimeError):
    """A safe, resource-local schema or connection policy failure."""


class CandidateProofIndexError(ValueError):
    """One canonical candidate-proof fact failed exact recomputation."""


@dataclass(frozen=True)
class SQLiteCandidateRecord:
    """Folded source input exposed to a pre-transaction plan builder."""

    origin_ordinal: int
    source_fold_v1: str

    def __post_init__(self) -> None:
        if type(self.origin_ordinal) is not int or self.origin_ordinal < 0:
            raise ValueError("origin_ordinal must be a non-negative integer")
        if type(self.source_fold_v1) is not str or not self.source_fold_v1:
            raise ValueError("source_fold_v1 must be a non-empty string")


@dataclass(frozen=True)
class SQLiteGramRow:
    """One declarative gram row for the store-owned transaction."""

    origin_ordinal: int
    gram_size: int
    gram: str
    term_frequency: int = 1

    def __post_init__(self) -> None:
        if type(self.origin_ordinal) is not int or self.origin_ordinal < 0:
            raise ValueError("origin_ordinal must be a non-negative integer")
        if type(self.gram_size) is not int or self.gram_size not in {1, 2, 3}:
            raise ValueError("gram_size must be 1, 2, or 3")
        if type(self.gram) is not str or len(self.gram) != self.gram_size:
            raise ValueError("gram length must equal gram_size")
        if type(self.term_frequency) is not int or self.term_frequency < 1:
            raise ValueError("term_frequency must be a positive integer")


@dataclass(frozen=True)
class SQLiteCandidateWritePlan:
    """Closed candidate rows returned without access to SQLite state."""

    gram_rows: tuple[SQLiteGramRow, ...] = ()
    fts_origin_ordinals: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if type(self.gram_rows) is not tuple:
            raise TypeError("gram_rows must contain SQLiteGramRow values")
        gram_keys: list[tuple[int, int, str]] = []
        for row in self.gram_rows:
            if type(row) is not SQLiteGramRow:
                raise TypeError("gram_rows must contain SQLiteGramRow values")
            origin_ordinal = row.origin_ordinal
            gram_size = row.gram_size
            gram = row.gram
            term_frequency = row.term_frequency
            if type(origin_ordinal) is not int or origin_ordinal < 0:
                raise ValueError(
                    "origin_ordinal must be a non-negative integer"
                )
            if type(gram_size) is not int or gram_size not in {1, 2, 3}:
                raise ValueError("gram_size must be 1, 2, or 3")
            if type(gram) is not str or len(gram) != gram_size:
                raise ValueError("gram length must equal gram_size")
            if type(term_frequency) is not int or term_frequency < 1:
                raise ValueError("term_frequency must be a positive integer")
            gram_keys.append((origin_ordinal, gram_size, gram))
        if len(set(gram_keys)) != len(gram_keys):
            raise ValueError("gram_rows must be unique")
        if type(self.fts_origin_ordinals) is not tuple or any(
            type(origin_ordinal) is not int or origin_ordinal < 0
            for origin_ordinal in self.fts_origin_ordinals
        ):
            raise ValueError(
                "fts_origin_ordinals must contain non-negative integers"
            )
        if len(set(self.fts_origin_ordinals)) != len(
            self.fts_origin_ordinals
        ):
            raise ValueError("fts_origin_ordinals must be unique")


def unique_character_ngrams(
    folded_text: str,
    gram_size: int,
) -> tuple[str, ...]:
    """Return first-occurrence unique code-point grams without folding."""

    if type(folded_text) is not str:
        raise TypeError("folded_text must be a built-in string")
    if type(gram_size) is not int:
        raise TypeError("gram_size must be a built-in integer")
    if gram_size not in {1, 2, 3}:
        raise ValueError("gram_size must be 1, 2, or 3")
    seen: set[str] = set()
    grams: list[str] = []
    for offset in range(max(0, len(folded_text) - gram_size + 1)):
        gram = folded_text[offset : offset + gram_size]
        if gram not in seen:
            seen.add(gram)
            grams.append(gram)
    return tuple(grams)


def character_ngram_frequencies(
    folded_text: str,
    gram_size: int,
) -> tuple[tuple[str, int], ...]:
    """Return exact multiset frequencies in first-occurrence gram order."""

    if type(folded_text) is not str:
        raise TypeError("folded_text must be a built-in string")
    if type(gram_size) is not int:
        raise TypeError("gram_size must be a built-in integer")
    if gram_size not in {1, 2, 3}:
        raise ValueError("gram_size must be 1, 2, or 3")
    frequencies: dict[str, int] = {}
    for offset in range(max(0, len(folded_text) - gram_size + 1)):
        gram = folded_text[offset : offset + gram_size]
        frequencies[gram] = frequencies.get(gram, 0) + 1
    return tuple(frequencies.items())


def build_candidate_write_plan(
    records: tuple[SQLiteCandidateRecord, ...],
    *,
    fts5_available: bool,
) -> SQLiteCandidateWritePlan:
    """Build the mandatory candidate plan for one generation."""

    if type(records) is not tuple:
        raise TypeError("records must be a built-in tuple")
    if type(fts5_available) is not bool:
        raise TypeError("fts5_available must be a built-in bool")
    prepared: list[tuple[int, str]] = []
    for record in records:
        if type(record) is not SQLiteCandidateRecord:
            raise TypeError(
                "records must contain exact SQLiteCandidateRecord values"
            )
        if type(record.origin_ordinal) is not int or record.origin_ordinal < 0:
            raise ValueError("origin_ordinal must be a non-negative integer")
        if type(record.source_fold_v1) is not str or not record.source_fold_v1:
            raise ValueError(
                "source_fold_v1 must be a non-empty built-in string"
            )
        prepared.append((record.origin_ordinal, record.source_fold_v1))
    gram_sizes = (1, 2) if fts5_available else (1, 2, 3)
    return SQLiteCandidateWritePlan(
        gram_rows=tuple(
            SQLiteGramRow(origin_ordinal, gram_size, gram, term_frequency)
            for origin_ordinal, folded_source in prepared
            for gram_size in gram_sizes
            for gram, term_frequency in character_ngram_frequencies(
                folded_source,
                gram_size,
            )
        ),
        fts_origin_ordinals=(
            tuple(origin_ordinal for origin_ordinal, _source in prepared)
            if fts5_available
            else ()
        ),
    )


@dataclass(frozen=True)
class SQLiteCandidateRecallSnapshot:
    """Private-value snapshot returned by the leased candidate read seam."""

    fts5_available: bool
    stage_matches: tuple[tuple[str, tuple[tuple[int, int], ...]], ...]
    folded_sources: tuple[tuple[int, str], ...]

    def __post_init__(self) -> None:
        if type(self.fts5_available) is not bool:
            raise TypeError("fts5_available must be a built-in bool")
        if type(self.stage_matches) is not tuple:
            raise TypeError("stage_matches must be a built-in tuple")
        stage_names: list[str] = []
        for stage_entry in self.stage_matches:
            if type(stage_entry) is not tuple or len(stage_entry) != 2:
                raise TypeError("stage_matches must contain built-in pairs")
            stage_name, matches = stage_entry
            if type(stage_name) is not str or type(matches) is not tuple:
                raise TypeError("candidate stage snapshot values are invalid")
            if stage_name not in {"FTS_TRIGRAM", "GRAM_3", "GRAM_2", "GRAM_1"}:
                raise ValueError("candidate stage snapshot name is invalid")
            stage_names.append(stage_name)
            record_ids: list[int] = []
            for match in matches:
                if type(match) is not tuple or len(match) != 2:
                    raise TypeError("candidate stage matches must be pairs")
                record_id, matched_count = match
                if (
                    type(record_id) is not int
                    or record_id < 1
                    or type(matched_count) is not int
                    or matched_count < 0
                ):
                    raise ValueError("candidate stage match is invalid")
                if matched_count < 1:
                    raise ValueError("candidate stage overlap is invalid")
                record_ids.append(record_id)
            if len(set(record_ids)) != len(record_ids):
                raise ValueError("candidate stage matches must be unique")
        if len(set(stage_names)) != len(stage_names):
            raise ValueError("candidate stage snapshot names must be unique")
        if type(self.folded_sources) is not tuple:
            raise TypeError("folded_sources must be a built-in tuple")
        source_ids: list[int] = []
        for source_entry in self.folded_sources:
            if type(source_entry) is not tuple or len(source_entry) != 2:
                raise TypeError("folded_sources must contain built-in pairs")
            record_id, folded_source = source_entry
            if (
                type(record_id) is not int
                or record_id < 1
                or type(folded_source) is not str
                or not folded_source
            ):
                raise ValueError("candidate folded source is invalid")
            source_ids.append(record_id)
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("candidate folded sources must be unique")


@dataclass(frozen=True)
class SQLiteCandidateProofBlock:
    """One verified block bound input without any record source text."""

    block_id: int
    first_record_id: int
    last_record_id: int
    record_count: int
    min_source_fold_length: int
    max_source_fold_length: int
    character_intersection_upper: int
    bigram_intersection_upper: int


@dataclass(frozen=True)
class SQLiteCandidateProofRecord:
    """Exact record-bound facts derived from relevant proof rows only."""

    record_id: int
    block_id: int
    source_fold_length: int
    character_multiset_intersection: int
    bigram_multiset_intersection: int


_CANDIDATE_PROOF_DENSE_RECEIPT_FACTORY_KEY = object()


class _SQLiteCandidateProofDenseReceipt:
    """Opaque store-issued identity binding for one dense projection."""

    __slots__ = (
        "phase",
        "binding_digest",
        "item_count",
        "record_ids",
        "source_folds_v1",
        "source_fold_lengths",
        "bigram_multiset_intersections",
        "_sealed",
    )

    phase: str
    binding_digest: str
    item_count: int
    record_ids: tuple[int, ...] | None
    source_folds_v1: tuple[str, ...] | None
    source_fold_lengths: tuple[int, ...]
    bigram_multiset_intersections: tuple[int, ...] | None
    _sealed: bool

    def __init__(
        self,
        *,
        phase: str,
        binding_digest: str,
        item_count: int,
        record_ids: tuple[int, ...] | None,
        source_folds_v1: tuple[str, ...] | None,
        source_fold_lengths: tuple[int, ...],
        bigram_multiset_intersections: tuple[int, ...] | None,
        _factory_key: object,
    ) -> None:
        if _factory_key is not _CANDIDATE_PROOF_DENSE_RECEIPT_FACTORY_KEY:
            raise TypeError("dense proof receipts are store-owned")
        self._sealed = False
        self.phase = phase
        self.binding_digest = binding_digest
        self.item_count = item_count
        self.record_ids = record_ids
        self.source_folds_v1 = source_folds_v1
        self.source_fold_lengths = source_fold_lengths
        self.bigram_multiset_intersections = bigram_multiset_intersections
        self._sealed = True

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("dense proof receipts are immutable")
        object.__setattr__(self, name, value)

    def __copy__(self) -> _SQLiteCandidateProofDenseReceipt:
        return self

    def __deepcopy__(
        self,
        memo: dict[int, object],
    ) -> _SQLiteCandidateProofDenseReceipt:
        memo[id(self)] = self
        return self

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        del protocol
        raise TypeError("dense proof receipts cannot be serialized")


@dataclass(frozen=True)
class SQLiteCandidateProofDensePhase1:
    """Length and exact-bigram facts from the committed dense phase one."""

    source_fold_lengths: tuple[int, ...]
    bigram_multiset_intersections: tuple[int, ...]
    binding_digest: str
    _receipt: _SQLiteCandidateProofDenseReceipt = field(repr=False)


@dataclass(frozen=True)
class SQLiteCandidateProofDensePhase2:
    """Strict proof-only folded projection from committed phase two."""

    record_ids: tuple[int, ...]
    source_folds_v1: tuple[str, ...]
    source_fold_lengths: tuple[int, ...]
    binding_digest: str
    _receipt: _SQLiteCandidateProofDenseReceipt = field(repr=False)


@dataclass(frozen=True)
class SQLiteCandidateProofSnapshot:
    """Seed and block frontiers for one query generation, without record facts."""

    index_kind: str
    seed_stages: tuple[tuple[str, tuple[int, ...]], ...]
    blocks: tuple[SQLiteCandidateProofBlock, ...]
    total_record_count: int
    head_revision: int
    query_maxima_digest: str


def validate_candidate_proof_dense_phase1_result(
    value: object,
    *,
    binding_digest: str,
    total_record_count: int,
) -> None:
    """Accept only an exact internally bound phase-one fact object."""

    if (
        type(binding_digest) is not str
        or len(binding_digest) != 64
        or any(character not in "0123456789abcdef" for character in binding_digest)
        or type(total_record_count) is not int
        or total_record_count < 0
        or type(value) is not SQLiteCandidateProofDensePhase1
    ):
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
    receipt = getattr(value, "_receipt", None)
    if (
        type(receipt) is not _SQLiteCandidateProofDenseReceipt
        or receipt.phase != "DENSE_PHASE1_V1"
        or receipt.binding_digest != binding_digest
        or receipt.binding_digest != value.binding_digest
        or receipt.item_count != total_record_count
        or receipt.record_ids is not None
        or receipt.source_folds_v1 is not None
        or receipt.source_fold_lengths is not value.source_fold_lengths
        or receipt.bigram_multiset_intersections
        is not value.bigram_multiset_intersections
    ):
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")


def validate_candidate_proof_dense_phase2_result(
    value: object,
    *,
    binding_digest: str,
    record_ids: tuple[int, ...],
    source_fold_lengths: tuple[int, ...],
) -> None:
    """Accept only an exact internally bound phase-two fact object."""

    if (
        type(binding_digest) is not str
        or len(binding_digest) != 64
        or any(character not in "0123456789abcdef" for character in binding_digest)
        or type(record_ids) is not tuple
        or any(type(record_id) is not int or record_id < 1 for record_id in record_ids)
        or len(set(record_ids)) != len(record_ids)
        or type(source_fold_lengths) is not tuple
        or len(source_fold_lengths) != len(record_ids)
        or any(
            type(source_length) is not int or source_length < 1
            for source_length in source_fold_lengths
        )
        or type(value) is not SQLiteCandidateProofDensePhase2
    ):
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
    receipt = getattr(value, "_receipt", None)
    if (
        type(receipt) is not _SQLiteCandidateProofDenseReceipt
        or receipt.phase != "DENSE_PHASE2_V1"
        or receipt.binding_digest != binding_digest
        or receipt.binding_digest != value.binding_digest
        or receipt.item_count != len(record_ids)
        or receipt.record_ids is not value.record_ids
        or receipt.source_folds_v1 is not value.source_folds_v1
        or receipt.source_fold_lengths is not value.source_fold_lengths
        or receipt.bigram_multiset_intersections is not None
        or value.record_ids != record_ids
        or value.source_fold_lengths != source_fold_lengths
    ):
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")


@runtime_checkable
class CandidatePostingPort(Protocol):
    """Transitional low-level FTS/gram recall behavior."""

    @property
    def resource_id(self) -> str: ...

    @property
    def candidate_port_scope(self) -> str: ...

    def fts5_candidate_ids(
        self,
        match_expression: str,
    ) -> tuple[int, ...] | None: ...

    def fts5_candidate_ids_for_trigrams(
        self,
        trigrams: tuple[str, ...],
    ) -> tuple[int, ...] | None: ...

    def gram_candidate_overlaps(
        self,
        query_postings: tuple[tuple[int, str], ...],
        *,
        candidate_cap: int,
    ) -> tuple[tuple[int, int], ...]: ...


@runtime_checkable
class CandidateRecallPort(Protocol):
    """Resource-bound complete candidate recall snapshot behavior."""

    @property
    def resource_id(self) -> str: ...

    @property
    def candidate_port_scope(self) -> str: ...

    def candidate_recall_snapshot(
        self,
        *,
        fts_query_trigrams: tuple[str, ...] | None,
        query_grams_by_size: tuple[tuple[int, tuple[str, ...]], ...],
        candidate_floor: int,
        fts_query_degenerate: bool,
    ) -> SQLiteCandidateRecallSnapshot: ...


@runtime_checkable
class CandidateProofPort(CandidateRecallPort, Protocol):
    """Captured-generation candidate proof behavior."""

    def candidate_proof_snapshot(
        self,
        *,
        folded_query: str,
        seed_limit: int,
    ) -> SQLiteCandidateProofSnapshot: ...

    def validate_candidate_proof_generation(
        self,
        *,
        head_revision: int,
        total_record_count: int,
    ) -> None: ...

    def candidate_proof_block_records(
        self,
        *,
        folded_query: str,
        block: SQLiteCandidateProofBlock,
        head_revision: int,
        total_record_count: int,
    ) -> tuple[SQLiteCandidateProofRecord, ...]: ...

    def candidate_proof_dense_phase1(
        self,
        *,
        folded_query: str,
        blocks: tuple[SQLiteCandidateProofBlock, ...],
        head_revision: int,
        total_record_count: int,
        query_maxima_digest: str,
    ) -> SQLiteCandidateProofDensePhase1: ...

    def candidate_proof_dense_phase2(
        self,
        *,
        folded_query: str,
        blocks: tuple[SQLiteCandidateProofBlock, ...],
        head_revision: int,
        total_record_count: int,
        query_maxima_digest: str,
        binding_digest: str,
        record_ids: tuple[int, ...],
        source_fold_lengths: tuple[int, ...],
    ) -> SQLiteCandidateProofDensePhase2: ...

    def validate_candidate_proof_dense_phase1_result(
        self,
        value: object,
        *,
        folded_query: str,
        blocks: tuple[SQLiteCandidateProofBlock, ...],
        head_revision: int,
        total_record_count: int,
        query_maxima_digest: str,
    ) -> None: ...

    def validate_candidate_proof_dense_phase2_result(
        self,
        value: object,
        *,
        binding_digest: str,
        record_ids: tuple[int, ...],
        source_fold_lengths: tuple[int, ...],
    ) -> None: ...


_PortT = TypeVar("_PortT", CandidatePostingPort, CandidateRecallPort, CandidateProofPort)


def _require_port(
    value: object,
    contract: type[_PortT],
    *,
    label: str,
    resource_id: str | None,
    required_scope: str,
) -> _PortT:
    if not isinstance(value, contract):
        raise TypeError(f"{label} does not implement the candidate storage port")
    observed_resource_id = value.resource_id
    if type(observed_resource_id) is not str or not observed_resource_id.strip():
        raise TypeError(f"{label} resource identity is invalid")
    if resource_id is not None and observed_resource_id != resource_id:
        raise ValueError(f"{label} resource identity does not match")
    observed_scope = value.candidate_port_scope
    if type(observed_scope) is not str or observed_scope != required_scope:
        raise TypeError(f"{label} has the wrong candidate port scope")
    return value


def require_candidate_posting_port(value: object) -> CandidatePostingPort:
    """Validate the low-level posting behavior without naming a store class."""

    return _require_port(
        value,
        CandidatePostingPort,
        label="posting port",
        resource_id=None,
        required_scope="STORE",
    )


def require_candidate_recall_port(
    value: object,
    *,
    resource_id: str,
    required_scope: str,
) -> CandidateRecallPort:
    """Validate one resource-bound recall port before storage calls."""

    if type(resource_id) is not str or not resource_id.strip():
        raise ValueError("resource_id must be a non-empty built-in string")
    if required_scope not in {"STORE", "QUERY_VIEW"}:
        raise ValueError("candidate recall scope is invalid")
    return _require_port(
        value,
        CandidateRecallPort,
        label="recall port",
        resource_id=resource_id,
        required_scope=required_scope,
    )


def require_candidate_proof_port(
    value: object,
    *,
    resource_id: str,
) -> CandidateProofPort:
    """Validate one resource-bound proof port before storage calls."""

    if type(resource_id) is not str or not resource_id.strip():
        raise ValueError("resource_id must be a non-empty built-in string")
    return _require_port(
        value,
        CandidateProofPort,
        label="proof port",
        resource_id=resource_id,
        required_scope="QUERY_VIEW",
    )


__all__ = [
    "CANDIDATE_INDEX_VERSION",
    "CANDIDATE_PROOF_BLOCK_SIZE",
    "CANDIDATE_PROOF_BLOCK_VERSION_V1",
    "CandidatePostingPort",
    "CandidateProofIndexError",
    "CandidateProofPort",
    "CandidateRecallPort",
    "SQLiteCandidateProofBlock",
    "SQLiteCandidateProofDensePhase1",
    "SQLiteCandidateProofDensePhase2",
    "SQLiteCandidateProofRecord",
    "SQLiteCandidateProofSnapshot",
    "SQLiteCandidateRecallSnapshot",
    "SQLiteCandidateRecord",
    "SQLiteCandidateWritePlan",
    "SQLiteGramRow",
    "SQLiteStoreSchemaError",
    "build_candidate_write_plan",
    "character_ngram_frequencies",
    "require_candidate_posting_port",
    "require_candidate_proof_port",
    "require_candidate_recall_port",
    "unique_character_ngrams",
    "validate_candidate_proof_dense_phase1_result",
    "validate_candidate_proof_dense_phase2_result",
]
