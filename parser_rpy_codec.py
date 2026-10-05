"""Bounded, non-executing lexical recognition of the renpy-tl-v1 profile.

These private format facts are not Foundation terminal or publication authority.
The caller supplies complete verified bytes; only a wholly valid input returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from parser_contracts import IssueSeverity, ParseIssue


_MIB = 1024 * 1024
_BOM = b'\xef\xbb\xbf'
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
                 reason: str) -> None:
        self.category = category
        self.byte_column = column
        self.issue = ParseIssue(
            code='PARSER.RPY.' + category.replace('-', '_').upper(),
            severity=IssueSeverity.FATAL,
            safe_summary=f'RPY line {line}, byte column {column}: {reason}',
            byte_offset=offset,
            line_number=line,
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
    while position < len(data):
        if (position - start) % 4096 == 0:
            checkpoint()
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
    attributes: list[str] = []
    temporary: list[str] = []
    has_separator = False
    if words:
        if not prefix.endswith(b' ') or not _identifier(words[0]):
            raise line.fail('unsupported-syntax', 'speaker must be a single identifier', start)
        speaker = words[0].decode('utf-8')
        if speaker in _STATEMENTS:
            raise line.fail('unsupported-syntax', 'statement is outside the TL profile', start)
        for word in words[1:]:
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
    literal, end = _literal(line, quote_at, limits, checkpoint)
    transition = _suffix(line, end, allow_transition=True)
    return _Say(literal, speaker, tuple(attributes), tuple(temporary), transition)


def _source_text(say: _Say, line: _Line, checkpoint: Callable[[], None]) -> None:
    text = say.literal.text
    if not text:
        raise line.fail('invalid-source', 'decoded source is empty', say.literal.start - line.offset)
    position = 0
    while position < len(text):
        if position % 4096 == 0:
            checkpoint()
        character = text[position]
        if text[position:position + 2] in ('[[', '{{'):
            position += 2
            continue
        if character == '[':
            # Only delimiters and quote state are recognized; expressions remain
            # opaque text, including conversion/format parts and nested indexing.
            stack = [']']
            quote = None
            position += 1
            while position < len(text) and stack:
                if position % 4096 == 0:
                    checkpoint()
                token = text[position]
                if quote:
                    if token == '\\':
                        position += 2
                        continue
                    if token == quote:
                        quote = None
                elif token in ('"', "'"):
                    quote = token
                elif token in '[({':
                    stack.append({'[': ']', '(': ')', '{': '}'}[token])
                elif token in '])}':
                    if token != stack.pop():
                        raise line.fail('invalid-source', 'mismatched interpolation delimiter')
                position += 1
            if stack or quote:
                raise line.fail('invalid-source', 'unterminated interpolation')
            continue
        if character == '{':
            end = text.find('}', position + 1)
            if end < 0 or '{' in text[position + 1:end]:
                raise line.fail('invalid-source', 'unterminated text tag')
            position = end + 1
            continue
        position += 1


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
                if say.speaker != block.source.speaker:
                    raise line.fail('ambiguous-slot', 'source and target speaker differ', start)
                block.target = say
        offset, number = end, number + 1
    finish()
    checkpoint()
    if language is None:
        raise RpyInputError('unsupported-syntax', 1, 0, 1, 'no translation header found')
    return _LexicalDocument(raw, language, tuple(slots))
