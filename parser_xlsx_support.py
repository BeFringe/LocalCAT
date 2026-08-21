"""Bounded, stdlib-only XLSX ZIP and OPC XML preflight.

This module deliberately stops at container/XML safety.  It neither imports
``openpyxl`` nor interprets workbook, worksheet, cell, formula, macro, link, or
embedded-object semantics.  A termbase codec may open a workbook only after a
successful report from :func:`preflight_xlsx`.
"""

from __future__ import annotations

from dataclasses import dataclass
import codecs
import hashlib
import io
import lzma
import math
import struct
from typing import BinaryIO
import xml.parsers.expat as expat
import zipfile
import zlib


ARCHIVE_INVALID = "PARSER.XLSX.ARCHIVE_INVALID"
SOURCE_NOT_SEEKABLE = "PARSER.XLSX.SOURCE_NOT_SEEKABLE"
SOURCE_RESTORE_FAILED = "PARSER.XLSX.SOURCE_RESTORE_FAILED"
MEMBER_DUPLICATE = "PARSER.XLSX.ARCHIVE_MEMBER_DUPLICATE"
MEMBER_NAME_UNSAFE = "PARSER.XLSX.ARCHIVE_MEMBER_NAME_UNSAFE"
DATA_DESCRIPTOR_UNSUPPORTED = "PARSER.XLSX.ARCHIVE_DATA_DESCRIPTOR_UNSUPPORTED"
MEMBER_LIMIT = "PARSER.LIMIT.ARCHIVE_MEMBER"
EXPANSION_LIMIT = "PARSER.LIMIT.EXPANSION"
COMPRESSION_RATIO_LIMIT = "PARSER.LIMIT.COMPRESSION_RATIO"
STRUCTURE_DEPTH_LIMIT = "PARSER.LIMIT.STRUCTURE_DEPTH"
XML_DECLARATION_FORBIDDEN = "PARSER.TERMBASE.XML_DECLARATION_FORBIDDEN"
ENCODING_FAILED = "PARSER.SOURCE.ENCODING_FAILED"
MALFORMED_XML = "PARSER.SYNTAX.MALFORMED"

_XML_READ_CHUNK_BYTES = 64 * 1024
_LOCAL_FILE_HEADER = struct.Struct("<4s5H3L2H")
_LOCAL_FILE_SIGNATURE = b"PK\x03\x04"
_MAX_DIAGNOSTIC_MEMBER_NAME_CHARS = 160
_SUPPORTED_COMPRESSIONS = frozenset(
    {
        zipfile.ZIP_STORED,
        zipfile.ZIP_DEFLATED,
        zipfile.ZIP_BZIP2,
        zipfile.ZIP_LZMA,
    }
)


@dataclass(frozen=True, slots=True)
class XlsxPreflightLimits:
    """Explicit archive/XML projection supplied by the owning descriptor."""

    max_archive_members: int
    max_expanded_bytes: int
    max_compression_ratio: float
    max_xml_depth: int

    def __post_init__(self) -> None:
        for field_name in (
            "max_archive_members",
            "max_expanded_bytes",
            "max_xml_depth",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        ratio = self.max_compression_ratio
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
            raise ValueError("max_compression_ratio must be a positive finite number")
        if not math.isfinite(float(ratio)) or ratio <= 0:
            raise ValueError("max_compression_ratio must be a positive finite number")

@dataclass(frozen=True, slots=True)
class XlsxMemberReport:
    """Resource observations for one ZIP member; never contains cell data."""

    name: str
    compressed_bytes: int
    file_size: int
    compression_ratio: float
    is_opc_xml: bool


@dataclass(frozen=True, slots=True)
class XlsxPreflightReport:
    """Immutable evidence that archive and all OPC XML preflights succeeded."""

    member_count: int
    total_compressed_bytes: int
    total_expanded_bytes: int
    crc_verified_member_count: int
    xml_member_count: int
    xml_member_names: tuple[str, ...]
    members: tuple[XlsxMemberReport, ...]


class XlsxPreflightError(Exception):
    """Stable structured failure emitted before workbook interpretation."""

    __slots__ = ("code", "member_name", "limit", "observed")

    def __init__(
        self,
        code: str,
        *,
        member_name: str | None = None,
        limit: int | float | None = None,
        observed: int | float | None = None,
    ) -> None:
        self.code = code
        self.member_name = _diagnostic_member_name(member_name)
        self.limit = limit
        self.observed = observed
        fields = [code]
        if self.member_name is not None:
            fields.append(f"member={self.member_name}")
        if limit is not None:
            fields.append(f"limit={limit}")
        if observed is not None:
            fields.append(f"observed={observed}")
        super().__init__("; ".join(fields))


def _diagnostic_member_name(name: str | None) -> str | None:
    if name is None:
        return None
    safe = "".join(
        character if character.isprintable() and character not in {";", "="} else "?"
        for character in name
    )
    if len(safe) <= _MAX_DIAGNOSTIC_MEMBER_NAME_CHARS:
        return safe
    digest = hashlib.sha256(name.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]
    suffix = f"…#{digest}"
    return safe[: _MAX_DIAGNOSTIC_MEMBER_NAME_CHARS - len(suffix)] + suffix


def _fail(
    code: str,
    *,
    member_name: str | None = None,
    limit: int | float | None = None,
    observed: int | float | None = None,
) -> XlsxPreflightError:
    return XlsxPreflightError(
        code,
        member_name=member_name,
        limit=limit,
        observed=observed,
    )


def _is_opc_xml_member(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith(".xml") or lowered.endswith(".rels")


def _member_name_is_safe(info: zipfile.ZipInfo) -> bool:
    name = info.filename
    if not name or info.orig_filename != name:
        return False
    if name.startswith(("/", "\\")) or "\\" in name or "\x00" in name:
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        return False
    if len(name) >= 2 and name[0].isalpha() and name[1] == ":":
        return False
    parts = name.split("/")
    if info.is_dir() and parts[-1] == "":
        parts = parts[:-1]
    return bool(parts) and all(part not in {"", ".", ".."} for part in parts)


def _compression_ratio(info: zipfile.ZipInfo) -> float:
    if info.file_size == 0:
        return 0.0
    if info.compress_size <= 0:
        return math.inf
    return info.file_size / info.compress_size


def _inspect_members(
    archive: zipfile.ZipFile,
    stream: BinaryIO,
    limits: XlsxPreflightLimits,
) -> tuple[tuple[zipfile.ZipInfo, ...], tuple[XlsxMemberReport, ...], int, int]:
    infos = tuple(archive.infolist())
    if not infos:
        raise _fail(ARCHIVE_INVALID)
    if len(infos) > limits.max_archive_members:
        raise _fail(
            MEMBER_LIMIT,
            limit=limits.max_archive_members,
            observed=len(infos),
        )

    names: set[str] = set()
    header_offsets: set[int] = set()
    total_compressed = 0
    total_expanded = 0
    reports: list[XlsxMemberReport] = []
    for info in infos:
        name = info.filename
        if name in names:
            raise _fail(MEMBER_DUPLICATE, member_name=name)
        names.add(name)
        if not _member_name_is_safe(info):
            raise _fail(MEMBER_NAME_UNSAFE, member_name=name)
        if info.flag_bits & 0x08:
            raise _fail(DATA_DESCRIPTOR_UNSUPPORTED, member_name=name)
        if info.header_offset < 0 or info.header_offset in header_offsets:
            raise _fail(ARCHIVE_INVALID, member_name=name)
        header_offsets.add(info.header_offset)
        if info.flag_bits & 0x1 or info.compress_type not in _SUPPORTED_COMPRESSIONS:
            raise _fail(ARCHIVE_INVALID, member_name=name)
        if info.file_size < 0 or info.compress_size < 0:
            raise _fail(ARCHIVE_INVALID, member_name=name)
        if info.is_dir() and (info.file_size != 0 or info.compress_size != 0):
            raise _fail(ARCHIVE_INVALID, member_name=name)

        total_expanded += info.file_size
        if total_expanded > limits.max_expanded_bytes:
            raise _fail(
                EXPANSION_LIMIT,
                member_name=name,
                limit=limits.max_expanded_bytes,
                observed=total_expanded,
            )
        total_compressed += info.compress_size
        ratio = _compression_ratio(info)
        if ratio > limits.max_compression_ratio:
            raise _fail(
                COMPRESSION_RATIO_LIMIT,
                member_name=name,
                limit=limits.max_compression_ratio,
                observed=ratio,
            )
        reports.append(
            XlsxMemberReport(
                name=name,
                compressed_bytes=info.compress_size,
                file_size=info.file_size,
                compression_ratio=ratio,
                is_opc_xml=_is_opc_xml_member(name),
            )
        )
    _validate_local_member_layout(stream, archive, infos)
    return infos, tuple(reports), total_compressed, total_expanded


def _validate_local_member_layout(
    stream: BinaryIO,
    archive: zipfile.ZipFile,
    infos: tuple[zipfile.ZipInfo, ...],
) -> None:
    """Reject central-directory claims that do not match local ZIP layout.

    ``zipfile`` normally checks a local header only when that member is opened.
    OPC preflight intentionally does not decompress binary objects, so this
    bounded header pass makes malformed binary members fail closed as well.
    """

    ordered = sorted(infos, key=lambda item: item.header_offset)
    archive_start = archive.start_dir
    if type(archive_start) is not int or archive_start < 0:
        raise _fail(ARCHIVE_INVALID)
    for index, info in enumerate(ordered):
        boundary = (
            ordered[index + 1].header_offset
            if index + 1 < len(ordered)
            else archive_start
        )
        try:
            stream.seek(info.header_offset)
            raw_header = stream.read(_LOCAL_FILE_HEADER.size)
        except (OSError, ValueError, TypeError) as exc:
            raise _fail(ARCHIVE_INVALID, member_name=info.filename) from exc
        if not isinstance(raw_header, bytes) or len(raw_header) != _LOCAL_FILE_HEADER.size:
            raise _fail(ARCHIVE_INVALID, member_name=info.filename)
        (
            signature,
            _extract_version,
            local_flags,
            local_compression,
            _modified_time,
            _modified_date,
            local_crc,
            local_compressed_size,
            local_file_size,
            filename_length,
            extra_length,
        ) = _LOCAL_FILE_HEADER.unpack(raw_header)
        if (
            signature != _LOCAL_FILE_SIGNATURE
            or local_flags != info.flag_bits
            or local_compression != info.compress_type
        ):
            raise _fail(ARCHIVE_INVALID, member_name=info.filename)
        try:
            raw_name = stream.read(filename_length)
        except (OSError, ValueError, TypeError) as exc:
            raise _fail(ARCHIVE_INVALID, member_name=info.filename) from exc
        if not isinstance(raw_name, bytes) or len(raw_name) != filename_length:
            raise _fail(ARCHIVE_INVALID, member_name=info.filename)
        try:
            local_name = raw_name.decode("utf-8" if local_flags & 0x800 else "cp437")
        except UnicodeDecodeError as exc:
            raise _fail(ARCHIVE_INVALID, member_name=info.filename) from exc
        if local_name != info.orig_filename:
            raise _fail(ARCHIVE_INVALID, member_name=info.filename)
        if local_flags & 0x08:
            if (
                local_crc not in {0, info.CRC}
                or local_compressed_size not in {0, info.compress_size}
                or local_file_size not in {0, info.file_size}
            ):
                raise _fail(ARCHIVE_INVALID, member_name=info.filename)
        elif (
            local_crc != info.CRC
            or local_compressed_size != info.compress_size
            or local_file_size != info.file_size
        ):
            raise _fail(ARCHIVE_INVALID, member_name=info.filename)
        data_start = info.header_offset + _LOCAL_FILE_HEADER.size + filename_length + extra_length
        data_end = data_start + info.compress_size
        if data_start > boundary or data_end > boundary:
            raise _fail(ARCHIVE_INVALID, member_name=info.filename)


def _verify_all_member_payloads(
    archive: zipfile.ZipFile,
    infos: tuple[zipfile.ZipInfo, ...],
    limits: XlsxPreflightLimits,
) -> None:
    """Boundedly decompress every member and verify its size and CRC."""

    total_actual = 0
    for info in infos:
        actual_size = 0
        actual_crc = 0
        try:
            with archive.open(info, "r") as member:
                while True:
                    chunk = member.read(_XML_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    actual_size += len(chunk)
                    total_actual += len(chunk)
                    if (
                        actual_size > info.file_size
                        or total_actual > limits.max_expanded_bytes
                    ):
                        raise _fail(
                            EXPANSION_LIMIT,
                            member_name=info.filename,
                            limit=limits.max_expanded_bytes,
                            observed=total_actual,
                        )
                    actual_crc = zlib.crc32(chunk, actual_crc)
        except XlsxPreflightError:
            raise
        except (
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
            lzma.LZMAError,
            zlib.error,
            NotImplementedError,
            RuntimeError,
            OSError,
            EOFError,
            struct.error,
        ):
            raise _fail(ARCHIVE_INVALID, member_name=info.filename) from None
        if actual_size != info.file_size or actual_crc != info.CRC:
            raise _fail(ARCHIVE_INVALID, member_name=info.filename)


def _preflight_xml_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    limits: XlsxPreflightLimits,
) -> None:
    name = info.filename
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    depth = 0

    def declaration_forbidden(*_args: object) -> None:
        raise _fail(XML_DECLARATION_FORBIDDEN, member_name=name)

    def external_entity_forbidden(*_args: object) -> int:
        raise _fail(XML_DECLARATION_FORBIDDEN, member_name=name)

    def start_element(_element_name: str, _attributes: dict[str, str]) -> None:
        nonlocal depth
        depth += 1
        if depth > limits.max_xml_depth:
            raise _fail(
                STRUCTURE_DEPTH_LIMIT,
                member_name=name,
                limit=limits.max_xml_depth,
                observed=depth,
            )

    def end_element(_element_name: str) -> None:
        nonlocal depth
        depth -= 1

    parser.StartDoctypeDeclHandler = declaration_forbidden
    parser.EntityDeclHandler = declaration_forbidden
    parser.ExternalEntityRefHandler = external_entity_forbidden
    parser.StartElementHandler = start_element
    parser.EndElementHandler = end_element

    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    actual_bytes = 0
    try:
        with archive.open(info, "r") as member:
            while True:
                chunk = member.read(_XML_READ_CHUNK_BYTES)
                if not chunk:
                    break
                actual_bytes += len(chunk)
                if actual_bytes > info.file_size:
                    raise _fail(ARCHIVE_INVALID, member_name=name)
                decoded = decoder.decode(chunk, final=False)
                if decoded:
                    parser.Parse(decoded, False)
        tail = decoder.decode(b"", final=True)
        parser.Parse(tail, True)
    except XlsxPreflightError:
        raise
    except UnicodeDecodeError:
        raise _fail(ENCODING_FAILED, member_name=name) from None
    except expat.ExpatError:
        raise _fail(MALFORMED_XML, member_name=name) from None
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        lzma.LZMAError,
        zlib.error,
        NotImplementedError,
        RuntimeError,
        OSError,
        EOFError,
        struct.error,
    ):
        raise _fail(ARCHIVE_INVALID, member_name=name) from None
    if actual_bytes != info.file_size:
        raise _fail(ARCHIVE_INVALID, member_name=name)


def _seekable_source(
    source: bytes | bytearray | memoryview | BinaryIO,
) -> tuple[BinaryIO, int | None]:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return io.BytesIO(bytes(source)), None
    if not all(callable(getattr(source, method, None)) for method in ("read", "seek", "tell")):
        raise _fail(SOURCE_NOT_SEEKABLE)
    try:
        original_offset = source.tell()
        if type(original_offset) is not int or original_offset < 0:
            raise _fail(SOURCE_NOT_SEEKABLE)
    except XlsxPreflightError:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        raise _fail(SOURCE_NOT_SEEKABLE) from None

    try:
        if callable(getattr(source, "seekable", None)) and not source.seekable():
            raise _fail(SOURCE_NOT_SEEKABLE)
        start_offset = source.seek(0)
        if start_offset != 0 or source.tell() != 0:
            raise _fail(SOURCE_NOT_SEEKABLE)
    except XlsxPreflightError:
        _restore_source_offset_if_needed(source, original_offset)
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        _restore_source_offset_if_needed(source, original_offset)
        raise _fail(SOURCE_NOT_SEEKABLE) from None
    return source, original_offset


def _restore_source_offset(stream: BinaryIO, original_offset: int) -> None:
    try:
        restored_offset = stream.seek(original_offset)
        if restored_offset != original_offset or stream.tell() != original_offset:
            raise _fail(SOURCE_RESTORE_FAILED)
    except XlsxPreflightError:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        raise _fail(SOURCE_RESTORE_FAILED) from None


def _restore_source_offset_if_needed(stream: BinaryIO, original_offset: int) -> None:
    try:
        current_offset = stream.tell()
    except (OSError, ValueError, TypeError, AttributeError):
        _restore_source_offset(stream, original_offset)
        return
    if current_offset != original_offset:
        _restore_source_offset(stream, original_offset)


def preflight_xlsx(
    source: bytes | bytearray | memoryview | BinaryIO,
    limits: XlsxPreflightLimits,
) -> XlsxPreflightReport:
    """Validate XLSX archive resources and every OPC XML/rels member.

    ``source`` must be sealed bytes or a readable, seekable binary cursor.  A
    caller-owned cursor is restored to its entry offset, including on failure.
    No workbook library is imported and no member is interpreted as a cell,
    formula, macro, external link, or embedded object.
    """

    if not isinstance(limits, XlsxPreflightLimits):
        raise TypeError("limits must be XlsxPreflightLimits")
    stream, original_offset = _seekable_source(source)
    try:
        try:
            with zipfile.ZipFile(stream, "r") as archive:
                infos, reports, total_compressed, total_expanded = _inspect_members(
                    archive, stream, limits
                )
                _verify_all_member_payloads(archive, infos, limits)
                xml_infos = tuple(info for info in infos if _is_opc_xml_member(info.filename))
                for info in xml_infos:
                    _preflight_xml_member(archive, info, limits)
        except XlsxPreflightError:
            raise
        except (
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
            lzma.LZMAError,
            zlib.error,
            NotImplementedError,
            RuntimeError,
            OSError,
            EOFError,
            struct.error,
        ):
            raise _fail(ARCHIVE_INVALID) from None
        return XlsxPreflightReport(
            member_count=len(infos),
            total_compressed_bytes=total_compressed,
            total_expanded_bytes=total_expanded,
            crc_verified_member_count=len(infos),
            xml_member_count=len(xml_infos),
            xml_member_names=tuple(info.filename for info in xml_infos),
            members=reports,
        )
    finally:
        if original_offset is not None:
            _restore_source_offset(stream, original_offset)


__all__ = [
    "XlsxMemberReport",
    "XlsxPreflightError",
    "XlsxPreflightLimits",
    "XlsxPreflightReport",
    "preflight_xlsx",
]
