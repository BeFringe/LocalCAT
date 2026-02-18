"""
LocalCAT Phase 2: Integration Verification Runner
Integrates GlossaryEngine, TMEngine, and POHandler to verify data flow.
"""

import os
import sys
from glossary_engine import GlossaryEngine, GlossaryLoader, TermHighlighter
from tm_engine import TMEngine, POHandler, SourceUnit

# =============================================================================
# Configuration / Paths
# =============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GLOSSARY_FILE = os.path.join(BASE_DIR, "terms.csv")
TM_FILE = os.path.join(BASE_DIR, "tm.jsonl")
PO_FILE = os.path.join(BASE_DIR, "example.po")

def main():
    print("=== LocalCAT Phase 2 Integration Test ===\n")

    # 1. Initialize Engines
    print("[1] Initializing Engines...")
    
    # Glossary Setup
    glossary_engine = GlossaryEngine()
    glossary_loader = GlossaryLoader(glossary_engine)
    if os.path.exists(GLOSSARY_FILE):
        glossary_loader.load_file(GLOSSARY_FILE)
        print(f"    - Glossary loaded from {GLOSSARY_FILE}")
    else:
        print(f"    - ERROR: {GLOSSARY_FILE} not found!")
        return

    # TM Setup
    tm_engine = TMEngine(TM_FILE)
    print(f"    - TM loaded from {TM_FILE}")

    # 2. Load Source Content
    print("\n[2] Loading Source Content...")
    if os.path.exists(PO_FILE):
        units = POHandler.parse_file(PO_FILE)
        print(f"    - Parsed {len(units)} units from {PO_FILE}")
    else:
        print(f"    - ERROR: {PO_FILE} not found!")
        return

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

if __name__ == "__main__":
    main()
