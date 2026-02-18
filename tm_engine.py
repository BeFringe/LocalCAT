"""
LocalCAT Phase 2: Translation Memory (TM) Engine
Module for managing Translation Memory using JSONL format.
"""

import json
import time
import os
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any
from pathlib import Path

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
    metadata: Dict[str, Any] = None

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

# =============================================================================
# 2. TM Engine Implementation
# =============================================================================

class TMEngine:
    """
    Core logic for Translation Memory.
    Uses append-only JSONL storage.
    """
    def __init__(self, tm_path: str):
        self.tm_path = Path(tm_path)
        # In-memory index for exact matching: {source_text: TMMatch}
        # Last write wins policy for duplicates
        self._exact_index: Dict[str, TMMatch] = {}
        self._load_tm()

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
        if not unit.text or not target:
            return False

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
    assert match_updated.target == "您好"
    print(f"  [PASS] Updated translation: {match_updated.target}")
    
    # Verify persistence of update
    engine_final = TMEngine(test_tm_file)
    match_final = engine_final.query_exact("Hello")
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
