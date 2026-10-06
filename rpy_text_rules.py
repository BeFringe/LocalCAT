"""Non-executing text protection and literal encoding for renpy-tl-v1.

This leaf accepts decoded, bounded strings. It has no Parser, project, filesystem,
Ren'Py runtime, or publication dependencies.
"""

from collections import Counter
from typing import Callable, Iterator


class RpyTextError(ValueError):
    def __init__(self, category: str, reason: str) -> None:
        self.category = category
        self.reason = reason
        super().__init__(reason)


def protection_tokens(text: str, checkpoint: Callable[[], None]) -> Iterator[str]:
    """Recognize opaque interpolation and tags without parsing expressions."""
    position = 0
    next_checkpoint = 0
    while position < len(text):
        if position >= next_checkpoint:
            checkpoint()
            next_checkpoint = position + 4096
        character = text[position]
        if text[position:position + 2] in ('[[', '{{'):
            position += 2
            continue
        if character == '[':
            stack = [']']
            quote = None
            start = position
            position += 1
            while position < len(text) and stack:
                if position >= next_checkpoint:
                    checkpoint()
                    next_checkpoint = position + 4096
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
                        raise RpyTextError('placeholder-mismatch', 'mismatched interpolation delimiter')
                position += 1
            if stack or quote:
                raise RpyTextError('placeholder-mismatch', 'unterminated interpolation')
            yield text[start:position]
            continue
        if character == '{':
            start = position
            position += 1
            while position < len(text) and text[position] != '}':
                if position >= next_checkpoint:
                    checkpoint()
                    next_checkpoint = position + 4096
                if text[position] == '{':
                    raise RpyTextError('placeholder-mismatch', 'malformed text tag')
                position += 1
            if position == len(text):
                raise RpyTextError('placeholder-mismatch', 'unterminated text tag')
            position += 1
            yield text[start:position]
            continue
        position += 1
    checkpoint()


# General/dialogue/style tags from the Ren'Py text contract. Unknown custom
# tags remain opaque; a source close token establishes their paired form.
_PAIRED_TAGS = frozenset({
    '', 'a', 'alpha', 'alt', 'art', 'b', 'color', 'cps', 'feature', 'font',
    'horiz', 'i', 'k', 'noalt', 'outlinecolor', 'plain', 'rb', 'rt', 's',
    'shader', 'size', 'u', 'vert',
})
_SELF_CLOSING_TAGS = frozenset({
    'image', 'space', 'vspace', 'w', 'p', 'nw', 'fast', 'done', 'clear',
})


def _tag(token: str) -> tuple[str, bool]:
    body = token[1:-1]
    closing = body.startswith('/')
    if closing:
        body = body[1:]
        if '=' in body or ':' in body or any(character.isspace() for character in body):
            raise RpyTextError('placeholder-mismatch', 'invalid closing text tag')
    elif body.startswith('#'):
        return body, False
    elif not body or body == '=':
        raise RpyTextError('placeholder-mismatch', 'empty text tag')
    name = body.split('=', 1)[0]
    if name.startswith('feature:'):
        name = 'feature'
    if any(character.isspace() for character in name):
        raise RpyTextError('placeholder-mismatch', 'invalid text tag name')
    return name, closing


def validate_target(source: str, target: str,
                    checkpoint: Callable[[], None]) -> None:
    """Protect exact token multiplicities while allowing valid text movement."""
    checkpoint()
    if target == '':
        return
    expected = Counter(protection_tokens(source, checkpoint))
    observed: Counter[str] = Counter()
    # No custom code is loaded. Only source-declared explicit closing names
    # supply additional pairing information beyond built-in tags.
    paired = set(_PAIRED_TAGS)
    for index, token in enumerate(expected):
        if index % 4096 == 0:
            checkpoint()
        if token.startswith('{/'):
            name, _ = _tag(token)
            paired.add(name)
    stack: list[str] = []
    for token in protection_tokens(target, checkpoint):
        checkpoint()
        observed[token] += 1
        if observed[token] > expected[token]:
            raise RpyTextError('placeholder-mismatch', 'protection token was added or changed')
        if not token.startswith('{'):
            continue
        name, closing = _tag(token)
        self_closing = name in _SELF_CLOSING_TAGS or name.startswith('#')
        if closing:
            if self_closing or not stack or stack.pop() != name:
                raise RpyTextError('placeholder-mismatch', 'text tags are not properly nested')
        elif not self_closing and name in paired:
            stack.append(name)
    if observed != expected:
        raise RpyTextError('placeholder-mismatch', 'protection tokens are missing')
    # Ren'Py implicitly closes open tags at the end of the text block.
    checkpoint()


def check_text(text: str, max_string_bytes: int,
               checkpoint: Callable[[], None]) -> None:
    """Reject unrepresentable controls and bound UTF-8 before allocating it."""
    if len(text) > max_string_bytes:
        raise RpyTextError('limit-exceeded', 'logical string byte limit exceeded')
    total = 0
    for index, character in enumerate(text):
        if index % 4096 == 0:
            checkpoint()
        point = ord(character)
        if (point < 32 and character not in '\n\r\t') or 127 <= point < 160 or 0xD800 <= point <= 0xDFFF:
            raise RpyTextError('invalid-escape', 'target contains an unrepresentable character')
        total += 1 if point < 128 else 2 if point < 2048 else 3 if point < 65536 else 4
        if total > max_string_bytes:
            raise RpyTextError('limit-exceeded', 'logical string byte limit exceeded')
    checkpoint()


def encode_target(text: str, quote: str, max_encoded_bytes: int,
                  checkpoint: Callable[[], None]) -> bytes:
    """Encode only the original literal interior, never script delimiters."""
    escapes = {'\\': b'\\\\', quote: b'\\' + quote.encode('ascii'),
               '\n': b'\\n', '\r': b'\\r', '\t': b'\\t'}
    output = bytearray()
    for index, character in enumerate(text):
        if index % 4096 == 0:
            checkpoint()
        chunk = escapes.get(character)
        if chunk is None:
            chunk = character.encode('utf-8')
        if len(output) + len(chunk) > max_encoded_bytes:
            raise RpyTextError('limit-exceeded', 'output byte limit exceeded')
        output.extend(chunk)
    checkpoint()
    return bytes(output)
