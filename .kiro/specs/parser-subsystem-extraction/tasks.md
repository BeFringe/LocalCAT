# Implementation Plan: Parser Subsystem Extraction

> **状态：遗留任务，全部暂停（2026-07-27）。** 不按本清单开始实现。它包含已经完成的 TMX、过时路径和相互矛盾的契约；下一轮任务必须在重新批准 Requirements 与 Design 后生成。

## Overview

This implementation plan extracts the parser/import functionality from the core matching engine into a separate subsystem (Layer 2B). The goal is to create clear architectural boundaries, improve extensibility for new file formats, and eliminate responsibility creep in the core engine. The implementation follows a bottom-up approach: base abstractions → concrete parsers → registry/orchestration → integration.

## Tasks

- [ ] 1. Create parser subsystem directory structure and base abstractions
  - Create `CAT/parsers/` directory with `__init__.py`
  - Create `CAT/parsers/exceptions.py` with ParseError hierarchy (ParseError, UnsupportedFormatError, InvalidStructureError, ValidationError)
  - Create `CAT/parsers/base.py` with BaseParser abstract class
  - Define SourceUnit dataclass (if not already in tm_engine.py, otherwise import it)
  - Implement abstract methods: parse_file, parse_stream, validate_file, supported_extensions property
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

- [ ]* 1.1 Write property test for BaseParser contract
  - **Property 1: Parser Returns Correct Types**
  - **Validates: Requirements 1.3, 1.5, 1.6**
  - Test that any parser implementing BaseParser returns List[SourceUnit] from parse_file and Iterator[SourceUnit] from parse_stream

- [ ] 2. Implement POParser for PO file format
  - [ ] 2.1 Create `CAT/parsers/po_parser.py` with POParser class
    - Implement parse_file method (delegates to parse_stream)
    - Implement parse_stream method with PO file structure parsing
    - Implement validate_file method for quick structure validation
    - Define supported_extensions property returning ['.po', '.pot']
    - _Requirements: 2.1, 2.5_

  - [ ] 2.2 Implement PO-specific parsing helpers
    - Implement _parse_multiline_string helper for handling multiline PO strings
    - Implement _unescape_po_string helper for escape sequences (\\n, \\t, \\", etc.)
    - Handle msgctxt, msgid, msgstr block parsing
    - _Requirements: 2.2, 2.3, 2.4_

  - [ ]* 2.3 Write property tests for POParser
    - **Property 2: PO Parser Preserves All Translation Pairs**
    - **Validates: Requirements 2.1**
    - **Property 3: PO Parser Preserves Context**
    - **Validates: Requirements 2.2**
    - **Property 4: PO Parser Handles Multiline Strings**
    - **Validates: Requirements 2.3**
    - **Property 5: PO Parser Unescapes Correctly**
    - **Validates: Requirements 2.4**

  - [ ]* 2.4 Write unit tests for POParser edge cases
    - Test empty PO files
    - Test PO files with only header entry
    - Test malformed PO structure (raises InvalidStructureError with line number)
    - Test various encoding scenarios (UTF-8, UTF-8-BOM, Latin-1)
    - _Requirements: 2.6, 7.3, 7.4_

- [ ] 3. Implement JSONParser for JSON file format
  - [ ] 3.1 Create `CAT/parsers/json_parser.py` with JSONParser class
    - Implement parse_file method for loading entire JSON file
    - Implement parse_stream method with streaming support for large files (>10MB)
    - Implement validate_file method for JSON structure validation
    - Define supported_extensions property returning ['.json']
    - _Requirements: 3.1, 3.4, 3.5_

  - [ ] 3.2 Implement JSON entry normalization
    - Implement _normalize_entry helper to convert JSON objects to SourceUnit
    - Validate required fields (source, target)
    - Handle optional fields (speaker, context) and preserve in metadata
    - Skip entries with missing required fields and log warnings
    - _Requirements: 3.2, 3.3_

  - [ ]* 3.3 Write property tests for JSONParser
    - **Property 6: JSON Parser Extracts All Valid Entries**
    - **Validates: Requirements 3.1**
    - **Property 7: JSON Parser Skips Invalid Entries**
    - **Validates: Requirements 3.2**
    - **Property 8: JSON Parser Preserves Optional Fields**
    - **Validates: Requirements 3.3**
    - **Property 9: JSON Parser Rejects Invalid Syntax**
    - **Validates: Requirements 3.6**

  - [ ]* 3.4 Write unit tests for JSONParser edge cases
    - Test empty JSON arrays
    - Test single entry JSON files
    - Test large file streaming (>10MB)
    - Test invalid JSON syntax handling
    - Test extra fields preservation in metadata
    - _Requirements: 3.5, 3.6, 8.1, 8.2_

- [ ] 4. Checkpoint - Ensure parser implementations are complete
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Implement ParserRegistry for parser discovery
  - [ ] 5.1 Create `CAT/parsers/registry.py` with ParserRegistry class
    - Implement __init__ with _parsers dictionary
    - Implement register_parser method to associate extensions with parsers
    - Implement get_parser method for extension-based lookup
    - Implement list_supported_formats method
    - Implement _register_default_parsers to register POParser and JSONParser
    - _Requirements: 4.1, 4.2, 4.3, 4.6_

  - [ ] 5.2 Implement case-insensitive extension matching
    - Normalize extensions to lowercase in register_parser
    - Normalize file extension to lowercase in get_parser
    - Raise UnsupportedFormatError with supported formats list when no parser found
    - _Requirements: 4.4, 4.5_

  - [ ]* 5.3 Write property tests for ParserRegistry
    - **Property 10: Parser Registry Associates All Extensions**
    - **Validates: Requirements 4.2, 4.3**
    - **Property 11: Parser Registry Rejects Unsupported Formats**
    - **Validates: Requirements 4.4**
    - **Property 12: Parser Registry Case-Insensitive Matching**
    - **Validates: Requirements 4.5**
    - **Property 27: Dynamic Parser Registration**
    - **Validates: Requirements 9.5, 13.1, 13.2**

  - [ ]* 5.4 Write unit tests for ParserRegistry
    - Test default parser registration
    - Test custom parser registration
    - Test multiple extensions per parser
    - Test extension override (last registered wins)
    - Test list_supported_formats output
    - _Requirements: 4.1, 4.6_

- [ ] 6. Implement TMImporter orchestrator
  - [ ] 6.1 Create `CAT/parsers/importer.py` with ImportResult dataclass
    - Define ImportResult with fields: files_processed, units_imported, units_skipped, errors, duration_seconds
    - _Requirements: 5.5_

  - [ ] 6.2 Implement TMImporter class
    - Implement __init__ accepting tm_engine and parser_registry
    - Implement import_file method with deduplication support
    - Implement import_directory method with glob pattern matching
    - Implement import_batch method for multiple files
    - Collect errors and continue processing on failures (error isolation)
    - Track statistics: files processed, units imported, units skipped
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.6_

  - [ ]* 6.3 Write property tests for TMImporter
    - **Property 13: Import Deduplication Prevents Duplicates**
    - **Validates: Requirements 5.2**
    - **Property 14: Batch Import Processes All Files**
    - **Validates: Requirements 5.3, 5.4**
    - **Property 15: Import Result Contains Required Statistics**
    - **Validates: Requirements 5.5**
    - **Property 16: Error Isolation in Batch Import**
    - **Validates: Requirements 5.6, 7.5**
    - **Property 25: Import Result Collects All Errors**
    - **Validates: Requirements 7.6**

  - [ ]* 6.4 Write unit tests for TMImporter
    - Test single file import with statistics
    - Test directory import with pattern matching
    - Test batch import with mixed valid/invalid files
    - Test deduplication logic
    - Test error collection and isolation
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

- [ ] 7. Checkpoint - Ensure core subsystem is functional
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 8. Implement data model validation
  - [ ] 8.1 Add validation to SourceUnit creation
    - Validate text field is non-empty
    - Validate id is unique within file context
    - Raise ValidationError for invalid data
    - _Requirements: 6.1, 6.2_

  - [ ] 8.2 Add validation to TMEntry creation
    - Validate source and target are non-empty
    - Validate last_used is valid ISO 8601 timestamp
    - Validate usage_count is positive integer
    - Raise ValidationError for invalid data
    - _Requirements: 6.3, 6.4, 6.5_

  - [ ]* 8.3 Write property tests for data validation
    - **Property 17: SourceUnit Requires Non-Empty Text**
    - **Validates: Requirements 6.1**
    - **Property 18: SourceUnit IDs Are Unique Within File**
    - **Validates: Requirements 6.2**
    - **Property 19: TMEntry Requires Non-Empty Fields**
    - **Validates: Requirements 6.3**
    - **Property 20: TMEntry Requires Valid Timestamp**
    - **Validates: Requirements 6.4**
    - **Property 21: TMEntry Requires Positive Usage Count**
    - **Validates: Requirements 6.5**

- [ ] 9. Implement comprehensive error handling
  - [ ] 9.1 Add file existence validation
    - Check file exists before parsing
    - Raise FileNotFoundError with clear message
    - _Requirements: 7.1_

  - [ ] 9.2 Add encoding detection and recovery
    - Attempt to detect file encoding
    - Fall back to UTF-8 with error replacement
    - Log warnings for encoding issues
    - _Requirements: 7.4_

  - [ ]* 9.3 Write property tests for error handling
    - **Property 22: Parser Raises FileNotFoundError for Missing Files**
    - **Validates: Requirements 7.1**
    - **Property 23: Unsupported Format Error Includes Format List**
    - **Validates: Requirements 7.2**
    - **Property 24: Invalid Structure Error Includes Details**
    - **Validates: Requirements 7.3**

- [ ] 10. Implement streaming mode and large file support
  - [ ] 10.1 Add file size detection in parsers
    - Check file size before parsing
    - Use parse_stream for files >10MB
    - Use parse_file for smaller files
    - _Requirements: 8.1_

  - [ ] 10.2 Optimize streaming implementations
    - Ensure bounded memory usage in parse_stream
    - Use generators to yield SourceUnit objects one at a time
    - _Requirements: 8.2, 8.3_

  - [ ]* 10.3 Write property tests for streaming mode
    - **Property 26: Streaming Equivalence**
    - **Validates: Requirements 8.4**
    - Test that list(parse_stream(file)) equals parse_file(file)
    - Test memory usage stays bounded for large files

  - [ ]* 10.4 Write integration tests for large file handling
    - Test parsing 10,000+ entry files
    - Test files up to 1GB size
    - Verify memory usage stays within limits
    - _Requirements: 8.5, 11.3_

- [ ] 11. Implement security and resource limits
  - [ ] 11.1 Add path validation
    - Validate file paths to prevent directory traversal
    - Sanitize paths before use
    - _Requirements: 12.1_

  - [ ] 11.2 Add resource limits
    - Implement configurable file size limits
    - Implement parsing timeout mechanism
    - Implement consecutive error limit (stop after N errors)
    - Enforce maximum memory usage per operation
    - _Requirements: 12.2, 12.3, 12.4, 12.5_

  - [ ] 11.3 Add content sanitization
    - Escape special characters in parsed content before storage
    - _Requirements: 12.6_

  - [ ]* 11.4 Write property tests for security features
    - **Property 28: Path Traversal Prevention**
    - **Validates: Requirements 12.1**
    - **Property 29: File Size Limit Enforcement**
    - **Validates: Requirements 12.2**
    - **Property 30: Consecutive Error Limit**
    - **Validates: Requirements 12.5**
    - **Property 31: Special Character Escaping**
    - **Validates: Requirements 12.6**

- [ ] 12. Checkpoint - Ensure robustness and security
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 13. Implement file validation functionality
  - [ ] 13.1 Implement fast validation in POParser
    - Check PO file structure without full parsing
    - Verify required elements are present
    - _Requirements: 14.1, 14.4, 14.5_

  - [ ] 13.2 Implement fast validation in JSONParser
    - Check JSON syntax without full parsing
    - Verify required fields are present in sample entries
    - _Requirements: 14.1, 14.4, 14.5_

  - [ ]* 13.3 Write property tests for validation
    - **Property 32: Validation Implies Parsing Success**
    - **Validates: Requirements 14.2**
    - **Property 33: Validation Failure Provides Information**
    - **Validates: Requirements 14.3**

  - [ ]* 13.4 Write unit tests for validation
    - Test validation is faster than full parsing
    - Test validation correctly identifies valid files
    - Test validation correctly identifies invalid files
    - Test validation provides useful error information
    - _Requirements: 14.1, 14.2, 14.3, 14.4_

- [ ] 14. Update parsers package initialization
  - [ ] 14.1 Create `CAT/parsers/__init__.py` with public API exports
    - Export BaseParser, POParser, JSONParser
    - Export ParserRegistry, TMImporter
    - Export ParseError, UnsupportedFormatError, InvalidStructureError, ValidationError
    - Export SourceUnit, ImportResult
    - Add module-level docstring explaining the parser subsystem

- [ ] 15. Integration with existing codebase
  - [ ] 15.1 Update translation_runner.py to use new parser subsystem
    - Import ParserRegistry and TMImporter
    - Replace direct parser calls with registry-based lookup
    - Maintain backward compatibility with existing functionality
    - _Requirements: 10.1, 10.2, 10.4_

  - [ ] 15.2 Verify tm_json_importer.py continues to work
    - Test existing JSON import functionality
    - Ensure no regressions in behavior
    - _Requirements: 10.3_

  - [ ]* 15.3 Write integration tests for backward compatibility
    - Test PO import produces same results as before
    - Test JSON import produces same results as before
    - Test translation_runner.py works with both old and new methods
    - _Requirements: 10.1, 10.2, 10.3, 10.4_

- [ ] 16. Performance optimization and benchmarking
  - [ ] 16.1 Optimize POParser performance
    - Use compiled regex for PO parsing
    - Profile and optimize hot paths
    - Target: 500+ entries/second
    - _Requirements: 11.1, 11.2_

  - [ ] 16.2 Optimize JSONParser performance
    - Use ijson for large file streaming (optional dependency)
    - Profile and optimize hot paths
    - Target: 1000+ entries/second
    - _Requirements: 11.1, 11.2_

  - [ ]* 16.3 Add parser performance to benchmark suite
    - Create benchmark tests for POParser
    - Create benchmark tests for JSONParser
    - Verify performance targets are met
    - Include results in existing benchmark suite
    - _Requirements: 11.3, 11.4, 11.5_

- [ ] 17. Verify parser independence from core engine
  - [ ] 17.1 Audit import statements in core engine files
    - Verify glossary_engine.py does not import from parsers package
    - Verify tm_engine.py does not import from parsers package
    - Ensure dependency flow is unidirectional (Parser → Engine)
    - _Requirements: 9.1, 9.2, 9.4_

  - [ ] 17.2 Verify adding new parser doesn't require engine changes
    - Create a mock parser for testing
    - Register it with ParserRegistry
    - Verify it works without modifying core engine code
    - _Requirements: 9.3, 9.5_

- [ ] 18. Documentation and examples
  - [ ] 18.1 Create parser subsystem documentation
    - Document BaseParser interface and contract
    - Document how to implement custom parsers
    - Document ParserRegistry usage
    - Document TMImporter usage
    - _Requirements: 13.5_

  - [ ] 18.2 Create example custom parser
    - Implement a simple example parser (e.g., CSV format)
    - Show how to register and use it
    - Include in documentation
    - _Requirements: 13.1, 13.2, 13.5_

  - [ ] 18.3 Document extension points
    - Document AI pipeline integration extension point
    - Document export functionality extension point
    - Document format conversion extension point
    - Provide code examples for each
    - _Requirements: 13.3, 13.4_

- [ ] 19. Final checkpoint - Complete system verification
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 20. Final integration and cleanup
  - [ ] 20.1 Run full test suite
    - Run all unit tests
    - Run all property tests
    - Run all integration tests
    - Run performance benchmarks
    - Verify all tests pass

  - [ ] 20.2 Verify all requirements are met
    - Review requirements document
    - Verify each acceptance criterion is satisfied
    - Document any deviations or future work

  - [ ] 20.3 Code cleanup and final review
    - Remove any debug code or temporary files
    - Ensure consistent code style
    - Verify all docstrings are complete
    - Run linter and fix any issues

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation at key milestones
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The implementation follows a bottom-up approach: base abstractions → concrete implementations → orchestration → integration
- Parser subsystem is completely independent from core engine (Layer 2A)
- All parsers follow the same BaseParser contract for consistency
- Streaming mode support ensures large files can be processed without memory issues
- Security features (path validation, resource limits) are built-in from the start
- Performance targets: JSONParser ≥1000 entries/sec, POParser ≥500 entries/sec
- Backward compatibility is maintained throughout the migration
