"""SQLite candidate read data plane for caller-owned connections.

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

from tm_candidate_store_contracts import (
    CANDIDATE_PROOF_BLOCK_SIZE,
    SQLiteCandidateProofBlock,
    SQLiteCandidateProofRecord,
    SQLiteStoreSchemaError,
    unique_character_ngrams,
)


CANDIDATE_QUERY_CHUNK_SIZE = 256
CANDIDATE_SEED_POSTING_CAP = 4096


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


__all__ = [
    "CANDIDATE_QUERY_CHUNK_SIZE",
    "CANDIDATE_SEED_POSTING_CAP",
    "bounded_seed_stages",
    "candidate_proof_block_records",
    "candidate_proof_dense_phase1",
    "candidate_proof_dense_phase2",
    "candidate_proof_query_block_uppers",
    "candidate_proof_query_maxima_digest",
    "candidate_proof_snapshot",
    "candidate_recall_snapshot",
    "fts5_candidate_ids",
    "fts5_candidate_ids_for_trigrams",
    "gram_candidate_overlaps",
    "validate_candidate_proof_blocks",
]
