"""Bounded, non-executing lexical recognition of the renpy-tl-v1 profile.

These private format facts are not Foundation terminal or publication authority.
The caller supplies complete verified bytes; only a wholly valid input returns.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Callable, Iterator

from parser_contracts import (
    CodecCapabilities, CodecDescriptor, CodecIdentity, ContractViolation,
    DocumentHeader, EffectivePurpose, FOUNDATION_GUARDED_ISSUE_CODES,
    FormatId, InputConsumptionPolicy, IssueSeverity, LimitProfile,
    OpaqueSourceState, ParseIssue, ParsedSegment, RawParseEvent, RawSpeaker,
    ReadRequest, RoundTripRequest, SnapshotCursorLease, SourceStateLimits,
    SourceStateRequest,
    PreparedFormatBytes, RoundTripLimits, RoundTripSegmentEdit,
    TargetPresence, TranslationState,
)

from rpy_text_rules import (
    RpyTextError, check_text, encode_target, protection_tokens, validate_target,
)


_MIB = 1024 * 1024
_BOM = b'\xef\xbb\xbf'
RPY_CODEC_IDENTITY = CodecIdentity('localcat.rpy', 'renpy-tl', '1')
# These introduce statements in Ren'Py and cannot be treated as character names.
_STATEMENTS = frozenset({
    'translate', 'old', 'new', 'voice', 'nvl', 'python', 'style', 'pass',
    'if', 'elif', 'else', 'while', 'for', 'init', 'define', 'default', 'label',
    'jump', 'call', 'return', 'menu', 'show', 'hide', 'scene', 'image', 'with',
    'pause', 'play', 'stop', 'queue', 'screen', 'transform', 'window',
})


@dataclass(frozen=True, slots=True)
class RpyLexicalLimits:
    max_input_bytes: int = 16 * _MIB
    max_slots: int = 100_000
    max_string_bytes: int = _MIB

    def __post_init__(self) -> None:
        for name, ceiling in (('max_input_bytes', 16 * _MIB),
                              ('max_slots', 100_000), ('max_string_bytes', _MIB)):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError('RPY limits must be exact integers')
            if not 0 < value <= ceiling:
                raise ValueError('RPY limits must stay within the declared profile')


class RpyInputError(ValueError):
    """Body-safe format failure, carrying the existing neutral issue container."""

    def __init__(self, category: str, line: int, offset: int, column: int,
                 reason: str, *, local_id: str | None = None,
                 record_number: int | None = None) -> None:
        self.category = category
        self.local_id = local_id
        self.byte_column = column
        self.issue = ParseIssue(
            code='PARSER.RPY.' + category.replace('-', '_').upper(),
            severity=IssueSeverity.FATAL,
            safe_summary=f'RPY line {line}, byte column {column}: {reason}',
            byte_offset=offset,
            line_number=line,
            record_number=record_number,
        )
        super().__init__(self.issue.safe_summary)


@dataclass(frozen=True, slots=True)
class _Literal:
    text: str
    quote: str
    start: int  # Half-open UTF-8 byte span, including both quotes.
    end: int
    line: int


@dataclass(frozen=True, slots=True)
class _Say:
    literal: _Literal
    speaker: str | None = None
    attributes: tuple[str, ...] = ()
    temporary_attributes: tuple[str, ...] = ()
    transition: str | None = None
    speaker_is_literal: bool = False


@dataclass(frozen=True, slots=True)
class _Slot:
    kind: str
    label: str
    header_line: int
    source: _Say
    target: _Say


@dataclass(frozen=True, slots=True)
class _LexicalDocument:
    raw: bytes
    language: str
    slots: tuple[_Slot, ...]


@dataclass(frozen=True, slots=True)
class _Line:
    data: bytes
    number: int
    offset: int
    column_base: int = 0

    def fail(self, category: str, reason: str, at: int = 0) -> RpyInputError:
        return RpyInputError(category, self.number, self.offset + at,
                             self.column_base + at + 1, reason)


@dataclass(slots=True)
class _Block:
    label: str
    header: _Line
    indent: int | None = None
    source: _Say | None = None
    target: _Say | None = None
    controls: int = 0
    pairs: int = 0


def _identifier(raw: bytes) -> bool:
    # UTF-8 has already been checked. No Python/Ren'Py evaluation is involved.
    return raw.decode('utf-8').isidentifier()


def _words(raw: bytes) -> tuple[bytes, ...]:
    return tuple(word for word in raw.split(b' ') if word)


def _quoted_start(raw: bytes, start: int) -> int:
    single, double = raw.find(b"'", start), raw.find(b'"', start)
    return double if single < 0 else single if double < 0 else min(single, double)


def _literal(line: _Line, start: int, limits: RpyLexicalLimits,
             checkpoint: Callable[[], None]) -> tuple[_Literal, int]:
    data = line.data
    if start >= len(data) or data[start] not in (34, 39):
        raise line.fail('unsupported-syntax', 'expected a single quoted literal', start)
    quote = data[start]
    if data[start:start + 3] == bytes([quote]) * 3:
        raise line.fail('unsupported-syntax', 'multiline quoting is unsupported', start)
    decoded = bytearray()
    escapes = {92: 92, quote: quote, 110: 10, 114: 13, 116: 9}
    position = start + 1
    next_checkpoint = position
    while position < len(data):
        if position >= next_checkpoint:
            checkpoint()
            next_checkpoint = position + 4096
        character = data[position]
        if character == quote:
            return (_Literal(decoded.decode('utf-8'), chr(quote),
                             line.offset + start, line.offset + position + 1,
                             line.number), position + 1)
        if character == 92:
            position += 1
            if position >= len(data):
                break
            if data[position] not in escapes:
                raise line.fail('invalid-escape', 'escape is outside the declared profile', position - 1)
            character = escapes[data[position]]
        decoded.append(character)
        if len(decoded) > limits.max_string_bytes:
            raise line.fail('limit-exceeded', 'logical string byte limit exceeded', start)
        position += 1
    raise line.fail('unsupported-syntax', 'unterminated single-line literal', start)


def _suffix(line: _Line, position: int, *, allow_transition: bool) -> str | None:
    remainder = line.data[position:]
    # A comment starts outside a literal; its quotes are ordinary comment bytes.
    code = remainder.split(b'#', 1)[0].strip(b' ')
    if not code:
        return None
    words = _words(code)
    if (allow_transition and remainder.startswith(b' ') and len(words) == 2
            and words[0] == b'with' and _identifier(words[1])):
        return words[1].decode('utf-8')
    raise line.fail('unsupported-syntax', 'unexpected code after literal', position)


def _say(line: _Line, start: int, limits: RpyLexicalLimits,
         checkpoint: Callable[[], None]) -> _Say:
    quote_at = _quoted_start(line.data, start)
    if quote_at < 0:
        raise line.fail('unsupported-syntax', 'expected a supported say statement', start)
    prefix = line.data[start:quote_at]
    words = _words(prefix)
    speaker = None
    speaker_is_literal = False
    literal, end = _literal(line, quote_at, limits, checkpoint)
    if not words:
        # A second literal makes the first one a name. Quotes in a trailing
        # comment or transition never turn narration into named dialogue.
        remainder = line.data[end:]
        next_quote = _quoted_start(remainder, 0)
        comment_at = remainder.find(b'#')
        if (next_quote >= 0 and (comment_at < 0 or next_quote < comment_at)
                and not remainder.lstrip(b' ').startswith(b'with ')):
            prefix = remainder[:next_quote]
            if not prefix.startswith(b' ') or not prefix.endswith(b' '):
                raise line.fail('unsupported-syntax', 'name and dialogue require spaces', end)
            speaker = literal.text
            speaker_is_literal = True
            words = _words(prefix)
            literal, end = _literal(line, end + next_quote, limits, checkpoint)
    else:
        if not prefix.endswith(b' ') or not _identifier(words[0]):
            raise line.fail('unsupported-syntax', 'speaker must be a single identifier', start)
        speaker = words[0].decode('utf-8')
        if speaker in _STATEMENTS:
            raise line.fail('unsupported-syntax', 'statement is outside the TL profile', start)
        words = words[1:]
    attributes: list[str] = []
    temporary: list[str] = []
    has_separator = False
    for word in words:
        if word == b'@':
            if has_separator:
                raise line.fail('unsupported-syntax', 'multiple temporary attribute separators', start)
            has_separator = True
        elif _identifier(word[1:] if word.startswith(b'-') else word):
            (temporary if has_separator else attributes).append(word.decode('utf-8'))
        else:
            raise line.fail('unsupported-syntax', 'unsupported say attribute', start)
    if has_separator and not temporary:
        raise line.fail('unsupported-syntax', 'temporary attributes are missing', start)
    transition = _suffix(line, end, allow_transition=True)
    return _Say(literal, speaker, tuple(attributes), tuple(temporary), transition,
                speaker_is_literal)


def _source_tokens(say: _Say, line: _Line,
                   checkpoint: Callable[[], None]) -> Iterator[str]:
    """One non-executing tokenizer for source recognition and private facts."""
    text = say.literal.text
    if not text:
        raise line.fail('invalid-source', 'decoded source is empty', say.literal.start - line.offset)
    try:
        yield from protection_tokens(text, checkpoint)
    except RpyTextError as error:
        raise line.fail('invalid-source', error.reason) from None


def _source_text(say: _Say, line: _Line, checkpoint: Callable[[], None]) -> None:
    for _ in _source_tokens(say, line, checkpoint):
        pass


def _comment_can_be_source(data: bytes, start: int) -> bool:
    quote = _quoted_start(data, start)
    if quote < 0:
        return False
    prefix = data[start:quote]
    if not prefix:
        return True
    words = _words(prefix)
    if words and words[0].decode('utf-8') in _STATEMENTS:
        return False
    # Location/ordinary punctuation comments are not commented say statements.
    return bool(words and all(word == b'@' or word == b'-' or
                              _identifier(word[1:] if word.startswith(b'-') else word)
                              for word in words))


def scan_tl(raw: bytes, *, limits: RpyLexicalLimits = RpyLexicalLimits(),
            check_cancelled: Callable[[], None] | None = None) -> _LexicalDocument:
    """Recognize a complete bounded document, raising without partial output.

    ``check_cancelled`` raises to abort. Foundation source identity/EOF validation
    belongs to the future reader adapter, not to these format-only lexical facts.
    """
    if type(raw) is not bytes or type(limits) is not RpyLexicalLimits:
        raise TypeError('RPY scanner requires exact bytes and lexical limits')
    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError('cancellation checkpoint must be callable')
    checkpoint = check_cancelled if check_cancelled is not None else lambda: None
    checkpoint()
    if len(raw) > limits.max_input_bytes:
        raise RpyInputError('limit-exceeded', 1, 0, 1, 'input byte limit exceeded')
    try:
        raw.decode('utf-8')
    except UnicodeDecodeError as error:
        line = raw.count(b'\n', 0, error.start) + 1
        column = error.start - raw.rfind(b'\n', 0, error.start)
        raise RpyInputError('invalid-encoding', line, error.start, column,
                            'input is not valid UTF-8') from None
    slots: list[_Slot] = []
    language: str | None = None
    labels: set[str] = set()
    old_keys: set[str] = set()
    block: _Block | None = None

    def append_slot(current: _Block, source: _Say, target: _Say, line: _Line) -> None:
        if len(slots) >= limits.max_slots:
            raise line.fail('limit-exceeded', 'translation slot limit exceeded')
        slots.append(_Slot('strings' if current.label == 'strings' else 'dialogue',
                           current.label, current.header.number, source, target))

    def finish() -> None:
        if block is None:
            return
        if block.label == 'strings':
            if block.source is not None:
                raise block.header.fail('ambiguous-slot', 'old has no matching new')
            if not block.pairs:
                raise block.header.fail('unsupported-syntax', 'empty strings block')
        elif block.source is not None and block.target is not None:
            append_slot(block, block.source, block.target, block.header)
        elif block.source is not None or block.target is not None:
            raise block.header.fail('ambiguous-slot', 'dialogue requires one source and one target')
        elif not block.controls:
            raise block.header.fail('unsupported-syntax', 'empty dialogue block')

    offset = 3 if raw.startswith(_BOM) else 0
    number = 1
    while offset < len(raw):
        checkpoint()
        end = raw.find(b'\n', offset)
        end = len(raw) if end < 0 else end + 1
        data = raw[offset:end]
        data = data[:-1] if data.endswith(b'\n') else data
        data = data[:-1] if data.endswith(b'\r') and raw[end - 1:end] == b'\n' else data
        line = _Line(data, number, offset, 3 if number == 1 and offset == 3 else 0)
        indent = len(data) - len(data.lstrip(b' '))
        body = data[indent:]
        # A line is physically bounded by LF; no universal-newline normalization.
        if b'\r' in data or b'\x00' in data or body.startswith(b'\t'):
            raise line.fail('unsupported-syntax', 'unsupported physical line or indentation', indent)
        if not body:
            offset, number = end, number + 1
            continue
        comment = body.startswith(b'#')
        start = indent
        if comment:
            start += 1
            while start < len(data) and data[start] == 32:
                start += 1
            if (block is None or block.label == 'strings' or
                    not _comment_can_be_source(data, start)):
                offset, number = end, number + 1
                continue
        elif indent == 0:
            header = data.split(b'#', 1)[0].rstrip(b' ')
            words = _words(header[:-1]) if header.endswith(b':') else ()
            if (len(words) != 3 or words[0] != b'translate' or
                    not _identifier(words[1]) or not _identifier(words[2])):
                raise line.fail('unsupported-syntax', 'expected a supported translate header')
            finish()
            lang, label = words[1].decode('utf-8'), words[2].decode('utf-8')
            if lang == 'None' and label != 'strings':
                raise line.fail('unsupported-syntax', 'None language supports strings only')
            if label in ('python', 'style'):
                raise line.fail('unsupported-syntax', 'executable translation block is unsupported')
            if language is not None and language != lang:
                raise line.fail('mixed-language', 'document contains multiple target languages')
            language = lang
            if label != 'strings':
                if label in labels:
                    raise line.fail('duplicate-identity', 'duplicate dialogue label')
                labels.add(label)
            block = _Block(label, line)
            offset, number = end, number + 1
            continue
        if block is None or not indent:
            raise line.fail('unsupported-syntax', 'statement is outside a translation block', start)
        if block.indent is None:
            block.indent = indent
        if indent != block.indent:
            raise line.fail('unsupported-syntax', 'translation block has inconsistent indentation', start)
        if block.label == 'strings':
            keyword, separator, remainder = body.partition(b' ')
            if keyword not in (b'old', b'new') or not separator:
                raise line.fail('unsupported-syntax', 'expected old or new literal', start)
            literal_at = indent + len(keyword) + len(separator) + len(remainder) - len(remainder.lstrip(b' '))
            literal, literal_end = _literal(line, literal_at, limits, checkpoint)
            _suffix(line, literal_end, allow_transition=False)
            say = _Say(literal)
            if keyword == b'old':
                if block.source is not None:
                    raise line.fail('ambiguous-slot', 'old has no matching new', start)
                _source_text(say, line, checkpoint)
                if literal.text in old_keys:
                    raise line.fail('duplicate-identity', 'duplicate decoded old key', start)
                old_keys.add(literal.text)
                block.source = say
            else:
                if block.source is None:
                    raise line.fail('ambiguous-slot', 'new has no matching old', start)
                append_slot(block, block.source, say, line)
                block.source = None
                block.pairs += 1
        elif not comment and (body.startswith(b'voice ') or body.split(b' ', 1)[0] == b'nvl'):
            if body.startswith(b'voice '):
                literal_at = indent + 6
                while literal_at < len(data) and data[literal_at] == 32:
                    literal_at += 1
                _, literal_end = _literal(line, literal_at, limits, checkpoint)
                _suffix(line, literal_end, allow_transition=False)
            elif _words(body.split(b'#', 1)[0]) != (b'nvl', b'clear'):
                raise line.fail('unsupported-syntax', 'unsupported control statement', start)
            block.controls += 1
        else:
            say = _say(line, start, limits, checkpoint)
            if comment:
                if block.source is not None or block.target is not None:
                    raise line.fail('ambiguous-slot', 'multiple or misplaced dialogue sources', start)
                _source_text(say, line, checkpoint)
                block.source = say
            else:
                if block.target is not None or block.source is None:
                    raise line.fail('ambiguous-slot', 'target requires exactly one preceding source', start)
                if ((say.speaker, say.speaker_is_literal) !=
                        (block.source.speaker, block.source.speaker_is_literal)):
                    raise line.fail('ambiguous-slot', 'source and target speaker differ', start)
                block.target = say
        offset, number = end, number + 1
    finish()
    checkpoint()
    if language is None:
        raise RpyInputError('unsupported-syntax', 1, 0, 1, 'no translation header found')
    return _LexicalDocument(raw, language, tuple(slots))


@dataclass(frozen=True, slots=True)
class _ParsedTl:
    lexical: _LexicalDocument
    records: tuple[ParsedSegment, ...]


def _strings_identity_digest(language: str, source: str) -> str:
    digest = hashlib.sha256(b'localcat:renpy-tl-v1:strings\x00')
    for field in (language, source):
        encoded = field.encode('utf-8')
        digest.update(len(encoded).to_bytes(8, 'big'))
        digest.update(encoded)
    return digest.hexdigest()


def parse_tl(raw: bytes, *, limits: RpyLexicalLimits = RpyLexicalLimits(),
             check_cancelled: Callable[[], None] | None = None) -> _ParsedTl:
    """Map fully checked TL slots to neutral records with stable format IDs."""
    lexical = scan_tl(raw, limits=limits, check_cancelled=check_cancelled)
    checkpoint = check_cancelled if check_cancelled is not None else lambda: None
    records: list[ParsedSegment] = []
    identities: set[str] = set()
    for slot in lexical.slots:
        checkpoint()
        if slot.kind == 'dialogue':
            # The UTF-8 byte length frames the language without delimiter
            # ambiguity. Label/source order and current target are not inputs.
            local_id = (f'renpy-tl-v1:d:{len(lexical.language.encode("utf-8"))}:'
                        f'{lexical.language}:{slot.label}')
        else:
            local_id = 'renpy-tl-v1:s:' + _strings_identity_digest(
                lexical.language, slot.source.literal.text,
            )
        if local_id in identities:
            source = slot.source.literal
            column = source.start - raw.rfind(b'\n', 0, source.start)
            raise RpyInputError('duplicate-identity', source.line, source.start,
                                column, 'translation slot identity collision')
        identities.add(local_id)
        target = slot.target.literal.text
        records.append(ParsedSegment(
            local_id=local_id,
            source=slot.source.literal.text,
            target=target,
            target_presence=TargetPresence.EXPLICIT_EMPTY if target == '' else TargetPresence.PRESENT,
            translation_state=TranslationState.UNCONFIRMED,
            speaker=RawSpeaker(slot.source.speaker or ''),
            format_metadata=(),
        ))
    checkpoint()
    return _ParsedTl(lexical, tuple(records))


def _private_error(reason: str, category: str = 'private-stale') -> RpyInputError:
    return RpyInputError(category, 1, 0, 1, reason)


def _private_arguments(raw: bytes, limits: RpyLexicalLimits, max_private_bytes: int,
                       check_cancelled: Callable[[], None] | None) -> Callable[[], None]:
    if type(raw) is not bytes or type(limits) is not RpyLexicalLimits:
        raise TypeError('RPY private mapping requires exact source bytes and lexical limits')
    if type(max_private_bytes) is not int:
        raise TypeError('private byte limit must be an exact integer')
    if not 0 < max_private_bytes <= 32 * _MIB:
        raise ValueError('private byte limit must stay within the declared profile')
    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError('cancellation checkpoint must be callable')
    checkpoint = check_cancelled if check_cancelled is not None else lambda: None
    checkpoint()
    if len(raw) > limits.max_input_bytes:
        raise _private_error('input byte limit exceeded', 'limit-exceeded')
    return checkpoint


def _private_header(parsed: _ParsedTl) -> dict:
    return {
        'version': 'rpy-roundtrip-v1',
        'codec_identity': {
            'provider_id': RPY_CODEC_IDENTITY.provider_id,
            'codec_id': RPY_CODEC_IDENTITY.codec_id,
            'codec_version': RPY_CODEC_IDENTITY.codec_version,
        },
        'source_sha256': hashlib.sha256(parsed.lexical.raw).hexdigest(),
        'profile': 'renpy-tl-v1',
        'language': parsed.lexical.language,
    }


def _private_slot(slot: _Slot, raw: bytes, checkpoint: Callable[[], None]) -> dict:
    source, target = slot.source.literal, slot.target.literal
    line_start = raw.rfind(b'\n', 0, source.start) + 1
    line = _Line(b'', source.line, line_start)
    return {
        # Half-open literal spans include quotes. Task 2.4 will replace only
        # their interiors when an edited target requires fresh encoding.
        'source_span': [source.start, source.end],
        'target_span': [target.start, target.end],
        'target_sha256': hashlib.sha256(target.text.encode('utf-8')).hexdigest(),
        'source_tokens': list(_source_tokens(slot.source, line, checkpoint)),
    }


def build_private_payload(raw: bytes, *, limits: RpyLexicalLimits = RpyLexicalLimits(),
                          max_private_bytes: int = 32 * _MIB,
                          check_cancelled: Callable[[], None] | None = None) -> bytes:
    """Encode source-derived format data, with no current edit or live authority.

    Source validation is all-or-nothing. Only a single slot is serialized at a
    time, and the accumulated byte budget is enforced before retaining it.
    """
    checkpoint = _private_arguments(raw, limits, max_private_bytes, check_cancelled)
    parsed = parse_tl(raw, limits=limits, check_cancelled=checkpoint)
    output = bytearray()

    def append(chunk: bytes) -> None:
        checkpoint()
        if len(output) + len(chunk) > max_private_bytes:
            raise _private_error('private payload byte limit exceeded', 'limit-exceeded')
        output.extend(chunk)

    def encode(value: dict) -> bytes:
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'),
                          allow_nan=False).encode('utf-8')

    append(encode(_private_header(parsed))[:-1] + b',"slots":{')
    for index, (record, slot) in enumerate(zip(parsed.records, parsed.lexical.slots, strict=True)):
        checkpoint()
        entry = {record.local_id: _private_slot(slot, raw, checkpoint)}
        append((b',' if index else b'') + encode(entry)[1:-1])
    append(b'}}')
    checkpoint()
    return bytes(output)


class _PrivateJsonError(ValueError):
    pass


def _private_json_preflight(payload: bytes, parsed: _ParsedTl,
                            checkpoint: Callable[[], None]) -> None:
    """Bound hostile structure and integers before JSON allocates containers.

    The schema's deepest containers are the slot span/token arrays (depth 4).
    Its separators are bounded by actual source slots and possible two-character
    protection tokens, so a tiny TL cannot induce a huge JSON object graph.
    Strings remain opaque here; the JSON decoder checks their exact grammar.
    """
    separator_limit = 16 + 12 * len(parsed.records)
    separator_limit += sum(len(record.source) // 2 for record in parsed.records)
    separators = 0
    depth = 0
    quoted = escaped = False
    integer_digits = 0
    for position, character in enumerate(payload):
        if position % 4096 == 0:
            checkpoint()
        if quoted:
            if escaped:
                escaped = False
            elif character == 92:
                escaped = True
            elif character == 34:
                quoted = False
            continue
        if character == 34:
            quoted = True
        elif character in (123, 91):
            depth += 1
            if depth > 4:
                raise _private_error('private JSON depth limit exceeded', 'limit-exceeded')
        elif character in (125, 93):
            depth -= 1
        elif character == 44:
            separators += 1
            if separators > separator_limit:
                raise _private_error('private JSON item limit exceeded', 'limit-exceeded')
        if 48 <= character <= 57:
            integer_digits += 1
            # All JSON integers are byte offsets, at most 16 MiB (8 digits).
            if integer_digits > 8:
                raise _private_error('private JSON integer is outside the profile')
        else:
            integer_digits = 0
    checkpoint()


def _private_json_decode(payload: bytes, checkpoint: Callable[[], None]) -> object:
    def unique_object(pairs: list[tuple[str, object]]) -> dict:
        checkpoint()
        result = {}
        for key, value in pairs:
            if key in result:
                raise _PrivateJsonError()
            result[key] = value
        return result

    def reject_number(value: str) -> None:
        raise _PrivateJsonError()

    try:
        return json.loads(payload.decode('utf-8'), object_pairs_hook=unique_object,
                          parse_float=reject_number, parse_constant=reject_number)
    except (UnicodeDecodeError, json.JSONDecodeError, _PrivateJsonError):
        raise _private_error('private payload is not strict profile JSON') from None


def _private_equal(actual: object, expected: object,
                    checkpoint: Callable[[], None]) -> bool:
    # Python equality alone would admit bool as int and float as byte offset.
    if type(actual) is not type(expected):
        return False
    if type(expected) is dict:
        return actual.keys() == expected.keys() and all(
            _private_equal(actual[key], value, checkpoint) for key, value in expected.items()
        )
    if type(expected) is list:
        if len(actual) != len(expected):
            return False
        for index, (left, right) in enumerate(zip(actual, expected, strict=True)):
            if index % 4096 == 0:
                checkpoint()
            if not _private_equal(left, right, checkpoint):
                return False
        return True
    return actual == expected


def validate_private_payload(raw: bytes, payload: bytes, *,
                             limits: RpyLexicalLimits = RpyLexicalLimits(),
                             max_private_bytes: int = 32 * _MIB,
                             check_cancelled: Callable[[], None] | None = None) -> _ParsedTl:
    """Reparse source and verify all opaque facts; return only immutable facts.

    The caller must retain/verify its source separately. Neither successful
    validation nor persisted JSON issues a Foundation terminal or writer token.
    """
    checkpoint = _private_arguments(raw, limits, max_private_bytes, check_cancelled)
    if type(payload) is not bytes:
        raise TypeError('RPY private payload must be exact bytes')
    if len(payload) > max_private_bytes:
        raise _private_error('private payload byte limit exceeded', 'limit-exceeded')
    parsed = parse_tl(raw, limits=limits, check_cancelled=checkpoint)
    _private_json_preflight(payload, parsed, checkpoint)
    document = _private_json_decode(payload, checkpoint)
    header = _private_header(parsed)
    if type(document) is not dict or document.keys() != header.keys() | {'slots'}:
        raise _private_error('private payload fields do not match the profile')
    if not _private_equal({key: document[key] for key in header}, header, checkpoint):
        raise _private_error('private identity, version or source facts do not match')
    slots = document['slots']
    if type(slots) is not dict or slots.keys() != {record.local_id for record in parsed.records}:
        raise _private_error('private slot identities do not match source')
    for record, slot in zip(parsed.records, parsed.lexical.slots, strict=True):
        checkpoint()
        if not _private_equal(slots[record.local_id], _private_slot(slot, raw, checkpoint), checkpoint):
            raise _private_error('private slot spans, digest or protection tokens do not match source')
    checkpoint()
    return parsed


def prepare_round_trip_bytes(raw: bytes, payload: bytes,
                             edits: tuple[RoundTripSegmentEdit, ...], *,
                             limits: RpyLexicalLimits = RpyLexicalLimits(),
                             round_trip_limits: RoundTripLimits = RoundTripLimits(32 * _MIB, 32 * _MIB),
                             check_cancelled: Callable[[], None] | None = None) -> PreparedFormatBytes:
    """Prepare bounded format bytes from source-verified slots and current edits.

    This is deliberately not a live writer factory. Source/token lifetimes,
    target selection, and publication stay with the Foundation and Application.
    """
    if type(round_trip_limits) is not RoundTripLimits:
        raise TypeError('round-trip limits must be exact RoundTripLimits')
    if type(edits) is not tuple:
        raise TypeError('round-trip edits must be an exact tuple')
    max_output = min(round_trip_limits.max_output_bytes, 32 * _MIB)
    parsed = validate_private_payload(
        raw, payload, limits=limits,
        max_private_bytes=min(round_trip_limits.max_opaque_payload_bytes, 32 * _MIB),
        check_cancelled=check_cancelled,
    )
    checkpoint = check_cancelled if check_cancelled is not None else lambda: None
    if len(edits) != len(parsed.records):
        raise _private_error('edit identities do not match the complete slot set', 'invalid-edits')
    by_id: dict[str, str] = {}
    for edit in edits:
        checkpoint()
        if type(edit) is not RoundTripSegmentEdit:
            raise TypeError('edits must be exact RoundTripSegmentEdit values')
        if type(edit.local_id) is not str or type(edit.target) is not str:
            raise TypeError('edit identity and target must be exact strings')
        if edit.local_id in by_id:
            raise _private_error('duplicate edit identity', 'invalid-edits')
        by_id[edit.local_id] = edit.target
    if by_id.keys() != {record.local_id for record in parsed.records}:
        raise _private_error('edit identities do not match source', 'invalid-edits')

    # Account for every untouched byte up front; target replacements may grow
    # or shrink independently. Encoded replacements never exceed the remaining
    # final-output budget, even before assembly.
    output_size = len(raw)
    for slot in parsed.lexical.slots:
        checkpoint()
        output_size -= slot.target.literal.end - slot.target.literal.start - 2
    if output_size > max_output:
        raise _private_error('output byte limit exceeded', 'limit-exceeded')
    replacements: list[bytes | None] = []
    for number, (record, slot) in enumerate(zip(parsed.records, parsed.lexical.slots, strict=True), 1):
        checkpoint()
        target = by_id[record.local_id]
        original = slot.target.literal
        try:
            check_text(target, limits.max_string_bytes, checkpoint)
            validate_target(record.source, target, checkpoint)
            if target == original.text:
                size = original.end - original.start - 2
                if output_size + size > max_output:
                    raise RpyTextError('limit-exceeded', 'output byte limit exceeded')
                replacement = None
            else:
                replacement = encode_target(target, original.quote, max_output - output_size, checkpoint)
                size = len(replacement)
        except RpyTextError as error:
            column = original.start - raw.rfind(b'\n', 0, original.start)
            raise RpyInputError(error.category, original.line, original.start,
                                column, error.reason, local_id=record.local_id,
                                record_number=number) from None
        output_size += size
        replacements.append(replacement)

    if all(replacement is None for replacement in replacements):
        checkpoint()
        output = raw
    else:
        output_buffer = bytearray()
        def append(chunk: bytes | memoryview) -> None:
            # Assembly also checkpoints long untouched comments/control spans.
            view = memoryview(chunk)
            for start in range(0, len(view), 65536):
                checkpoint()
                part = view[start:start + 65536]
                if len(output_buffer) + len(part) > max_output:
                    raise _private_error('output byte limit exceeded', 'limit-exceeded')
                output_buffer.extend(part)
        cursor = 0
        source_view = memoryview(raw)
        for slot, replacement in zip(parsed.lexical.slots, replacements, strict=True):
            checkpoint()
            if replacement is None:
                continue
            literal = slot.target.literal
            append(source_view[cursor:literal.start + 1])
            append(replacement)
            cursor = literal.end - 1
        append(source_view[cursor:])
        checkpoint()
        output = bytes(output_buffer)
    checkpoint()
    return PreparedFormatBytes(
        codec_identity=RPY_CODEC_IDENTITY, format_id=FormatId('renpy-tl-v1'),
        source_fingerprint=hashlib.sha256(raw).hexdigest(),
        output_fingerprint=hashlib.sha256(output).hexdigest(), payload=output,
    )


class RpyProvider:
    provider_id = 'localcat.rpy'
    provider_version = '1'

    def descriptors(self) -> tuple[CodecDescriptor, ...]:
        return (RPY_DESCRIPTOR,)


def _lease_bytes(source: SnapshotCursorLease, limits: RpyLexicalLimits) -> bytes:
    # A bounded complete read proves EOF to Foundation before any grammar runs.
    raw = source.read(limits.max_input_bytes + 1)
    if len(raw) > limits.max_input_bytes:
        raise _private_error('input byte limit exceeded', 'limit-exceeded')
    if source.read(1):
        raise _private_error('input byte limit exceeded', 'limit-exceeded')
    return raw


def _lexical_limits(profile: LimitProfile) -> RpyLexicalLimits:
    return RpyLexicalLimits(
        max_input_bytes=min(profile.max_input_bytes, 16 * _MIB),
        max_slots=min(profile.max_records, profile.max_materialized_records, 100_000),
        max_string_bytes=min(profile.max_decoded_field_chars, _MIB),
    )


class RpyCodec:
    """Consume a live lease; retain neither that lease nor caller authority."""

    def __init__(self) -> None:
        self.descriptor = RPY_DESCRIPTOR

    def iter_raw(self, source: SnapshotCursorLease, request: ReadRequest) -> Iterator[RawParseEvent]:
        try:
            limits = _lexical_limits(self.descriptor.limit_profile)
            raw = _lease_bytes(source, limits)
            parsed = parse_tl(raw, limits=limits, check_cancelled=lambda: source.read(0))
        except RpyInputError as error:
            yield error.issue
            return
        yield DocumentHeader('Ren\'Py TL', None, parsed.lexical.language, ())
        yield from parsed.records

    def prepare_source_state(self, request: SourceStateRequest) -> OpaqueSourceState:
        try:
            limits = _lexical_limits(request.limit_profile)
            raw = _lease_bytes(request.source, limits)
            payload = build_private_payload(
                raw, limits=limits,
                max_private_bytes=min(request.limits.max_opaque_payload_bytes, 32 * _MIB),
                check_cancelled=lambda: request.source.read(0),
            )
        except RpyInputError as error:
            raise ContractViolation(error.issue.code, error.issue.safe_summary) from None
        return OpaqueSourceState(self.descriptor.identity, self.descriptor.format_id,
                                 'rpy-roundtrip-v1', payload)

    def prepare(self, request: RoundTripRequest) -> PreparedFormatBytes:
        limits = _lexical_limits(request.limit_profile)
        raw = _lease_bytes(request.source, limits)
        try:
            return prepare_round_trip_bytes(
                raw, request.token.opaque_payload, request.edits,
                limits=limits, round_trip_limits=request.limits,
                check_cancelled=lambda: request.source.read(0),
            )
        except RpyInputError as error:
            # Fatal data is validated and rejected by Foundation before it can
            # issue publication authority; preserve exact format locations.
            return PreparedFormatBytes(
                self.descriptor.identity, self.descriptor.format_id,
                hashlib.sha256(raw).hexdigest(), hashlib.sha256(b'').hexdigest(),
                b'', (error.issue,),
            )


RPY_DESCRIPTOR = CodecDescriptor(
    identity=RPY_CODEC_IDENTITY,
    purpose=EffectivePurpose.PROJECT_DOCUMENT,
    format_id=FormatId('renpy-tl-v1'),
    extensions=('.rpy',), mime_types=(), sniff_prefixes=(),
    capabilities=CodecCapabilities(
        readable=True, validatable=True, canonical_write=False,
        source_round_trip_write=True, streaming_input=False,
        iterator_view=True, materialized_view=True, format_profile='renpy-tl-v1',
    ),
    limit_profile=LimitProfile(
        profile_id='renpy-tl-v1', profile_version=1,
        max_input_bytes=16 * _MIB, max_decoded_field_chars=_MIB,
        max_records=100_000, max_materialized_records=100_000,
        max_retained_issues=256,
        declared_issue_codes=tuple(sorted(set(FOUNDATION_GUARDED_ISSUE_CODES) | {
            'PARSER.RPY.' + category for category in (
                'AMBIGUOUS_SLOT', 'DUPLICATE_IDENTITY', 'INVALID_EDITS',
                'INVALID_ENCODING', 'INVALID_ESCAPE', 'INVALID_SOURCE',
                'LIMIT_EXCEEDED', 'MIXED_LANGUAGE', 'PLACEHOLDER_MISMATCH',
                'PRIVATE_STALE', 'UNSUPPORTED_SYNTAX',
            )
        })),
        max_metadata_entries_per_container=16,
        max_metadata_decoded_chars_per_container=_MIB,
        max_metadata_decoded_chars_total=16 * _MIB,
        max_structure_depth=8,
    ),
    input_consumption_policy=InputConsumptionPolicy.SEALED_BYTES_EOF,
    reader_factory=RpyCodec, canonical_serializer_factory=None,
    round_trip_serializer_factory=RpyCodec,
    round_trip_limits=RoundTripLimits(32 * _MIB, 32 * _MIB),
    source_state_factory=RpyCodec,
    source_state_limits=SourceStateLimits(32 * _MIB),
)
