"""
LocalCAT Phase 3: Excel Adapter
Experimental frontend integration.
Strictly limited to manual triggering via CLI.
"""

import sys
import os

try:
    import xlwings as xw
except ImportError:
    print("Error: xlwings not installed. Please install it to run this adapter.")
    sys.exit(1)

# Import Logic Controller
# Ensure current directory is in path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from logic_controller import LogicController

def main():
    print("=== LocalCAT Excel Adapter (Manual Trigger) ===\n")

    try:
        # 1. Connect to Active Excel Instance
        # This will attach to the currently active Excel window
        app = xw.apps.active
        if not app:
            print("Error: No active Excel instance found.")
            return

        wb = app.books.active
        sheet = wb.sheets.active
        selection = app.selection

        # 2. Get Source Text (Handle Single vs Multi-Cell)
        # xlwings behavior:
        # - Single cell: returns value (e.g., "hello")
        # - Multi-cell (Range): returns list of values (e.g., ["hello", "world"]) or nested list
        
        raw_value = selection.value
        
        if not raw_value:
            print("Warning: Selection is empty.")
            return

        # Normalize to list of tuples [(row_offset, col_offset, text)]
        # We process cell by cell to ensure correct output mapping
        tasks = []
        
        # Check if it's a list (multiple cells selected)
        if isinstance(raw_value, list):
            # 1D list (single column multiple rows OR single row multiple columns)
            # xlwings returns 1D list for single column/row ranges usually
            # But we need to be careful about orientation.
            # For simplicity in this experiment, we assume vertical selection (A1:A3)
            # which maps to output B1:B3.
            
            # Let's iterate through the range object itself to be safe about coordinates
            for cell in selection:
                val = cell.value
                if val:
                    tasks.append((cell, str(val).strip()))
        else:
            # Single cell
            tasks.append((selection, str(raw_value).strip()))
            
        print(f"Processing {len(tasks)} cells...")

        # 3. Call Logic Layer & Write Back
        # We instantiate controller once
        print("Calling Logic Controller...")
        controller = LogicController()
        
        for cell, clean_text in tasks:
            print(f"  -> Querying: {repr(clean_text)}")
            result = controller.get_suggestions(clean_text)

            # 4. Format Result
            output_str = ""
            if result["status"] == "TM_HIT":
                tm = result["tm_match"]
                output_str = f"[TM] {tm['target']}"
            elif result["status"] == "TERMS_FOUND":
                terms = result["terms"]
                sorted_terms = sorted(terms, key=lambda x: x["start_index"])
                term_strs = [f"{t['source_term']}->{t['target_term']}" for t in sorted_terms]
                output_str = f"[Terms] {', '.join(term_strs)}"
            else:
                output_str = "[No Match]"
            
            # 5. Write to Right Neighbor
            target_cell = cell.offset(0, 1)
            target_cell.value = output_str
            print(f"     Written to {target_cell.address}: {output_str}")


    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
