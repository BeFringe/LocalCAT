"""
LocalCAT Phase 1: Glossary Engine
Module for term extraction and glossary management.
"""

import csv
from dataclasses import dataclass
from typing import List, Dict, Optional, Set
from pathlib import Path

# =============================================================================
# 1. Data Contract (Immutable)
# =============================================================================

@dataclass(frozen=True)
class TermHit:
    """
    Represents a term match found in the source text.
    Strictly follows Blueprint V1.
    """
    source_term: str        # The matched term in source text
    target_term: str        # The translation of the term
    start_index: int        # Start offset in source text (inclusive)
    end_index: int          # End offset in source text (exclusive)
    glossary_source: str    # Name of the glossary file/source
    definition: Optional[str] = None
    priority: int = 1

# =============================================================================
# 2. Core Engine Implementation
# =============================================================================

class TrieNode:
    """Helper class for Trie-based term matching."""
    __slots__ = ('children', 'is_end_of_word', 'term_data')
    
    def __init__(self):
        self.children: Dict[str, 'TrieNode'] = {}
        self.is_end_of_word: bool = False
        self.term_data: List[Dict] = []  # Stores list of {target, glossary, priority}

class GlossaryEngine:
    """
    Core logic for term extraction using Trie data structure.
    Supports overlapping terms and multiple glossaries.
    """
    def __init__(self):
        self.root = TrieNode()
        self._term_count = 0

    def add_term(self, source: str, target: str, glossary_source: str, priority: int = 1):
        """Adds a term to the internal Trie."""
        if not source:
            return
            
        node = self.root
        for char in source:
            if char not in node.children:
                node.children[char] = TrieNode()
            node = node.children[char]
        
        node.is_end_of_word = True
        # Store term data at the leaf node
        node.term_data.append({
            'target': target,
            'glossary': glossary_source,
            'priority': priority
        })
        self._term_count += 1

    def extract_terms(self, text: str) -> List[TermHit]:
        """
        Extracts all known terms from the text.
        Handles overlapping terms (e.g. "Apple" and "Apple Pie").
        Complexity: O(N * L) where N is text length, L is max term length.
        """
        hits = []
        n = len(text)
        
        # Iterate through each character as a potential start of a term
        for i in range(n):
            node = self.root
            # Traverse the Trie from the current character
            for j in range(i, n):
                char = text[j]
                if char not in node.children:
                    break
                
                node = node.children[char]
                
                if node.is_end_of_word:
                    # Found a match! Create a hit for each variant
                    current_match_text = text[i : j+1]
                    for data in node.term_data:
                        hits.append(TermHit(
                            source_term=current_match_text,
                            target_term=data['target'],
                            start_index=i,
                            end_index=j+1,  # Python slice convention (exclusive)
                            glossary_source=data['glossary'],
                            priority=data['priority']
                        ))
        
        # Sort results: by start_index, then by length (longest first for preference), then priority
        # This helps UI layer decide what to show, though we return ALL overlapping hits as requested.
        hits.sort(key=lambda x: (x.start_index, -(x.end_index - x.start_index)))
        return hits

# =============================================================================
# 3. Loader Implementation
# =============================================================================

class GlossaryLoader:
    """Handles loading terms from files (CSV, Excel)."""
    
    def __init__(self, engine: GlossaryEngine):
        self.engine = engine

    def load_file(self, file_path: str):
        """Detects file type and loads terms."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        suffix = path.suffix.lower()
        if suffix == '.csv':
            self._load_csv(path)
        elif suffix in ('.xlsx', '.xls'):
            self._load_excel(path)
        else:
            raise ValueError(f"Unsupported file format: {suffix}")

    def _load_csv(self, path: Path):
        try:
            with open(path, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.reader(f)
                for row in reader:
                    if len(row) >= 2:
                        # Simple convention: Column A = Source, Column B = Target
                        source = row[0].strip()
                        target = row[1].strip()
                        if source and target:
                            self.engine.add_term(source, target, path.name)
        except Exception as e:
            print(f"Error loading CSV {path}: {e}")

    def _load_excel(self, path: Path):
        try:
            # Conditional import to avoid hard dependency if not needed
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            ws = wb.active
            for row in ws.iter_rows(min_row=1, values_only=True):
                if row and len(row) >= 2:
                    source = str(row[0]).strip() if row[0] else ""
                    target = str(row[1]).strip() if row[1] else ""
                    if source and target:
                        self.engine.add_term(source, target, path.name)
            wb.close()
        except ImportError:
            print("Warning: openpyxl not installed. Skipping Excel file.")
        except Exception as e:
            print(f"Error loading Excel {path}: {e}")

# =============================================================================
# 4. Highlighter Implementation (Phase 1 Closing)
# =============================================================================

class TermHighlighter:
    """
    Renders text with highlighted terms for terminal verification.
    Product Policy:
    - Prefer longer terms over shorter ones (e.g., 'Apple Pie' > 'Apple').
    - Use simple bracket markup [Source|Target] for visibility.
    """
    
    @staticmethod
    def highlight(text: str, hits: List[TermHit]) -> str:
        if not hits:
            return text
            
        # 1. Sort hits by length (descending) to prioritize longest matches
        #    Then by start_index to keep order deterministic
        sorted_hits = sorted(hits, key=lambda h: (-(h.end_index - h.start_index), h.start_index))
        
        # 2. Filter overlapping hits, keeping only the highest priority ones (longest)
        #    Mask array: False = free, True = occupied
        mask = [False] * len(text)
        final_hits = []
        
        for hit in sorted_hits:
            # Check if range is free
            is_free = True
            for i in range(hit.start_index, hit.end_index):
                if mask[i]:
                    is_free = False
                    break
            
            if is_free:
                final_hits.append(hit)
                # Mark range as occupied
                for i in range(hit.start_index, hit.end_index):
                    mask[i] = True
        
        # 3. Sort final hits by position for sequential reconstruction
        final_hits.sort(key=lambda h: h.start_index)
        
        # 4. Reconstruct string
        result = []
        current_idx = 0
        
        for hit in final_hits:
            # Append text before the hit
            result.append(text[current_idx : hit.start_index])
            
            # Append highlighted term
            # Using simple bracket style for universal terminal compatibility
            # Format: [SourceTerm|TargetTerm]
            result.append(f"[{hit.source_term}|{hit.target_term}]")
            
            current_idx = hit.end_index
            
        # Append remaining text
        result.append(text[current_idx:])
        
        return "".join(result)

# =============================================================================
# 5. Self-Test / Verification
# =============================================================================

if __name__ == "__main__":
    print("Running Glossary Engine Self-Tests...")
    
    # Setup
    engine = GlossaryEngine()
    
    # Populate Glossary for Testing
    # Case A terms
    engine.add_term("Hello", "你好", "TestGlossary")
    # Case B terms
    engine.add_term("Apple", "苹果", "TestGlossary")
    engine.add_term("Apple Pie", "苹果派", "TestGlossary")
    # Case D (Dense) terms
    engine.add_term("cat", "猫", "TestGlossary")
    engine.add_term("dog", "狗", "TestGlossary")
    
    # --- Case A: Basic Matching ---
    text_a = "Hello World"
    hits_a = engine.extract_terms(text_a)
    print(f"\nCase A: Input '{text_a}'")
    for hit in hits_a:
        print(f"  Found: {hit}")
    
    assert len(hits_a) == 1
    assert hits_a[0].source_term == "Hello"
    assert hits_a[0].target_term == "你好"
    print("  [PASS] Case A")

    # --- Case B: Overlapping Matching ---
    text_b = "Apple Pie"
    hits_b = engine.extract_terms(text_b)
    print(f"\nCase B: Input '{text_b}'")
    for hit in hits_b:
        print(f"  Found: {hit}")
    
    # Should find "Apple" and "Apple Pie"
    assert len(hits_b) == 2
    sources = [h.source_term for h in hits_b]
    assert "Apple" in sources
    assert "Apple Pie" in sources
    print("  [PASS] Case B")

    # --- Case C: No Match ---
    text_c = "Unknown Text"
    hits_c = engine.extract_terms(text_c)
    print(f"\nCase C: Input '{text_c}'")
    for hit in hits_c:
        print(f"  Found: {hit}")
    
    assert len(hits_c) == 0
    print("  [PASS] Case C")

    # --- Phase 1 Closing: Visualization Verification ---
    print("\n--- Phase 1 Closing: Visualization Verification ---")
    highlighter = TermHighlighter()

    # Scenario 1: Basic
    hl_a = highlighter.highlight(text_a, hits_a)
    print(f"Scenario 1 (Basic): {hl_a}")
    assert hl_a == "[Hello|你好] World"
    
    # Scenario 2: Overlap (Apple vs Apple Pie)
    # Should prefer 'Apple Pie' because it is longer
    hl_b = highlighter.highlight(text_b, hits_b)
    print(f"Scenario 2 (Overlap): {hl_b}")
    assert hl_b == "[Apple Pie|苹果派]"
    
    # Scenario 3: Dense / Multiple terms
    text_d = "I have a cat and a dog."
    hits_d = engine.extract_terms(text_d)
    hl_d = highlighter.highlight(text_d, hits_d)
    print(f"Scenario 3 (Dense): {hl_d}")
    assert hl_d == "I have a [cat|猫] and a [dog|狗]."
    
    print("\nAll tests passed successfully. Phase 1 Complete.")
