"""
LocalCAT Phase 2: Structural Integrity Stress Test
Verifies data flow stability and structural correctness under stress conditions.
Strip UI Shadow: No TermHighlighter, raw data output only.
"""

import os
import sys
from glossary_engine import GlossaryEngine, GlossaryLoader
from tm_engine import TMEngine, POHandler, SourceUnit

# =============================================================================
# Configuration / Paths
# =============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GLOSSARY_FILE = os.path.join(BASE_DIR, "terms_stress.csv")
TM_FILE = os.path.join(BASE_DIR, "tm_stress.jsonl")
PO_FILE = os.path.join(BASE_DIR, "stress_test.po")

def main():
    print("=== LocalCAT Phase 2 Structural Integrity Test ===\n")

    # 1. Initialize Engines
    print("[1] Initializing Engines...")
    glossary_engine = GlossaryEngine()
    glossary_loader = GlossaryLoader(glossary_engine)
    if os.path.exists(GLOSSARY_FILE):
        glossary_loader.load_file(GLOSSARY_FILE)
    
    tm_engine = TMEngine(TM_FILE)

    # 2. Load Source Content
    print("[2] Loading Source Content...")
    if os.path.exists(PO_FILE):
        units = POHandler.parse_file(PO_FILE)
    else:
        print("ERROR: PO file not found")
        return

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

if __name__ == "__main__":
    main()
