"""
LocalCAT Phase 2: Translation Memory (TM) Engine
Module for managing Translation Memory using JSONL format.
"""

import json
import time
import os
import sqlite3
import stat
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any
from pathlib import Path

from tm_activation_journal import (
    ActivationPreparationError,
    _lstat_any_entry,
    _lstat_activation_journal_identity,
    _parse_activation_journal_bytes,
    _read_activation_journal_file,
)
from tm_contracts import CanonicalResourceIdentity, TMRecordDraft
from tm_sqlite_store import (
    ResourceStoreCoordinator,
    SQLiteStoreLifecycleError,
    SQLiteStoreSchemaError,
    SQLiteTMStore,
)


_CANONICAL_RECOVERY_FAILED_CODE = "TM.CANONICAL_RECOVERY_FAILED"
_CANONICAL_AMBIGUOUS_CODE = "TM.CANONICAL_ACTIVATION_AMBIGUOUS"
_CANONICAL_IDENTITY_MISSING_CODE = "TM.CANONICAL_IDENTITY_MISSING"
_CANONICAL_UNHEALTHY_CODE = "TM.CANONICAL_UNHEALTHY"
_CANONICAL_REATTESTATION_REQUIRED_CODE = (
    "TM.CANONICAL_REATTESTATION_REQUIRED"
)


# =============================================================================
# 1. Data Contracts (Immutable)
# =============================================================================

@dataclass(frozen=True)
class SourceUnit:
    """
    Represents a minimal translation unit.
    Strictly follows Blueprint V1.
    """
    id: str                 # Unique identifier (Hash or Sequence ID)
    text: str               # Source text to be translated
    context_prev: Optional[str] = None
    context_next: Optional[str] = None
    speaker: Optional[str] = None
    file_source: str = ""
    metadata: Optional[Dict[str, Any]] = None

@dataclass(frozen=True)
class TMMatch:
    """
    Represents a translation memory match result.
    Strictly follows Blueprint V1.
    """
    source: str             # Source text in TM
    target: str             # Translation in TM
    similarity: float       # Similarity score (0.0 - 1.0)
    match_type: str         # "EXACT", "FUZZY", "CONTEXT"
    tm_source: str          # Source TM filename
    usage_count: int = 0
    last_used: str = ""     # ISO timestamp


def _configured_jsonl_path(tm_path: Path) -> Path:
    """Resolve the configured JSONL path used for canonical identity."""

    return tm_path.expanduser().resolve()


def _canonical_artifact_paths(
    configured_jsonl: Path,
) -> tuple[Path, Path, Path, Path, Path]:
    """Deterministic adjacent activation artifact paths (sidecar family).

    Mirrors ``CanonicalResourceIdentity`` and the activation journal
    naming so the facade can bootstrap durable facts before constructing
    the coordinator that re-proves them.
    """

    resolved = _configured_jsonl_path(configured_jsonl)
    sidecar = resolved.with_name(f"{resolved.name}.sqlite3")
    manifest = resolved.with_name(f"{resolved.name}.localcat-snapshot.json")
    journal = sidecar.with_name(
        f".{sidecar.name}.localcat-activation-journal.json"
    )
    terminal = sidecar.with_name(
        f".{sidecar.name}.localcat-activation-terminal.json"
    )
    marker = sidecar.with_name(
        f".{sidecar.name}.localcat-activated-lineage.json"
    )
    return sidecar, manifest, journal, terminal, marker


def _sidecar_activation_facts(sidecar: Path) -> tuple[str, str]:
    """Bootstrap ``(resource_id, canonical_store_id)`` from sidecar meta.

    This is a read-only identity bootstrap only: recovery re-proves every
    fact before any generation view is trusted.
    """

    try:
        initial = os.lstat(sidecar)
        if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
            raise ValueError(_CANONICAL_IDENTITY_MISSING_CODE)
        connection = sqlite3.connect(
            f"{sidecar.as_uri()}?mode=ro",
            uri=True,
        )
        try:
            connection.execute("PRAGMA query_only = ON")
            rows = connection.execute(
                "SELECT key, value FROM tm_meta "
                "WHERE key IN ('resource_id', 'canonical_store_id')"
            ).fetchall()
        finally:
            connection.close()
        final = os.lstat(sidecar)
        if (
            not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or (final.st_dev, final.st_ino)
            != (initial.st_dev, initial.st_ino)
        ):
            raise ValueError(_CANONICAL_IDENTITY_MISSING_CODE)
    except (OSError, sqlite3.DatabaseError) as error:
        raise ValueError(_CANONICAL_IDENTITY_MISSING_CODE) from error
    meta = {str(key): str(value) for key, value in rows}
    resource_id = meta.get("resource_id")
    canonical_store_id = meta.get("canonical_store_id")
    if not resource_id or not canonical_store_id:
        raise ValueError(_CANONICAL_IDENTITY_MISSING_CODE)
    return resource_id, canonical_store_id


def _journal_activation_facts(
    journal: Path,
    *,
    expected_journal_path: Path,
) -> tuple[str, str]:
    """Bootstrap identity facts from one durable journal/terminal record.

    A pending replacement journal is bound to its candidate store id, so
    the coordinator is bootstrapped with the prior id when one is
    recorded; recovery then re-proves and adopts the correct authority.
    """

    try:
        journal_identity = _lstat_activation_journal_identity(journal)
        if journal_identity is None:
            raise ValueError(_CANONICAL_IDENTITY_MISSING_CODE)
        payload, observed_identity = _read_activation_journal_file(
            journal,
            journal_identity,
        )
        if observed_identity != journal_identity:
            raise ValueError(_CANONICAL_IDENTITY_MISSING_CODE)
        record = _parse_activation_journal_bytes(
            payload,
            expected_journal_path=expected_journal_path,
        )
    except ActivationPreparationError as error:
        raise ValueError(_CANONICAL_AMBIGUOUS_CODE) from error
    canonical_store_id = (
        record.prior_canonical_store_id
        if record.prior_canonical_store_id is not None
        else record.canonical_store_id
    )
    return record.resource_id, canonical_store_id


def _activation_facts(configured_jsonl: Path) -> tuple[str, str] | None:
    """Return durable ``(resource_id, canonical_store_id)`` or ``None``.

    ``None`` means the resource carries no activation artifacts at all and
    stays on the legacy JSONL path.  Any present-but-ambiguous or tampered
    durable fact raises ``ValueError`` (fail-stop) instead of silently
    continuing on JSONL.
    """

    sidecar, _manifest, journal, terminal, marker = (
        _canonical_artifact_paths(configured_jsonl)
    )
    if not any(
        _lstat_any_entry(path)
        for path in (sidecar, _manifest, journal, terminal, marker)
    ):
        return None
    if _lstat_any_entry(sidecar):
        return _sidecar_activation_facts(sidecar)
    if _lstat_any_entry(journal):
        return _journal_activation_facts(
            journal,
            expected_journal_path=journal,
        )
    if _lstat_any_entry(terminal):
        return _journal_activation_facts(
            terminal,
            expected_journal_path=journal,
        )
    raise ValueError(_CANONICAL_AMBIGUOUS_CODE)


def canonical_authority_facts(
    configured_jsonl: Path,
) -> tuple[str, str] | None:
    """Return strict durable identity facts without opening the authority.

    This read-only Core projection exists for an explicit maintenance owner
    when the ordinary canonical open has already refused the resource.  It
    never hydrates, repairs, or grants query authority.
    """

    path = _configured_jsonl_path(configured_jsonl)
    facts = _activation_facts(path)
    if facts is None:
        return None
    resource_id, canonical_store_id = facts
    if (
        type(resource_id) is not str
        or not resource_id.strip()
        or type(canonical_store_id) is not str
        or not canonical_store_id.strip()
    ):
        raise ValueError(_CANONICAL_AMBIGUOUS_CODE)
    return resource_id, canonical_store_id


def open_canonical_tm_store(
    configured_jsonl: Path,
    *,
    drain_timeout_seconds: float = 5.0,
) -> SQLiteTMStore | None:
    """Open one activated resource's canonical store, or ``None`` for legacy.

    Task 6.1/6.2 shared seam: a resource with no (or a provably cancelled
    first) activation authority returns ``None`` so callers keep the
    existing JSONL path.  A completed activation is re-proven and hydrated
    as the one canonical generation without requiring the activation-time
    snapshot parity: the ``SourceBindingMonitor`` derives
    ``VERIFIED_CURRENT`` / ``VERIFIED_HISTORY`` / ``SOURCE_DIVERGED`` on
    first observation, so a normal canonical save/import and a latched
    divergence reopen on the same canonical lineage.  Any
    present-but-ambiguous or tampered durable fact, an unhealthy prior
    canonical, an unclosed activation, or a recovery failure raises
    ``ValueError`` with a stable code-only message; JSONL is never an
    implicit fallback for an activated resource.
    """

    try:
        facts = _activation_facts(configured_jsonl)
    except ActivationPreparationError as error:
        raise ValueError(
            f"{_CANONICAL_RECOVERY_FAILED_CODE}:{error.code}"
        ) from error
    if facts is None:
        return None
    resource_id, canonical_store_id = facts
    coordinator: ResourceStoreCoordinator | None = None
    try:
        identity = CanonicalResourceIdentity.from_configured_jsonl(
            resource_id,
            _configured_jsonl_path(configured_jsonl),
        )
        coordinator = ResourceStoreCoordinator(
            resource_identity=identity,
            canonical_store_id=canonical_store_id,
            drain_timeout_seconds=drain_timeout_seconds,
        )
        report = coordinator.rehydrate_runtime_authority()
        if report is None or (
            report.action == "CANCELLED" and report.generation is None
        ):
            return None
        if coordinator.current_generation is None:
            raise ValueError(_CANONICAL_UNHEALTHY_CODE)
        store = SQLiteTMStore.from_coordinator(coordinator)
        _ = store.canonical_revision()
    except (
        ActivationPreparationError,
        SQLiteStoreSchemaError,
        SQLiteStoreLifecycleError,
        sqlite3.DatabaseError,
        OSError,
    ) as error:
        if (
            isinstance(error, ActivationPreparationError)
            and error.code
            == "ACTIVATION.ACTIVE_ATTESTATION_IDENTITY_INVALID"
            and coordinator is not None
            and coordinator.completed_authority_requires_reattestation()
        ):
            raise ValueError(
                _CANONICAL_REATTESTATION_REQUIRED_CODE
            ) from error
        code = getattr(error, "code", None)
        if isinstance(code, str) and code.startswith("ACTIVATION."):
            raise ValueError(
                f"{_CANONICAL_RECOVERY_FAILED_CODE}:{code}"
            ) from error
        raise ValueError(_CANONICAL_UNHEALTHY_CODE) from error
    return store

# =============================================================================
# 2. TM Engine Implementation
# =============================================================================

class TMEngine:
    """
    Legacy TM compatibility facade (Task 6.2).

    Decides, per resource, between the legacy JSONL engine and the
    canonical SQLite authority.  Before first physical activation (or a
    provably cancelled first activation) every public operation keeps the
    exact legacy JSONL last-write-wins behavior; after activation every
    query/save uses the canonical store under one stable generation
    lease, and the JSONL is never loaded, written, or used as an implicit
    fallback.
    """

    def __init__(
        self,
        tm_path: str,
        *,
        active: bool = True,
        lookup: bool = True,
        update: bool = True,
        drain_timeout_seconds: float = 5.0,
    ) -> None:
        for field_name, value in (
            ("active", active),
            ("lookup", lookup),
            ("update", update),
        ):
            if type(value) is not bool:
                raise TypeError(f"{field_name} must be bool")
        self.tm_path = Path(tm_path)
        self._active = active
        self._lookup = lookup
        self._update = update
        # In-memory index for exact matching: {source_text: TMMatch}
        # Last write wins policy for duplicates
        self._exact_index: Dict[str, TMMatch] = {}
        self._store = open_canonical_tm_store(
            self.tm_path,
            drain_timeout_seconds=drain_timeout_seconds,
        )
        if self._store is None:
            self._load_tm()

    @property
    def canonical_active(self) -> bool:
        """True when this facade is bound to the canonical store."""

        return self._store is not None

    @property
    def canonical_store(self) -> SQLiteTMStore | None:
        """Return the store selected by this facade's one open-time decision.

        This read-only projection exists only so the application composition
        root can turn the already-completed ``TMEngine`` classification into
        a canonical or legacy runtime binding.  It never accepts a caller
        claim, never reopens the resource, and cannot force legacy fallback.
        """

        return self._store

    def _load_tm(self):
        """Loads TM from JSONL file into memory index."""

        if not self.tm_path.exists():
            return

        try:
            with open(self.tm_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        # Construct TMMatch object
                        match = TMMatch(
                            source=record.get('source', ''),
                            target=record.get('target', ''),
                            similarity=1.0,  # Stored records are implicitly 100% matches of themselves
                            match_type="EXACT",
                            tm_source=self.tm_path.name,
                            usage_count=record.get('usage_count', 0),
                            last_used=record.get('last_used', '')
                        )
                        # Index by source text.
                        # Since we read sequentially, later records overwrite earlier ones (correct behavior).
                        if match.source:
                            self._exact_index[match.source] = match
                    except json.JSONDecodeError:
                        print(f"Warning: Skipping invalid JSON line in {self.tm_path}")
        except Exception as e:
            print(f"Error loading TM {self.tm_path}: {e}")

    def save_record(self, unit: SourceUnit, target: str) -> bool:
        """
        Appends a new translation record to the TM file.
        Updates in-memory index immediately.
        """

        if not self._active or not self._update:
            return False
        if not unit.text or not target:
            return False

        if self._store is not None:
            try:
                draft = TMRecordDraft(
                    source_raw=unit.text,
                    target_raw=target,
                    speaker_raw=unit.speaker,
                    context_prev_raw=unit.context_prev,
                    context_next_raw=unit.context_next,
                    file_source=unit.file_source,
                    provenance=(("source", "local-write"),),
                )
                self._store.append(draft)
            except (
                SQLiteStoreSchemaError,
                SQLiteStoreLifecycleError,
                sqlite3.DatabaseError,
                OSError,
            ) as e:
                print(f"Error saving to TM {self.tm_path}: {e}")
                return False
            return True

        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())

        # Prepare record for storage
        # We store more fields than TMMatch needs, for future proofing/context matching
        record = {
            'source': unit.text,
            'target': target,
            'context_prev': unit.context_prev,
            'context_next': unit.context_next,
            'speaker': unit.speaker,
            'file_source': unit.file_source,
            'last_used': timestamp,
            'usage_count': 1 # Initial count
        }

        try:
            # Append to file
            with open(self.tm_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
            
            # Update memory index
            new_match = TMMatch(
                source=unit.text,
                target=target,
                similarity=1.0,
                match_type="EXACT",
                tm_source=self.tm_path.name,
                usage_count=1,
                last_used=timestamp
            )
            self._exact_index[unit.text] = new_match
            return True
        except Exception as e:
            print(f"Error saving to TM {self.tm_path}: {e}")
            return False

    def query_exact(self, text: str) -> Optional[TMMatch]:
        """
        Queries the TM for an exact match.
        Returns TMMatch or None.
        """

        if not self._active or not self._lookup:
            return None
        if self._store is not None:
            records = self._store.exact_records(text)
            if not records:
                return None
            record = records[0]
            return TMMatch(
                source=record.source_raw,
                target=record.target_raw,
                similarity=1.0,
                match_type="EXACT",
                tm_source=self.tm_path.name,
                usage_count=0,
                last_used="",
            )
        return self._exact_index.get(text)

# =============================================================================
# 3. File Handler Implementation (PO Support)
# =============================================================================

class POHandler:
    """
    Parses .po files into SourceUnits.
    Simple parser implementation to avoid external dependencies like polib for now.
    """
    
    @staticmethod
    def parse_file(file_path: str) -> list[SourceUnit]:
        units = []
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"PO file not found: {file_path}")

        current_msgctxt = None
        current_msgid = None
        current_msgstr = None # Not used in SourceUnit but good to track state
        
        # Simple state machine for PO parsing
        # Note: This is a basic implementation. Multiline strings require more robust handling.
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                
                if line.startswith('msgctxt '):
                    current_msgctxt = POHandler._extract_string(line)
                elif line.startswith('msgid '):
                    current_msgid = POHandler._extract_string(line)
                elif line.startswith('msgstr '):
                    # End of a unit block (usually)
                    if current_msgid: # Ignore empty header msgid ""
                        unit = SourceUnit(
                            id=f"{path.name}_{len(units)}", # Simple ID generation
                            text=current_msgid,
                            context_prev=current_msgctxt, # Mapping context to prev for now as per instructions
                            file_source=path.name
                        )
                        units.append(unit)
                    
                    # Reset state
                    current_msgctxt = None
                    current_msgid = None
                
                i += 1
                
        except Exception as e:
            print(f"Error parsing PO file {path}: {e}")
            
        return units

    @staticmethod
    def _extract_string(line: str) -> str:
        """Helper to extract content between quotes."""
        # Finds first and last quote
        first = line.find('"')
        last = line.rfind('"')
        if first != -1 and last != -1 and last > first:
            return line[first+1 : last]
        return ""

# =============================================================================
# 4. Self-Test / Verification
# =============================================================================

if __name__ == "__main__":
    print("Running TM Engine Self-Tests...")
    
    test_tm_file = "test_tm.jsonl"
    
    # Clean up previous test run
    if os.path.exists(test_tm_file):
        os.remove(test_tm_file)
        
    # --- Case A: Persistence Verification ---
    print("\n--- Case A: Persistence Verification ---")
    engine = TMEngine(test_tm_file)
    
    unit1 = SourceUnit(id="1", text="Hello", context_prev=None)
    unit2 = SourceUnit(id="2", text="World", context_prev=None)
    
    engine.save_record(unit1, "你好")
    engine.save_record(unit2, "世界")
    
    # Re-instantiate engine to simulate app restart
    engine_reloaded = TMEngine(test_tm_file)
    match1 = engine_reloaded.query_exact("Hello")
    match2 = engine_reloaded.query_exact("World")
    
    assert match1 is not None and match1.target == "你好"
    assert match2 is not None and match2.target == "世界"
    print(f"  [PASS] Saved and reloaded: {match1.target}, {match2.target}")

    # --- Case B: Overwrite Verification ---
    print("\n--- Case B: Overwrite Verification ---")
    # Save a new translation for "Hello"
    engine_reloaded.save_record(unit1, "您好") # Changed from 你好 to 您好
    
    match_updated = engine_reloaded.query_exact("Hello")
    assert match_updated is not None
    assert match_updated.target == "您好"
    print(f"  [PASS] Updated translation: {match_updated.target}")
    
    # Verify persistence of update
    engine_final = TMEngine(test_tm_file)
    match_final = engine_final.query_exact("Hello")
    assert match_final is not None
    assert match_final.target == "您好"
    print("  [PASS] Update persisted to disk")

    # --- Case C: PO Parsing Verification ---
    print("\n--- Case C: PO Parsing Verification ---")
    test_po_content = """
msgctxt "Menu Context"
msgid "Open File"
msgstr "打开文件"

msgid "Save"
msgstr "保存"
"""
    test_po_file = "test_temp.po"
    with open(test_po_file, "w", encoding="utf-8") as f:
        f.write(test_po_content)
        
    parsed_units = POHandler.parse_file(test_po_file)
    
    # Check Unit 1
    assert parsed_units[0].text == "Open File"
    assert parsed_units[0].context_prev == "Menu Context"
    
    # Check Unit 2
    assert parsed_units[1].text == "Save"
    assert parsed_units[1].context_prev is None
    
    print(f"  [PASS] Parsed {len(parsed_units)} units correctly.")
    
    # Cleanup
    if os.path.exists(test_tm_file):
        os.remove(test_tm_file)
    if os.path.exists(test_po_file):
        os.remove(test_po_file)
        
    print("\nAll tests passed successfully.")
