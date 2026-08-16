# Requirements Document: Parser Subsystem Extraction

> **状态：遗留草案，禁止直接实施（2026-07-27）。** 本文仍保存早期目标和验收历史，但其 `SourceUnit`、扩展名注册、TMImporter、流式与 round-trip 约束已与当前代码冲突。当前裁决与重新基线范围见 `research.md` 和 `rebaseline-plan.md`；只有重新生成的 Requirements → Design → Tasks 经人工批准后，才成为未来实现权威。

## Introduction

本需求文档定义了 Parser Subsystem Extraction 功能的业务需求和验收标准。该功能旨在解决当前架构中的职责混乱问题，将文件解析逻辑从核心匹配引擎中分离出来，创建清晰的架构边界，提高系统的可扩展性和可维护性。

## Glossary

- **Parser**: 文件格式解析器，负责将特定格式的文件转换为标准化的数据结构
- **Core_Engine**: 核心匹配引擎，包括 glossary_engine 和 tm_engine，负责纯粹的匹配算法
- **SourceUnit**: 标准化的翻译单元数据结构，作为 Parser 和 Engine 之间的数据契约
- **ParserRegistry**: 解析器注册表，负责管理和发现可用的解析器
- **TMImporter**: 翻译记忆导入协调器，负责协调解析和存储过程
- **BaseParser**: 抽象基类，定义所有解析器必须实现的接口
- **POParser**: PO 文件格式解析器
- **JSONParser**: JSON 文件格式解析器

## Requirements

### Requirement 1: Parser Interface Definition

**User Story:** 作为系统架构师，我希望定义统一的解析器接口，以便所有文件格式解析器遵循相同的契约。

#### Acceptance Criteria

1. THE BaseParser SHALL define abstract methods for parse_file, parse_stream, and validate_file
2. THE BaseParser SHALL require all concrete parsers to declare their supported_extensions
3. WHEN a parser implements BaseParser THEN it SHALL return SourceUnit objects from parsing methods
4. THE BaseParser SHALL define consistent exception types for parsing errors
5. WHEN parse_file is called THEN THE parser SHALL return a complete list of SourceUnit objects
6. WHEN parse_stream is called THEN THE parser SHALL yield SourceUnit objects one at a time

### Requirement 2: PO File Format Support

**User Story:** 作为翻译项目管理员，我希望系统能够解析 PO 文件，以便导入 gettext 格式的翻译数据。

#### Acceptance Criteria

1. WHEN a valid PO file is provided THEN THE POParser SHALL extract all msgid/msgstr pairs as SourceUnit objects
2. WHEN a PO file contains msgctxt THEN THE POParser SHALL preserve context information in SourceUnit
3. WHEN a PO file contains multiline strings THEN THE POParser SHALL correctly concatenate them
4. WHEN a PO file contains escape sequences THEN THE POParser SHALL unescape them correctly
5. THE POParser SHALL support both .po and .pot file extensions
6. WHEN a PO file has invalid structure THEN THE POParser SHALL raise InvalidStructureError with line number

### Requirement 3: JSON File Format Support

**User Story:** 作为翻译项目管理员，我希望系统能够解析 JSON 格式的翻译文件，以便导入自定义格式的翻译数据。

#### Acceptance Criteria

1. WHEN a valid JSON file is provided THEN THE JSONParser SHALL extract all translation entries as SourceUnit objects
2. WHEN a JSON entry lacks required fields THEN THE JSONParser SHALL skip the entry and log a warning
3. WHEN a JSON entry contains optional fields THEN THE JSONParser SHALL preserve them in SourceUnit metadata
4. THE JSONParser SHALL support .json file extension
5. WHEN a JSON file is larger than 10MB THEN THE JSONParser SHALL use streaming mode to avoid memory issues
6. WHEN a JSON file has invalid syntax THEN THE JSONParser SHALL raise InvalidStructureError

### Requirement 4: Parser Discovery and Registration

**User Story:** 作为系统开发者，我希望有一个中心化的解析器注册机制，以便根据文件扩展名自动选择合适的解析器。

#### Acceptance Criteria

1. THE ParserRegistry SHALL maintain a mapping from file extensions to parser instances
2. WHEN a parser is registered THEN THE ParserRegistry SHALL associate all its supported extensions with the parser
3. WHEN get_parser is called with a file path THEN THE ParserRegistry SHALL return the appropriate parser based on extension
4. WHEN no parser is found for an extension THEN THE ParserRegistry SHALL raise UnsupportedFormatError
5. THE ParserRegistry SHALL perform case-insensitive extension matching
6. THE ParserRegistry SHALL register POParser and JSONParser by default

### Requirement 5: Translation Memory Import Orchestration

**User Story:** 作为翻译项目管理员，我希望能够批量导入翻译文件到翻译记忆库，以便快速构建翻译资源。

#### Acceptance Criteria

1. WHEN import_file is called THEN THE TMImporter SHALL use ParserRegistry to select the appropriate parser
2. WHEN import_file is called with deduplicate=True THEN THE TMImporter SHALL skip entries already in TM
3. WHEN import_directory is called THEN THE TMImporter SHALL process all matching files in the directory
4. WHEN import_batch is called THEN THE TMImporter SHALL process multiple files and aggregate results
5. THE TMImporter SHALL return ImportResult with statistics including files_processed, units_imported, and units_skipped
6. WHEN a parsing error occurs THEN THE TMImporter SHALL collect the error and continue processing remaining files

### Requirement 6: Data Model Validation

**User Story:** 作为系统开发者，我希望确保解析后的数据符合预期的结构和约束，以便保证数据质量。

#### Acceptance Criteria

1. THE SourceUnit SHALL require non-empty text field
2. THE SourceUnit SHALL require unique id within a file
3. THE TMEntry SHALL require non-empty source and target fields
4. THE TMEntry SHALL require valid ISO 8601 timestamp for last_used field
5. THE TMEntry SHALL require positive integer for usage_count field
6. WHEN a SourceUnit is created with invalid data THEN THE system SHALL raise ValidationError

### Requirement 7: Error Handling and Recovery

**User Story:** 作为翻译项目管理员，我希望系统能够优雅地处理错误，以便在部分文件损坏时仍能继续处理其他文件。

#### Acceptance Criteria

1. WHEN a file does not exist THEN THE parser SHALL raise FileNotFoundError immediately
2. WHEN a file has unsupported format THEN THE system SHALL raise UnsupportedFormatError with list of supported formats
3. WHEN a file has invalid structure THEN THE parser SHALL raise InvalidStructureError with error details
4. WHEN a file has encoding issues THEN THE parser SHALL attempt to detect encoding and continue with best-effort parsing
5. WHEN parsing errors occur in batch import THEN THE TMImporter SHALL isolate errors and continue processing other files
6. THE ImportResult SHALL include a list of all errors encountered during import

### Requirement 8: Streaming Mode for Large Files

**User Story:** 作为翻译项目管理员，我希望系统能够处理大型翻译文件而不耗尽内存，以便导入包含数万条记录的文件。

#### Acceptance Criteria

1. WHEN a file is larger than 10MB THEN THE parser SHALL use parse_stream method
2. WHEN parse_stream is used THEN THE parser SHALL yield SourceUnit objects one at a time
3. THE streaming mode SHALL maintain bounded memory usage regardless of file size
4. WHEN parse_stream is called THEN the results SHALL be equivalent to parse_file results
5. THE system SHALL support files up to 1GB without memory issues

### Requirement 9: Parser Independence from Core Engine

**User Story:** 作为系统架构师，我希望解析器子系统与核心引擎完全解耦，以便添加新的文件格式不需要修改核心引擎代码。

#### Acceptance Criteria

1. THE Core_Engine SHALL NOT import any modules from the parsers package
2. THE Core_Engine SHALL only accept SourceUnit objects or primitive types as input
3. WHEN a new parser is added THEN THE Core_Engine code SHALL remain unchanged
4. THE dependency flow SHALL be unidirectional from Parser to Core_Engine
5. THE ParserRegistry SHALL allow dynamic parser registration without modifying existing code

### Requirement 10: Backward Compatibility

**User Story:** 作为系统维护者，我希望新的解析器子系统保持向后兼容，以便现有功能继续正常工作。

#### Acceptance Criteria

1. WHEN existing PO import functionality is used THEN it SHALL produce the same results as before
2. WHEN existing JSON import functionality is used THEN it SHALL produce the same results as before
3. THE existing tm_json_importer.py functionality SHALL continue to work without modification
4. THE translation_runner.py SHALL be able to use both old and new import methods during migration

### Requirement 11: Performance Requirements

**User Story:** 作为翻译项目管理员，我希望文件解析速度足够快，以便能够高效地导入大量翻译数据。

#### Acceptance Criteria

1. THE JSONParser SHALL parse at least 1000 entries per second
2. THE POParser SHALL parse at least 500 entries per second
3. WHEN importing a file with 10000 entries THEN the operation SHALL complete within 30 seconds
4. THE streaming mode SHALL not significantly impact parsing speed compared to batch mode
5. THE parser performance SHALL be included in the existing benchmark suite

### Requirement 12: Security and Resource Limits

**User Story:** 作为系统管理员，我希望解析器有适当的安全限制，以便防止恶意文件导致系统资源耗尽。

#### Acceptance Criteria

1. THE parser SHALL validate file paths to prevent directory traversal attacks
2. THE parser SHALL reject files exceeding configurable size limits
3. THE parser SHALL enforce maximum memory usage per import operation
4. THE parser SHALL set timeout for parsing operations to prevent denial of service
5. WHEN more than N consecutive parsing errors occur THEN THE parser SHALL stop processing to prevent resource exhaustion
6. THE parser SHALL escape special characters in parsed content before storage

### Requirement 13: Extensibility for Future Formats

**User Story:** 作为系统开发者，我希望能够轻松添加新的文件格式支持，以便系统能够适应未来的需求。

#### Acceptance Criteria

1. WHEN a new parser class implements BaseParser THEN it SHALL be usable without modifying existing code
2. THE ParserRegistry SHALL support dynamic registration of custom parsers
3. THE system SHALL provide clear extension points for AI pipeline integration
4. THE system SHALL provide clear extension points for export functionality
5. THE documentation SHALL include examples of implementing custom parsers

### Requirement 14: File Validation

**User Story:** 作为翻译项目管理员，我希望在完整解析之前能够快速验证文件格式，以便提前发现格式错误。

#### Acceptance Criteria

1. WHEN validate_file is called THEN THE parser SHALL check file structure without full parsing
2. WHEN validate_file returns True THEN parse_file SHALL succeed without raising exceptions
3. WHEN validate_file returns False THEN THE system SHALL provide information about validation failures
4. THE validation process SHALL be significantly faster than full parsing
5. THE validation SHALL check for required file structure elements specific to each format
