"""SQLite candidate read/write data plane for caller-owned connections.

This module owns candidate recall/proof SQL and row decoding.  It never opens
connections, completes transactions, validates generation authority, or maps
SQLite failures.  Callers must validate and privately copy all scalar, tuple,
and leaf-DTO inputs before invoking these helpers.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import sqlite3
from typing import Any

from tm_candidate_store_contracts import (
    CANDIDATE_PROOF_BLOCK_SIZE,
    CandidateProofIndexError,
    SQLiteCandidateProofBlock,
    SQLiteCandidateProofRecord,
    SQLiteStoreSchemaError,
    character_ngram_frequencies,
    unique_character_ngrams,
)
from text_matcher import fold_text_value_v1


CANDIDATE_QUERY_CHUNK_SIZE = 256
CANDIDATE_SEED_POSTING_CAP = 4096
_STREAMED_STAGE_SECONDARY_INDEX_NAMES = (
    "idx_tm_exact",
    "idx_tm_context_speaker",
    "idx_tm_gram_lookup",
    "idx_tm_gram_block_lookup",
)


def _chunks(values: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    return tuple(
        values[offset : offset + CANDIDATE_QUERY_CHUNK_SIZE]
        for offset in range(0, len(values), CANDIDATE_QUERY_CHUNK_SIZE)
    )


def _fts5_match_expression(trigrams: tuple[str, ...]) -> str:
    return " OR ".join(
        f'"{trigram.replace(chr(34), chr(34) * 2)}"'
        for trigram in trigrams
    )


def _record_id(value: object, code: str) -> int:
    if type(value) is int:
        record_id = value
    elif type(value) is str and value.isdecimal():
        record_id = int(value)
    else:
        raise SQLiteStoreSchemaError(code)
    if record_id < 1:
        raise SQLiteStoreSchemaError(code)
    return record_id


def _proof_int(value: object, code: str) -> int:
    if type(value) is not int or value < 0:
        raise SQLiteStoreSchemaError(code)
    return value


def _proof_text(value: object, code: str) -> str:
    if type(value) is not str or not value:
        raise SQLiteStoreSchemaError(code)
    return value


def fts5_candidate_ids(
    connection: sqlite3.Connection,
    match_expression: str,
) -> tuple[int, ...]:
    """Return sorted unique record identities for one prepared FTS query."""

    rows = connection.execute(
        "SELECT record_id FROM tm_fts WHERE tm_fts MATCH ?",
        (match_expression,),
    ).fetchall()
    record_ids: set[int] = set()
    for row in rows:
        if type(row) is not tuple or len(row) != 1:
            raise SQLiteStoreSchemaError("STORE.FTS5_RESULT_INVALID")
        record_ids.add(_record_id(row[0], "STORE.FTS5_RESULT_INVALID"))
    return tuple(sorted(record_ids))


def fts5_candidate_ids_for_trigrams(
    connection: sqlite3.Connection,
    trigrams: tuple[str, ...],
) -> tuple[int, ...]:
    """Return the bounded-query union for prepared unique trigrams."""

    record_ids: set[int] = set()
    for chunk in _chunks(trigrams):
        rows = connection.execute(
            "SELECT record_id FROM tm_fts WHERE tm_fts MATCH ?",
            (_fts5_match_expression(chunk),),
        ).fetchall()
        for row in rows:
            if type(row) is not tuple or len(row) != 1:
                raise SQLiteStoreSchemaError("STORE.FTS5_RESULT_INVALID")
            record_ids.add(_record_id(row[0], "STORE.FTS5_RESULT_INVALID"))
    return tuple(sorted(record_ids))


def gram_candidate_overlaps(
    connection: sqlite3.Connection,
    query_postings: tuple[tuple[int, str], ...],
    *,
    candidate_cap: int,
) -> tuple[tuple[int, int], ...]:
    """Return records ordered by unique posting overlap then identity."""

    matched_by_id: dict[int, int] = {}
    for offset in range(0, len(query_postings), CANDIDATE_QUERY_CHUNK_SIZE):
        chunk = query_postings[
            offset : offset + CANDIDATE_QUERY_CHUNK_SIZE
        ]
        values_sql = ",".join("(?, ?)" for _ in chunk)
        parameters = tuple(value for posting in chunk for value in posting)
        rows = connection.execute(
            "WITH query_grams(gram_size, gram) AS (VALUES "
            f"{values_sql}) "
            "SELECT postings.record_id, COUNT(*) AS matched_count "
            "FROM tm_gram AS postings "
            "JOIN query_grams AS query "
            "ON query.gram_size = postings.gram_size "
            "AND query.gram = postings.gram "
            "GROUP BY postings.record_id",
            parameters,
        ).fetchall()
        for row in rows:
            if (
                type(row) is not tuple
                or len(row) != 2
                or type(row[0]) is not int
                or row[0] < 1
                or type(row[1]) is not int
                or not 1 <= row[1] <= len(chunk)
            ):
                raise SQLiteStoreSchemaError("STORE.GRAM_RESULT_INVALID")
            matched_by_id[row[0]] = matched_by_id.get(row[0], 0) + row[1]
    return tuple(
        sorted(matched_by_id.items(), key=lambda item: (-item[1], item[0]))[
            :candidate_cap
        ]
    )


def candidate_recall_snapshot(
    connection: sqlite3.Connection,
    *,
    fts5_available: bool,
    fts_query_trigrams: tuple[str, ...] | None,
    query_grams_by_size: tuple[tuple[int, tuple[str, ...]], ...],
    candidate_floor: int,
    fts_query_degenerate: bool,
) -> tuple[
    tuple[tuple[str, tuple[tuple[int, int], ...]], ...],
    tuple[tuple[int, str], ...],
]:
    """Read authority-free recall rows from an already captured generation."""

    stage_matches: list[tuple[str, tuple[tuple[int, int], ...]]] = []
    cumulative_ids: set[int] = set()
    fts_ids: set[int] = set()
    if fts_query_trigrams is not None and fts5_available:
        for chunk in _chunks(fts_query_trigrams):
            rows = connection.execute(
                "SELECT record_id FROM tm_fts WHERE tm_fts MATCH ?",
                (_fts5_match_expression(chunk),),
            ).fetchall()
            for row in rows:
                if type(row) is not tuple or len(row) != 1:
                    raise SQLiteStoreSchemaError(
                        "STORE.CANDIDATE_RESULT_INVALID"
                    )
                record_id = _record_id(
                    row[0], "STORE.CANDIDATE_RESULT_INVALID"
                )
                fts_ids.add(record_id)
                cumulative_ids.add(record_id)

    if fts_query_trigrams is None or not fts5_available:
        selected_stages = query_grams_by_size
    elif (
        not cumulative_ids
        or fts_query_degenerate
        or len(cumulative_ids) < candidate_floor
    ):
        selected_stages = tuple(
            stage for stage in query_grams_by_size if stage[0] in {1, 2}
        )
    else:
        selected_stages = ()

    for gram_size, grams in selected_stages:
        if (
            fts_query_trigrams is not None
            and fts5_available
            and gram_size == 1
            and len(cumulative_ids) >= candidate_floor
        ):
            continue
        matched_by_id: dict[int, int] = {}
        for chunk in _chunks(grams):
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                "SELECT record_id, COUNT(*) AS matched_count "
                "FROM tm_gram WHERE gram_size = ? "
                f"AND gram IN ({placeholders}) "
                "GROUP BY record_id",
                (gram_size, *chunk),
            ).fetchall()
            for row in rows:
                if (
                    type(row) is not tuple
                    or len(row) != 2
                    or type(row[0]) is not int
                    or row[0] < 1
                    or type(row[1]) is not int
                    or not 1 <= row[1] <= len(chunk)
                ):
                    raise SQLiteStoreSchemaError(
                        "STORE.CANDIDATE_RESULT_INVALID"
                    )
                matched_by_id[row[0]] = matched_by_id.get(row[0], 0) + row[1]
                cumulative_ids.add(row[0])
        stage_matches.append(
            (f"GRAM_{gram_size}", tuple(matched_by_id.items()))
        )

    folded_sources: list[tuple[int, str]] = []
    candidate_ids = tuple(cumulative_ids)
    for offset in range(0, len(candidate_ids), 512):
        chunk = candidate_ids[offset : offset + 512]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            "SELECT record_id, source_fold_v1 FROM tm_record "
            f"WHERE record_id IN ({placeholders})",
            chunk,
        ).fetchall()
        for row in rows:
            if (
                type(row) is not tuple
                or len(row) != 2
                or type(row[0]) is not int
                or row[0] < 1
                or type(row[1]) is not str
                or not row[1]
            ):
                raise SQLiteStoreSchemaError(
                    "STORE.CANDIDATE_RESULT_INVALID"
                )
            folded_sources.append((row[0], row[1]))
    if {record_id for record_id, _source in folded_sources} != cumulative_ids:
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_SOURCE_MISSING")
    if fts_query_trigrams is not None and fts5_available:
        sources_by_id = dict(folded_sources)
        query_gram_set = set(fts_query_trigrams)
        fts_matches = tuple(
            (
                record_id,
                len(
                    query_gram_set.intersection(
                        unique_character_ngrams(sources_by_id[record_id], 3)
                    )
                ),
            )
            for record_id in sorted(fts_ids)
        )
        if any(count < 1 for _record_id, count in fts_matches):
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_RESULT_INVALID")
        stage_matches.insert(0, ("FTS_TRIGRAM", fts_matches))
    return tuple(stage_matches), tuple(folded_sources)


def bounded_seed_stages(
    connection: sqlite3.Connection,
    *,
    folded_query: str,
    fts5_available: bool,
    seed_limit: int,
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    """Execute one real bounded seed on the selected candidate index."""

    if len(folded_query) >= 3 and fts5_available:
        trigrams = unique_character_ngrams(folded_query, 3)
        if not trigrams:
            return (("FTS_TRIGRAM", ()),)
        ids: list[int] = []
        seen: set[int] = set()
        for chunk in _chunks(trigrams):
            rows = connection.execute(
                "SELECT record_id FROM tm_fts WHERE tm_fts MATCH ? "
                "ORDER BY rowid DESC LIMIT ?",
                (_fts5_match_expression(chunk), seed_limit),
            ).fetchall()
            for row in rows:
                if type(row) is not tuple or len(row) != 1:
                    raise SQLiteStoreSchemaError(
                        "STORE.CANDIDATE_SEED_INVALID"
                    )
                record_id = _record_id(
                    row[0], "STORE.CANDIDATE_SEED_INVALID"
                )
                if record_id not in seen and len(ids) < seed_limit:
                    seen.add(record_id)
                    ids.append(record_id)
        return (("FTS_TRIGRAM", tuple(ids)),)

    if not folded_query:
        return (("GRAM_1", ()),)
    if len(folded_query) == 1:
        sizes = (1,)
    elif len(folded_query) == 2:
        sizes = (2,)
    else:
        sizes = (3, 2, 1)
    stages: list[tuple[str, tuple[int, ...]]] = []
    for size in sizes:
        grams = unique_character_ngrams(folded_query, size)
        per_gram, remainder = divmod(CANDIDATE_SEED_POSTING_CAP, len(grams))
        matched: dict[int, int] = {}
        for gram_ordinal, gram in enumerate(grams):
            gram_limit = per_gram + (1 if gram_ordinal < remainder else 0)
            rows = connection.execute(
                "SELECT record_id FROM tm_gram "
                "WHERE gram_size = ? AND gram = ? "
                "ORDER BY record_id DESC LIMIT ?",
                (size, gram, gram_limit),
            ).fetchall()
            previous_record_id: int | None = None
            for row in rows:
                if (
                    type(row) is not tuple
                    or len(row) != 1
                    or type(row[0]) is not int
                    or row[0] < 1
                    or (
                        previous_record_id is not None
                        and row[0] >= previous_record_id
                    )
                ):
                    raise SQLiteStoreSchemaError(
                        "STORE.CANDIDATE_SEED_INVALID"
                    )
                previous_record_id = row[0]
                matched[row[0]] = matched.get(row[0], 0) + 1
        ordered = tuple(
            record_id
            for record_id, _count in sorted(
                matched.items(), key=lambda item: (-item[1], -item[0])
            )[:seed_limit]
        )
        stages.append((f"GRAM_{size}", ordered))
    return tuple(stages)


def candidate_proof_query_block_uppers(
    connection: sqlite3.Connection,
    *,
    query_terms: tuple[tuple[int, str, int], ...],
) -> dict[tuple[int, int], int]:
    """Aggregate only conservative per-block maxima used by traversal."""

    block_uppers: dict[tuple[int, int], int] = {}
    for offset in range(0, len(query_terms), CANDIDATE_QUERY_CHUNK_SIZE):
        chunk = query_terms[offset : offset + CANDIDATE_QUERY_CHUNK_SIZE]
        values_sql = ",".join("(?, ?, ?)" for _ in chunk)
        rows = connection.execute(
            "WITH query_terms(gram_size, gram, query_frequency) "
            f"AS (VALUES {values_sql}) "
            "SELECT facts.block_id, facts.gram_size, SUM(CASE "
            "WHEN facts.max_term_frequency < query_terms.query_frequency "
            "THEN facts.max_term_frequency ELSE query_terms.query_frequency END) "
            "FROM query_terms CROSS JOIN tm_gram_block_max AS facts "
            "ON facts.gram_size = query_terms.gram_size "
            "AND facts.gram = query_terms.gram "
            "GROUP BY facts.block_id, facts.gram_size",
            tuple(value for term in chunk for value in term),
        ).fetchall()
        for row in rows:
            if type(row) is not tuple or len(row) != 3:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            key = (
                _proof_int(row[0], "STORE.CANDIDATE_PROOF_INVALID"),
                _proof_int(row[1], "STORE.CANDIDATE_PROOF_INVALID"),
            )
            upper = _proof_int(row[2], "STORE.CANDIDATE_PROOF_INVALID")
            if key[1] not in {1, 2} or upper < 1:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            block_uppers[key] = block_uppers.get(key, 0) + upper
    return block_uppers


def candidate_proof_query_maxima_digest(
    blocks: tuple[SQLiteCandidateProofBlock, ...],
) -> str:
    """Digest the exact query-dependent block maxima projection."""

    payload = json.dumps(
        tuple(
            (
                block.block_id,
                block.character_intersection_upper,
                block.bigram_intersection_upper,
            )
            for block in blocks
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def candidate_proof_snapshot(
    connection: sqlite3.Connection,
    *,
    folded_query: str,
    seed_limit: int,
    fts5_available: bool,
    total_record_count: int,
) -> tuple[
    tuple[tuple[str, tuple[int, ...]], ...],
    tuple[SQLiteCandidateProofBlock, ...],
    str,
]:
    """Read authority-free seed stages and proof-frontier rows."""

    query_characters = Counter(folded_query)
    query_bigrams = Counter(
        folded_query[offset : offset + 2]
        for offset in range(max(0, len(folded_query) - 1))
    )
    query_terms = tuple(
        (size, gram, frequency)
        for size, frequencies in ((1, query_characters), (2, query_bigrams))
        for gram, frequency in frequencies.items()
    )
    seed_stages = bounded_seed_stages(
        connection,
        folded_query=folded_query,
        fts5_available=fts5_available,
        seed_limit=seed_limit,
    )
    block_uppers = candidate_proof_query_block_uppers(
        connection,
        query_terms=query_terms,
    )
    block_rows = connection.execute(
        "SELECT block_id, first_record_id, last_record_id, record_count, "
        "min_source_fold_length, max_source_fold_length "
        "FROM tm_candidate_block ORDER BY block_id"
    ).fetchall()
    expected_block_count = (
        total_record_count + CANDIDATE_PROOF_BLOCK_SIZE - 1
    ) // CANDIDATE_PROOF_BLOCK_SIZE
    blocks: list[SQLiteCandidateProofBlock] = []
    covered_records = 0
    for expected_block_id, row in enumerate(block_rows):
        if type(row) is not tuple or len(row) != 6:
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        values = tuple(
            _proof_int(value, "STORE.CANDIDATE_PROOF_INVALID")
            for value in row
        )
        block_id, first_id, last_id, count, minimum, maximum = values
        expected_first = block_id * CANDIDATE_PROOF_BLOCK_SIZE + 1
        expected_count = min(
            CANDIDATE_PROOF_BLOCK_SIZE,
            total_record_count - expected_first + 1,
        )
        if (
            block_id != expected_block_id
            or expected_count < 1
            or first_id != expected_first
            or last_id != expected_first + CANDIDATE_PROOF_BLOCK_SIZE - 1
            or count != expected_count
            or minimum < 1
            or maximum < minimum
        ):
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        blocks.append(
            SQLiteCandidateProofBlock(
                block_id=block_id,
                first_record_id=first_id,
                last_record_id=last_id,
                record_count=count,
                min_source_fold_length=minimum,
                max_source_fold_length=maximum,
                character_intersection_upper=block_uppers.get((block_id, 1), 0),
                bigram_intersection_upper=block_uppers.get((block_id, 2), 0),
            )
        )
        covered_records += count
    if (
        len(blocks) != expected_block_count
        or covered_records != total_record_count
        or any(key[0] >= expected_block_count for key in block_uppers)
    ):
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
    copied_blocks = tuple(blocks)
    return (
        seed_stages,
        copied_blocks,
        candidate_proof_query_maxima_digest(copied_blocks),
    )


def candidate_proof_block_records(
    connection: sqlite3.Connection,
    *,
    folded_query: str,
    block: SQLiteCandidateProofBlock,
    total_record_count: int,
) -> tuple[SQLiteCandidateProofRecord, ...]:
    """Read exact proof facts for one already authorized proof block."""

    query_characters = Counter(folded_query)
    query_bigrams = Counter(
        folded_query[offset : offset + 2]
        for offset in range(max(0, len(folded_query) - 1))
    )
    query_terms = tuple(
        (size, gram, frequency)
        for size, frequencies in ((1, query_characters), (2, query_bigrams))
        for gram, frequency in frequencies.items()
    )
    final_record_id = min(block.last_record_id, total_record_count)
    row = connection.execute(
        "SELECT block_id, first_record_id, last_record_id, record_count, "
        "min_source_fold_length, max_source_fold_length "
        "FROM tm_candidate_block WHERE block_id = ?",
        (block.block_id,),
    ).fetchone()
    expected_block = (
        block.block_id,
        block.first_record_id,
        block.last_record_id,
        block.record_count,
        block.min_source_fold_length,
        block.max_source_fold_length,
    )
    if row != expected_block:
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
    record_rows = connection.execute(
        "SELECT record_id, source_fold_length FROM tm_record "
        "WHERE record_id BETWEEN ? AND ? ORDER BY record_id",
        (block.first_record_id, final_record_id),
    ).fetchall()
    lengths: dict[int, int] = {}
    for offset, record_row in enumerate(record_rows):
        if type(record_row) is not tuple or len(record_row) != 2:
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        record_id = _proof_int(
            record_row[0], "STORE.CANDIDATE_PROOF_INVALID"
        )
        length = _proof_int(record_row[1], "STORE.CANDIDATE_PROOF_INVALID")
        if record_id != block.first_record_id + offset or length < 1:
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        lengths[record_id] = length
    if (
        len(lengths) != block.record_count
        or not lengths
        or min(lengths.values()) != block.min_source_fold_length
        or max(lengths.values()) != block.max_source_fold_length
    ):
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")

    intersections = {record_id: [0, 0] for record_id in lengths}
    exact_maxima: dict[tuple[int, str], int] = {}
    persisted_maxima: dict[tuple[int, str], int] = {}
    for offset in range(0, len(query_terms), CANDIDATE_QUERY_CHUNK_SIZE):
        chunk = query_terms[offset : offset + CANDIDATE_QUERY_CHUNK_SIZE]
        if not chunk:
            continue
        values_sql = ",".join("(?, ?, ?)" for _ in chunk)
        parameters = tuple(value for term in chunk for value in term)
        rows = connection.execute(
            "WITH query_terms(gram_size, gram, query_frequency) "
            f"AS (VALUES {values_sql}) "
            "SELECT facts.record_id, facts.gram_size, facts.gram, "
            "facts.term_frequency, query_terms.query_frequency "
            "FROM tm_gram AS facts JOIN query_terms "
            "ON query_terms.gram_size = facts.gram_size "
            "AND query_terms.gram = facts.gram "
            "WHERE facts.record_id BETWEEN ? AND ? "
            "ORDER BY facts.record_id, facts.gram_size, facts.gram",
            (*parameters, block.first_record_id, final_record_id),
        ).fetchall()
        for fact_row in rows:
            if type(fact_row) is not tuple or len(fact_row) != 5:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            record_id = _proof_int(
                fact_row[0], "STORE.CANDIDATE_PROOF_INVALID"
            )
            size = _proof_int(fact_row[1], "STORE.CANDIDATE_PROOF_INVALID")
            gram = _proof_text(fact_row[2], "STORE.CANDIDATE_PROOF_INVALID")
            frequency = _proof_int(
                fact_row[3], "STORE.CANDIDATE_PROOF_INVALID"
            )
            query_frequency = _proof_int(
                fact_row[4], "STORE.CANDIDATE_PROOF_INVALID"
            )
            if record_id not in lengths or size not in {1, 2} or frequency < 1:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            intersections[record_id][size - 1] += min(
                frequency, query_frequency
            )
            exact_maxima[(size, gram)] = max(
                exact_maxima.get((size, gram), 0), frequency
            )
        maxima_rows = connection.execute(
            "WITH query_terms(gram_size, gram, query_frequency) "
            f"AS (VALUES {values_sql}) "
            "SELECT facts.gram_size, facts.gram, facts.max_term_frequency "
            "FROM tm_gram_block_max AS facts JOIN query_terms "
            "ON query_terms.gram_size = facts.gram_size "
            "AND query_terms.gram = facts.gram "
            "WHERE facts.block_id = ? ORDER BY facts.gram_size, facts.gram",
            (*parameters, block.block_id),
        ).fetchall()
        for maxima_row in maxima_rows:
            if type(maxima_row) is not tuple or len(maxima_row) != 3:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            key = (
                _proof_int(maxima_row[0], "STORE.CANDIDATE_PROOF_INVALID"),
                _proof_text(maxima_row[1], "STORE.CANDIDATE_PROOF_INVALID"),
            )
            frequency = _proof_int(
                maxima_row[2], "STORE.CANDIDATE_PROOF_INVALID"
            )
            if key in persisted_maxima or frequency < 1:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            persisted_maxima[key] = frequency
    if persisted_maxima != exact_maxima:
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
    character_upper = sum(
        min(query_frequency, persisted_maxima.get((1, gram), 0))
        for gram, query_frequency in query_characters.items()
    )
    bigram_upper = sum(
        min(query_frequency, persisted_maxima.get((2, gram), 0))
        for gram, query_frequency in query_bigrams.items()
    )
    if (
        character_upper != block.character_intersection_upper
        or bigram_upper != block.bigram_intersection_upper
    ):
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
    return tuple(
        SQLiteCandidateProofRecord(
            record_id=record_id,
            block_id=block.block_id,
            source_fold_length=length,
            character_multiset_intersection=intersections[record_id][0],
            bigram_multiset_intersection=intersections[record_id][1],
        )
        for record_id, length in lengths.items()
    )


def validate_candidate_proof_blocks(
    connection: sqlite3.Connection,
    *,
    blocks: tuple[SQLiteCandidateProofBlock, ...],
    query_maxima_digest: str,
) -> None:
    """Validate exact persisted block layout and query-maxima binding."""

    block_rows = connection.execute(
        "SELECT block_id, first_record_id, last_record_id, record_count, "
        "min_source_fold_length, max_source_fold_length "
        "FROM tm_candidate_block ORDER BY block_id"
    ).fetchall()
    expected_blocks = tuple(
        (
            block.block_id,
            block.first_record_id,
            block.last_record_id,
            block.record_count,
            block.min_source_fold_length,
            block.max_source_fold_length,
        )
        for block in blocks
    )
    if tuple(block_rows) != expected_blocks:
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
    if candidate_proof_query_maxima_digest(blocks) != query_maxima_digest:
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")


def candidate_proof_dense_phase1(
    connection: sqlite3.Connection,
    *,
    folded_query: str,
    blocks: tuple[SQLiteCandidateProofBlock, ...],
    total_record_count: int,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Return all length and exact-bigram facts for one validated binding."""

    query_bigrams = Counter(
        folded_query[offset : offset + 2]
        for offset in range(max(0, len(folded_query) - 1))
    )
    length_row = connection.execute(
        "SELECT json_group_array(source_fold_length), "
        "MIN(record_id), MAX(record_id), COUNT(*) FROM ("
        "SELECT record_id, source_fold_length FROM tm_record "
        "ORDER BY record_id)"
    ).fetchone()
    if (
        type(length_row) is not tuple
        or len(length_row) != 4
        or type(length_row[0]) is not str
        or type(length_row[1]) is not int
        or length_row[1] != 1
        or type(length_row[2]) is not int
        or length_row[2] != total_record_count
        or type(length_row[3]) is not int
        or length_row[3] != total_record_count
    ):
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
    try:
        length_payload = json.loads(length_row[0])
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID") from error
    if (
        type(length_payload) is not list
        or len(length_payload) != total_record_count
        or any(type(length) is not int or length < 1 for length in length_payload)
    ):
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
    lengths = tuple(length_payload)
    for block in blocks:
        final_record_id = min(block.last_record_id, total_record_count)
        block_lengths = lengths[block.first_record_id - 1 : final_record_id]
        if (
            len(block_lengths) != block.record_count
            or not block_lengths
            or min(block_lengths) != block.min_source_fold_length
            or max(block_lengths) != block.max_source_fold_length
        ):
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")

    bigram_intersections = [0] * (total_record_count + 1)
    query_terms = tuple(query_bigrams.items())
    for offset in range(0, len(query_terms), CANDIDATE_QUERY_CHUNK_SIZE):
        chunk = query_terms[offset : offset + CANDIDATE_QUERY_CHUNK_SIZE]
        values_sql = ",".join("(?, ?)" for _ in chunk)
        row = connection.execute(
            "WITH query_terms(gram, query_frequency) "
            f"AS (VALUES {values_sql}) "
            "SELECT json_group_array(record_id), "
            "json_group_array(intersection) FROM ("
            "SELECT facts.record_id AS record_id, "
            "SUM(MIN(facts.term_frequency, query_terms.query_frequency)) "
            "AS intersection "
            "FROM query_terms CROSS JOIN tm_gram AS facts "
            "ON facts.gram_size = 2 AND facts.gram = query_terms.gram "
            "GROUP BY facts.record_id ORDER BY facts.record_id)",
            tuple(value for term in chunk for value in term),
        ).fetchone()
        if (
            type(row) is not tuple
            or len(row) != 2
            or any(type(payload) is not str for payload in row)
        ):
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        try:
            record_ids = json.loads(row[0])
            intersections = json.loads(row[1])
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise SQLiteStoreSchemaError(
                "STORE.CANDIDATE_PROOF_INVALID"
            ) from error
        if (
            type(record_ids) is not list
            or type(intersections) is not list
            or len(record_ids) != len(intersections)
        ):
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        prior_record_id = 0
        for record_id, intersection in zip(record_ids, intersections, strict=True):
            decoded_id = _proof_int(
                record_id, "STORE.CANDIDATE_PROOF_INVALID"
            )
            decoded_intersection = _proof_int(
                intersection, "STORE.CANDIDATE_PROOF_INVALID"
            )
            if not prior_record_id < decoded_id <= total_record_count:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            bigram_intersections[decoded_id] += decoded_intersection
            prior_record_id = decoded_id
    ordered_intersections = tuple(bigram_intersections[1:])
    return lengths, ordered_intersections


def candidate_proof_dense_phase2(
    connection: sqlite3.Connection,
    *,
    total_record_count: int,
    record_ids: tuple[int, ...],
    source_fold_lengths: tuple[int, ...],
) -> tuple[tuple[int, ...], tuple[str, ...], tuple[int, ...]]:
    """Return the ordered folded-source projection for one validated R set."""

    if len(record_ids) != len(source_fold_lengths):
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
    prior_record_id = 0
    for record_id, source_fold_length in zip(
        record_ids, source_fold_lengths, strict=True
    ):
        if (
            type(record_id) is not int
            or not prior_record_id < record_id <= total_record_count
            or type(source_fold_length) is not int
            or source_fold_length < 1
        ):
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        prior_record_id = record_id
    use_dense_projection = total_record_count - len(record_ids) <= 2_048
    if use_dense_projection:
        requested = iter(record_ids)
        current = next(requested, None)
        excluded: list[int] = []
        for possible_record_id in range(1, total_record_count + 1):
            if current == possible_record_id:
                current = next(requested, None)
            else:
                excluded.append(possible_record_id)
        if current is not None or len(excluded) > 2_048:
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        where = ""
        parameters: tuple[int, ...] = ()
        if excluded:
            placeholders = ",".join("?" for _ in excluded)
            where = f"WHERE record_id NOT IN ({placeholders}) "
            parameters = tuple(excluded)
        projection_row = connection.execute(
            "SELECT json_group_array(record_id), "
            "json_group_array(source_fold_v1), "
            "json_group_array(source_fold_length) FROM ("
            "SELECT record_id, source_fold_v1, source_fold_length "
            f"FROM tm_record {where}ORDER BY record_id)",
            parameters,
        ).fetchone()
    else:
        request_json = json.dumps(
            record_ids,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        projection_row = connection.execute(
            "SELECT json_group_array(record_id), "
            "json_group_array(source_fold_v1), "
            "json_group_array(source_fold_length) FROM ("
            "SELECT records.record_id, records.source_fold_v1, "
            "records.source_fold_length "
            "FROM json_each(?) AS requested "
            "CROSS JOIN tm_record AS records "
            "ON records.record_id = CAST(requested.value AS INTEGER) "
            "ORDER BY CAST(requested.key AS INTEGER))",
            (request_json,),
        ).fetchone()
    if (
        type(projection_row) is not tuple
        or len(projection_row) != 3
        or any(type(payload) is not str for payload in projection_row)
    ):
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
    try:
        projected_record_ids = json.loads(projection_row[0])
        projected_source_folds = json.loads(projection_row[1])
        projected_source_lengths = json.loads(projection_row[2])
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID") from error
    if (
        type(projected_record_ids) is not list
        or type(projected_source_folds) is not list
        or type(projected_source_lengths) is not list
        or len(projected_record_ids) != len(record_ids)
        or len(projected_source_folds) != len(record_ids)
        or len(projected_source_lengths) != len(record_ids)
    ):
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
    copied_record_ids: list[int] = []
    copied_source_folds: list[str] = []
    copied_source_lengths: list[int] = []
    for ordinal, projected_record_id in enumerate(projected_record_ids):
        projected_source_fold = projected_source_folds[ordinal]
        projected_source_length = projected_source_lengths[ordinal]
        expected_source_length = source_fold_lengths[ordinal]
        if (
            type(projected_record_id) is not int
            or projected_record_id != record_ids[ordinal]
            or type(projected_source_fold) is not str
            or not projected_source_fold
            or type(projected_source_length) is not int
            or projected_source_length != expected_source_length
            or len(projected_source_fold) != expected_source_length
        ):
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        copied_record_ids.append(projected_record_id)
        copied_source_folds.append(projected_source_fold)
        copied_source_lengths.append(projected_source_length)
    ordered_record_ids = tuple(copied_record_ids)
    ordered_source_folds = tuple(copied_source_folds)
    ordered_source_lengths = tuple(copied_source_lengths)
    return ordered_record_ids, ordered_source_folds, ordered_source_lengths


type ValidatedCandidateWritePlan = tuple[
    tuple[tuple[int, int, str, int], ...],
    tuple[int, ...],
]


def project_candidate_write_plan(
    plan: ValidatedCandidateWritePlan,
    *,
    record_ids_by_ordinal: tuple[tuple[int, int], ...],
    folded_sources_by_ordinal: tuple[tuple[int, str], ...],
) -> tuple[
    tuple[tuple[int, str, int, int], ...],
    tuple[tuple[str, int], ...],
    tuple[tuple[int, str], ...],
    tuple[tuple[int, int, str, int], ...],
]:
    """Privately project one caller-validated bounded candidate plan."""

    if type(plan) is not tuple or len(plan) != 2:
        raise TypeError("validated candidate plan is invalid")
    gram_rows, fts_origin_ordinals = plan
    if type(gram_rows) is not tuple or type(fts_origin_ordinals) is not tuple:
        raise TypeError("validated candidate plan is invalid")
    if type(record_ids_by_ordinal) is not tuple or type(
        folded_sources_by_ordinal
    ) is not tuple:
        raise TypeError("candidate ordinal projections must be tuples")
    record_ids: dict[int, int] = {}
    for item in record_ids_by_ordinal:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not int
            or type(item[1]) is not int
            or item[1] < 1
            or item[0] in record_ids
        ):
            raise ValueError("candidate record identity projection is invalid")
        record_ids[item[0]] = item[1]
    folded_sources: dict[int, str] = {}
    for item in folded_sources_by_ordinal:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not int
            or type(item[1]) is not str
            or not item[1]
            or item[0] in folded_sources
        ):
            raise ValueError("candidate folded-source projection is invalid")
        folded_sources[item[0]] = item[1]

    seen_grams: set[tuple[int, int, str]] = set()
    projected_gram_rows: list[tuple[int, str, int, int]] = []
    for row in gram_rows:
        if type(row) is not tuple or len(row) != 4:
            raise TypeError("validated gram row is invalid")
        origin_ordinal, gram_size, gram, term_frequency = row
        key = (origin_ordinal, gram_size, gram)
        if (
            type(origin_ordinal) is not int
            or origin_ordinal not in record_ids
            or type(gram_size) is not int
            or gram_size not in {1, 2, 3}
            or type(gram) is not str
            or len(gram) != gram_size
            or type(term_frequency) is not int
            or term_frequency < 1
            or key in seen_grams
        ):
            raise ValueError("validated gram row is invalid")
        seen_grams.add(key)
        projected_gram_rows.append(
            (gram_size, gram, record_ids[origin_ordinal], term_frequency)
        )
    seen_fts_ordinals: set[int] = set()
    projected_fts_rows: list[tuple[str, int]] = []
    for origin_ordinal in fts_origin_ordinals:
        if (
            type(origin_ordinal) is not int
            or origin_ordinal not in record_ids
            or origin_ordinal not in folded_sources
            or origin_ordinal in seen_fts_ordinals
        ):
            raise ValueError("validated FTS row is invalid")
        seen_fts_ordinals.add(origin_ordinal)
        projected_fts_rows.append(
            (folded_sources[origin_ordinal], record_ids[origin_ordinal])
        )
    proof_records = tuple(
        (record_id, folded_sources[origin_ordinal])
        for origin_ordinal, record_id in sorted(
            record_ids.items(), key=lambda pair: pair[1]
        )
    )
    proof_gram_rows = tuple(
        (record_ids[origin_ordinal], gram_size, gram, term_frequency)
        for origin_ordinal, gram_size, gram, term_frequency in gram_rows
    )
    return (
        tuple(projected_gram_rows),
        tuple(projected_fts_rows),
        proof_records,
        proof_gram_rows,
    )


def insert_candidate_gram_rows(
    connection: sqlite3.Connection,
    plan: ValidatedCandidateWritePlan,
    *,
    record_ids_by_ordinal: tuple[tuple[int, int], ...],
    folded_sources_by_ordinal: tuple[tuple[int, str], ...],
) -> None:
    gram_rows, _fts_rows, _proof_records, _proof_gram_rows = (
        project_candidate_write_plan(
            plan,
            record_ids_by_ordinal=record_ids_by_ordinal,
            folded_sources_by_ordinal=folded_sources_by_ordinal,
        )
    )
    connection.executemany(
        "INSERT INTO tm_gram(gram_size, gram, record_id, term_frequency) "
        "VALUES (?, ?, ?, ?)",
        gram_rows,
    )


def insert_candidate_fts_rows(
    connection: sqlite3.Connection,
    plan: ValidatedCandidateWritePlan,
    *,
    record_ids_by_ordinal: tuple[tuple[int, int], ...],
    folded_sources_by_ordinal: tuple[tuple[int, str], ...],
) -> None:
    _gram_rows, fts_rows, _proof_records, _proof_gram_rows = (
        project_candidate_write_plan(
            plan,
            record_ids_by_ordinal=record_ids_by_ordinal,
            folded_sources_by_ordinal=folded_sources_by_ordinal,
        )
    )
    if fts_rows:
        connection.executemany(
            "INSERT INTO tm_fts(source_fold_v1, record_id) VALUES (?, ?)",
            fts_rows,
        )


def maintain_candidate_proof_summaries(
    connection: sqlite3.Connection,
    *,
    plan: ValidatedCandidateWritePlan,
    record_ids_by_ordinal: tuple[tuple[int, int], ...],
    folded_sources_by_ordinal: tuple[tuple[int, str], ...],
) -> None:
    """Maintain exact proof blocks inside the caller-owned transaction."""

    _gram_rows, _fts_rows, proof_records, proof_gram_rows = (
        project_candidate_write_plan(
            plan,
            record_ids_by_ordinal=record_ids_by_ordinal,
            folded_sources_by_ordinal=folded_sources_by_ordinal,
        )
    )

    for record_id, folded_source in proof_records:
        source_fold_length = len(folded_source)
        block_id = (record_id - 1) // CANDIDATE_PROOF_BLOCK_SIZE
        first_record_id = block_id * CANDIDATE_PROOF_BLOCK_SIZE + 1
        last_record_id = first_record_id + CANDIDATE_PROOF_BLOCK_SIZE - 1
        connection.execute(
            "INSERT INTO tm_candidate_block("
            "block_id, first_record_id, last_record_id, record_count, "
            "min_source_fold_length, max_source_fold_length) "
            "VALUES (?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(block_id) DO UPDATE SET "
            "record_count = record_count + 1, "
            "min_source_fold_length = min(min_source_fold_length, "
            "excluded.min_source_fold_length), "
            "max_source_fold_length = max(max_source_fold_length, "
            "excluded.max_source_fold_length)",
            (
                block_id,
                first_record_id,
                last_record_id,
                source_fold_length,
                source_fold_length,
            ),
        )
    for record_id, gram_size, gram, term_frequency in proof_gram_rows:
        if gram_size not in {1, 2}:
            continue
        block_id = (record_id - 1) // CANDIDATE_PROOF_BLOCK_SIZE
        connection.execute(
            "INSERT INTO tm_gram_block_max("
            "gram_size, gram, block_id, max_term_frequency) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(gram_size, gram, block_id) DO UPDATE SET "
            "max_term_frequency = max(max_term_frequency, "
            "excluded.max_term_frequency)",
            (gram_size, gram, block_id, term_frequency),
        )


def project_streamed_candidate_index(
    candidate_records: tuple[tuple[int, str], ...],
    record_ids_by_ordinal: tuple[tuple[int, int], ...],
    candidate_gram_facts: tuple[tuple[int, int, str, int], ...],
    *,
    fts5_available: bool,
) -> tuple[
    tuple[tuple[tuple[int, str, int, int], ...], ...],
    tuple[tuple[str, int], ...],
    tuple[tuple[int, int, int, int, int, int], ...],
    tuple[tuple[int, str, int, int], ...],
]:
    """Project one bounded streamed chunk's candidate facts."""

    if type(candidate_records) is not tuple:
        raise TypeError("candidate_records must be a built-in tuple")
    if type(record_ids_by_ordinal) is not tuple:
        raise TypeError("record_ids_by_ordinal must be a built-in tuple")
    if type(candidate_gram_facts) is not tuple:
        raise TypeError("candidate_gram_facts must be a built-in tuple")
    if type(fts5_available) is not bool:
        raise TypeError("fts5_available must be a built-in bool")
    record_ids: dict[int, int] = {}
    for item in record_ids_by_ordinal:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not int
            or type(item[1]) is not int
            or item[1] < 1
            or item[0] in record_ids
        ):
            raise ValueError("candidate record identity projection is invalid")
        record_ids[item[0]] = item[1]
    prepared: list[tuple[int, str, int]] = []
    seen_ordinals: set[int] = set()
    for item in candidate_records:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not int
            or type(item[1]) is not str
            or not item[1]
            or item[0] in seen_ordinals
            or item[0] not in record_ids
        ):
            raise ValueError("streamed candidate record is invalid")
        seen_ordinals.add(item[0])
        prepared.append((item[0], item[1], record_ids[item[0]]))
    if seen_ordinals != set(record_ids):
        raise ValueError("streamed candidate record identity is incomplete")

    gram_sizes = (1, 2) if fts5_available else (1, 2, 3)
    grams_by_size: dict[int, dict[str, list[tuple[int, int]]]] = {
        gram_size: {} for gram_size in gram_sizes
    }
    block_stats: dict[int, list[int]] = {}
    block_maxima: dict[tuple[int, str, int], int] = {}
    fts_rows: list[tuple[str, int]] = []
    for _origin_ordinal, folded_source, record_id in prepared:
        source_fold_length = len(folded_source)
        block_id = (record_id - 1) // CANDIDATE_PROOF_BLOCK_SIZE
        stats = block_stats.setdefault(
            block_id,
            [0, source_fold_length, source_fold_length],
        )
        stats[0] += 1
        stats[1] = min(stats[1], source_fold_length)
        stats[2] = max(stats[2], source_fold_length)
        if fts5_available:
            fts_rows.append((folded_source, record_id))
    seen_gram_keys: set[tuple[int, int, str]] = set()
    for fact in candidate_gram_facts:
        if type(fact) is not tuple or len(fact) != 4:
            raise TypeError("streamed candidate gram fact is invalid")
        origin_ordinal, gram_size, gram, term_frequency = fact
        key = (origin_ordinal, gram_size, gram)
        if (
            type(origin_ordinal) is not int
            or origin_ordinal not in record_ids
            or type(gram_size) is not int
            or gram_size not in gram_sizes
            or type(gram) is not str
            or len(gram) != gram_size
            or type(term_frequency) is not int
            or term_frequency < 1
            or key in seen_gram_keys
        ):
            raise ValueError("streamed candidate gram fact is invalid")
        seen_gram_keys.add(key)
        record_id = record_ids[origin_ordinal]
        grams_by_size[gram_size].setdefault(gram, []).append(
            (record_id, term_frequency)
        )
        if gram_size in {1, 2}:
            block_id = (record_id - 1) // CANDIDATE_PROOF_BLOCK_SIZE
            maximum_key = (gram_size, gram, block_id)
            block_maxima[maximum_key] = max(
                block_maxima.get(maximum_key, 0),
                term_frequency,
            )
    gram_row_groups: list[tuple[tuple[int, str, int, int], ...]] = []
    for gram_size in gram_sizes:
        bucket = grams_by_size[gram_size]
        gram_row_groups.append(
            tuple(
                (gram_size, gram, record_id, term_frequency)
                for gram in sorted(bucket)
                for record_id, term_frequency in bucket[gram]
            )
        )
    block_rows = tuple(
        (
            block_id,
            block_id * CANDIDATE_PROOF_BLOCK_SIZE + 1,
            (block_id + 1) * CANDIDATE_PROOF_BLOCK_SIZE,
            stats[0],
            stats[1],
            stats[2],
        )
        for block_id, stats in sorted(block_stats.items())
    )
    maximum_rows = tuple(
        (gram_size, gram, block_id, maximum)
        for (gram_size, gram, block_id), maximum in sorted(
            block_maxima.items()
        )
    )
    return tuple(gram_row_groups), tuple(fts_rows), block_rows, maximum_rows


def insert_streamed_candidate_gram_rows(
    connection: sqlite3.Connection,
    candidate_records: tuple[tuple[int, str], ...],
    record_ids_by_ordinal: tuple[tuple[int, int], ...],
    candidate_gram_facts: tuple[tuple[int, int, str, int], ...],
    *,
    fts5_available: bool,
) -> None:
    gram_row_groups, _fts_rows, _block_rows, _maximum_rows = (
        project_streamed_candidate_index(
            candidate_records,
            record_ids_by_ordinal,
            candidate_gram_facts,
            fts5_available=fts5_available,
        )
    )
    for gram_rows in gram_row_groups:
        connection.executemany(
            "INSERT INTO tm_gram("
            "gram_size, gram, record_id, term_frequency) "
            "VALUES (?, ?, ?, ?)",
            gram_rows,
        )


def insert_streamed_candidate_fts_rows(
    connection: sqlite3.Connection,
    candidate_records: tuple[tuple[int, str], ...],
    record_ids_by_ordinal: tuple[tuple[int, int], ...],
    candidate_gram_facts: tuple[tuple[int, int, str, int], ...],
    *,
    fts5_available: bool,
) -> None:
    _gram_row_groups, fts_rows, _block_rows, _maximum_rows = (
        project_streamed_candidate_index(
            candidate_records,
            record_ids_by_ordinal,
            candidate_gram_facts,
            fts5_available=fts5_available,
        )
    )
    if fts_rows:
        connection.executemany(
            "INSERT INTO tm_fts(source_fold_v1, record_id) VALUES (?, ?)",
            fts_rows,
        )


def insert_streamed_candidate_proof_rows(
    connection: sqlite3.Connection,
    candidate_records: tuple[tuple[int, str], ...],
    record_ids_by_ordinal: tuple[tuple[int, int], ...],
    candidate_gram_facts: tuple[tuple[int, int, str, int], ...],
    *,
    fts5_available: bool,
) -> None:
    _gram_row_groups, _fts_rows, block_rows, maximum_rows = (
        project_streamed_candidate_index(
            candidate_records,
            record_ids_by_ordinal,
            candidate_gram_facts,
            fts5_available=fts5_available,
        )
    )
    connection.executemany(
        "INSERT INTO tm_candidate_block("
        "block_id, first_record_id, last_record_id, record_count, "
        "min_source_fold_length, max_source_fold_length) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(block_id) DO UPDATE SET "
        "record_count = record_count + excluded.record_count, "
        "min_source_fold_length = min(min_source_fold_length, "
        "excluded.min_source_fold_length), "
        "max_source_fold_length = max(max_source_fold_length, "
        "excluded.max_source_fold_length)",
        block_rows,
    )
    connection.executemany(
        "INSERT INTO tm_gram_block_max("
        "gram_size, gram, block_id, max_term_frequency) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(gram_size, gram, block_id) DO UPDATE SET "
        "max_term_frequency = max(max_term_frequency, "
        "excluded.max_term_frequency)",
        maximum_rows,
    )


def streamed_stage_secondary_index_inventory(
    connection: sqlite3.Connection,
) -> tuple[str, ...]:
    """Read the exact rebuildable secondary-index inventory."""

    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND name IN (?, ?, ?, ?) ORDER BY name",
        _STREAMED_STAGE_SECONDARY_INDEX_NAMES,
    ).fetchall()
    inventory: list[str] = []
    for row in rows:
        if (
            type(row) is not tuple
            or len(row) != 1
            or type(row[0]) is not str
        ):
            raise CandidateProofIndexError(
                "streamed secondary-index inventory row is invalid"
            )
        inventory.append(row[0])
    return tuple(inventory)


def suspend_streamed_stage_secondary_indexes(
    connection: sqlite3.Connection,
) -> None:
    """Drop the exact frozen rebuildable secondary-index set."""

    connection.execute("DROP INDEX idx_tm_exact")
    connection.execute("DROP INDEX idx_tm_context_speaker")
    connection.execute("DROP INDEX idx_tm_gram_lookup")
    connection.execute("DROP INDEX idx_tm_gram_block_lookup")


def restore_streamed_stage_secondary_indexes(
    connection: sqlite3.Connection,
) -> None:
    """Build the exact frozen secondary-index statement set."""

    connection.execute(
        "\n    CREATE INDEX idx_tm_exact\n"
        "    ON tm_record(source_raw, record_id DESC)\n"
        "    "
    )
    connection.execute(
        "\n    CREATE INDEX idx_tm_context_speaker\n"
        "    ON tm_record(source_raw, speaker_raw, record_id DESC)\n"
        "    "
    )
    connection.execute(
        "\n    CREATE INDEX idx_tm_gram_lookup\n"
        "    ON tm_gram(gram_size, gram, record_id)\n"
        "    "
    )
    connection.execute(
        "\n    CREATE INDEX idx_tm_gram_block_lookup\n"
        "    ON tm_gram_block_max(gram_size, gram, block_id)\n"
        "    "
    )


_CANDIDATE_PROJECTION_DIGEST_VERSION = "candidate-projection-digest-v2"
_CANDIDATE_PROJECTION_DIGEST_GRAM_CHUNK_ROWS = 50_000


def _candidate_projection_table_digest(table: str) -> Any:
    digest = hashlib.sha256()
    digest.update(_CANDIDATE_PROJECTION_DIGEST_VERSION.encode("ascii"))
    digest.update(b"\0")
    digest.update(table.encode("ascii"))
    digest.update(b"\0")
    return digest


def _update_candidate_projection_digest(
    digest: Any,
    row: tuple[object, ...],
) -> None:
    framed = bytearray()
    for value in row:
        if type(value) is int and not isinstance(value, bool):
            encoded = str(value).encode("ascii")
            framed.extend(b"i")
        elif type(value) is str:
            encoded = value.encode("utf-8")
            framed.extend(b"s")
        else:
            raise CandidateProofIndexError(
                "candidate projection digest fact is invalid"
            )
        framed.extend(str(len(encoded)).encode("ascii"))
        framed.extend(b":")
        framed.extend(encoded)
        framed.extend(b";")
    framed.extend(b"\n")
    digest.update(framed)


def _finish_candidate_projection_digest(
    table_digests: dict[str, Any],
    *,
    fts5_available: bool,
) -> str:
    expected_tables = (
        "tm_gram",
        "tm_candidate_block",
        "tm_gram_block_max",
        *(("tm_fts",) if fts5_available else ()),
    )
    if tuple(table_digests) != expected_tables:
        raise CandidateProofIndexError(
            "candidate projection digest domain is invalid"
        )
    digest = hashlib.sha256()
    digest.update(_CANDIDATE_PROJECTION_DIGEST_VERSION.encode("ascii"))
    digest.update(b"\0complete\0")
    for table in expected_tables:
        digest.update(table.encode("ascii"))
        digest.update(b"\0")
        digest.update(table_digests[table].digest())
    return digest.hexdigest()


def _update_candidate_gram_projection_digest(
    connection: sqlite3.Connection,
    digest: Any,
    *,
    gram_chunk_rows: int,
) -> None:
    """Hash term-major gram rows in bounded SQLite-native chunks."""

    if type(gram_chunk_rows) is not int or gram_chunk_rows < 1:
        raise TypeError("gram_chunk_rows must be a positive integer")
    count_row = connection.execute(
        "SELECT COUNT(*) FROM tm_gram NOT INDEXED"
    ).fetchone()
    if (
        type(count_row) is not tuple
        or len(count_row) != 1
        or type(count_row[0]) is not int
        or count_row[0] < 0
    ):
        raise CandidateProofIndexError(
            "candidate projection digest count is invalid"
        )
    actual_count = count_row[0]
    processed_count = 0
    last_rowid: int | None = None
    row_payload = (
        "json_array("
        "typeof(rowid), hex(CAST(rowid AS BLOB)), "
        "typeof(record_id), hex(CAST(record_id AS BLOB)), "
        "typeof(gram_size), hex(CAST(gram_size AS BLOB)), "
        "typeof(gram), hex(CAST(gram AS BLOB)), "
        "typeof(term_frequency), hex(CAST(term_frequency AS BLOB)), "
        "rowid)"
    )
    while True:
        where = ""
        parameters: tuple[object, ...] = (
            gram_chunk_rows,
        )
        if last_rowid is not None:
            where = "WHERE rowid > ?"
            parameters = (
                last_rowid,
                gram_chunk_rows,
            )
        row = connection.execute(
            "SELECT group_concat(row_payload, char(10)), COUNT(*) FROM ("
            f"SELECT {row_payload} AS row_payload "
            f"FROM tm_gram NOT INDEXED {where} "
            "ORDER BY rowid LIMIT ?)",
            parameters,
        ).fetchone()
        if (
            type(row) is not tuple
            or len(row) != 2
            or type(row[1]) is not int
            or row[1] < 0
        ):
            raise CandidateProofIndexError(
                "candidate projection digest chunk is invalid"
            )
        chunk_count = row[1]
        if chunk_count == 0:
            if row[0] is not None:
                raise CandidateProofIndexError(
                    "candidate projection digest chunk is invalid"
                )
            break
        if (
            chunk_count > gram_chunk_rows
            or type(row[0]) is not str
        ):
            raise CandidateProofIndexError(
                "candidate projection digest chunk is invalid"
            )
        payload = row[0]
        encoded = payload.encode("utf-8")
        digest.update(b"chunk:")
        digest.update(str(chunk_count).encode("ascii"))
        digest.update(b":")
        digest.update(str(len(encoded)).encode("ascii"))
        digest.update(b":")
        digest.update(encoded)
        digest.update(b";")
        try:
            tail = json.loads(payload.rsplit("\n", 1)[-1])
        except (TypeError, ValueError) as error:
            raise CandidateProofIndexError(
                "candidate projection digest tail is invalid"
            ) from error
        if (
            type(tail) is not list
            or len(tail) != 11
            or type(tail[10]) is not int
            or isinstance(tail[10], bool)
        ):
            raise CandidateProofIndexError(
                "candidate projection digest tail is invalid"
            )
        next_rowid = tail[10]
        if last_rowid is not None and next_rowid <= last_rowid:
            raise CandidateProofIndexError(
                "candidate projection digest order is invalid"
            )
        last_rowid = next_rowid
        processed_count += chunk_count
        if processed_count > actual_count:
            raise CandidateProofIndexError(
                "candidate projection digest count is invalid"
            )
    if processed_count != actual_count:
        raise CandidateProofIndexError(
            "candidate projection digest count is invalid"
        )


def candidate_proof_projection_digest(
    connection: sqlite3.Connection,
    *,
    fts5_available: bool,
    gram_chunk_rows: int = _CANDIDATE_PROJECTION_DIGEST_GRAM_CHUNK_ROWS,
) -> str:
    """Hash every actual candidate projection row in canonical order."""

    if type(connection) is not sqlite3.Connection:
        raise TypeError("connection must be an exact sqlite3 connection")
    if type(fts5_available) is not bool:
        raise TypeError("fts5_available must be a built-in bool")
    table_digests: dict[str, Any] = {
        "tm_gram": _candidate_projection_table_digest("tm_gram")
    }
    _update_candidate_gram_projection_digest(
        connection,
        table_digests["tm_gram"],
        gram_chunk_rows=gram_chunk_rows,
    )
    block_digest = _candidate_projection_table_digest("tm_candidate_block")
    for row in connection.execute(
        "SELECT block_id, first_record_id, last_record_id, record_count, "
        "min_source_fold_length, max_source_fold_length "
        "FROM tm_candidate_block ORDER BY block_id"
    ):
        _update_candidate_projection_digest(block_digest, row)
    table_digests["tm_candidate_block"] = block_digest
    maximum_digest = _candidate_projection_table_digest("tm_gram_block_max")
    for row in connection.execute(
        "SELECT block_id, gram_size, gram, max_term_frequency "
        "FROM tm_gram_block_max ORDER BY block_id, gram_size, gram"
    ):
        _update_candidate_projection_digest(maximum_digest, row)
    table_digests["tm_gram_block_max"] = maximum_digest
    if fts5_available:
        fts_digest = _candidate_projection_table_digest("tm_fts")
        for row in connection.execute(
            "SELECT record_id, source_fold_v1 FROM tm_fts "
            "ORDER BY record_id"
        ):
            _update_candidate_projection_digest(fts_digest, row)
        table_digests["tm_fts"] = fts_digest
    return _finish_candidate_projection_digest(
        table_digests,
        fts5_available=fts5_available,
    )


def _validate_candidate_proof_index_core(
    connection: sqlite3.Connection,
    *,
    required_sizes: tuple[int, ...],
    fts5_available: bool,
    include_projection_digest: bool,
    gram_chunk_rows: int = _CANDIDATE_PROJECTION_DIGEST_GRAM_CHUNK_ROWS,
) -> tuple[tuple[tuple[int, int], ...], int, str | None]:
    """Stream-recompute exact facts, optionally binding projection rows."""

    if type(connection) is not sqlite3.Connection:
        raise TypeError("connection must be an exact sqlite3 connection")
    if type(required_sizes) is not tuple or any(
        type(size) is not int for size in required_sizes
    ):
        raise TypeError("required_sizes must be a tuple of built-in integers")
    if type(fts5_available) is not bool:
        raise TypeError("fts5_available must be a built-in bool")
    if type(include_projection_digest) is not bool:
        raise TypeError("include_projection_digest must be a built-in bool")
    expected_sizes = (1, 2) if fts5_available else (1, 2, 3)
    if required_sizes != expected_sizes:
        raise ValueError("candidate gram sizes do not match the index path")
    try:
        worker_row = connection.execute("PRAGMA threads=2").fetchone()
    except sqlite3.Error as error:
        raise CandidateProofIndexError(
            "candidate proof worker configuration is invalid"
        ) from error
    if (
        type(worker_row) is not tuple
        or len(worker_row) != 1
        or type(worker_row[0]) is not int
        or worker_row[0] not in {0, 1, 2}
    ):
        raise CandidateProofIndexError(
            "candidate proof worker configuration is invalid"
        )

    def proof_int(value: object) -> int:
        if type(value) is not int:
            raise CandidateProofIndexError("candidate integer fact is invalid")
        return value

    def proof_text(value: object) -> str:
        if type(value) is not str or not value:
            raise CandidateProofIndexError("candidate text fact is invalid")
        return value

    record_cursor = connection.execute(
        "SELECT record_id, source_raw, source_fold_v1, source_fold_length "
        "FROM tm_record ORDER BY record_id"
    )
    gram_cursor = connection.execute(
        "SELECT record_id, gram_size, gram, term_frequency FROM tm_gram "
        "ORDER BY record_id, gram_size, gram"
    )
    block_cursor = connection.execute(
        "SELECT block_id, first_record_id, last_record_id, record_count, "
        "min_source_fold_length, max_source_fold_length "
        "FROM tm_candidate_block ORDER BY block_id"
    )
    maximum_cursor = connection.execute(
        "SELECT block_id, gram_size, gram, max_term_frequency "
        "FROM tm_gram_block_max ORDER BY block_id, gram_size, gram"
    )
    fts_cursor = (
        connection.execute(
            "SELECT record_id, source_fold_v1 FROM tm_fts ORDER BY record_id"
        )
        if fts5_available
        else None
    )
    current_gram = gram_cursor.fetchone()
    current_block = block_cursor.fetchone()
    current_maximum = maximum_cursor.fetchone()
    current_fts = fts_cursor.fetchone() if fts_cursor is not None else None
    gram_counts = {size: 0 for size in required_sizes}
    fts_count = 0
    block_id: int | None = None
    block_lengths: list[int] = []
    block_maxima: dict[tuple[int, str], int] = {}

    def flush_block() -> None:
        nonlocal current_block, current_maximum
        if block_id is None:
            return
        first_record_id = block_id * CANDIDATE_PROOF_BLOCK_SIZE + 1
        expected_block = (
            block_id,
            first_record_id,
            first_record_id + CANDIDATE_PROOF_BLOCK_SIZE - 1,
            len(block_lengths),
            min(block_lengths),
            max(block_lengths),
        )
        if current_block is None or tuple(
            proof_int(value) for value in current_block
        ) != expected_block:
            raise CandidateProofIndexError("candidate block fact is invalid")
        current_block = block_cursor.fetchone()
        actual_maxima: dict[tuple[int, str], int] = {}
        while current_maximum is not None:
            maximum_block_id = proof_int(current_maximum[0])
            if maximum_block_id != block_id:
                break
            size = proof_int(current_maximum[1])
            gram = proof_text(current_maximum[2])
            frequency = proof_int(current_maximum[3])
            key = (size, gram)
            if size not in {1, 2} or frequency < 1 or key in actual_maxima:
                raise CandidateProofIndexError(
                    "candidate block maximum is invalid"
                )
            actual_maxima[key] = frequency
            current_maximum = maximum_cursor.fetchone()
        if actual_maxima != block_maxima:
            raise CandidateProofIndexError(
                "candidate block maximum is invalid"
            )

    expected_record_id = 1
    for record_row in record_cursor:
        record_id = proof_int(record_row[0])
        source_raw = proof_text(record_row[1])
        stored_folded_source = proof_text(record_row[2])
        source_fold_length = proof_int(record_row[3])
        folded_source = fold_text_value_v1(source_raw)
        if (
            not folded_source
            or record_id != expected_record_id
            or stored_folded_source != folded_source
            or source_fold_length != len(folded_source)
        ):
            raise CandidateProofIndexError("candidate record fact is invalid")
        expected_record_id += 1
        next_block_id = (record_id - 1) // CANDIDATE_PROOF_BLOCK_SIZE
        if block_id is not None and next_block_id != block_id:
            flush_block()
            block_lengths.clear()
            block_maxima.clear()
        block_id = next_block_id
        block_lengths.append(source_fold_length)

        actual_grams: dict[tuple[int, str], int] = {}
        while current_gram is not None:
            gram_record_id = proof_int(current_gram[0])
            if gram_record_id != record_id:
                break
            size = proof_int(current_gram[1])
            gram = proof_text(current_gram[2])
            frequency = proof_int(current_gram[3])
            key = (size, gram)
            if (
                size not in required_sizes
                or frequency < 1
                or key in actual_grams
            ):
                raise CandidateProofIndexError(
                    "candidate gram fact is invalid"
                )
            actual_grams[key] = frequency
            current_gram = gram_cursor.fetchone()
        expected_grams = {
            (size, gram): frequency
            for size in required_sizes
            for gram, frequency in character_ngram_frequencies(
                folded_source, size
            )
        }
        if actual_grams != expected_grams:
            raise CandidateProofIndexError("candidate gram fact is invalid")
        for (size, gram), frequency in actual_grams.items():
            gram_counts[size] += 1
            if size in {1, 2}:
                key = (size, gram)
                block_maxima[key] = max(
                    block_maxima.get(key, 0), frequency
                )

        if fts5_available:
            if current_fts is None:
                raise CandidateProofIndexError(
                    "candidate FTS fact is invalid"
                )
            fts_record_id = proof_int(current_fts[0])
            fts_source = proof_text(current_fts[1])
            if (fts_record_id, fts_source) != (record_id, folded_source):
                raise CandidateProofIndexError(
                    "candidate FTS fact is invalid"
                )
            fts_count += 1
            current_fts = (
                fts_cursor.fetchone() if fts_cursor is not None else None
            )

    flush_block()
    if any(
        row is not None
        for row in (current_gram, current_block, current_maximum)
    ):
        raise CandidateProofIndexError(
            "candidate proof index has extra rows"
        )
    if current_fts is not None:
        raise CandidateProofIndexError("candidate FTS fact is invalid")
    return (
        tuple(sorted(gram_counts.items())),
        fts_count,
        (
            candidate_proof_projection_digest(
                connection,
                fts5_available=fts5_available,
                gram_chunk_rows=gram_chunk_rows,
            )
            if include_projection_digest
            else None
        ),
    )


def validate_candidate_proof_index_with_digest(
    connection: sqlite3.Connection,
    *,
    required_sizes: tuple[int, ...],
    fts5_available: bool,
    gram_chunk_rows: int = _CANDIDATE_PROJECTION_DIGEST_GRAM_CHUNK_ROWS,
) -> tuple[tuple[tuple[int, int], ...], int, str]:
    gram_counts, fts_count, projection_digest = (
        _validate_candidate_proof_index_core(
            connection,
            required_sizes=required_sizes,
            fts5_available=fts5_available,
            include_projection_digest=True,
            gram_chunk_rows=gram_chunk_rows,
        )
    )
    if type(projection_digest) is not str:
        raise AssertionError("candidate projection digest is missing")
    return gram_counts, fts_count, projection_digest


def validate_candidate_proof_index(
    connection: sqlite3.Connection,
    *,
    required_sizes: tuple[int, ...],
    fts5_available: bool,
    gram_chunk_rows: int = _CANDIDATE_PROJECTION_DIGEST_GRAM_CHUNK_ROWS,
) -> tuple[tuple[tuple[int, int], ...], int]:
    """Stream-recompute exact length, TF, block and optional FTS facts."""

    gram_counts, fts_count, projection_digest = (
        _validate_candidate_proof_index_core(
            connection,
            required_sizes=required_sizes,
            fts5_available=fts5_available,
            include_projection_digest=False,
            gram_chunk_rows=gram_chunk_rows,
        )
    )
    if projection_digest is not None:
        raise AssertionError("candidate projection digest is unexpected")
    return gram_counts, fts_count


__all__ = [
    "CANDIDATE_QUERY_CHUNK_SIZE",
    "CANDIDATE_SEED_POSTING_CAP",
    "bounded_seed_stages",
    "candidate_proof_block_records",
    "candidate_proof_dense_phase1",
    "candidate_proof_dense_phase2",
    "candidate_proof_query_block_uppers",
    "candidate_proof_query_maxima_digest",
    "candidate_proof_projection_digest",
    "candidate_proof_snapshot",
    "candidate_recall_snapshot",
    "fts5_candidate_ids",
    "fts5_candidate_ids_for_trigrams",
    "gram_candidate_overlaps",
    "insert_candidate_fts_rows",
    "insert_candidate_gram_rows",
    "insert_streamed_candidate_fts_rows",
    "insert_streamed_candidate_gram_rows",
    "insert_streamed_candidate_proof_rows",
    "maintain_candidate_proof_summaries",
    "project_candidate_write_plan",
    "project_streamed_candidate_index",
    "restore_streamed_stage_secondary_indexes",
    "streamed_stage_secondary_index_inventory",
    "suspend_streamed_stage_secondary_indexes",
    "validate_candidate_proof_blocks",
    "validate_candidate_proof_index",
    "validate_candidate_proof_index_with_digest",
]
