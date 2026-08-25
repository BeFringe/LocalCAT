"""
LocalCAT Phase 2: Integration Verification Runner
Integrates GlossaryEngine, TMEngine, and the Parser Application adapter.
"""

import os
from glossary_engine import GlossaryEngine, TermHighlighter
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
GLOSSARY_FILE = os.path.join(BASE_DIR, "terms.csv")
TM_FILE = os.path.join(BASE_DIR, "tm.jsonl")
PO_FILE = os.path.join(BASE_DIR, "example.po")

def main() -> int:
    print("=== LocalCAT Phase 2 Integration Test ===\n")

    # 1. Initialize Engines
    print("[1] Initializing Engines...")
    
    # Glossary Setup
    glossary_engine = GlossaryEngine()
    if os.path.exists(GLOSSARY_FILE):
        try:
            load_glossary_file(glossary_engine, GLOSSARY_FILE)
        except GlossaryLoadError as exc:
            print(f"    - ERROR: {exc.code}")
            return 1
        print(f"    - Glossary loaded from {GLOSSARY_FILE}")
    else:
        print(f"    - ERROR: {GLOSSARY_FILE} not found!")
        return 1

    # TM Setup
    tm_engine = TMEngine(TM_FILE)
    print(f"    - TM loaded from {TM_FILE}")

    # 2. Load Source Content
    print("\n[2] Loading Source Content...")
    if os.path.exists(PO_FILE):
        try:
            units = load_gettext_source_units(PO_FILE)
        except ProjectSourceLoadError as exc:
            print(f"    - ERROR: {exc.code}")
            return 1
        print(f"    - Parsed {len(units)} units from {PO_FILE}")
    else:
        print(f"    - ERROR: {PO_FILE} not found!")
        return 1

    # 3. Process Units (Simulation Loop)
    print("\n[3] Processing Units...")
    print("=" * 60)

    for i, unit in enumerate(units, 1):
        print(f"\nUnit #{i}: [{unit.text}]")
        
        # Step A: Check TM (Exact Match)
        tm_match = tm_engine.query_exact(unit.text)
        
        if tm_match:
            # Scenario 1: TM Match Found
            print(f"  [TM HIT]  Source: {tm_match.tm_source}")
            print(f"  >>> Translation: {tm_match.target}")
        else:
            # Step B: Check Glossary (Term Extraction)
            print("  [TM MISS] Checking Glossary...")
            terms = glossary_engine.extract_terms(unit.text)
            
            if terms:
                # Scenario 2: Terms Found
                highlighted_text = TermHighlighter.highlight(unit.text, terms)
                print(f"  [TERMS] Found {len(terms)} term(s)")
                print(f"  >>> Highlight: {highlighted_text}")
                for t in terms:
                    print(f"      - {t.source_term} -> {t.target_term} ({t.glossary_source})")
            else:
                # Scenario 3: No Match
                print("  [NO MATCH] No suggestions available.")

    print("\n" + "=" * 60)
    print("Integration Test Complete.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
