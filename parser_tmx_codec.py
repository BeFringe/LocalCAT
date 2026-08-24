"""Safe streaming TMX Level 1 reader for the neutral Parser boundary.

The codec owns XML, locale, and translation-unit syntax only.  Records remain
provisional until ``parser_source.GuardedParseSession`` observes natural EOF and
issues the sole verified terminal.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field
from typing import Iterator
from xml.parsers import expat

from parser_contracts import (
    CodecCapabilities,
    CodecDescriptor,
    CodecIdentity,
    EffectivePurpose,
    FOUNDATION_GUARDED_ISSUE_CODES,
    InputConsumptionPolicy,
    IssueSeverity,
    LimitProfile,
    MetadataEntry,
    ParseIssue,
    RawParseEvent,
    RawSpeaker,
    ReadRequest,
    ResourceRecord,
    SnapshotCursorLease,
    TMX_LEVEL1_V1,
    TmxReadOptions,
)


_READ_CHUNK_BYTES = 64 * 1024
_XML_FEED_CHARS = 64 * 1024
_XML_NAMESPACE_SEPARATOR = "}"
_XML_LANG = "http://www.w3.org/XML/1998/namespace}lang"

_CODE_ENCODING = "PARSER.SOURCE.ENCODING_FAILED"
_CODE_DEPTH = "PARSER.LIMIT.DEPTH"
_CODE_INLINE = "PARSER.TMX.INLINE_XML_UNSUPPORTED"
_CODE_AMBIGUOUS = "PARSER.TMX.LOCALE_FALLBACK_AMBIGUOUS"
_CODE_MISSING_PAIR = "PARSER.TMX.LOCALE_PAIR_MISSING"
_CODE_INVALID_SELECTION = "PARSER.TMX.LOCALE_SELECTION_INVALID"
_CODE_NO_UNITS = "PARSER.TMX.NO_TRANSLATION_UNITS"
_CODE_SEGMENT_LIMIT = "PARSER.TMX.SEGMENT_LIMIT"
_CODE_UNSAFE_XML = "PARSER.TMX.UNSAFE_XML"

_TMX_ISSUE_CODES = (
    _CODE_ENCODING,
    _CODE_DEPTH,
    _CODE_INLINE,
    _CODE_AMBIGUOUS,
    _CODE_MISSING_PAIR,
    _CODE_INVALID_SELECTION,
    _CODE_NO_UNITS,
    _CODE_SEGMENT_LIMIT,
    _CODE_UNSAFE_XML,
)


TMX_LIMIT_PROFILE = LimitProfile(
    profile_id="tmx-level1-v1",
    profile_version=1,
    max_input_bytes=100 * 1024 * 1024,
    max_decoded_field_chars=1_000_000,
    max_records=1_000_000,
    max_materialized_records=100_000,
    max_retained_issues=256,
    declared_issue_codes=tuple(
        sorted(set(FOUNDATION_GUARDED_ISSUE_CODES).union(_TMX_ISSUE_CODES))
    ),
    max_metadata_entries_per_container=256,
    max_metadata_decoded_chars_per_container=1024 * 1024,
    max_metadata_decoded_chars_total=16 * 1024 * 1024,
    max_structure_depth=64,
)


TMX_CAPABILITIES = CodecCapabilities(
    readable=True,
    validatable=True,
    canonical_write=False,
    source_round_trip_write=False,
    streaming_input=True,
    iterator_view=True,
    materialized_view=True,
    format_profile=TMX_LIMIT_PROFILE.profile_id,
)


class _TmxAbort(Exception):
    """Private, body-free control flow from an Expat callback."""

    __slots__ = ("code", "safe_summary")

    def __init__(self, code: str, safe_summary: str) -> None:
        self.code = code
        self.safe_summary = safe_summary
        super().__init__(code)


@dataclass(slots=True)
class _LocaleChoice:
    requested: str
    exact_text: str | None = None
    exact_props: tuple[tuple[str, str, str], ...] = ()
    fallback_locale: str | None = None
    fallback_text: str | None = None
    fallback_props: tuple[tuple[str, str, str], ...] = ()
    fallback_ambiguous: bool = False

    def observe(
        self,
        locale: str,
        text: str,
        props: tuple[tuple[str, str, str], ...],
    ) -> None:
        if locale == self.requested:
            self.exact_text = text
            self.exact_props = props
            return
        if _locale_base(locale) != _locale_base(self.requested):
            return
        if self.fallback_locale is None:
            self.fallback_locale = locale
            self.fallback_text = text
            self.fallback_props = props
            return
        if locale == self.fallback_locale:
            self.fallback_text = text
            self.fallback_props = props
            return
        self.fallback_ambiguous = True

    def select(self) -> tuple[str | None, str, tuple[tuple[str, str, str], ...]]:
        if self.exact_text is not None:
            return self.exact_text, "exact", self.exact_props
        if self.fallback_ambiguous:
            return None, "ambiguous", ()
        if self.fallback_text is not None:
            return self.fallback_text, "fallback", self.fallback_props
        return None, "missing", ()


@dataclass(slots=True)
class _UnitState:
    ordinal: int
    depth: int
    source_choice: _LocaleChoice
    target_choice: _LocaleChoice
    props: list[tuple[str, str, str, str]] = field(default_factory=list)
    metadata_entries: int = 0
    metadata_chars: int = 0
    inline_xml: bool = False
    segment_oversized: bool = False


@dataclass(slots=True)
class _TuvState:
    depth: int
    locale: str | None
    segment_depth: int | None = None
    segment_seen: bool = False
    decoded_chars: int = 0
    text_chunks: list[str] = field(default_factory=list)
    text: str | None = None
    props: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass(slots=True)
class _PropState:
    depth: int
    scope: str
    prop_type: str
    language: str
    decoded_chars: int = 0
    text_chunks: list[str] = field(default_factory=list)


def _local_name(name: str) -> str:
    return name.rsplit(_XML_NAMESPACE_SEPARATOR, 1)[-1].lower()


def _normalize_locale(raw: str) -> str | None:
    if type(raw) is not str:
        return None
    normalized = raw.strip().replace("_", "-").lower()
    pieces = normalized.split("-")
    if (
        not normalized
        or any(not piece or not piece.isalnum() for piece in pieces)
    ):
        return None
    return "-".join(pieces)


def _locale_base(locale: str) -> str:
    return locale.split("-", 1)[0]


def _fatal(
    code: str,
    safe_summary: str,
    *,
    parser: expat.xmlparser | None = None,
) -> ParseIssue:
    line_number = None
    byte_offset = None
    if parser is not None:
        current_line = getattr(parser, "CurrentLineNumber", 0)
        current_byte = getattr(parser, "CurrentByteIndex", -1)
        if type(current_line) is int and current_line > 0:
            line_number = current_line
        if type(current_byte) is int and current_byte >= 0:
            byte_offset = current_byte
    return ParseIssue(
        code=code,
        severity=IssueSeverity.FATAL,
        safe_summary=safe_summary,
        line_number=line_number,
        byte_offset=byte_offset,
    )


def _warning(code: str, safe_summary: str, ordinal: int) -> ParseIssue:
    return ParseIssue(
        code=code,
        severity=IssueSeverity.WARNING,
        safe_summary=safe_summary,
        record_number=ordinal,
    )


def _iter_xml_feed_pieces(text: str) -> Iterator[str]:
    """Bound Expat calls and return promptly after each completed XML tag.

    Expat callbacks cannot suspend a ``Parse`` call.  Ending a feed piece at the
    next ``>`` lets a completed ``</tu>`` return to the raw generator, which in
    turn lets the Foundation run its per-event cancellation checkpoint before
    another physical unit is parsed.  A long text node remains bounded by the
    same 64 Ki character ceiling.
    """

    start = 0
    while start < len(text):
        bounded_end = min(len(text), start + _XML_FEED_CHARS)
        tag_end = text.find(">", start, bounded_end)
        end = tag_end + 1 if tag_end >= 0 else bounded_end
        yield text[start:end]
        start = end


class _TmxEventParser:
    """One bounded Expat grammar state machine for a single read request."""

    __slots__ = (
        "profile",
        "source_locale",
        "target_locale",
        "parser",
        "depth",
        "physical_units",
        "unit",
        "tuv",
        "prop",
        "events",
    )

    def __init__(
        self,
        *,
        profile: LimitProfile,
        source_locale: str,
        target_locale: str,
    ) -> None:
        self.profile = profile
        self.source_locale = source_locale
        self.target_locale = target_locale
        self.depth = 0
        self.physical_units = 0
        self.unit: _UnitState | None = None
        self.tuv: _TuvState | None = None
        self.prop: _PropState | None = None
        self.events: list[RawParseEvent] = []
        parser = expat.ParserCreate(namespace_separator=_XML_NAMESPACE_SEPARATOR)
        parser.buffer_text = True
        parser.StartElementHandler = self._start_element
        parser.EndElementHandler = self._end_element
        parser.CharacterDataHandler = self._character_data
        parser.XmlDeclHandler = self._xml_declaration
        parser.StartDoctypeDeclHandler = self._unsafe_declaration
        parser.EntityDeclHandler = self._unsafe_declaration
        parser.UnparsedEntityDeclHandler = self._unsafe_declaration
        parser.NotationDeclHandler = self._unsafe_declaration
        parser.ExternalEntityRefHandler = self._unsafe_external_entity
        parser.SkippedEntityHandler = self._unsafe_declaration
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        self.parser = parser

    def drain(self) -> tuple[RawParseEvent, ...]:
        events = tuple(self.events)
        self.events.clear()
        return events

    def _xml_declaration(
        self,
        _version: str,
        encoding: str | None,
        _standalone: int,
    ) -> None:
        if encoding is None:
            return
        normalized = encoding.strip().lower().replace("_", "-")
        if normalized not in {"utf-8", "utf8"}:
            raise _TmxAbort(
                _CODE_ENCODING,
                "TMX XML declaration must use UTF-8 encoding",
            )

    def _unsafe_declaration(self, *_args: object) -> None:
        raise _TmxAbort(
            _CODE_UNSAFE_XML,
            "TMX DTD and entity declarations are not supported",
        )

    def _unsafe_external_entity(self, *_args: object) -> int:
        raise _TmxAbort(
            _CODE_UNSAFE_XML,
            "TMX external entity resolution is disabled",
        )

    def _start_element(self, name: str, attributes: dict[str, str]) -> None:
        self.depth += 1
        if self.depth > self.profile.max_structure_depth:
            raise _TmxAbort(
                _CODE_DEPTH,
                "TMX XML structure exceeds the active depth limit",
            )

        local = _local_name(name)
        if self.prop is not None:
            if self.depth > self.prop.depth:
                assert self.unit is not None
                self.unit.inline_xml = True
            return
        if self.tuv is not None and self.tuv.segment_depth is not None:
            if self.depth > self.tuv.segment_depth:
                assert self.unit is not None
                self.unit.inline_xml = True
                return

        if local == "tu" and self.unit is None:
            self.physical_units += 1
            if self.physical_units > self.profile.max_records:
                raise _TmxAbort(
                    "PARSER.LIMIT.RECORD",
                    "TMX translation-unit count exceeds the active record limit",
                )
            self.unit = _UnitState(
                ordinal=self.physical_units,
                depth=self.depth,
                source_choice=_LocaleChoice(self.source_locale),
                target_choice=_LocaleChoice(self.target_locale),
            )
            return

        unit = self.unit
        if unit is None:
            return
        if local == "prop" and self.prop is None:
            tuv = self.tuv
            is_tu_prop = self.depth == unit.depth + 1 and tuv is None
            is_tuv_prop = tuv is not None and self.depth == tuv.depth + 1
            if is_tu_prop or is_tuv_prop:
                if unit.metadata_entries >= self.profile.max_metadata_entries_per_container:
                    raise _TmxAbort(
                        "PARSER.LIMIT.METADATA",
                        "TMX unit metadata exceeds the active entry limit",
                    )
                raw_locale = attributes.get(_XML_LANG) or attributes.get("lang") or ""
                normalized_locale = _normalize_locale(raw_locale) if raw_locale else ""
                self.prop = _PropState(
                    depth=self.depth,
                    scope="tu" if tuv is None else "tuv",
                    prop_type=attributes.get("type", ""),
                    language=normalized_locale or "",
                )
                return
        if local == "tuv" and self.depth == unit.depth + 1 and self.tuv is None:
            raw_locale = attributes.get(_XML_LANG) or attributes.get("lang")
            self.tuv = _TuvState(
                depth=self.depth,
                locale=_normalize_locale(raw_locale) if raw_locale is not None else None,
            )
            return
        tuv = self.tuv
        if (
            tuv is not None
            and local == "seg"
            and self.depth == tuv.depth + 1
            and not tuv.segment_seen
        ):
            tuv.segment_seen = True
            tuv.segment_depth = self.depth

    def _character_data(self, data: str) -> None:
        prop = self.prop
        unit = self.unit
        if data and prop is not None and unit is not None:
            maximum = self.profile.max_decoded_field_chars
            remaining = maximum + 1 - prop.decoded_chars
            accepted = data[:remaining]
            if accepted:
                prop.text_chunks.append(accepted)
                prop.decoded_chars += len(accepted)
            if len(data) > remaining or prop.decoded_chars > maximum:
                raise _TmxAbort(
                    "PARSER.LIMIT.METADATA",
                    "TMX property exceeds the active decoded-character limit",
                )
            return
        tuv = self.tuv
        if (
            not data
            or tuv is None
            or unit is None
            or tuv.segment_depth is None
        ):
            return
        maximum = self.profile.max_decoded_field_chars
        if tuv.decoded_chars > maximum:
            return
        remaining = maximum + 1 - tuv.decoded_chars
        accepted = data[:remaining]
        if accepted:
            tuv.text_chunks.append(accepted)
            tuv.decoded_chars += len(accepted)
        if len(data) > remaining or tuv.decoded_chars > maximum:
            unit.segment_oversized = True

    def _end_element(self, name: str) -> None:
        local = _local_name(name)
        unit = self.unit
        tuv = self.tuv
        prop = self.prop
        if prop is not None and local == "prop" and prop.depth == self.depth:
            assert unit is not None
            value = "".join(prop.text_chunks)
            metadata_chars = (
                len(prop.scope)
                + len(prop.prop_type)
                + len(prop.language)
                + len(value)
            )
            if (
                unit.metadata_chars + metadata_chars
                > self.profile.max_metadata_decoded_chars_per_container
            ):
                raise _TmxAbort(
                    "PARSER.LIMIT.METADATA",
                    "TMX unit metadata exceeds the active decoded-character limit",
                )
            if prop.scope == "tu":
                unit.props.append(("tu", prop.prop_type, prop.language, value))
            else:
                assert tuv is not None
                tuv.props.append((prop.prop_type, prop.language, value))
            unit.metadata_entries += 1
            unit.metadata_chars += metadata_chars
            self.prop = None
        elif (
            unit is not None
            and tuv is not None
            and local == "seg"
            and tuv.segment_depth == self.depth
        ):
            tuv.text = "".join(tuv.text_chunks).strip()
            tuv.text_chunks.clear()
            tuv.segment_depth = None
        elif (
            unit is not None
            and tuv is not None
            and local == "tuv"
            and tuv.depth == self.depth
        ):
            if (
                tuv.locale is not None
                and tuv.text is not None
                and tuv.text
                and not unit.inline_xml
                and not unit.segment_oversized
            ):
                selected_props = tuple(tuv.props)
                unit.source_choice.observe(tuv.locale, tuv.text, selected_props)
                unit.target_choice.observe(tuv.locale, tuv.text, selected_props)
            self.tuv = None
        elif unit is not None and local == "tu" and unit.depth == self.depth:
            self._finish_unit(unit)
            self.tuv = None
            self.unit = None
        self.depth -= 1

    def _finish_unit(self, unit: _UnitState) -> None:
        if unit.segment_oversized:
            self.events.append(
                _warning(
                    _CODE_SEGMENT_LIMIT,
                    "TMX unit segment exceeds the active decoded-character limit",
                    unit.ordinal,
                )
            )
            return
        if unit.inline_xml:
            self.events.append(
                _warning(
                    _CODE_INLINE,
                    "TMX unit contains unsupported inline XML",
                    unit.ordinal,
                )
            )
            return
        source, source_result, source_props = unit.source_choice.select()
        target, target_result, target_props = unit.target_choice.select()
        if source_result == "ambiguous" or target_result == "ambiguous":
            self.events.append(
                _warning(
                    _CODE_AMBIGUOUS,
                    "TMX unit has an ambiguous base-language fallback",
                    unit.ordinal,
                )
            )
            return
        if source is None or target is None:
            self.events.append(
                _warning(
                    _CODE_MISSING_PAIR,
                    "TMX unit has no usable source and target locale pair",
                    unit.ordinal,
                )
            )
            return
        metadata_props = tuple(unit.props) + tuple(
            ("source_tuv", prop_type, language, value)
            for prop_type, language, value in source_props
        ) + tuple(
            ("target_tuv", prop_type, language, value)
            for prop_type, language, value in target_props
        )
        self.events.append(
            ResourceRecord(
                local_id=f"tu-{unit.ordinal}",
                source=source,
                target=target,
                speaker=RawSpeaker(""),
                format_metadata=(
                    MetadataEntry(
                        key="tmx.props",
                        value=metadata_props,
                    ),
                ) if metadata_props else (),
            )
        )


class TmxLevel1Codec:
    """Stateless TMX reader; all caller choices arrive in ``ReadRequest``."""

    descriptor: CodecDescriptor

    def __init__(self) -> None:
        self.descriptor = TMX_CODEC_DESCRIPTOR

    def iter_raw(
        self,
        source: SnapshotCursorLease,
        request: ReadRequest,
    ) -> Iterator[RawParseEvent]:
        if type(request) is not ReadRequest:
            raise TypeError("request must be exact ReadRequest")
        options = request.tmx_options
        if type(options) is not TmxReadOptions:
            yield _fatal(
                _CODE_INVALID_SELECTION,
                "TMX read requires explicit source and target locale selection",
            )
            return
        source_locale = _normalize_locale(options.source_locale)
        target_locale = _normalize_locale(options.target_locale)
        if (
            source_locale is None
            or target_locale is None
            or source_locale == target_locale
        ):
            yield _fatal(
                _CODE_INVALID_SELECTION,
                "TMX source and target locale selection is invalid",
            )
            return
        if source.byte_count > self.descriptor.limit_profile.max_input_bytes:
            yield _fatal(
                "PARSER.LIMIT.INPUT",
                "TMX snapshot bytes exceed the active input limit",
            )
            return

        grammar = _TmxEventParser(
            profile=self.descriptor.limit_profile,
            source_locale=source_locale,
            target_locale=target_locale,
        )
        decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        fatal_issue: ParseIssue | None = None
        complete = False

        while not complete and fatal_issue is None:
            raw = source.read(_READ_CHUNK_BYTES)
            at_eof = not raw
            try:
                text = decoder.decode(raw, final=at_eof)
            except UnicodeDecodeError:
                fatal_issue = _fatal(
                    _CODE_ENCODING,
                    "TMX input is not valid UTF-8",
                    parser=grammar.parser,
                )
                text = ""
            if fatal_issue is None:
                pieces = _iter_xml_feed_pieces(text)
                for piece in pieces:
                    try:
                        grammar.parser.Parse(piece, False)
                    except _TmxAbort as exc:
                        fatal_issue = _fatal(
                            exc.code,
                            exc.safe_summary,
                            parser=grammar.parser,
                        )
                    except expat.ExpatError:
                        fatal_issue = _fatal(
                            "PARSER.SYNTAX.MALFORMED",
                            "TMX XML is not well formed",
                            parser=grammar.parser,
                        )
                    yield from grammar.drain()
                    if fatal_issue is not None:
                        break
                if at_eof and fatal_issue is None:
                    try:
                        grammar.parser.Parse("", True)
                    except _TmxAbort as exc:
                        fatal_issue = _fatal(
                            exc.code,
                            exc.safe_summary,
                            parser=grammar.parser,
                        )
                    except expat.ExpatError:
                        fatal_issue = _fatal(
                            "PARSER.SYNTAX.MALFORMED",
                            "TMX XML is not well formed",
                            parser=grammar.parser,
                        )
                    yield from grammar.drain()
            if at_eof:
                complete = True

        if fatal_issue is not None:
            yield fatal_issue
            return
        if grammar.physical_units == 0:
            yield _fatal(
                _CODE_NO_UNITS,
                "TMX input contains no translation units",
                parser=grammar.parser,
            )


TMX_CODEC_DESCRIPTOR = CodecDescriptor(
    identity=CodecIdentity("localcat", "tmx-level1", "1"),
    purpose=EffectivePurpose.TRANSLATION_MEMORY,
    format_id=TMX_LEVEL1_V1,
    extensions=(".tmx",),
    mime_types=("application/x-tmx+xml",),
    sniff_prefixes=(b"<?xml", b"<tmx", b"\xef\xbb\xbf<?xml"),
    capabilities=TMX_CAPABILITIES,
    limit_profile=TMX_LIMIT_PROFILE,
    input_consumption_policy=InputConsumptionPolicy.SEALED_BYTES_EOF,
    reader_factory=TmxLevel1Codec,
    canonical_serializer_factory=None,
)


__all__ = (
    "TMX_CAPABILITIES",
    "TMX_CODEC_DESCRIPTOR",
    "TMX_LIMIT_PROFILE",
    "TmxLevel1Codec",
)
