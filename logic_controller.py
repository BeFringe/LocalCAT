"""
LocalCAT Phase 3: Logic Controller
Stateless logic layer that orchestrates calls to Engine layer.
Strictly follows the data flow pattern established in stress_runner.py.
"""

import os
from pathlib import Path
import tempfile
from typing import Dict, Any, List
from glossary_engine import GlossaryEngine, GlossaryTerm
from parser_composition import create_parser_application_surface
from parser_contracts import (
    ContractViolation,
    EffectivePurpose,
    GETTEXT_PO_V1,
    GETTEXT_POT_V1,
    ParsedSegment,
    ReadRequest,
    ResourceRecord,
    SelectionFailure,
    SelectionRequest,
    SourceReference,
    TERMBASE_CSV_V1,
    TERMBASE_XLSX_V1,
    TermbaseColumnSelection,
    TermbaseReadOptions,
)
from tm_engine import SourceUnit, TMEngine

# Paths configuration (mirroring runner logic)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GLOSSARY_FILE = os.path.join(BASE_DIR, "terms.csv")
TM_FILE = os.path.join(BASE_DIR, "tm.jsonl")


class GlossaryLoadError(Exception):
    """Stable Application failure for one glossary source."""

    def __init__(self, code: str, safe_summary: str) -> None:
        self.code = code
        self.safe_summary = safe_summary
        super().__init__(f"{code}: {safe_summary}")


class ProjectSourceLoadError(Exception):
    """Stable Application failure for one project-document source."""

    def __init__(self, code: str, safe_summary: str) -> None:
        self.code = code
        self.safe_summary = safe_summary
        super().__init__(f"{code}: {safe_summary}")


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def load_glossary_file(engine: GlossaryEngine, file_path: str | Path) -> None:
    """Stage one termbase through Parser, then mutate the Trie after terminal."""

    if not isinstance(engine, GlossaryEngine):
        raise TypeError("engine must be a GlossaryEngine")
    source = _absolute_without_resolving(Path(file_path))
    format_by_suffix = {
        ".csv": TERMBASE_CSV_V1,
        ".xlsx": TERMBASE_XLSX_V1,
    }
    format_id = format_by_suffix.get(source.suffix.lower())
    if format_id is None:
        raise GlossaryLoadError(
            "PARSER.SELECTION.UNSUPPORTED",
            "glossary input format is not supported by the Parser v1 profile",
        )
    selection = SelectionRequest(
        purpose=EffectivePurpose.TERMBASE,
        format_id=format_id,
    )
    request = ReadRequest(
        purpose=EffectivePurpose.TERMBASE,
        format_id=format_id,
        termbase_options=TermbaseReadOptions(
            TermbaseColumnSelection.legacy_first_two_columns()
        ),
    )
    surface = create_parser_application_surface()
    try:
        opened = surface.open_input(
            SourceReference(
                safe_root=str(source.parent),
                selected_path=str(source),
                display_hint=source.name,
            ),
            selection,
            request,
        )
        if type(opened) is SelectionFailure:
            raise GlossaryLoadError(opened.code, opened.safe_summary)
        with opened:
            materialized = opened.materialize()
    except ContractViolation as exc:
        raise GlossaryLoadError(exc.code, exc.safe_summary) from None
    except OSError:
        raise GlossaryLoadError(
            "PARSER.SOURCE.READ_FAILED",
            "glossary input could not be read through the Parser surface",
        ) from None

    staged: list[GlossaryTerm] = []
    for record in materialized.records:
        if type(record) is not ResourceRecord:
            raise TypeError("termbase codec returned an incompatible record type")
        staged.append(GlossaryTerm(record.source, record.target, source.name))
    engine.apply_terms_atomic(tuple(staged))


def load_gettext_source_units(file_path: str | Path) -> list[SourceUnit]:
    """Read one singular PO/POT input, then map terminal records to SourceUnit."""

    source = _absolute_without_resolving(Path(file_path))
    format_by_suffix = {
        ".po": GETTEXT_PO_V1,
        ".pot": GETTEXT_POT_V1,
    }
    format_id = format_by_suffix.get(source.suffix.lower())
    if format_id is None:
        raise ProjectSourceLoadError(
            "PARSER.SELECTION.UNSUPPORTED",
            "project input format is not supported by the gettext runner adapter",
        )
    selection = SelectionRequest(
        purpose=EffectivePurpose.PROJECT_DOCUMENT,
        format_id=format_id,
    )
    request = ReadRequest(
        purpose=EffectivePurpose.PROJECT_DOCUMENT,
        format_id=format_id,
    )
    surface = create_parser_application_surface()
    try:
        opened = surface.open_input(
            SourceReference(
                safe_root=str(source.parent),
                selected_path=str(source),
                display_hint=source.name,
            ),
            selection,
            request,
        )
        if type(opened) is SelectionFailure:
            raise ProjectSourceLoadError(opened.code, opened.safe_summary)
        with opened:
            materialized = opened.materialize()
    except ContractViolation as exc:
        raise ProjectSourceLoadError(exc.code, exc.safe_summary) from None
    except OSError:
        raise ProjectSourceLoadError(
            "PARSER.SOURCE.READ_FAILED",
            "gettext input could not be read through the Parser surface",
        ) from None

    staged: list[ParsedSegment] = []
    for record in materialized.records:
        if type(record) is not ParsedSegment:
            raise TypeError("gettext codec returned an incompatible project record type")
        staged.append(record)
    return [
        SourceUnit(
            id=f"{source.name}_{index}",
            text=segment.source,
            context_prev=None,
            context_next=None,
            speaker=None,
            file_source=source.name,
            metadata={entry.key: entry.value for entry in segment.format_metadata},
        )
        for index, segment in enumerate(staged)
    ]

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
        
        # Load Glossary
        if os.path.exists(GLOSSARY_FILE):
            load_glossary_file(self.glossary_engine, GLOSSARY_FILE)
            
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

    # Parser verification belongs to this Application boundary, not TMEngine.
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / "selfcheck.po"
        source.write_text(
            'msgctxt "opaque-ui-context"\nmsgid "Open"\nmsgstr "打开"\n',
            encoding="utf-8",
        )
        units = load_gettext_source_units(source)
    assert len(units) == 1
    assert units[0].text == "Open"
    assert units[0].context_prev is None
    assert units[0].metadata == {"gettext.msgctxt": "opaque-ui-context"}
    print(f"Parser Application Boundary: {len(units)} singular unit(s)")
    
    print("\nLogic Controller Self-Test Passed.")
