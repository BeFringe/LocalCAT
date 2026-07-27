"""
LocalCAT Phase 3: Logic Controller
Stateless logic layer that orchestrates calls to Engine layer.
Strictly follows the data flow pattern established in stress_runner.py.
"""

import os
from typing import Dict, Any, List
from glossary_engine import GlossaryEngine, GlossaryLoader
from tm_engine import TMEngine

# Paths configuration (mirroring runner logic)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GLOSSARY_FILE = os.path.join(BASE_DIR, "terms.csv")
TM_FILE = os.path.join(BASE_DIR, "tm.jsonl")

class LogicController:
    """
    Stateless forwarder between Frontend and Engine.
    Responsibility:
    1. Initialize engines.
    2. Receive raw text.
    3. Query TM first.
    4. If TM miss, Query Glossary.
    5. Return structured dict (NO formatting).
    """
    
    def __init__(self):
        # Initialize engines
        self.glossary_engine = GlossaryEngine()
        self.glossary_loader = GlossaryLoader(self.glossary_engine)
        
        # Load Glossary
        if os.path.exists(GLOSSARY_FILE):
            self.glossary_loader.load_file(GLOSSARY_FILE)
            
        # Initialize TM
        self.tm_engine = TMEngine(TM_FILE)

    def get_suggestions(self, text: str) -> Dict[str, Any]:
        """
        Main entry point for frontend requests.
        Returns a dictionary mirroring the structure verified in Phase 2 stress test.
        """
        if not text:
            return {"status": "NO_MATCH"}

        # Step A: Check TM (Exact Match)
        # Priority Rule: TM > Glossary
        tm_match = self.tm_engine.query_exact(text)
        
        if tm_match:
            # Scenario A: Priority Conflict / TM Hit
            # We return ONLY the fields validated in stress test output
            return {
                "status": "TM_HIT",
                "tm_match": {
                    "source": tm_match.source,
                    "target": tm_match.target,
                    "match_type": tm_match.match_type,
                    "similarity": tm_match.similarity
                }
            }
        
        # Step B: Check Glossary (Term Extraction)
        # Only executed if TM misses
        terms = self.glossary_engine.extract_terms(text)
        
        if terms:
            # Scenario B: Terms Found
            # We return raw term hits list
            term_list = []
            for t in terms:
                term_list.append({
                    "source_term": t.source_term,
                    "target_term": t.target_term,
                    "start_index": t.start_index,
                    "end_index": t.end_index,
                    "glossary_source": t.glossary_source
                })
            
            return {
                "status": "TERMS_FOUND",
                "terms": term_list
            }

        # Scenario C: No Match
        return {"status": "NO_MATCH"}

# =============================================================================
# Self-Test / Verification (Mirroring stress_runner.py output)
# =============================================================================
if __name__ == "__main__":
    print("=== Logic Controller Self-Test ===\n")
    
    controller = LogicController()
    
    # Test Case 1: TM Hit (default tm.jsonl fixture)
    # Input: "System Ready"
    # Expected: TM_HIT, no terms
    res1 = controller.get_suggestions("System Ready")
    print(f"Case 1 (TM Hit): {res1}")
    assert res1["status"] == "TM_HIT"
    assert res1["tm_match"]["target"] == "系统就绪"
    
    # Test Case 2: Nested Terms
    # Input: "A high performance Glossary Engine"
    # Expected: TERMS_FOUND for the two terms in default terms.csv
    res2 = controller.get_suggestions("A high performance Glossary Engine")
    print(f"Case 2 (Terms): {res2}")
    assert res2["status"] == "TERMS_FOUND"
    assert len(res2["terms"]) == 2
    
    # Test Case 3: No Match
    # Input: "Submit"
    res3 = controller.get_suggestions("Submit")
    print(f"Case 3 (No Match): {res3}")
    assert res3["status"] == "NO_MATCH"
    
    print("\nLogic Controller Self-Test Passed.")
