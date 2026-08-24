"""Deterministic LocalCAT TMX Level 1 context profile and Parser seam."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from xml.parsers import expat
from xml.sax.saxutils import escape, quoteattr

from parser_composition import create_parser_application_surface
from parser_contracts import (
    EffectivePurpose,
    ReadRequest,
    ResourceRecord,
    SelectionFailure,
    SelectionRequest,
    SourceReference,
    TMX_LEVEL1_V1,
    TmxReadOptions,
)
from tmx_context_contracts import (
    TMX_CONTEXT_PROFILE_ID,
    TmxContextError,
    TmxEffectiveLocales,
    TmxExportUnit,
    TmxLossCount,
    TmxLossDisposition,
    TmxLossReport,
    TmxOrderedProp,
    TmxPayloadProof,
    TmxPreparedPayload,
    TmxPropScope,
    TmxSafeIssue,
    TmxScopeBinding,
)


_ISSUE_LIMIT = 32
_MAX_INPUT_BYTES = 100 * 1024 * 1024
_MAX_FIELD_CHARS = 1_000_000
_MAX_RECORDS = 1_000_000
_MAX_PROPS_PER_UNIT = 256
_MAX_METADATA_CHARS_PER_UNIT = 1024 * 1024
_MAX_METADATA_CHARS_TOTAL = 16 * 1024 * 1024
_PROP_SCOPE_RANK = {
    TmxPropScope.TU: 0,
    TmxPropScope.SOURCE_TUV: 1,
    TmxPropScope.TARGET_TUV: 2,
}


@dataclass(frozen=True, slots=True)
class _ParserColdFacts:
    payload_digest: str
    content_digest: str
    record_count: int
    prop_count: int
    warning_occurrences: tuple[tuple[str, TmxLossDisposition, int | None], ...]


class _LocaleInventoryAbort(Exception):
    pass


def _read_bounded_regular(path: Path) -> bytes:
    try:
        observed = path.lstat()
    except OSError as exc:
        raise TmxContextError("TMX.COLD_READ_FAILED", "staged TMX could not be inspected") from exc
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise TmxContextError("TMX.COLD_SOURCE_UNSAFE", "staged TMX must be a single-link regular file")
    if observed.st_size > _MAX_INPUT_BYTES:
        raise TmxContextError("TMX.PAYLOAD_LIMIT", "staged TMX exceeds the active profile limit")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise TmxContextError("TMX.COLD_READ_FAILED", "staged TMX could not be opened safely") from exc
    try:
        pinned = os.fstat(fd)
        if (pinned.st_dev, pinned.st_ino) != (observed.st_dev, observed.st_ino):
            raise TmxContextError("TMX.COLD_SOURCE_STALE", "staged TMX changed during inspection")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_INPUT_BYTES:
                raise TmxContextError("TMX.PAYLOAD_LIMIT", "staged TMX exceeds the active profile limit")
            chunks.append(chunk)
        after = os.fstat(fd)
        if (after.st_size, after.st_mtime_ns) != (pinned.st_size, pinned.st_mtime_ns):
            raise TmxContextError("TMX.COLD_SOURCE_STALE", "staged TMX changed while being read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _inventory_effective_locales(data: bytes) -> TmxEffectiveLocales:
    """Inspect only profile/header locale attributes; never expose body text."""

    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise TmxContextError("TMX.COLD_UNSAFE_XML", "TMX locale inventory rejects DTD and ENTITY")
    parser = expat.ParserCreate(namespace_separator="}")
    depth = 0
    root_seen = False
    body_seen = False
    header_count = 0
    source_locale: str | None = None
    tuv_locales: list[str] = []

    def local_name(name: str) -> str:
        return name.rsplit("}", 1)[-1].casefold()

    def abort(*_values) -> None:
        del _values
        raise _LocaleInventoryAbort()

    def start(name: str, attributes: dict[str, str]) -> None:
        nonlocal depth, root_seen, body_seen, header_count, source_locale
        depth += 1
        if depth > 64:
            raise _LocaleInventoryAbort()
        local = local_name(name)
        if depth == 1:
            if local != "tmx" or attributes.get("version") != "1.4":
                raise _LocaleInventoryAbort()
            root_seen = True
        elif local == "header":
            header_count += 1
            if header_count != 1:
                raise _LocaleInventoryAbort()
            required = {
                "creationtool": "LocalCAT",
                "segtype": "sentence",
                "adminlang": "en",
                "datatype": "PlainText",
            }
            if any(attributes.get(key) != value for key, value in required.items()):
                raise _LocaleInventoryAbort()
            source_locale = attributes.get("srclang")
        elif local == "body":
            body_seen = True
        elif local == "tuv":
            raw_locale = attributes.get("http://www.w3.org/XML/1998/namespace}lang")
            if raw_locale is None:
                raw_locale = attributes.get("lang")
            if raw_locale is None or not raw_locale:
                raise _LocaleInventoryAbort()
            if raw_locale not in tuv_locales:
                tuv_locales.append(raw_locale)
                if len(tuv_locales) > 2:
                    raise _LocaleInventoryAbort()

    def end(_name: str) -> None:
        nonlocal depth
        depth -= 1

    parser.StartDoctypeDeclHandler = abort
    parser.EntityDeclHandler = abort
    parser.ExternalEntityRefHandler = abort
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(data, True)
    except (_LocaleInventoryAbort, expat.ExpatError, UnicodeError) as exc:
        raise TmxContextError("TMX.COLD_PROFILE_INVALID", "TMX profile locale inventory is invalid") from exc
    if not root_seen or not body_seen or header_count != 1 or source_locale is None:
        raise TmxContextError("TMX.COLD_PROFILE_INVALID", "TMX profile header/body is incomplete")
    if len(tuv_locales) != 2 or source_locale not in tuv_locales:
        raise TmxContextError("TMX.COLD_LOCALE_INVALID", "TMX profile does not contain one exact locale pair")
    target_locale = tuv_locales[1] if tuv_locales[0] == source_locale else tuv_locales[0]
    try:
        return TmxEffectiveLocales(source_locale, target_locale)
    except (TypeError, ValueError) as exc:
        raise TmxContextError("TMX.COLD_LOCALE_INVALID", "TMX effective locale pair is invalid") from exc


def _xml_text_representable(value: str, *, segment: bool) -> bool:
    if "\r" in value:
        return False
    for char in value:
        codepoint = ord(char)
        if not (
            codepoint in (0x9, 0xA)
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        ):
            return False
    # The Parser Level 1 reader deliberately trims segments.  Refuse values it
    # could not cold-reopen byte-for-semantic-byte.
    return not segment or value == value.strip()


def _normalized_locale(value: str) -> str:
    return value.replace("_", "-").lower()


def _hash_facts(facts: object) -> str:
    encoded = json.dumps(
        facts,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _update_fact_digest(digest, fact: object) -> None:
    encoded = json.dumps(
        fact,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _hash_fact_sequence(facts) -> str:
    digest = hashlib.sha256()
    for fact in facts:
        _update_fact_digest(digest, fact)
    return digest.hexdigest()


def _scope_text(prop: TmxOrderedProp, locales: TmxEffectiveLocales) -> str:
    del locales
    return prop.scope.value


def _parser_facts(
    source: str,
    target: str,
    props: tuple[TmxOrderedProp, ...],
    locales: TmxEffectiveLocales,
) -> tuple[object, ...]:
    return (
        source,
        target,
        tuple(
            (
                _scope_text(prop, locales),
                prop.type,
                _normalized_locale(prop.xml_lang) if prop.xml_lang else "",
                prop.value,
            )
            for prop in props
        ),
    )


def _localcat_props(unit: TmxExportUnit) -> tuple[TmxOrderedProp, ...]:
    props: list[TmxOrderedProp] = []
    for prop_type, value in (
        ("x-localcat-speaker", unit.speaker),
        ("x-localcat-context-prev", unit.context_prev),
        ("x-localcat-context-next", unit.context_next),
        ("x-localcat-file-source", unit.file_source),
    ):
        if value is not None:
            props.append(TmxOrderedProp(prop_type, value))
    props.append(TmxOrderedProp("x-localcat-confirmed", "true" if unit.confirmed else "false"))
    if unit.status is not None:
        props.append(TmxOrderedProp("x-localcat-status", unit.status))
    for provenance in unit.provenance:
        props.append(
            TmxOrderedProp(
                "x-localcat-provenance",
                json.dumps(
                    [provenance.key, provenance.value],
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ),
            )
        )
    props.extend(unit.imported_props)
    return tuple(props)


def _tu_id(binding: TmxScopeBinding, unit: TmxExportUnit) -> str:
    facts = (
        TMX_CONTEXT_PROFILE_ID,
        binding.scope_kind.value,
        binding.scope_id,
        unit.unit_identity,
    )
    return f"localcat-{_hash_facts(facts)}"


def _prop_xml(prop: TmxOrderedProp, *, indent: str) -> str:
    attributes = f" type={quoteattr(prop.type)}"
    if prop.xml_lang is not None:
        attributes += f" xml:lang={quoteattr(prop.xml_lang)}"
    return f"{indent}<prop{attributes}>{escape(prop.value)}</prop>"


def _counted_report(
    included: int,
    excluded: int,
    occurrences: list[tuple[str, TmxLossDisposition, int | None]],
) -> TmxLossReport:
    counter = Counter((code, disposition) for code, disposition, _ordinal in occurrences)
    counts = tuple(
        TmxLossCount(code, disposition, count)
        for (code, disposition), count in sorted(
            counter.items(), key=lambda item: (item[0][0], item[0][1].value)
        )
    )
    issues = tuple(
        TmxSafeIssue(code, disposition, ordinal)
        for code, disposition, ordinal in occurrences[:_ISSUE_LIMIT]
    )
    return TmxLossReport(
        included_count=included,
        excluded_count=excluded,
        warning_count=sum(
            count.count for count in counts
            if count.disposition is TmxLossDisposition.WARNING
        ),
        blocking_count=sum(
            count.count for count in counts
            if count.disposition is TmxLossDisposition.BLOCKING
        ),
        counts=counts,
        issues=issues,
        issues_truncated=len(occurrences) > _ISSUE_LIMIT,
    )


def prepare_tmx_payload(
    binding: TmxScopeBinding,
    effective_locales: TmxEffectiveLocales,
    units: tuple[TmxExportUnit, ...],
) -> TmxPreparedPayload:
    """Apply loss policy and return deterministic bytes plus body-safe proof."""

    if type(binding) is not TmxScopeBinding:
        raise TypeError("binding must be exact TmxScopeBinding")
    if type(effective_locales) is not TmxEffectiveLocales:
        raise TypeError("effective_locales must be exact TmxEffectiveLocales")
    if type(units) is not tuple:
        raise TypeError("units must be an exact tuple")
    for unit in units:
        if type(unit) is not TmxExportUnit:
            raise TypeError("units entries must be exact TmxExportUnit")
    if binding.unit_count != len(units):
        raise TmxContextError("TMX.SCOPE_BINDING_STALE", "scope unit count no longer matches")
    if binding.attached_count != sum(unit.attached for unit in units):
        raise TmxContextError("TMX.SCOPE_BINDING_STALE", "scope attachment count no longer matches")
    identities = tuple(unit.unit_identity for unit in units)
    if len(units) > _MAX_RECORDS:
        raise TmxContextError("TMX.RECORD_LIMIT", "TMX scope exceeds the profile record limit")
    if len(set(identities)) != len(identities):
        raise TmxContextError("TMX.SCOPE_IDENTITY_DUPLICATE", "scope unit identities are not unique")

    occurrences: list[tuple[str, TmxLossDisposition, int | None]] = []
    included: list[tuple[TmxExportUnit, tuple[TmxOrderedProp, ...]]] = []
    excluded = 0
    for ordinal, unit in enumerate(units, 1):
        if not unit.attached:
            excluded += 1
            occurrences.append(("detached_member", TmxLossDisposition.EXCLUDED, ordinal))
            continue
        if not unit.target.strip():
            excluded += 1
            occurrences.append(("empty_target", TmxLossDisposition.EXCLUDED, ordinal))
            continue
        props = _localcat_props(unit)
        if unit.has_inline_xml:
            occurrences.append(("inline_xml", TmxLossDisposition.BLOCKING, ordinal))
        if not unit.source:
            occurrences.append(("empty_source", TmxLossDisposition.BLOCKING, ordinal))
        for text in (unit.source, unit.target):
            if not _xml_text_representable(text, segment=True):
                occurrences.append(("segment_unrepresentable", TmxLossDisposition.BLOCKING, ordinal))
                break
        previous_rank = -1
        if len(props) > _MAX_PROPS_PER_UNIT:
            occurrences.append(("metadata_limit", TmxLossDisposition.BLOCKING, ordinal))
        metadata_chars = 0
        for prop in props:
            rank = _PROP_SCOPE_RANK[prop.scope]
            if rank < previous_rank:
                occurrences.append(("prop_scope_order", TmxLossDisposition.BLOCKING, ordinal))
                break
            previous_rank = rank
            fields = (prop.type, prop.value) + ((prop.xml_lang,) if prop.xml_lang is not None else ())
            if any(not _xml_text_representable(value, segment=False) for value in fields):
                occurrences.append(("metadata_unrepresentable", TmxLossDisposition.BLOCKING, ordinal))
                break
            if prop.xml_lang is not None and (
                prop.xml_lang != prop.xml_lang.strip()
                or not all(piece and piece.isalnum() for piece in prop.xml_lang.replace("_", "-").split("-"))
            ):
                occurrences.append(("metadata_unrepresentable", TmxLossDisposition.BLOCKING, ordinal))
                break
            metadata_chars += len(prop.scope.value) + sum(len(value) for value in fields)
        if metadata_chars > _MAX_METADATA_CHARS_PER_UNIT:
            occurrences.append(("metadata_limit", TmxLossDisposition.BLOCKING, ordinal))
        if len(unit.source) > _MAX_FIELD_CHARS or len(unit.target) > _MAX_FIELD_CHARS:
            occurrences.append(("segment_limit", TmxLossDisposition.BLOCKING, ordinal))
        if not unit.confirmed:
            occurrences.append(("unconfirmed_target", TmxLossDisposition.WARNING, ordinal))
        if unit.source == unit.target:
            occurrences.append(("source_equals_target", TmxLossDisposition.WARNING, ordinal))
        included.append((unit, props))

    report = _counted_report(len(included), excluded, occurrences)
    if report.blocking_count:
        raise TmxContextError(
            "TMX.BLOCKING_LOSS",
            "TMX payload contains facts the active profile cannot represent",
            loss_report=report,
        )
    if not included:
        raise TmxContextError(
            "TMX.NO_INCLUDED_UNITS",
            "TMX payload has no included translation units",
            loss_report=report,
        )

    source_locale = effective_locales.source_locale
    target_locale = effective_locales.target_locale
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<tmx version="1.4">',
        (
            '  <header creationtool="LocalCAT" creationtoolversion="1" '
            f'segtype="sentence" adminlang="en" srclang={quoteattr(source_locale)} '
            'datatype="PlainText"/>'
        ),
        "  <body>",
    ]
    parser_units: list[object] = []
    prop_count = 0
    metadata_chars_total = 0
    for unit, props in included:
        lines.append(f"    <tu tuid={quoteattr(_tu_id(binding, unit))}>")
        for prop in props:
            if prop.scope is TmxPropScope.TU:
                lines.append(_prop_xml(prop, indent="      "))
        lines.append(f"      <tuv xml:lang={quoteattr(source_locale)}>")
        for prop in props:
            if prop.scope is TmxPropScope.SOURCE_TUV:
                lines.append(_prop_xml(prop, indent="        "))
        lines.append(f"        <seg>{escape(unit.source)}</seg>")
        lines.append("      </tuv>")
        lines.append(f"      <tuv xml:lang={quoteattr(target_locale)}>")
        for prop in props:
            if prop.scope is TmxPropScope.TARGET_TUV:
                lines.append(_prop_xml(prop, indent="        "))
        lines.append(f"        <seg>{escape(unit.target)}</seg>")
        lines.append("      </tuv>")
        lines.append("    </tu>")
        parser_units.append(_parser_facts(unit.source, unit.target, props, effective_locales))
        prop_count += len(props)
        metadata_chars_total += sum(
            len(prop.scope.value)
            + len(prop.type)
            + len(prop.value)
            + (len(prop.xml_lang) if prop.xml_lang is not None else 0)
            for prop in props
        )
    lines.extend(("  </body>", "</tmx>"))
    data = ("\n".join(lines) + "\n").encode("utf-8")
    if len(data) > _MAX_INPUT_BYTES or metadata_chars_total > _MAX_METADATA_CHARS_TOTAL:
        limit_occurrences = occurrences + [
            ("payload_limit", TmxLossDisposition.BLOCKING, None)
        ]
        raise TmxContextError(
            "TMX.PAYLOAD_LIMIT",
            "TMX payload exceeds the active profile limit",
            loss_report=_counted_report(len(included), excluded, limit_occurrences),
        )
    proof = TmxPayloadProof(
        profile_id=TMX_CONTEXT_PROFILE_ID,
        effective_locales=effective_locales,
        payload_digest=hashlib.sha256(data).hexdigest(),
        parser_content_digest=_hash_fact_sequence(parser_units),
        included_count=len(included),
        prop_count=prop_count,
        loss_report=report,
    )
    return TmxPreparedPayload(
        data=data,
        proof=proof,
        scope_kind=binding.scope_kind,
        scope_id=binding.scope_id,
        binding_digest=binding.binding_digest,
    )


def _collect_parser_cold_facts(path: Path, locales: TmxEffectiveLocales) -> _ParserColdFacts:
    selection = SelectionRequest(
        purpose=EffectivePurpose.TRANSLATION_MEMORY,
        format_id=TMX_LEVEL1_V1,
    )
    request = ReadRequest(
        purpose=EffectivePurpose.TRANSLATION_MEMORY,
        format_id=TMX_LEVEL1_V1,
        tmx_options=TmxReadOptions(locales.source_locale, locales.target_locale),
    )
    surface = create_parser_application_surface()
    try:
        opened = surface.open_input(
            SourceReference(str(path.parent), str(path), path.name),
            selection,
            request,
        )
        if type(opened) is SelectionFailure:
            raise TmxContextError("TMX.COLD_PARSER_UNAVAILABLE", "Parser TMX profile is unavailable")
        with opened:
            session = opened.stream()
            record_count = 0
            prop_count = 0
            content_digest = hashlib.sha256()
            warning_occurrences: list[tuple[str, TmxLossDisposition, int | None]] = []
            try:
                for event in session:
                    if type(event) is ResourceRecord:
                        record_count += 1
                        props: tuple[object, ...] = ()
                        for metadata in event.format_metadata:
                            if metadata.key == "tmx.props":
                                if type(metadata.value) is not tuple:
                                    raise TmxContextError(
                                        "TMX.COLD_METADATA_MISMATCH",
                                        "Parser metadata shape is invalid",
                                    )
                                props = metadata.value
                                prop_count += len(props)
                        confirmed: str | None = None
                        for raw_prop in props:
                            if (
                                type(raw_prop) is not tuple
                                or len(raw_prop) != 4
                                or any(type(part) is not str for part in raw_prop)
                            ):
                                raise TmxContextError(
                                    "TMX.COLD_METADATA_MISMATCH",
                                    "Parser metadata entry shape is invalid",
                                )
                            scope, prop_type, _language, value = raw_prop
                            if (
                                confirmed is None
                                and scope == "tu"
                                and prop_type.casefold() == "x-localcat-confirmed"
                            ):
                                confirmed = value.casefold()
                        if confirmed not in {"true", "false"}:
                            raise TmxContextError(
                                "TMX.COLD_PROFILE_INVALID",
                                "TMX profile unit lacks one canonical confirmation fact",
                            )
                        if confirmed == "false":
                            warning_occurrences.append(
                                ("unconfirmed_target", TmxLossDisposition.WARNING, record_count)
                            )
                        if event.source == event.target:
                            warning_occurrences.append(
                                ("source_equals_target", TmxLossDisposition.WARNING, record_count)
                            )
                        _update_fact_digest(
                            content_digest,
                            (event.source, event.target, props),
                        )
                terminal = session.verified_terminal()
            finally:
                session.close()
    except TmxContextError:
        raise
    except Exception as exc:
        raise TmxContextError("TMX.COLD_VALIDATION_FAILED", "Parser rejected the staged TMX") from exc

    if terminal.warning_counts:
        raise TmxContextError("TMX.COLD_PARSER_WARNING", "Parser reported a lossy staged TMX warning")
    if terminal.record_count != record_count:
        raise TmxContextError("TMX.COLD_COUNT_MISMATCH", "Parser TU terminal count is inconsistent")
    return _ParserColdFacts(
        payload_digest=terminal.source.content_sha256,
        content_digest=content_digest.hexdigest(),
        record_count=record_count,
        prop_count=prop_count,
        warning_occurrences=tuple(warning_occurrences),
    )


def cold_validate_tmx_file(path: Path, proof: TmxPayloadProof) -> None:
    """Cold-open one staged file through the Parser application surface.

    Exact byte digest proves the planned deterministic metadata/loss facts; the
    independent Parser pass proves safe grammar, selected locale pair, ordered
    source/target bodies, ordered duplicate props, and terminal TU count.
    """

    if not isinstance(path, Path) or not path.is_absolute():
        raise TypeError("cold validation path must be an absolute Path")
    if type(proof) is not TmxPayloadProof:
        raise TypeError("proof must be exact TmxPayloadProof")
    data = _read_bounded_regular(path)
    if hashlib.sha256(data).hexdigest() != proof.payload_digest:
        raise TmxContextError("TMX.COLD_DIGEST_MISMATCH", "staged TMX digest does not match preview")
    facts = _collect_parser_cold_facts(path, proof.effective_locales)
    if facts.payload_digest != proof.payload_digest:
        raise TmxContextError("TMX.COLD_DIGEST_MISMATCH", "Parser snapshot digest does not match preview")
    if facts.record_count != proof.included_count:
        raise TmxContextError("TMX.COLD_COUNT_MISMATCH", "Parser TU count does not match preview")
    if facts.prop_count != proof.prop_count or facts.content_digest != proof.parser_content_digest:
        raise TmxContextError("TMX.COLD_METADATA_MISMATCH", "Parser metadata facts do not match preview")


def inspect_tmx_payload(path: Path) -> TmxPayloadProof:
    """Cold-validate a profile payload without caller-supplied locale state.

    A bounded Expat pass inventories only the fixed profile/header and TUV locale
    attributes.  Body records and ordered props are consumed exclusively through
    the Parser application surface.
    """

    if not isinstance(path, Path) or not path.is_absolute():
        raise TypeError("TMX inspection path must be an absolute Path")
    data = _read_bounded_regular(path)
    payload_digest = hashlib.sha256(data).hexdigest()
    locales = _inventory_effective_locales(data)
    facts = _collect_parser_cold_facts(path, locales)
    if facts.payload_digest != payload_digest:
        raise TmxContextError("TMX.COLD_DIGEST_MISMATCH", "Parser snapshot changed after locale inventory")
    report = _counted_report(
        facts.record_count,
        0,
        list(facts.warning_occurrences),
    )
    return TmxPayloadProof(
        profile_id=TMX_CONTEXT_PROFILE_ID,
        effective_locales=locales,
        payload_digest=payload_digest,
        parser_content_digest=facts.content_digest,
        included_count=facts.record_count,
        prop_count=facts.prop_count,
        loss_report=report,
    )


class ParserTmxColdValidator:
    """Injectable validation seam used by direct and package carriers."""

    __slots__ = ()

    def __call__(self, path: Path, proof: TmxPayloadProof) -> None:
        cold_validate_tmx_file(path, proof)
