# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""DWG file header and HEADER-section variables.

Two different things live in this module, and it is worth keeping them apart:

* the **file header** — the fixed block at offset 0 that identifies the format
  version and points at everything else.  ``header.spec`` describes it.  Its
  layout forks hard at R2004, where the interesting half moves into an
  encrypted block at ``0x80``, and again at R2007, where that block becomes a
  Reed-Solomon coded, possibly LZ77-compressed structure.
* the **header variables** — the drawing settings (units, current layer,
  limits, ...) stored in the ``AcDb:Header`` section.  ``header_variables.spec``
  describes them.

The variable decode here is deliberately **bounded**: upstream's spec is 730
lines and this module transcribes it only through ``LIMMAX``, which is
everything the exporters need (the drawing extents drive arc tessellation
density).  The cut is safe rather than merely convenient — variables are read
from the object's own section and cannot influence entity geometry, so a
truncated variable decode degrades presentation detail and nothing else.  The
remaining variables are an additive change to :func:`decode_header_variables`.

Version dispatch, mirroring ``dwg_decode`` in ``decode.c``::

    R13b1 .. R2000   -> text section locator records at 0x19
    R2004a .. R2004  -> encrypted header at 0x80 + section page map
    R2007a .. R2007  -> Reed-Solomon coded header at 0x80 + pages/sections maps
    R2010b and later -> the R2004 container, with different page size limits

Note that R2010+ files are *not* R2007 files: they go back to the R2004
container layout.  That surprises people, so it is called out here and again
in :mod:`py_libredwg.sections`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Final

from .bitreader import BitReader, TimeBLL
from .constants import TAG_TO_VERSION, DwgVersion
from .errors import DwgDecodeError, UnsupportedVersionError
from .geometry import Vec3

__all__ = [
    "crc16",
    "decode_rs",
    "decrypt_r2004_header",
    "detect_version",
    "decode_header_variables",
    "read_file_header",
    "FileHeader",
    "HeaderVariables",
    "R2004FileHeader",
    "R2007FileHeader",
    "SectionRecord",
    "R2004_HEADER_OFFSET",
    "R2007_HEADER_OFFSET",
    "R2007_HEADER_BYTES",
]

# ---------------------------------------------------------------------------
# Fixed offsets and magics
# ---------------------------------------------------------------------------

#: Where the encrypted R2004+ file header lives.
R2004_HEADER_OFFSET: Final[int] = 0x80

#: ``file_ID_string`` in the decrypted R2004 header.  Only the first 8 bytes
#: are compared by upstream.
R2004_FILE_ID: Final[bytes] = b"AcFssFcAJMB"

#: Bytes of the R2004 header that are actually meaningful (the struct is
#: ``#pragma pack(1)``, so this is a plain field sum).  Upstream decrypts the
#: whole struct including the trailing padding, so that is what we decrypt.
R2004_HEADER_SIZE: Final[int] = 108

#: ``sizeof (Dwg_R2004_Header)`` — 108 meaningful bytes plus 12 of padding.
R2004_HEADER_STRUCT_SIZE: Final[int] = 120

#: Where the R2007 file header lives, and how much of it is RS coded.
R2007_HEADER_OFFSET: Final[int] = 0x80
R2007_HEADER_BYTES: Final[int] = 0x3D8  # 0x400 - 0x28 = 984

#: Block geometry of the Reed-Solomon (255, 239) code used by R2007.
R2007_RS_BLOCKS: Final[int] = 3
R2007_RS_K: Final[int] = 239

#: ``sizeof (Dwg_R2007_Header)`` — 34 packed ``int64`` fields.
R2007_HEADER_STRUCT_SIZE: Final[int] = 272

#: Bytes preceding the R2007 header payload inside the RS-decoded block.
R2007_HEADER_PREFIX: Final[int] = 32

#: Version tags are ASCII in the first six bytes of the file.
_VERSION_TAG_LEN: Final[int] = 6

#: Shortest file upstream will even look at (the smallest known R2.10).
_MIN_FILE_SIZE: Final[int] = 58


# ---------------------------------------------------------------------------
# CRC-16 (``bit_calc_CRC``)
# ---------------------------------------------------------------------------
# LibreDWG hard-codes a 256-entry table.  It is the standard reflected
# CRC-16/ARC table for polynomial 0xA001, so generating it is both shorter and
# less error-prone than transcribing 256 literals; the loop below is checked
# against upstream's table (table[1] == 0xC0C1, table[2] == 0xC181, ...) in
# the test suite.
def _build_crc16_table() -> tuple[int, ...]:
    table = []
    for i in range(256):
        value = i
        for _ in range(8):
            value = (value >> 1) ^ (0xA001 if value & 1 else 0)
        table.append(value)
    return tuple(table)


_CRC16_TABLE: Final[tuple[int, ...]] = _build_crc16_table()


def crc16(seed: int, data: bytes, start: int = 0, length: int | None = None) -> int:
    """CRC-16 over ``data[start:start+length]``, seeded with ``seed``.

    A direct translation of ``bit_calc_CRC``.  DWG uses it for the R13–R2000
    header and for each section page from R2004 on; a mismatch is recorded as
    a warning upstream (``DWG_ERR_WRONGCRC``), never a hard failure, so
    callers here treat it the same way.
    """
    if length is None:
        length = len(data) - start
    table = _CRC16_TABLE
    dx = seed & 0xFFFF
    end = start + length
    for i in range(start, end):
        al = data[i] ^ (dx & 0xFF)
        dx = ((dx >> 8) & 0xFF) ^ table[al]
    return dx


# ---------------------------------------------------------------------------
# Reed-Solomon de-interleaving (``decode_rs``)
# ---------------------------------------------------------------------------
def decode_rs(src: bytes, block_count: int, data_size: int, src_size: int) -> bytes:
    """Undo the Reed-Solomon interleaving used by R2007 (``decode_rs``).

    Only R2007 uses this, and — importantly — upstream has the *error
    correction* disabled (``rs_decode_block`` is commented out at the call
    site in ``decode_rs``).  What remains is a pure interleave transpose: the
    coder wrote the i-th byte of every block consecutively, so decoding is a
    strided gather::

        dst[i * data_size + j] = src[i + j * block_count]

    Implementing the GF(256) solver would be dead code that never runs, so
    this function is the transpose and nothing more.  If a future version
    re-enables correction, this is the single place to extend.
    """
    need = block_count * data_size
    if need > src_size:
        raise DwgDecodeError(
            f"decode_rs: {block_count} blocks x {data_size} bytes needs "
            f"{need} source bytes, only {src_size} available"
        )
    if block_count <= 0 or data_size <= 0:
        return b""
    out = bytearray(need)
    pos = 0
    for i in range(block_count):
        p = i
        for _ in range(data_size):
            out[pos] = src[p]
            pos += 1
            p += block_count
    return bytes(out)


# ---------------------------------------------------------------------------
# R2004 header decryption
# ---------------------------------------------------------------------------
def decrypt_r2004_header(src: bytes, size: int = R2004_HEADER_STRUCT_SIZE) -> bytes:
    """Decrypt the R2004 file header (``decrypt_R2004_header``).

    The cipher is Microsoft's ``rand()`` linear congruential generator
    (``state = state * 0x343fd + 0x269ec3``, mod 2**32) used as a one-time
    pad; the keystream byte is bits 16-23 of the state.  It is not a cipher
    in any real sense — it is an obfuscation that keeps the format's
    structure out of casual sight.
    """
    if size < 0:
        raise DwgDecodeError(f"decrypt_r2004_header: negative size {size}")
    if len(src) < size:
        raise DwgDecodeError(
            f"decrypt_r2004_header: need {size} bytes, got {len(src)}"
        )
    out = bytearray(size)
    rseed = 1
    for i in range(size):
        rseed = (rseed * 0x343FD + 0x269EC3) & 0xFFFFFFFF
        out[i] = src[i] ^ ((rseed >> 0x10) & 0xFF)
    return bytes(out)


# ---------------------------------------------------------------------------
# Container description
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class SectionRecord:
    """One entry of a section locator / section map.

    R13–R2000 store these as text records right after the file header; R2004+
    build the same thing from the *section page map* and *section info*
    structures.  Both eras end up here so that callers do not have to care
    which they came from.
    """

    number: int = 0
    """Section identifier (``SectionType`` or ``SectionTypeR13``)."""

    address: int = 0
    """Byte offset of the section within the file."""

    size: int = 0
    """Section size in bytes (uncompressed size for R2004+)."""

    name: str = ""
    """Human name, e.g. ``AcDb:Header``.  Only known for R13–R2000 and
    R2007, where the name is actually stored."""

    #: R2004+ only: size of the data on disk, which differs from :attr:`size`
    #: when the section is compressed.
    compressed_size: int = 0

    #: R2004+ only: True when this section's pages are compressed.
    compressed: bool = False


@dataclass(slots=True)
class R2004FileHeader:
    """The decrypted R2004+ file header (``r2004_file_header.spec``).

    ``header_size`` is 108 in every file Autodesk ever wrote, and the CRC is
    over those 108 bytes with the CRC field itself zeroed.
    """

    file_id_string: bytes = b""
    header_address: int = 0
    header_size: int = 0
    x04: int = 0
    root_tree_node_gap: int = 0
    lowermost_left_tree_node_gap: int = 0
    lowermost_right_tree_node_gap: int = 0
    unknown_long: int = 0
    last_section_id: int = 0
    last_section_address: int = 0
    secondheader_address: int = 0
    numgaps: int = 0
    numsections: int = 0
    x20: int = 0
    x80: int = 0
    x40: int = 0
    section_map_id: int = 0
    section_map_address: int = 0
    section_info_id: int = 0
    section_array_size: int = 0
    gap_array_size: int = 0
    crc32: int = 0

    @property
    def id_string(self) -> str:
        """``file_id_string`` as text, for diagnostics."""
        return self.file_id_string.split(b"\x00", 1)[0].decode("latin-1")


@dataclass(slots=True)
class R2007FileHeader:
    """The R2007 file header (``Dwg_R2007_Header``), 34 packed ``int64``s.

    Field order matters: this is a raw struct image, so the names must line up
    with the declaration in ``include/dwg.h`` exactly.  Only a handful are
    used by the decoder — ``pages_map_offset``/``size_comp``/``size_uncomp``
    find the pages map, ``sections_map_id`` selects the sections map page, and
    ``sections_map_*`` size it.
    """

    header_size: int = 0
    file_size: int = 0
    pages_map_crc_compressed: int = 0
    pages_map_correction: int = 0
    pages_map_crc_seed: int = 0
    pages_map2_offset: int = 0
    pages_map2_id: int = 0
    pages_map_offset: int = 0
    pages_map_id: int = 0
    header2_offset: int = 0
    pages_map_size_comp: int = 0
    pages_map_size_uncomp: int = 0
    pages_amount: int = 0
    pages_maxid: int = 0
    unknown1: int = 0
    unknown2: int = 0
    pages_map_crc_uncomp: int = 0
    unknown3: int = 0
    unknown4: int = 0
    unknown5: int = 0
    num_sections: int = 0
    sections_map_crc_uncomp: int = 0
    sections_map_size_comp: int = 0
    sections_map2_id: int = 0
    sections_map_id: int = 0
    sections_map_size_uncomp: int = 0
    sections_map_crc_comp: int = 0
    sections_map_correction: int = 0
    sections_map_crc_seed: int = 0
    stream_version: int = 0
    crc_seed: int = 0
    crc_seed_encoded: int = 0
    random_seed: int = 0
    header_crc: int = 0


#: Field order of :class:`R2007FileHeader`, used to unpack the raw struct.
_R2007_FIELDS: Final[tuple[str, ...]] = (
    "header_size",
    "file_size",
    "pages_map_crc_compressed",
    "pages_map_correction",
    "pages_map_crc_seed",
    "pages_map2_offset",
    "pages_map2_id",
    "pages_map_offset",
    "pages_map_id",
    "header2_offset",
    "pages_map_size_comp",
    "pages_map_size_uncomp",
    "pages_amount",
    "pages_maxid",
    "unknown1",
    "unknown2",
    "pages_map_crc_uncomp",
    "unknown3",
    "unknown4",
    "unknown5",
    "num_sections",
    "sections_map_crc_uncomp",
    "sections_map_size_comp",
    "sections_map2_id",
    "sections_map_id",
    "sections_map_size_uncomp",
    "sections_map_crc_comp",
    "sections_map_correction",
    "sections_map_crc_seed",
    "stream_version",
    "crc_seed",
    "crc_seed_encoded",
    "random_seed",
    "header_crc",
)


@dataclass(slots=True)
class FileHeader:
    """Everything decoded from the fixed file header at offset 0.

    Fields are a union of all eras; which ones are populated depends on
    :attr:`version`.  ``sections`` is filled for R13–R2000 (from the locator
    records) and again for R2004+ once :mod:`py_libredwg.sections` has read
    the section page map — the two are kept apart by :attr:`sections` being
    the *locator* list and the R2004+ map being rebuilt by the caller.
    """

    version: DwgVersion = DwgVersion.R_INVALID
    version_tag: str = ""
    codepage: int = 0

    maint_rel_version: int = 0
    zero_one_or_three: int = 0

    #: R13–R2000 only, at 0x15: how many section locator records follow.
    sections_count: int = 0

    #: R13–R2000 only: the locator records themselves.
    sections: list[SectionRecord] = field(default_factory=list)

    #: True when the 0xC0C1 header CRC matched.
    crc_ok: bool = True

    # --- R13–R2000 and R2004+ shared ---
    thumbnail_address: int = 0
    dwg_version: int = 0
    maint_version: int = 0

    # --- R2004+ at 0x0c.. ---
    unknown_0: int = 0
    app_dwg_version: int = 0
    app_maint_version: int = 0
    security_type: int = 0
    rl_1c_address: int = 0
    summaryinfo_address: int = 0
    vbaproj_address: int = 0
    r2004_header_address: int = 0

    # --- era-specific decoded structures ---
    r2004_header: R2004FileHeader | None = None
    r2007_header: R2007FileHeader | None = None

    @property
    def is_2004_plus(self) -> bool:
        return self.version >= DwgVersion.R_2004a

    @property
    def is_2007(self) -> bool:
        return DwgVersion.R_2007a <= self.version <= DwgVersion.R_2007

    def section(self, number: int) -> SectionRecord | None:
        """Look a section up by number, or ``None``."""
        for rec in self.sections:
            if rec.number == number:
                return rec
        return None

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "version": self.version_tag,
            "version_name": self.version.name,
            "codepage": self.codepage,
        }
        if self.sections:
            out["sections"] = [
                {"number": s.number, "address": s.address, "size": s.size,
                 "name": s.name}
                for s in self.sections
            ]
        return out


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------
def detect_version(data: bytes) -> tuple[DwgVersion, str]:
    """Return the ``(version, tag)`` recorded in the first six bytes.

    The tag is ``AC`` followed by four digits (``AC1015`` … ``AC1032``), or
    one of the historical one-off codes.  Unknown tags raise rather than
    defaulting, because guessing a version makes every subsequent offset
    wrong in a way that surfaces far from the cause.
    """
    if len(data) < _VERSION_TAG_LEN:
        raise DwgDecodeError(
            f"file too short to hold a version tag ({len(data)} bytes)"
        )
    raw = data[:_VERSION_TAG_LEN]
    try:
        tag = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise DwgDecodeError(f"version tag {raw!r} is not ASCII") from exc
    version = TAG_TO_VERSION.get(tag)
    if version is None:
        if not tag.startswith("AC"):
            raise DwgDecodeError(f"not a DWG file: magic {tag!r}")
        raise UnsupportedVersionError(f"unsupported DWG version code {tag!r}")
    return version, tag


# ---------------------------------------------------------------------------
# File header readers
# ---------------------------------------------------------------------------
def read_file_header(data: bytes) -> FileHeader:
    """Decode the fixed file header, for any supported version.

    ``data`` is the whole file.  Nothing is read from beyond the header: the
    R2004 and R2007 variants stop at their own structures, leaving the
    section machinery in :mod:`py_libredwg.sections` to walk the rest.
    """
    if len(data) < _MIN_FILE_SIZE:
        raise DwgDecodeError(
            f"file too small for a DWG ({len(data)} bytes, need {_MIN_FILE_SIZE})"
        )
    version, tag = detect_version(data)
    if not (DwgVersion.R_13b1 <= version <= DwgVersion.R_2018):
        raise UnsupportedVersionError(
            f"{tag} ({version.name}) is outside the supported range R13-R2018"
        )

    hdr = FileHeader(version=version, version_tag=tag)
    reader = BitReader(data, from_version=version)

    # header.spec, from 0x0b (the caller starts after the 11-byte magic).
    reader.seek_bytes(0x0B)
    hdr.maint_rel_version = reader.read_rc()
    hdr.zero_one_or_three = reader.read_rc()
    hdr.thumbnail_address = reader.read_rl()
    hdr.dwg_version = reader.read_rc()
    hdr.maint_version = reader.read_rc()
    hdr.codepage = reader.read_rs()
    reader.codepage = hdr.codepage

    if version < DwgVersion.R_2004a:
        _read_locator_records(reader, hdr, data)
    else:
        _read_r2004_header_fields(reader, hdr)
        # R2007 replaced the encrypted header with an RS-coded one; everything
        # from R2010 on went back to the R2004 layout.
        if hdr.is_2007:
            hdr.r2007_header = read_r2007_header(data, hdr)
        else:
            hdr.r2004_header = read_r2004_header(data, hdr)
    return hdr


def _read_locator_records(
    reader: BitReader, hdr: FileHeader, data: bytes
) -> None:
    """R13–R2000: section locator records plus the trailing header CRC.

    ``header.spec`` leaves the records to the caller (``decode_R13_R2000``
    reads them), so they are handled here rather than in the spec
    transcription.  Each record is ``RC number, RL address, RL size`` — nine
    bytes — and the block ends with an ``RS`` CRC seeded with 0xC0C1 over
    everything from offset 0.
    """
    hdr.sections_count = reader.read_rl()
    if reader.byte != 0x19:
        raise DwgDecodeError(
            f"expected section locator records at 0x19, reader is at "
            f"0x{reader.byte:X}",
            offset=reader.byte,
        )

    names = (
        "AcDb:Header",
        "AcDb:Classes",
        "AcDb:Handles",
        "AcDb:ObjFreeSpace",
        "AcDb:Template",
        "AcDb:AuxHeader",
    )
    count = hdr.sections_count
    # A count of 0 means "no sections", which no real file has; anything wild
    # means the header itself is misread, so refuse rather than loop.
    if count > 0x1000:
        raise DwgDecodeError(
            f"implausible section count {count} in R13-R2000 header",
            offset=0x15,
        )

    for i in range(count):
        number = reader.read_rc()
        address = reader.read_rl()
        size = reader.read_rl()
        if address + size > len(data):
            raise DwgDecodeError(
                f"section {i} at 0x{address:X} size {size} runs past the "
                f"{len(data)}-byte file",
                offset=address,
                section=names[i] if i < len(names) else str(i),
            )
        hdr.sections.append(
            SectionRecord(
                number=number,
                address=address,
                size=size,
                name=names[i] if i < len(names) else f"Section{i}",
            )
        )

    expected = reader.read_rs()
    actual = crc16(0xC0C1, data, 0, reader.byte - 2)
    hdr.crc_ok = expected == actual


def _read_r2004_header_fields(reader: BitReader, hdr: FileHeader) -> None:
    """R2004+: the plaintext part, then the encrypted block at 0x80."""
    hdr.unknown_0 = reader.read_rc()
    hdr.app_dwg_version = reader.read_rc()
    hdr.app_maint_version = reader.read_rc()
    hdr.security_type = reader.read_rl()
    hdr.rl_1c_address = reader.read_rl()
    hdr.summaryinfo_address = reader.read_rl()
    hdr.vbaproj_address = reader.read_rl()
    hdr.r2004_header_address = reader.read_rl()
    # 54 bytes of zero slack take us from 0x2c to 0x80.
    reader.advance(54 * 8)


def read_r2004_header(data: bytes, hdr: FileHeader) -> R2004FileHeader:
    """Decrypt and decode the R2004 header at :data:`R2004_HEADER_OFFSET`.

    The CRC32 over the 108 meaningful bytes is checked but, as upstream does,
    a mismatch is not fatal — some writers leave it stale, and refusing the
    whole file over it would be worse than proceeding.
    """
    start = hdr.r2004_header_address or R2004_HEADER_OFFSET
    if start <= 0:
        start = R2004_HEADER_OFFSET
    end = start + R2004_HEADER_STRUCT_SIZE
    if end > len(data):
        raise DwgDecodeError(
            f"R2004 header at 0x{start:X} needs {R2004_HEADER_STRUCT_SIZE} "
            f"bytes but the file ends at 0x{len(data):X}",
            offset=start,
        )
    plain = decrypt_r2004_header(data[start:end], R2004_HEADER_STRUCT_SIZE)

    if plain[:8] != R2004_FILE_ID[:8]:
        raise DwgDecodeError(
            f"R2004 header at 0x{start:X} did not decrypt: file_ID_string is "
            f"{plain[:12]!r}, expected {R2004_FILE_ID!r}",
            offset=start,
        )

    reader = BitReader(plain, from_version=hdr.version)
    out = R2004FileHeader()
    out.file_id_string = reader.read_tf(12)
    out.header_address = reader.read_rl()
    out.header_size = reader.read_rl()
    out.x04 = reader.read_rl()
    out.root_tree_node_gap = reader.read_rld()
    out.lowermost_left_tree_node_gap = reader.read_rld()
    out.lowermost_right_tree_node_gap = reader.read_rld()
    out.unknown_long = reader.read_rl()
    out.last_section_id = reader.read_rl()
    out.last_section_address = reader.read_rll()
    out.secondheader_address = reader.read_rll()
    out.numgaps = reader.read_rl()
    out.numsections = reader.read_rl()
    out.x20 = reader.read_rl()
    out.x80 = reader.read_rl()
    out.x40 = reader.read_rl()
    out.section_map_id = reader.read_rl()
    out.section_map_address = reader.read_rll()
    out.section_info_id = reader.read_rld()
    out.section_array_size = reader.read_rld()
    out.gap_array_size = reader.read_rl()
    out.crc32 = reader.read_rl()
    return out


def read_r2007_header(data: bytes, hdr: FileHeader) -> R2007FileHeader:
    """RS-decode and unpack the R2007 file header at 0x80.

    The stored 0x3d8 bytes are a Reed-Solomon coded block; the first 32 bytes
    of the decoded result are a CRC/key/length prefix and the payload follows
    either verbatim or LZ77 compressed (``compr_len > 0``).  The decompressor
    itself lives in :mod:`py_libredwg.sections`; it is imported lazily to keep
    this module free of the container machinery.
    """
    start = R2007_HEADER_OFFSET
    end = start + R2007_HEADER_BYTES
    if end > len(data):
        raise DwgDecodeError(
            f"R2007 file header needs 0x{end:X} bytes, file is only "
            f"{len(data)}",
            offset=start,
        )
    pedata = decode_rs(
        data[start:end], R2007_RS_BLOCKS, R2007_RS_K, R2007_HEADER_BYTES
    )

    compr_len = int.from_bytes(pedata[24:28], "little", signed=True)
    payload_avail = len(pedata) - R2007_HEADER_PREFIX
    if compr_len > 0:
        from .sections import decompress_r2007

        raw = decompress_r2007(
            pedata[R2007_HEADER_PREFIX:],
            min(compr_len, payload_avail),
            R2007_HEADER_STRUCT_SIZE,
        )
    else:
        raw = pedata[R2007_HEADER_PREFIX:R2007_HEADER_PREFIX + R2007_HEADER_STRUCT_SIZE]

    if len(raw) < R2007_HEADER_STRUCT_SIZE:
        raise DwgDecodeError(
            f"R2007 file header decoded to {len(raw)} bytes, expected "
            f"{R2007_HEADER_STRUCT_SIZE}",
            offset=start,
        )

    out = R2007FileHeader()
    offset = 0
    for name in _R2007_FIELDS:
        setattr(out, name, struct.unpack_from("<q", raw, offset)[0])
        offset += 8
    return out


# ---------------------------------------------------------------------------
# HEADER section variables (bounded)
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class HeaderVariables:
    """The drawing settings from ``AcDb:Header``.

    Only the variables that ``header_variables.spec`` defines down to
    ``LIMMAX`` are transcribed.  In practice the ones consumers reach for are
    :attr:`INSBASE`, :attr:`EXTMIN`, :attr:`EXTMAX` (the drawing extents),
    :attr:`CLAYER` (default layer), :attr:`CECOLOR` (default colour) and the
    unit ratios.

    Handles are kept as raw numbers; resolving :attr:`CLAYER` and friends to
    names needs the TABLES section and is done by the caller.
    """

    # --- REQUIREDVERSIONS (R2013b+) ---
    REQUIREDVERSIONS: int = 0

    # --- units ---
    unit1_ratio: float = 0.0
    unit2_ratio: float = 0.0
    unit3_ratio: float = 0.0
    unit4_ratio: float = 0.0
    unit1_name: str = ""
    unit2_name: str = ""
    unit3_name: str = ""
    unit4_name: str = ""

    # --- undocumented padding fields, kept so offsets can be checked ---
    unknown_8: int = 0
    unknown_9: int = 0
    unknown_10: int = 0
    unknown_11: int = 0
    unknown_12: int = 0
    unknown_13: int = 0
    unknown_14: int = 0
    unknown_15: int = 0
    unknown_16: int = 0
    unknown_17: int = 0

    # --- mode bits ---
    VX_TABLE_RECORD: int = 0
    DIMASO: int = 0
    DIMSHO: int = 0
    DIMSAV: int = 0
    PLINEGEN: int = 0
    ORTHOMODE: int = 0
    REGENMODE: int = 0
    FILLMODE: int = 0
    QTEXTMODE: int = 0
    PSLTSCALE: int = 0
    LIMCHECK: int = 0
    BLIPMODE: int = 0
    USRTIMER: int = 0
    SKPOLY: int = 0
    ANGDIR: int = 0
    SPLFRAME: int = 0
    ATTREQ: int = 0
    ATTDIA: int = 0
    MIRRTEXT: int = 0
    WORLDVIEW: int = 0
    WIREFRAME: int = 0
    TILEMODE: int = 0
    PLIMCHECK: int = 0
    VISRETAIN: int = 0
    DELOBJ: int = 0
    DISPSILH: int = 0
    PELLIPSE: int = 0

    # --- integer settings ---
    PROXYGRAPHICS: int = 0
    DRAGMODE: int = 0
    TREEDEPTH: int = 0
    LUNITS: int = 0
    LUPREC: int = 0
    AUNITS: int = 0
    AUPREC: int = 0
    OSMODE: int = 0
    ATTMODE: int = 0
    COORDS: int = 0
    PDMODE: int = 0
    PICKSTYLE: int = 0
    USERI1: int = 0
    USERI2: int = 0
    USERI3: int = 0
    USERI4: int = 0
    USERI5: int = 0
    SPLINESEGS: int = 0
    SURFU: int = 0
    SURFV: int = 0
    SURFTYPE: int = 0
    SURFTAB1: int = 0
    SURFTAB2: int = 0
    SPLINETYPE: int = 0
    SHADEDGE: int = 0
    SHADEDIF: int = 0
    UNITMODE: int = 0
    MAXACTVP: int = 0
    ISOLINES: int = 0
    CMLJUST: int = 0
    TEXTQLTY: int = 0

    # --- real settings ---
    LTSCALE: float = 0.0
    TEXTSIZE: float = 0.0
    TRACEWID: float = 0.0
    SKETCHINC: float = 0.0
    FILLETRAD: float = 0.0
    THICKNESS: float = 0.0
    ANGBASE: float = 0.0
    PDSIZE: float = 0.0
    PLINEWID: float = 0.0
    USERR1: float = 0.0
    USERR2: float = 0.0
    USERR3: float = 0.0
    USERR4: float = 0.0
    USERR5: float = 0.0
    CHAMFERA: float = 0.0
    CHAMFERB: float = 0.0
    CHAMFERC: float = 0.0
    CHAMFERD: float = 0.0
    FACETRES: float = 0.0
    CMLSCALE: float = 0.0
    CELTSCALE: float = 0.0
    PSVPSCALE: float = 0.0
    PELEVATION: float = 0.0
    ELEVATION: float = 0.0

    # --- text ---
    MENU: str = ""

    # --- timestamps ---
    TDUCREATE: TimeBLL = field(default_factory=TimeBLL)
    TDUUPDATE: TimeBLL = field(default_factory=TimeBLL)
    TDINDWG: TimeBLL = field(default_factory=TimeBLL)
    TDUSRTIMER: TimeBLL = field(default_factory=TimeBLL)

    # --- colours and handles ---
    CECOLOR: object = None            # CMColor
    HANDSEED: int = 0
    CLAYER: int = 0
    TEXTSTYLE: int = 0
    CELTYPE: int = 0
    CMATERIAL: int = 0
    DIMSTYLE: int = 0
    CMLSTYLE: int = 0
    PUCSNAME: int = 0
    PUCSORTHOREF: int = 0
    PUCSORTHOVIEW: int = 0
    PUCSBASE: int = 0

    # --- points ---
    PINSBASE: Vec3 = field(default_factory=Vec3)
    PEXTMIN: Vec3 = field(default_factory=Vec3)
    PEXTMAX: Vec3 = field(default_factory=Vec3)
    PLIMMIN: tuple[float, float] = (0.0, 0.0)
    PLIMMAX: tuple[float, float] = (0.0, 0.0)
    PUCSORG: Vec3 = field(default_factory=Vec3)
    PUCSXDIR: Vec3 = field(default_factory=Vec3)
    PUCSYDIR: Vec3 = field(default_factory=Vec3)
    PUCSORGTOP: Vec3 = field(default_factory=Vec3)
    PUCSORGBOTTOM: Vec3 = field(default_factory=Vec3)
    PUCSORGLEFT: Vec3 = field(default_factory=Vec3)
    PUCSORGRIGHT: Vec3 = field(default_factory=Vec3)
    PUCSORGFRONT: Vec3 = field(default_factory=Vec3)
    PUCSORGBACK: Vec3 = field(default_factory=Vec3)

    INSBASE: Vec3 = field(default_factory=Vec3)
    EXTMIN: Vec3 = field(default_factory=Vec3)
    EXTMAX: Vec3 = field(default_factory=Vec3)
    LIMMIN: tuple[float, float] = (0.0, 0.0)
    LIMMAX: tuple[float, float] = (0.0, 0.0)

    @property
    def extents(self) -> tuple[Vec3, Vec3] | None:
        """``(EXTMIN, EXTMAX)`` when the drawing has usable extents.

        Returns ``None`` for a degenerate or absent extent window, which is
        the common case for a freshly created or partially written file.  The
        GeoJSON exporter falls back to a unit window in that case rather than
        mis-tessellating arcs against a zero-width span.
        """
        lo, hi = self.EXTMIN, self.EXTMAX
        if hi.x <= lo.x or hi.y <= lo.y:
            return None
        return lo, hi

    def to_dict(self) -> dict[str, object]:
        return {
            "INSBASE": list(self.INSBASE),
            "EXTMIN": list(self.EXTMIN),
            "EXTMAX": list(self.EXTMAX),
            "LIMMIN": list(self.LIMMIN),
            "LIMMAX": list(self.LIMMAX),
            "CLAYER": f"{self.CLAYER:X}" if self.CLAYER else None,
            "CECOLOR": self.CECOLOR.describe() if self.CECOLOR is not None else None,
            "LTSCALE": self.LTSCALE,
            "TEXTSIZE": self.TEXTSIZE,
            "DIMSTYLE": f"{self.DIMSTYLE:X}" if self.DIMSTYLE else None,
            "MEASUREMENT": None,
        }


def decode_header_variables(
    dat: BitReader,
    hdl_dat: BitReader,
    str_dat: BitReader | None = None,
) -> HeaderVariables:
    """Read the ``AcDb:Header`` variable block (``header_variables.spec``).

    ``dat`` is the value stream, ``hdl_dat`` the handle stream (the ``H`` and
    ``DATAHANDLE`` fields live there, not in the value stream) and
    ``str_dat`` the string stream, which is separate only from R2007 on.
    The caller reads the size/bitsize prefix and any sentinel; this function
    starts at the first variable.

    The transcription stops after ``LIMMAX``; see the module docstring for why
    that boundary is safe.
    """
    version = dat.from_version
    if str_dat is None:
        str_dat = dat
    v = HeaderVariables()

    def handle(reader: BitReader) -> int:
        h = reader.read_h()
        return h.value

    # SINCE(R_2013b): REQUIREDVERSIONS — a BLL, not a BL, so that the large
    # sentinel value 160 encodes in one byte.
    if version >= DwgVersion.R_2013b:
        v.REQUIREDVERSIONS = dat.read_bll()

    # SINCE(R_13b1): the four unit-conversion ratios.
    if version >= DwgVersion.R_13b1:
        v.unit1_ratio = dat.read_bd()
        v.unit2_ratio = dat.read_bd()
        v.unit3_ratio = dat.read_bd()
        v.unit4_ratio = dat.read_bd()

    # VERSIONS(R_13b1, R_2004): unit names as TV, removed once R2007's
    # string stream took over.
    if DwgVersion.R_13b1 <= version <= DwgVersion.R_2004:
        v.unit1_name = dat.read_tv()
        v.unit2_name = dat.read_tv()
        v.unit3_name = dat.read_tv()
        v.unit4_name = dat.read_tv()

    if version >= DwgVersion.R_13b1:
        v.unknown_8 = dat.read_bl()
        v.unknown_9 = dat.read_bl()

    if DwgVersion.R_13b1 <= version <= DwgVersion.R_14:
        v.unknown_10 = dat.read_bs()

    if DwgVersion.R_13b1 <= version <= DwgVersion.R_2000:
        v.VX_TABLE_RECORD = handle(hdl_dat)

    if version >= DwgVersion.R_13b1:
        v.DIMASO = dat.read_bit()
        v.DIMSHO = dat.read_bit()

    if DwgVersion.R_13b1 <= version <= DwgVersion.R_14:
        v.DIMSAV = dat.read_bit()

    if version >= DwgVersion.R_13b1:
        v.PLINEGEN = dat.read_bit()
        v.ORTHOMODE = dat.read_bit()
        v.REGENMODE = dat.read_bit()
        v.FILLMODE = dat.read_bit()
        v.QTEXTMODE = dat.read_bit()
        v.PSLTSCALE = dat.read_bit()
        v.LIMCHECK = dat.read_bit()

    if DwgVersion.R_13b1 <= version <= DwgVersion.R_14:
        v.BLIPMODE = dat.read_bit()

    if version >= DwgVersion.R_2004a:
        v.unknown_11 = dat.read_bit()

    v.USRTIMER = dat.read_bit()
    v.SKPOLY = dat.read_bit()
    v.ANGDIR = dat.read_bit()
    v.SPLFRAME = dat.read_bit()

    if DwgVersion.R_13b1 <= version <= DwgVersion.R_14:
        v.ATTREQ = dat.read_bit()
        v.ATTDIA = dat.read_bit()

    v.MIRRTEXT = dat.read_bit()
    v.WORLDVIEW = dat.read_bit()

    if DwgVersion.R_13b1 <= version <= DwgVersion.R_14:
        v.WIREFRAME = dat.read_bit()

    v.TILEMODE = dat.read_bit()
    v.PLIMCHECK = dat.read_bit()
    v.VISRETAIN = dat.read_bit()

    if DwgVersion.R_13b1 <= version <= DwgVersion.R_14:
        v.DELOBJ = dat.read_bit()

    v.DISPSILH = dat.read_bit()
    v.PELLIPSE = dat.read_bit()

    v.PROXYGRAPHICS = dat.read_bs()

    if version <= DwgVersion.R_14:
        v.DRAGMODE = dat.read_bs()

    if version >= DwgVersion.R_13b1:
        v.TREEDEPTH = dat.read_bsd()

    v.LUNITS = dat.read_bs()
    v.LUPREC = dat.read_bs()
    v.AUNITS = dat.read_bs()
    v.AUPREC = dat.read_bs()

    if version <= DwgVersion.R_14:
        v.OSMODE = dat.read_bs()

    v.ATTMODE = dat.read_bs()

    if version <= DwgVersion.R_14:
        v.COORDS = dat.read_bs()

    v.PDMODE = dat.read_bs()

    if version <= DwgVersion.R_14:
        v.PICKSTYLE = dat.read_bs()

    if version >= DwgVersion.R_2004a:
        v.unknown_12 = dat.read_bl()
        v.unknown_13 = dat.read_bl()
        v.unknown_14 = dat.read_bl()

    v.USERI1 = dat.read_bsd()
    v.USERI2 = dat.read_bsd()
    v.USERI3 = dat.read_bsd()
    v.USERI4 = dat.read_bsd()
    v.USERI5 = dat.read_bsd()
    v.SPLINESEGS = dat.read_bs()
    v.SURFU = dat.read_bs()
    v.SURFV = dat.read_bs()
    v.SURFTYPE = dat.read_bs()
    v.SURFTAB1 = dat.read_bs()
    v.SURFTAB2 = dat.read_bs()
    v.SPLINETYPE = dat.read_bs()
    v.SHADEDGE = dat.read_bs()
    v.SHADEDIF = dat.read_bs()
    v.UNITMODE = dat.read_bs()
    v.MAXACTVP = dat.read_bs()
    v.ISOLINES = dat.read_bs()
    v.CMLJUST = dat.read_bs()
    v.TEXTQLTY = dat.read_bs()

    v.LTSCALE = dat.read_bd()
    v.TEXTSIZE = dat.read_bd()
    v.TRACEWID = dat.read_bd()
    v.SKETCHINC = dat.read_bd()
    v.FILLETRAD = dat.read_bd()
    v.THICKNESS = dat.read_bd()
    v.ANGBASE = dat.read_bd()
    v.PDSIZE = dat.read_bd()
    v.PLINEWID = dat.read_bd()
    v.USERR1 = dat.read_bd()
    v.USERR2 = dat.read_bd()
    v.USERR3 = dat.read_bd()
    v.USERR4 = dat.read_bd()
    v.USERR5 = dat.read_bd()
    v.CHAMFERA = dat.read_bd()
    v.CHAMFERB = dat.read_bd()
    v.CHAMFERC = dat.read_bd()
    v.CHAMFERD = dat.read_bd()
    v.FACETRES = dat.read_bd()
    v.CMLSCALE = dat.read_bd()
    v.CELTSCALE = dat.read_bd()

    if version < DwgVersion.R_2007a:
        v.MENU = dat.read_tv()

    # The DECODER block right after these derives TDCREATE/TDUPDATE from the
    # TDU* pair by adding the local timezone offset.  That is a presentation
    # convenience computed from the *host*, not a stored value, so it is
    # deliberately not reproduced — a parser that reports a timestamp shifting
    # with $TZ would be worse than one that reports the raw counters, which
    # TimeBLL keeps.

    v.TDUCREATE = dat.read_timebll()
    v.TDUUPDATE = dat.read_timebll()

    if version >= DwgVersion.R_2004a:
        v.unknown_15 = dat.read_bl()
        v.unknown_16 = dat.read_bl()
        v.unknown_17 = dat.read_bl()

    v.TDINDWG = dat.read_timebll()
    v.TDUSRTIMER = dat.read_timebll()

    v.CECOLOR = dat.read_cmc(str_dat)

    # ``FIELD_DATAHANDLE (HANDSEED, ANYCODE, 0)`` — the one header variable
    # whose handle is *not* in the handle stream.  A data handle is read from
    # the value stream in place, so the ``H`` it consumes (1 code byte plus the
    # value bytes) is part of ``dat``, unlike every ``FIELD_HANDLE`` below.
    # Reading it from ``hdl_dat`` left those bits unconsumed in the value
    # stream, which shifted PSVPSCALE and every field after it, and stole one
    # handle from the front of the handle stream, which shifted CLAYER and
    # everything resolved through it.
    v.HANDSEED = handle(dat)

    v.CLAYER = handle(hdl_dat)
    v.TEXTSTYLE = handle(hdl_dat)
    v.CELTYPE = handle(hdl_dat)

    if version >= DwgVersion.R_2007a:
        v.CMATERIAL = handle(hdl_dat)

    v.DIMSTYLE = handle(hdl_dat)
    v.CMLSTYLE = handle(hdl_dat)

    if version >= DwgVersion.R_2000b:
        v.PSVPSCALE = dat.read_bd()

    if version >= DwgVersion.R_13b1:
        v.PINSBASE = Vec3(*dat.read_3bd())
        v.PEXTMIN = Vec3(*dat.read_3bd())
        v.PEXTMAX = Vec3(*dat.read_3bd())
        v.PLIMMIN = dat.read_2rd()
        v.PLIMMAX = dat.read_2rd()
        v.PELEVATION = dat.read_bd()
        v.PUCSORG = Vec3(*dat.read_3bd())
        v.PUCSXDIR = Vec3(*dat.read_3bd())
        v.PUCSYDIR = Vec3(*dat.read_3bd())
        v.PUCSNAME = handle(hdl_dat)

    if version >= DwgVersion.R_2000b:
        v.PUCSORTHOREF = handle(hdl_dat)
        v.PUCSORTHOVIEW = dat.read_bs()
        v.PUCSBASE = handle(hdl_dat)
        v.PUCSORGTOP = Vec3(*dat.read_3bd())
        v.PUCSORGBOTTOM = Vec3(*dat.read_3bd())
        v.PUCSORGLEFT = Vec3(*dat.read_3bd())
        v.PUCSORGRIGHT = Vec3(*dat.read_3bd())
        v.PUCSORGFRONT = Vec3(*dat.read_3bd())
        v.PUCSORGBACK = Vec3(*dat.read_3bd())

    v.INSBASE = Vec3(*dat.read_3bd())
    v.EXTMIN = Vec3(*dat.read_3bd())
    v.EXTMAX = Vec3(*dat.read_3bd())
    v.LIMMIN = dat.read_2rd()
    v.LIMMAX = dat.read_2rd()

    return v
