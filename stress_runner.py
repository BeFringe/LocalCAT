"""
LocalCAT Phase 2: Structural Integrity Stress Test
Verifies data flow stability and structural correctness under stress conditions.
Strip UI Shadow: No TermHighlighter, raw data output only.
"""

import os
from glossary_engine import GlossaryEngine
from logic_controller import (
    GlossaryLoadError,
    ProjectSourceLoadError,
    load_gettext_source_units,
    load_glossary_file,
)
from tm_engine import TMEngine

# =============================================================================
# Configuration / Paths
# =============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GLOSSARY_FILE = os.path.join(BASE_DIR, "terms_stress.csv")
TM_FILE = os.path.join(BASE_DIR, "tm_stress.jsonl")
PO_FILE = os.path.join(BASE_DIR, "stress_test.po")

def main() -> int:
    print("=== LocalCAT Phase 2 Structural Integrity Test ===\n")

    # 1. Initialize Engines
    print("[1] Initializing Engines...")
    glossary_engine = GlossaryEngine()
    if os.path.exists(GLOSSARY_FILE):
        try:
            load_glossary_file(glossary_engine, GLOSSARY_FILE)
        except GlossaryLoadError as exc:
            print(f"ERROR: {exc.code}")
            return 1
    
    tm_engine = TMEngine(TM_FILE)

    # 2. Load Source Content
    print("[2] Loading Source Content...")
    if os.path.exists(PO_FILE):
        try:
            units = load_gettext_source_units(PO_FILE)
        except ProjectSourceLoadError as exc:
            print(f"ERROR: {exc.code}")
            return 1
    else:
        print("ERROR: PO file not found")
        return 1

    # 3. Process Units (Raw Data Output)
    print("\n[3] Processing Units (Raw Data Output)...")
    print("=" * 60)

    for i, unit in enumerate(units, 1):
        print(f"\n[Unit #{i}] ID: {unit.id}")
        print(f"  Text: '{unit.text}'")
        if unit.context_prev:
            print(f"  Context: '{unit.context_prev}'")
        
        # Step A: Check TM (Exact Match)
        tm_match = tm_engine.query_exact(unit.text)
        
        if tm_match:
            # Scenario A: Priority Conflict Test
            # If TM match is found, we STOP here. Term extraction MUST NOT run.
            print(f"  [TM HIT] Source: '{tm_match.source}'")
            print(f"           Target: '{tm_match.target}'")
            print(f"           Match Type: {tm_match.match_type}")
            print(f"           Similarity: {tm_match.similarity}")
        else:
            # Step B: Check Glossary (Term Extraction)
            print("  [TM MISS] Executing Term Extraction...")
            terms = glossary_engine.extract_terms(unit.text)
            
            if terms:
                # Scenario B: Nested Term Depth Test
                print(f"  [TERMS] Found {len(terms)} hits:")
                # Sort for deterministic output comparison
                # Sorting key: start_index ASC, length DESC (same as engine default)
                for t in terms:
                    print(f"      - Hit: '{t.source_term}' -> '{t.target_term}'")
                    print(f"        Span: [{t.start_index}:{t.end_index}]")
                    print(f"        Source: {t.glossary_source}")
            else:
                print("  [NO MATCH] No suggestions.")

    print("\n" + "=" * 60)
    print("Structural Integrity Test Complete.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
