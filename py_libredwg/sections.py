# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""Section containers, section maps and the object decoder.

This is the module that turns a file into objects.  It translates three pieces
of LibreDWG:

``decode.c``
    ``decode_R2004`` and the ``read_2004_section_*`` family,
    ``decompress_R2004_section``, ``dwg_decode_add_object``,
    ``dwg_decode_entity`` / ``dwg_decode_object`` and
    ``dwg_decode_common_entity_handle_data``.
``decode_r2007.c``
    ``read_r2007_meta_data`` with ``read_pages_map`` / ``read_sections_map`` /
    ``read_data_section`` / ``section_string_stream``, ``read_system_page`` /
    ``read_data_page``, and the R2007 LZ77 variant with its
    ``copy_compressed_bytes`` permutation.
``dwg.spec``
    the per-type bodies, via the ``DWG_ENTITY`` / ``DWG_TABLE`` blocks.

How a file becomes objects
--------------------------

A DWG file is a *container* of named sections, and it is the container format
that changed at R2004.  Everything downstream of it is the same code:

============  =============================================================
R13–R2000     section locator records at ``0x19``; sections are stored
              verbatim and addressed by absolute file offset
R2004         an LZ77-compressed section page map at
              ``section_map_address + 0x100``, followed by a *section info*
              block describing which pages belong to which section; each
              page carries an encrypted 32-byte header
R2007+        Reed-Solomon coded pages map and sections map, and every page
              is RS coded and optionally LZ77 compressed
============  =============================================================

Each container reader therefore yields the same two things: a byte buffer per
section type, and a list of object addresses. :class:`ObjectDecoder` then only
needs to know the version — never how the bytes got there.

Two deliberate departures from the C
------------------------------------

**No ``Dwg_Data`` side table.**  LibreDWG threads a mutable ``dwg`` pointer
through every function and stashes each object into ``dwg->object[]`` as a
side effect.  Here the readers are pure functions returning values, and
:func:`decode_objects` is a generator.  That removes the pointer chasing and
the ``realloc`` hazards the C has to defend against with its
``abs_dat``/``bit_reset_chain`` dance.

**One index space for the three object substreams.**  LibreDWG gives each
object three restricted ``Bit_Chain`` sub-chains (value, handle, string) that
share one buffer at three offsets, then moves between them with
``bit_reset_chain`` and bare pointer arithmetic.  This module keeps three
:class:`~py_libredwg.bitreader.BitReader` instances over the objects-section
buffer at *absolute* bit offsets.  Same three positions, but a mistake in one
stream cannot silently move another — which in the C is exactly what
``START_HANDLE_STREAM`` does (it repositions ``dat``, the *value* stream, to
``obj->hdlpos`` while the handle fields read from ``hdl_dat``; the whole macro
is therefore a no-op for decoding, and is not implemented here at all).

Everything the C does for *validation* that cannot change the decoded values —
page checksums, CRCs, the ``max_decomp_size`` sanities — is still done, but
only as far as it can produce a warning.  A mismatch never aborts a decode
that would otherwise succeed, exactly as upstream.

.. note::
   ``super(C, self)`` is used rather than the zero-argument ``super()`` for the
   reason documented in :mod:`py_libredwg.objects.base_object`:
   ``@dataclass(slots=True)`` discards the class object the compiler's
   ``__class__`` cell points at, so the bare form raises ``TypeError``.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final

from .bitreader import BitReader, Handle
from .constants import BYLAYER, DwgVersion, ObjectType, SectionType
from .errors import BitStreamError, DwgDecodeError, DwgSectionError
from .geometry import Vec3
from .header import (
    HeaderVariables,
    R2004FileHeader,
    R2007FileHeader,
    SectionRecord,
    decode_header_variables,
    decode_rs,
)
from .objects import entity_class_for
from .objects.base_object import (
    BlockHeader,
    CMColor,
    ControlObject,
    DwgObject,
    Entity,
    Layer,
    ProxyEntity,
    UnknownObject,
)

__all__ = [
    # shared primitives (header.py imports decompress_r2007 from here)
    "dwg_section_page_checksum",
    "decompress_R2004_section",
    "decompress_r2007",
    "find_sentinel",
    "section_type_from_name",
    "section_max_decomp_size",
    "position_string_stream",
    # R2004 container
    "SectionPage",
    "SectionInfo",
    "R2004Container",
    "read_r2004_sections",
    "read_2004_section",
    # R2007 container
    "R2007Page",
    "R2007SectionPage",
    "R2007Section",
    "R2007Container",
    "read_r2007_sections",
    "read_data_section",
    # object map
    "ObjectAddress",
    "read_object_map",
    # tables
    "DwgClass",
    "decode_classes",
    "read_header_section",
    # objects
    "ObjectState",
    "ObjectDecoder",
    "decode_objects",
    "obj_has_strings",
]


# ---------------------------------------------------------------------------
# Limits  (DBG_MAX_COUNT / DBG_MAX_SIZE and friends from common.h)
# ---------------------------------------------------------------------------
#: Sizes past which a plausible-looking length field stops being believable.
#: Upstream keeps these so a corrupt field cannot ask for gigabytes; the same
#: guard is kept here, because a Python bytearray of 0x7FFFFFFF bytes is just
#: as fatal as the C's failed calloc, only slower to find out about.
_DBG_MAX_COUNT: Final[int] = 0xFFFFFF
_DBG_MAX_SIZE: Final[int] = 0xFFFFFF

#: Reed-Solomon parameters.  The *system* pages — the maps themselves and the
#: R2007 file header — use (255, 239); data pages use (255, 251).  The two are
#: not interchangeable: a different codeword count for the same payload, which
#: is why the page size is the only thing that says which one was used.
_RS_K_SYSTEM: Final[int] = 239
_RS_K_DATA: Final[int] = 0xFB

#: The R2007 file header is itself an RS-coded system page occupying 0x3d8
#: bytes from 0x80: three RS blocks' worth, read whole before decoding.  After
#: it ``read_r2007_meta_data`` skips a further 0x28 bytes (an overread-check
#: field, not a page offset) and only then adds ``pages_map_offset``.  The
#: three addends are kept separate because collapsing them into one constant
#: is exactly how the offset goes wrong silently.
_R2007_HEADER_PAGE_ADDRESS: Final[int] = 0x80
_R2007_HEADER_PAGE_SIZE: Final[int] = 0x3D8
_R2007_POST_HEADER_SKIP: Final[int] = 0x28

#: The section page map header lives 0x100 bytes into the map area, and the
#: first *data* page also sits at 0x100.  Two different meanings of the same
#: constant upstream; kept apart here so they cannot be conflated.
_R2004_MAP_HEADER_OFFSET: Final[int] = 0x100
_R2004_MAP_FIRST_ADDRESS: Final[int] = 0x100
_R2004_MAP_HEADER_SIZE: Final[int] = 0x14      # 5 x RL
_R2004_PAGE_HEADER_SIZE: Final[int] = 32

#: A decompressed section larger than this is rejected — 790 MB, the same
#: ceiling upstream uses before allocating.
_MAX_DECOMP_SECTION: Final[int] = 0x2F000000

#: Caps on counts read out of the file, so a corrupt length cannot build an
#: unbounded list before the sanity checks further down can run.
_MAX_SECTIONS: Final[int] = 100_000
_MAX_OBJECTS: Final[int] = 4_000_000
_MAX_CLASSES: Final[int] = 5_000
_MAX_PAGES: Final[int] = 1_000_000

#: Object-map page ceilings.  The ODA spec cuts pages at 2032 bytes, but files
#: exist with 2040 (R13–R2004) and 2050 (R2007+) pages, so upstream accepts
#: those and so does this.
_PAGE_LIMIT_R13: Final[int] = 2040
_PAGE_LIMIT_R2007: Final[int] = 2050

#: Nothing bigger than this can be a real object; the check also stops a
#: corrupt MS from sending the reader to a nonsense address.
_MAX_OBJECT_SIZE: Final[int] = 0x7FFFFFFF


# ---------------------------------------------------------------------------
# Sentinels  (dwg_sentinel, common.c)
# ---------------------------------------------------------------------------
#: Fixed 16-byte magics marking section boundaries.  Only the ones this module
#: and :mod:`py_libredwg.header` need are listed.
SENTINEL_HEADER_END: Final[bytes] = bytes(
    (0x95, 0xA0, 0x4E, 0x28, 0x99, 0x82, 0x1A, 0xE5,
     0x5E, 0x41, 0xE0, 0x5F, 0x9D, 0x3A, 0x4D, 0x00)
)
SENTINEL_VARIABLE_BEGIN: Final[bytes] = bytes(
    (0xCF, 0x7B, 0x1F, 0x23, 0xFD, 0xDE, 0x38, 0xA9,
     0x5F, 0x7C, 0x68, 0xB8, 0x4E, 0x6D, 0x33, 0x5F)
)
SENTINEL_VARIABLE_END: Final[bytes] = bytes(
    (0x30, 0x84, 0xE0, 0xDC, 0x02, 0x21, 0xC7, 0x56,
     0xA0, 0x83, 0x97, 0x47, 0xB1, 0x92, 0xCC, 0xA0)
)
SENTINEL_CLASS_BEGIN: Final[bytes] = bytes(
    (0x8D, 0xA1, 0xC4, 0xB8, 0xC4, 0xA9, 0xF8, 0xC5,
     0xC0, 0xDC, 0xF4, 0x5F, 0xE7, 0xCF, 0xB6, 0x8A)
)
SENTINEL_CLASS_END: Final[bytes] = bytes(
    (0x72, 0x5E, 0x3B, 0x47, 0x3B, 0x56, 0x07, 0x3A,
     0x3F, 0x23, 0x0B, 0xA0, 0x18, 0x30, 0x49, 0x75)
)


def find_sentinel(data: bytes, sentinel: bytes, start: int = 0) -> int:
    """Locate ``sentinel`` in ``data`` (``bit_search_sentinel``).

    Returns the byte offset *just past* the sentinel, or ``-1`` when it is
    absent — upstream's convention, including the sign, so the call sites keep
    their ``>= 0`` tests.

    ``bit_search_sentinel`` always scans from byte 0 of the chain;
    ``start`` exists so a caller that already knows where to look need not
    rescan the whole section.
    """
    index = data.find(sentinel, start)
    return index + len(sentinel) if index >= 0 else -1


# ---------------------------------------------------------------------------
# Section names  (dwg_section_type / dwg_section_wtype, dwg.c)
# ---------------------------------------------------------------------------
_NAME_TO_TYPE: Final[dict[str, int]] = {
    "AcDb:Header": int(SectionType.HEADER),
    "AcDb:AuxHeader": int(SectionType.AUXHEADER),
    "AcDb:Classes": int(SectionType.CLASSES),
    "AcDb:Handles": int(SectionType.HANDLES),
    "AcDb:Template": int(SectionType.TEMPLATE),
    "AcDb:ObjFreeSpace": int(SectionType.OBJFREESPACE),
    "AcDb:AcDbObjects": int(SectionType.OBJECTS),
    "AcDb:RevHistory": int(SectionType.REVHISTORY),
    "AcDb:SummaryInfo": int(SectionType.SUMMARYINFO),
    "AcDb:Preview": int(SectionType.PREVIEW),
    "AcDb:AppInfo": int(SectionType.APPINFO),
    "AcDb:AppInfoHistory": int(SectionType.APPINFOHISTORY),
    "AcDb:FileDepList": int(SectionType.FILEDEPLIST),
    "AcDb:Security": int(SectionType.SECURITY),
    "AcDb:VBAProject": int(SectionType.VBAPROJECT),
    "AcDb:Signature": int(SectionType.SIGNATURE),
    "AcDb:AcDsPrototype_1b": int(SectionType.ACDS),
}


def section_type_from_name(name: str) -> int:
    """Map a section name to a :class:`~py_libredwg.constants.SectionType`.

    ``dwg_section_wtype`` compares the raw UTF-16 bytes, so its comparison is
    on the low byte of each character.  R2007 section names are ASCII, which
    makes the truncation unobservable; comparing the decoded name is the same
    test without the artefact.
    """
    return _NAME_TO_TYPE.get(name, int(SectionType.UNKNOWN))


def section_max_decomp_size(sec_type: int, version: DwgVersion) -> int:
    """``section_max_decomp_size`` — the allowed decompressed block size.

    A section descriptor claiming a larger block than its type allows is
    dropped by the decoder, because an over-sized claim means the descriptor
    is corrupt.
    """
    if sec_type == int(SectionType.APPINFOHISTORY):
        return 0x1000
    if sec_type == int(SectionType.APPINFO):
        return 0x400
    if sec_type == int(SectionType.PREVIEW):
        if DwgVersion.R_2007 <= version <= DwgVersion.R_2010:
            return 0x4A000
        if version == DwgVersion.R_2004:
            return 0x144400
        return 0x7FFF
    if sec_type == int(SectionType.SUMMARYINFO):
        return 0x100
    return 0x7400


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------
def dwg_section_page_checksum(
    seed: int, data: bytes, start: int, size: int, skip_checksum: bool = False
) -> int:
    """``dwg_section_page_checksum`` — the Adler-like page checksum.

    Two running sums are kept modulo ``0xFFF1`` and folded into one 32-bit
    word.  ``skip_checksum`` masks out bytes 16..19, which hold the checksum
    itself: the encoder computes the header checksum before that field has been
    filled in, so the verifier has to ignore it or the two can never agree.

    The C takes a ``Bit_Chain`` and works at ``dat->byte`` without advancing;
    this takes the offset explicitly — the same arithmetic minus the side
    effect the caller did not want.
    """
    if start < 0 or size < 0 or start + size > len(data):
        return 0
    sum1 = seed & 0xFFFF
    sum2 = (seed >> 0x10) & 0xFFFF

    pos = start
    end = start + size
    skip_lo = start + 16
    skip_hi = start + 20

    remaining = size
    while remaining > 0 and pos < end:
        chunk = min(remaining, 0x15B0)
        remaining -= chunk
        for _ in range(chunk):
            if not skip_checksum or pos < skip_lo or pos >= skip_hi:
                sum1 += data[pos]
            sum2 += sum1
            pos += 1
        sum1 %= 0xFFF1
        sum2 %= 0xFFF1
    return ((sum2 << 0x10) | (sum1 & 0xFFFF)) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# LZ77 — the R2004 flavour  (decode.c)
# ---------------------------------------------------------------------------
def _read_literal_length_2004(src: bytes, pos: int, opcode: int) -> tuple[int, int]:
    """``read_literal_length`` (``decode.c``): how many literals follow.

    Reached only when the opcode's low nibble is zero, which selects the
    extended form: 0xFF bytes accumulate in steps of 255.  Returns
    ``(length, new_pos)``.
    """
    lowbits = opcode & 0xF
    if lowbits == 0:
        lastbyte = 0
        size = len(src)
        while True:
            if pos >= size:
                lastbyte = 0
                break
            lastbyte = src[pos]
            pos += 1
            if lastbyte != 0 or pos >= size:
                break
            lowbits += 0xFF
        lowbits += 0xF + lastbyte
    return lowbits + 3, pos


def _read_compressed_bytes_2004(
    src: bytes, pos: int, opcode: int, bits: int
) -> tuple[int, int]:
    """``read_compressed_bytes`` (``decode.c``): the back-reference length.

    ``bits`` is the opcode mask — 7 for the ``0x10..0x1F`` class, 0x1F for
    ``0x20..0x3F``.  Same 0xFF accumulation rule as the literal length, with a
    different base added at the end.
    """
    compressed = opcode & bits
    if compressed == 0:
        lastbyte = 0
        size = len(src)
        while True:
            if pos >= size:
                lastbyte = 0
                break
            lastbyte = src[pos]
            pos += 1
            if lastbyte != 0 or pos >= size:
                break
            compressed += 0xFF
        compressed += lastbyte + bits
    return compressed + 2, pos


def _two_byte_offset_2004(
    src: bytes, pos: int, plus: int, offset: int
) -> tuple[int, int, int]:
    """``two_byte_offset`` (``decode.c``): read a 14-bit back-distance.

    Returns ``(first_byte, offset, new_pos)``.  ``first_byte`` doubles as the
    next opcode, which is why the decompression loop can end on 0x11: that byte
    arrives where a length field would, and is the end-of-stream marker.
    """
    first = src[pos]
    second = src[pos + 1]
    pos += 2
    offset |= first >> 2
    offset |= second << 6
    offset += plus
    return first, offset, pos


def _copy_literals_2004(
    src: bytes, pos: int, dst: bytearray, dst_pos: int, count: int
) -> tuple[int, int]:
    """``copy_bytes`` (``decode.c``): copy literals, return the next opcode.

    The C returns ``bit_read_RC(src)``, so the byte *after* the literals is the
    next opcode — the run cannot simply stop when it is done.  That read
    advances the cursor, so the returned position is one past the opcode, not
    at it; returning ``pos`` unchanged would make the next iteration re-read
    the opcode as its operand.
    """
    dst[dst_pos:dst_pos + count] = src[pos:pos + count]
    pos += count
    nxt = src[pos] if pos < len(src) else 0x11
    return nxt, pos + 1


def decompress_R2004_section(
    src: bytes, src_pos: int, src_end: int,
    dst: bytearray, dst_pos: int, dst_end: int,
) -> int:
    """``decompress_R2004_section`` — the R2004 LZ77 flavour.

    Consumes ``src[src_pos:src_end]``, writes into ``dst`` from ``dst_pos``
    without ever passing ``dst_end``, and returns the final write position —
    what ``dec->byte`` ends up as in the C.

    The opcode classes:

    * ``0x00..0x0F`` — literals only, run length extended by 0xFF steps.
    * ``0x10..0x1F`` — long back-reference: distance from two bytes, plus
      0x4000.  An 0x11 in opcode position ends the stream.
    * ``0x20..0x3F`` — short back-reference: two bytes, plus 1.
    * ``0x40..0xFF`` — back-reference of length ``(opcode >> 4) - 1`` with the
      distance packed into the opcode and one byte.

    Raises :class:`~py_libredwg.errors.DwgDecodeError` where upstream returns
    ``DWG_ERR_VALUEOUTOFBOUNDS``.  A silently wrong decompression is
    undetectable downstream, so this is the one class of error the module
    refuses to swallow.
    """
    if src_pos > src_end:
        raise DwgDecodeError(
            f"decompress_R2004_section: start {src_pos} past end {src_end}",
            section="LZ77",
        )
    src_end = min(src_end, len(src))
    dst_end = min(dst_end, len(dst))

    pos = src_pos
    if pos >= src_end:
        return dst_pos

    opcode1 = src[pos]
    pos += 1
    if (opcode1 & 0xF0) == 0:
        lit_length, pos = _read_literal_length_2004(src, pos, opcode1)
        if dst_pos + lit_length > dst_end:
            raise DwgDecodeError(
                f"LZ77 literal run {lit_length} overflows the output buffer",
                section="LZ77",
            )
        opcode1, pos = _copy_literals_2004(src, pos, dst, dst_pos, lit_length)
        dst_pos += lit_length

    while pos < src_end and dst_pos < dst_end and opcode1 != 0x11:
        comp_bytes = 0
        comp_offset = 0

        if opcode1 < 0x10 or opcode1 >= 0x40:
            # Length and the high offset bits both live in the opcode.
            comp_bytes = (opcode1 >> 4) - 1
            if pos >= src_end:
                break
            opcode2 = src[pos]
            pos += 1
            comp_offset = (((opcode1 >> 2) & 3) | (opcode2 << 2)) + 1
        elif opcode1 < 0x20:
            comp_bytes, pos = _read_compressed_bytes_2004(src, pos, opcode1, 7)
            comp_offset = (opcode1 & 8) << 11
            if pos + 2 > src_end:
                break
            opcode1, comp_offset, pos = _two_byte_offset_2004(
                src, pos, 0x4000, comp_offset
            )
        else:                                        # 0x20..0x3F
            comp_bytes, pos = _read_compressed_bytes_2004(src, pos, opcode1, 0x1F)
            if pos + 2 > src_end:
                break
            opcode1, comp_offset, pos = _two_byte_offset_2004(src, pos, 1, comp_offset)

        end = dst_pos + comp_bytes
        if (
            end > dst_end
            or dst_pos < comp_offset
            or (dst_pos - comp_offset) >= dst_end
            or comp_offset > dst_end
        ):
            raise DwgDecodeError(
                f"LZ77 back-reference out of bounds: length {comp_bytes}, "
                f"offset {comp_offset}, dst {dst_pos}/{dst_end}",
                section="LZ77",
            )
        # Byte by byte on purpose: when the offset is smaller than the length
        # the source and destination overlap and the bytes just written must be
        # read back (LZ77 run extension).  A block copy would be wrong here.
        p = dst_pos
        while p < end:
            dst[p] = dst[p - comp_offset]
            p += 1
        dst_pos = end

        # Trailing literals, their count carried in the low two bits.
        lit_length = opcode1 & 3
        if lit_length == 0:
            if pos >= src_end:
                break
            opcode1 = src[pos]
            pos += 1
            if (opcode1 & 0xF0) == 0:
                lit_length, pos = _read_literal_length_2004(src, pos, opcode1)
        if lit_length:
            if dst_pos + lit_length > dst_end:
                break
            opcode1, pos = _copy_literals_2004(src, pos, dst, dst_pos, lit_length)
            dst_pos += lit_length
    return dst_pos


# ---------------------------------------------------------------------------
# LZ77 — the R2007 flavour  (decode_r2007.c)
# ---------------------------------------------------------------------------
def _r2007_read_literal_length(
    src: bytes, pos: int, src_end: int, opcode: int
) -> tuple[int, int]:
    """``read_literal_length`` (``decode_r2007.c``).

    ``length = opcode + 8``, with 0x17 selecting the extended form: one byte,
    or — when that byte is 0xFF — little-endian 16-bit values until one is not
    0xFFFF.  Returns ``(length, new_pos)``.
    """
    length = opcode + 8
    if length == 0x17:
        if pos >= src_end:
            raise DwgDecodeError("R2007 decompression: literal length past end",
                                 section="LZ77/R2007")
        n = src[pos]
        pos += 1
        length += n
        if n == 0xFF:
            while True:
                if src_end - pos < 2:
                    raise DwgDecodeError(
                        "R2007 decompression: literal length past end",
                        section="LZ77/R2007")
                n = src[pos] | (src[pos + 1] << 8)
                pos += 2
                length += n
                if n != 0xFFFF:
                    break
    return length, pos


def _r2007_read_instructions(
    src: bytes, pos: int, src_end: int, opcode: int
) -> tuple[int, int, int, int]:
    """``read_instructions`` (``decode_r2007.c``).

    The top nibble of ``opcode`` picks one of four encodings; each rewrites
    ``opcode`` from the next byte, which is why it is threaded in and out.
    Returns ``(opcode, offset, length, new_pos)``.
    """
    def need(n: int) -> None:
        if src_end - pos < n:
            raise DwgDecodeError("R2007 decompression: instructions past end",
                                 section="LZ77/R2007")

    top = opcode >> 4
    if top == 0:
        length = opcode & 0xF
        need(2)
        offset = src[pos]
        opcode = src[pos + 1]
        pos += 2
        length = ((opcode >> 3) & 0x10) + length + 0x13
        offset = ((opcode & 0x78) << 5) + 1 + offset
    elif top == 1:
        length = opcode & 0xF
        need(2)
        offset = src[pos]
        opcode = src[pos + 1]
        pos += 2
        length += 3
        offset = ((opcode & 0xF8) << 5) + 1 + offset
    elif top == 2:
        need(2)
        offset = src[pos]
        offset = ((src[pos + 1] << 8) & 0xFF00) | offset
        pos += 2
        length = opcode & 7
        if (opcode & 8) == 0:
            need(1)
            opcode = src[pos]
            pos += 1
            length = (opcode & 0xF8) + length
        else:
            offset += 1
            need(1)
            length = (src[pos] << 3) + length
            pos += 1
            need(1)
            opcode = src[pos]
            pos += 1
            length = (((opcode & 0xF8) << 8) + length) + 0x100
    else:                                            # 0x30..0xFF
        length = opcode >> 4
        offset = opcode & 15
        need(1)
        opcode = src[pos]
        pos += 1
        offset = (((opcode & 0xF8) << 1) + offset) + 1
    return opcode, offset, length, pos


def _r2007_copy_bytes(dst: bytearray, dst_pos: int, length: int, offset: int) -> int:
    """``copy_bytes`` (``decode_r2007.c``): an overlapping back-copy."""
    src = dst_pos - offset
    for _ in range(length):
        dst[dst_pos] = dst[src]
        dst_pos += 1
        src += 1
    return dst_pos


def _r2007_copy_compressed_bytes(
    dst: bytearray, dst_pos: int, src: bytes, src_pos: int, length: int
) -> int:
    """``copy_compressed_bytes`` (``decode_r2007.c``).

    Not a copy but a byte-shuffling permutation: whole 32-byte groups are
    *reversed*, and the 1..31 byte remainder has a hand-written order that the
    upstream ``switch`` spells out as sequences of ``copy_1/2/3/4/8/16``.

    The remainder cases below are transcribed from that switch one for one
    rather than reduced to a formula.  The order is genuinely irregular — case
    18 is ``[17][1..17][0]``, not ``[17][9..17][1..9][0]`` — and a plausible
    "simplification" here corrupts data silently, with the damage surfacing
    arbitrarily far away in the decoded output.

    ``src`` is read at fixed offsets from ``src_pos`` and does not advance
    inside the switch; only the 32-byte loop moves it.
    """
    # 32-byte groups: copy_16 (16); copy_16 (0).  Each copy_16 *swaps* two
    # 8-byte halves without reversing inside them, so the group comes out as
    # halves 3, 2, 1, 0 — not the byte reversal it superficially resembles.
    # (Upstream's own comment: "4 and 8 is not reverse, 16 is".)
    while length >= 32:
        block = src[src_pos:src_pos + 32]
        dst[dst_pos:dst_pos + 32] = (block[24:32] + block[16:24]
                                     + block[8:16] + block[0:8])
        src_pos += 32
        dst_pos += 32
        length -= 32

    def c1(offset: int) -> None:
        nonlocal dst_pos
        dst[dst_pos] = src[src_pos + offset]
        dst_pos += 1

    def c2(offset: int) -> None:
        nonlocal dst_pos
        dst[dst_pos] = src[src_pos + offset + 1]
        dst[dst_pos + 1] = src[src_pos + offset]
        dst_pos += 2

    def c3(offset: int) -> None:
        nonlocal dst_pos
        dst[dst_pos] = src[src_pos + offset + 2]
        dst[dst_pos + 1] = src[src_pos + offset + 1]
        dst[dst_pos + 2] = src[src_pos + offset]
        dst_pos += 3

    def c4(offset: int) -> None:
        nonlocal dst_pos
        dst[dst_pos:dst_pos + 4] = src[src_pos + offset:src_pos + offset + 4]
        dst_pos += 4

    def c8(offset: int) -> None:
        nonlocal dst_pos
        dst[dst_pos:dst_pos + 8] = src[src_pos + offset:src_pos + offset + 8]
        dst_pos += 8

    def c16(offset: int) -> None:
        nonlocal dst_pos
        dst[dst_pos:dst_pos + 8] = src[src_pos + offset + 8:src_pos + offset + 16]
        dst[dst_pos + 8:dst_pos + 16] = src[src_pos + offset:src_pos + offset + 8]
        dst_pos += 16

    if length == 1:
        c1(0)
    elif length == 2:
        c2(0)
    elif length == 3:
        c3(0)
    elif length == 4:
        c4(0)
    elif length == 5:
        c1(4); c4(0)
    elif length == 6:
        c1(5); c4(1); c1(0)
    elif length == 7:
        c2(5); c4(1); c1(0)
    elif length == 8:
        c8(0)
    elif length == 9:
        c1(8); c8(0)
    elif length == 10:
        c1(9); c8(1); c1(0)
    elif length == 11:
        c2(9); c8(1); c1(0)
    elif length == 12:
        c4(8); c8(0)
    elif length == 13:
        c1(12); c4(8); c8(0)
    elif length == 14:
        c1(13); c4(9); c8(1); c1(0)
    elif length == 15:
        c2(13); c4(9); c8(1); c1(0)
    elif length == 16:
        c16(0)
    elif length == 17:
        c8(9); c1(8); c8(0)
    elif length == 18:
        c1(17); c16(1); c1(0)
    elif length == 19:
        c3(16); c16(0)
    elif length == 20:
        c4(16); c16(0)
    elif length == 21:
        c1(20); c4(16); c16(0)
    elif length == 22:
        c2(20); c4(16); c16(0)
    elif length == 23:
        c3(20); c4(16); c16(0)
    elif length == 24:
        c8(16); c16(0)
    elif length == 25:
        c8(17); c1(16); c16(0)
    elif length == 26:
        c1(25); c8(17); c1(16); c16(0)
    elif length == 27:
        c2(25); c8(17); c1(16); c16(0)
    elif length == 28:
        c4(24); c8(16); c16(0)
    elif length == 29:
        c1(28); c4(24); c8(16); c16(0)
    elif length == 30:
        c2(28); c4(24); c8(16); c16(0)
    elif length == 31:
        c1(30); c4(26); c8(18); c16(2); c2(0)
    return dst_pos


def decompress_r2007(
    src: bytes, src_size: int, dst_size: int, dst_end: int | None = None
) -> bytes:
    """``decompress_r2007`` — the R2007 LZ77 flavour.

    .. warning::
       The argument order deliberately differs from the C, which is
       ``(dst, dst_size, src, src_size, dst_end)``.  This returns the
       destination instead of filling a caller-supplied buffer, so ``src``
       comes first; :mod:`py_libredwg.header` calls it in this order.

    ``src_size`` is how many bytes of ``src`` to consume.  ``dst_size`` is the
    nominal output size and is used only to derive the bound when the caller
    does not supply ``dst_end`` — the real limit is ``dst_end``, because a data
    page decompresses into a *slice* of a larger section buffer.

    Raises :class:`~py_libredwg.errors.DwgDecodeError` on any overrun, where
    the C returns ``DWG_ERR_INTERNALERROR``.
    """
    if dst_size < 0 or src_size < 0:
        raise DwgDecodeError("R2007 decompression: negative size",
                             section="LZ77/R2007")
    if src_size < 2:
        raise DwgDecodeError("R2007 decompression: input too short",
                             section="LZ77/R2007")
    src_end = min(len(src), src_size)
    out_len = dst_size if dst_end is None else dst_end
    if out_len < 0:
        raise DwgDecodeError("R2007 decompression: negative output size",
                             section="LZ77/R2007")
    dst = bytearray(out_len)

    pos = 0
    dst_pos = 0
    length = 0
    opcode = src[pos]
    pos += 1
    if (opcode & 0xF0) == 0x20:
        # A leading 0x20-ish opcode carries a 3-byte prologue whose last byte
        # holds the first literal-run length in its low three bits.
        pos += 2
        if pos >= src_end:
            raise DwgDecodeError("R2007 decompression: truncated prologue",
                                 section="LZ77/R2007")
        length = src[pos] & 0x07
        pos += 1
        if length == 0:
            raise DwgDecodeError("R2007 decompression: zero length",
                                 section="LZ77/R2007")

    while pos < src_end:
        if length == 0:
            length, pos = _r2007_read_literal_length(src, pos, src_end, opcode)

        if dst_pos + length > out_len or pos + length > src_end:
            raise DwgDecodeError(
                f"R2007 decompression: literal run overflow "
                f"(len {length}, dst {dst_pos}/{out_len}, src {pos}/{src_end})",
                section="LZ77/R2007",
            )
        dst_pos = _r2007_copy_compressed_bytes(dst, dst_pos, src, pos, length)
        pos += length
        length = 0

        if pos >= src_end:
            return bytes(dst)

        opcode = src[pos]
        pos += 1
        opcode, offset, length, pos = _r2007_read_instructions(
            src, pos, src_end, opcode
        )

        while True:
            if dst_pos + length > out_len:
                raise DwgDecodeError(
                    "R2007 decompression: back-reference past output end",
                    section="LZ77/R2007",
                )
            if offset > dst_pos:
                raise DwgDecodeError(
                    f"R2007 decompression: back-reference offset {offset} "
                    f"underflows at {dst_pos}",
                    section="LZ77/R2007",
                )
            dst_pos = _r2007_copy_bytes(dst, dst_pos, length, offset)

            length = opcode & 7
            if length != 0 or pos >= src_end:
                break
            opcode = src[pos]
            pos += 1
            if (opcode >> 4) == 0:
                break
            if (opcode >> 4) == 0x0F:
                opcode &= 0xF
            opcode, offset, length, pos = _r2007_read_instructions(
                src, pos, src_end, opcode
            )
    return bytes(dst)


def page_size_if_rs_coded(size_comp: int) -> int:
    """``page_size_if_rs_coded`` (``decode_r2007.c``).

    The on-disk size a Reed-Solomon coded page of ``size_comp`` payload bytes
    would have.  A page record carries no "is RS coded" flag, so its size is
    the only evidence: an exact match means the bytes in the file are
    codewords rather than payload.
    """
    pesize = (size_comp + 7) & ~7
    block_count = (pesize + _RS_K_DATA - 1) // _RS_K_DATA
    return (block_count * 255 + 31) & ~31


# ---------------------------------------------------------------------------
# R2004 container
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class SectionPage:
    """One page record of a section descriptor (``_section_page``)."""

    number: int = 0
    size: int = 0
    address: int = 0


@dataclass(slots=True)
class SectionInfo:
    """``Dwg_Section_Info`` — the R2004 descriptor for one section."""

    size: int = 0
    num_sections: int = 0
    max_decomp_size: int = 0
    unknown: int = 0
    compressed: int = 0            # 1 = stored, 2 = LZ77 compressed
    type: int = 0
    encrypted: int = 0
    name: str = ""
    fixedtype: int = 0             # ``type`` mapped through the name table
    pages: list[SectionPage] = field(default_factory=list)


@dataclass(slots=True)
class R2004Container:
    """The R2004 section page map, the section info, and the file image.

    :attr:`data` is the whole file, because R2004 page addresses are absolute
    file offsets.  Keeping it here rather than passing it to every function is
    the one place this module keeps state that upstream keeps in ``Dwg_Data``
    — and it is immutable, so it cannot be the source of the aliasing bugs the
    C's mutable ``dwg`` is.
    """

    data: bytes = b""
    sections: list[SectionRecord] = field(default_factory=list)
    infos: list[SectionInfo] = field(default_factory=list)
    version: DwgVersion = DwgVersion.R_2004
    problems: list[str] = field(default_factory=list)

    def by_number(self, number: int) -> SectionRecord | None:
        for rec in self.sections:
            if rec.number == number:
                return rec
        return None

    def info_for(self, sec_type: int) -> SectionInfo | None:
        for info in self.infos:
            if info.fixedtype == sec_type:
                return info
        return None

    def section(self, sec_type: int) -> bytes | None:
        """The decompressed bytes of a section, or ``None`` if absent.

        ``None`` — rather than an exception — for a section that is simply not
        in the file, which is routine: a drawing saved without a template has
        no TEMPLATE section.  A section that *is* described but cannot be
        assembled also returns ``None``, with the reason appended to
        :attr:`problems`, because upstream carries on and a partially readable
        drawing is worth more than none.
        """
        return read_2004_section(self, sec_type)


def _read_2004_section_map(
    data: bytes, hdr: R2004FileHeader
) -> tuple[list[SectionRecord], list[str]]:
    """``read_R2004_section_map`` — the "System Section" page map.

    The 20-byte header sits at ``section_map_address + 0x100`` and is followed
    by the LZ77-compressed map itself.  Both checksums are verified; a
    mismatch is recorded, never fatal.
    """
    problems: list[str] = []
    sections: list[SectionRecord] = []

    start = hdr.section_map_address + _R2004_MAP_HEADER_OFFSET
    if start > len(data):
        # Some files carry a section_map_address that overflows the file
        # (GH #617).  Upstream then walks backwards in 0x20 steps looking for
        # the magic; the same search here.
        problems.append(
            f"section_map_address 0x{hdr.section_map_address:X} is past the "
            "end of the file; searching backwards for the page map"
        )
        probe = len(data) & 0xFFFFFF00
        found = False
        while probe > 0x120:
            probe -= 0x20
            if probe + 4 > len(data):
                continue
            if struct.unpack_from("<I", data, probe)[0] == 0x41630E3B:
                start = probe
                found = True
                break
        if not found:
            raise DwgSectionError(
                "section page map not found (section_map_address "
                f"0x{hdr.section_map_address:X})",
                section="SECTION_PAGE_MAP",
            )

    if start + _R2004_MAP_HEADER_SIZE > len(data):
        raise DwgSectionError(
            f"section page map at 0x{start:X} runs past the end of the file "
            f"({len(data)} bytes)",
            section="SECTION_PAGE_MAP",
        )

    section_type, decomp_size, comp_size, _compression, checksum = \
        struct.unpack_from("<5I", data, start)
    if section_type != 0x41630E3B:
        raise DwgSectionError(
            f"invalid section page map magic 0x{section_type:X}, expected "
            "0x41630E3B",
            section="SECTION_PAGE_MAP",
        )
    if decomp_size > 0xFF000000:
        raise DwgSectionError(
            f"implausible section page map decompressed size {decomp_size}",
            section="SECTION_PAGE_MAP",
        )

    checksum1 = dwg_section_page_checksum(
        0, data, start, _R2004_MAP_HEADER_SIZE, True)
    body = start + _R2004_MAP_HEADER_SIZE
    checksum2 = dwg_section_page_checksum(checksum1, data, body, comp_size)
    if checksum2 != checksum:
        problems.append(
            f"section page map checksum mismatch (computed 0x{checksum2:08X}, "
            f"stored 0x{checksum:08X})"
        )
    if body + comp_size > len(data):
        raise DwgSectionError(
            "section page map body runs past the end of the file",
            section="SECTION_PAGE_MAP",
        )

    dec = bytearray(decomp_size)
    written = decompress_R2004_section(
        data, body, body + comp_size, dec, 0, decomp_size
    )

    # The map is a flat vector of {number, size} pairs, with addresses
    # accumulated from 0x100.  ``number`` is signed: a negative one marks a
    # gap, and the gap carries four more RLs (parent/left/right/x00) that must
    # be stepped over.  Note that the address advance test upstream is
    # ``number <= section_array_size``, which a negative number also passes.
    array_size = hdr.section_array_size
    address = _R2004_MAP_FIRST_ADDRESS
    pos = 0
    while pos + 8 <= written:
        number, size = struct.unpack_from("<iI", dec, pos)
        pos += 8
        sections.append(SectionRecord(number=number, size=size, address=address))
        if number <= array_size:
            address += size
        if number < 0 and pos + 16 <= written:
            pos += 16
        if len(sections) > _MAX_SECTIONS:
            problems.append(
                f"section page map has more than {_MAX_SECTIONS} entries"
            )
            break
    return sections, problems


def _read_2004_section_info(
    data: bytes, sections: list[SectionRecord], info_id: int
) -> tuple[list[SectionInfo], list[str]]:
    """``read_R2004_section_info`` — the "Data Section (Section Info)".

    The block at the info record's address opens with a 20-byte header
    ``{magic, decomp_size, comp_size, compression, checksum}``, then an
    LZ77-compressed body that itself starts with five RLs of counts, then one
    96-byte descriptor per section, then one 16-byte page record per page.
    """
    problems: list[str] = []
    infos: list[SectionInfo] = []

    info_rec = None
    for rec in sections:
        if rec.number == info_id:
            info_rec = rec
            break
    if info_rec is None:
        raise DwgSectionError(
            f"section info id {info_id} is not in the section page map",
            section="SECTION_INFO",
        )

    start = info_rec.address
    if start + _R2004_MAP_HEADER_SIZE > len(data):
        raise DwgSectionError(
            f"section info at 0x{start:X} runs past the end of the file "
            f"({len(data)} bytes)",
            section="SECTION_INFO",
        )
    section_type, decomp_size, comp_size, _compression, _checksum = \
        struct.unpack_from("<5I", data, start)
    if section_type != 0x4163003B:
        raise DwgSectionError(
            f"invalid section info magic 0x{section_type:X}, expected "
            "0x4163003B (section_info_id is wrong)",
            section="SECTION_INFO",
        )
    if decomp_size > _MAX_DECOMP_SECTION and (
        decomp_size > 8 * comp_size or comp_size > len(data)
    ):
        raise DwgSectionError(
            f"implausible section info decompressed size {decomp_size}",
            section="SECTION_INFO",
        )

    body = start + _R2004_MAP_HEADER_SIZE
    if body + comp_size > len(data):
        raise DwgSectionError("section info body runs past the end of the file",
                              section="SECTION_INFO")
    dec = bytearray(decomp_size)
    written = decompress_R2004_section(
        data, body, body + comp_size, dec, 0, decomp_size
    )
    if written < 20:
        raise DwgSectionError(
            f"section info decoded only {written} bytes", section="SECTION_INFO"
        )

    num_desc, _compressed, max_size, _encrypted, num_desc2 = \
        struct.unpack_from("<5I", dec, 0)
    if num_desc > _MAX_SECTIONS:
        raise DwgSectionError(
            f"illegal section info num_desc {num_desc}", section="SECTION_INFO"
        )
    if num_desc != num_desc2:
        problems.append(
            f"section info num_desc {num_desc} != num_desc2 {num_desc2}"
        )
    if max_size == 0:
        # Teigha writes 0 here; upstream substitutes the same default.
        max_size = 0x7400

    pos = 20
    for i in range(num_desc):
        # 8 (RLL size) + 6 x 4 (RL fields) + 64 (name)
        if pos + 96 >= written:
            problems.append(f"section info truncated at descriptor {i}")
            break
        size = struct.unpack_from("<q", dec, pos)[0]
        pos += 8
        (num_sec, max_decomp, unknown, compressed_flag, stype,
         encrypted_flag) = struct.unpack_from("<6I", dec, pos)
        pos += 24
        name = dec[pos:pos + 64].split(b"\x00", 1)[0].decode("latin-1")
        pos += 64

        info = SectionInfo(
            size=size,
            num_sections=num_sec,
            max_decomp_size=max_decomp,
            unknown=unknown,
            compressed=compressed_flag,
            type=stype,
            encrypted=encrypted_flag,
            name=name,
            fixedtype=section_type_from_name(name),
        )

        # A block size larger than the type allows means a corrupt descriptor;
        # upstream zeroes the section out rather than trusting the number.
        allowed = section_max_decomp_size(info.fixedtype, DwgVersion.R_2004)
        if info.max_decomp_size > allowed:
            problems.append(
                f"section {name}: max_decomp_size 0x{info.max_decomp_size:X} "
                f"> 0x{allowed:X}, skipping"
            )
            info.max_decomp_size = 0
            info.size = 0
            info.num_sections = 0
        if info.num_sections > 1 and info.size < info.max_decomp_size:
            # With several blocks the total must exceed one block, or the
            # sizes contradict each other.
            problems.append(
                f"section {name}: size {info.size} < max_decomp_size "
                f"{info.max_decomp_size}, skipping"
            )
            info.max_decomp_size = 0
            info.size = 0
            info.num_sections = 0

        if info.num_sections <= _MAX_PAGES:
            for _ in range(info.num_sections):
                if pos + 16 > written:
                    problems.append(f"section {name}: page records truncated")
                    break
                number, psize, paddress = struct.unpack_from("<iiq", dec, pos)
                pos += 16
                info.pages.append(
                    SectionPage(number=number, size=psize, address=paddress)
                )
        infos.append(info)
    return infos, problems


def read_r2004_sections(data: bytes, hdr: R2004FileHeader,
                        version: DwgVersion = DwgVersion.R_2004) -> R2004Container:
    """Read both R2004 section structures into an :class:`R2004Container`."""
    container = R2004Container(data=data, version=version)
    sections, problems = _read_2004_section_map(data, hdr)
    container.sections = sections
    container.problems.extend(problems)

    infos, problems = _read_2004_section_info(data, sections, hdr.section_info_id)
    container.infos = infos
    container.problems.extend(problems)
    return container


def read_2004_section(container: R2004Container, sec_type: int) -> bytes | None:
    """``read_2004_compressed_section`` — assemble one section from its pages.

    Each page is introduced by a 32-byte *encrypted section header*: eight
    little-endian 32-bit words XORed with ``0x4164536b ^ page_address``.  A page
    is either LZ77 compressed into a slot of ``max_decomp_size`` bytes at
    ``header.address``, or stored verbatim right after the header.

    Returns ``None`` when the section is absent, or when a page is damaged
    enough that continuing would mean fabricating data.
    """
    info = container.info_for(sec_type)
    if info is None:
        return None
    if info.num_sections == 0 or not info.pages:
        return None

    data = container.data
    capacity = info.num_sections * info.max_decomp_size
    if capacity <= 0 or capacity > _MAX_DECOMP_SECTION:
        container.problems.append(
            f"section {info.name}: invalid size "
            f"({info.num_sections} x {info.max_decomp_size})"
        )
        return None
    if info.size < 0 or info.size > capacity:
        container.problems.append(
            f"section {info.name}: size {info.size} exceeds {info.num_sections} "
            f"x {info.max_decomp_size}"
        )
        return None

    dec = bytearray(capacity)
    bytes_left = capacity
    filled = 0
    num_pages = len(info.pages)

    for i, page in enumerate(info.pages):
        rec = container.by_number(page.number)
        if rec is None:
            if i == num_pages - 1:
                # A missing *last* page makes the section unusable; a missing
                # interior page is skipped and the rest still read.
                container.problems.append(
                    f"section {info.name}: last page {i} (section "
                    f"{page.number}) is not in the section map"
                )
                return None
            container.problems.append(
                f"section {info.name}: page {i} (section {page.number}) is not "
                "in the section map"
            )
            continue

        address = rec.address
        if address + _R2004_PAGE_HEADER_SIZE > len(data):
            container.problems.append(
                f"section {info.name}: page {i} header at 0x{address:X} is past "
                "the end of the file"
            )
            return None

        words = struct.unpack_from("<8I", data, address)
        mask = 0x4164536B ^ address
        (page_type, _stype, data_size, page_size, start_offset,
         _unknown, _hdr_crc, _data_crc) = [w ^ mask for w in words]
        if page_type != 0x4163043B:
            container.problems.append(
                f"section {info.name}: page {i} has page_type "
                f"0x{page_type:X}, expected 0x4163043B"
            )

        payload = address + _R2004_PAGE_HEADER_SIZE
        if (
            info.compressed == 2
            and bytes_left > 0
            and start_offset <= capacity
            and start_offset + info.max_decomp_size <= capacity
        ):
            if payload + data_size > len(data):
                container.problems.append(
                    f"section {info.name}: page {i} compressed data runs past "
                    "the end of the file"
                )
                return None
            try:
                decompress_R2004_section(
                    data, payload, payload + data_size, dec, start_offset, capacity
                )
            except DwgDecodeError as exc:
                container.problems.append(f"section {info.name}: page {i} {exc}")
                return None
            bytes_left -= info.max_decomp_size
            filled = max(filled, start_offset + info.max_decomp_size)
        else:
            size = min(info.size - start_offset, page_size)
            if (
                info.compressed == 2
                or bytes_left < 0
                or start_offset > capacity
                or start_offset + size > capacity
                or size < 0
                or payload + size > len(data)
            ):
                container.problems.append(
                    f"section {info.name}: page {i} stored-page bounds "
                    f"(offset {start_offset}, size {size})"
                )
                return None
            dec[start_offset:start_offset + size] = data[payload:payload + size]
            bytes_left -= size
            filled = max(filled, start_offset + size)

    # ``sec_dat->size = info->size``: the tail past the last page is padding,
    # and truncating here stops a caller reading it as data.
    return bytes(dec[:info.size])


# ---------------------------------------------------------------------------
# R2007 container
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class R2007Page:
    """``Dwg_R2007_Page`` — one entry of the pages map."""

    size: int = 0
    id: int = 0
    offset: int = 0


@dataclass(slots=True)
class R2007SectionPage:
    """``Dwg_R2007_Section_Page`` — where one page of a section lives."""

    offset: int = 0          # offset into the decompressed section
    size: int = 0            # size on disk
    id: int = 0              # -> R2007Page.id
    uncomp_size: int = 0
    comp_size: int = 0
    checksum: int = 0
    crc: int = 0


@dataclass(slots=True)
class R2007Section:
    """``Dwg_R2007_Section`` — one section of the R2007 container."""

    data_size: int = 0
    max_size: int = 0
    encrypted: int = 0
    hashcode: int = 0
    name_length: int = 0
    unknown: int = 0
    encoded: int = 0
    num_pages: int = 0
    name: str = ""
    type: int = 0            # SectionType, derived from the name
    pages: list[R2007SectionPage] = field(default_factory=list)


@dataclass(slots=True)
class R2007Container:
    """The R2007 pages map, sections map, and the raw file image."""

    data: bytes = b""
    pages: list[R2007Page] = field(default_factory=list)
    sections: list[R2007Section] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def page_by_id(self, page_id: int) -> R2007Page | None:
        for page in self.pages:
            if page.id == page_id:
                return page
        return None

    def info_for(self, sec_type: int) -> R2007Section | None:
        for section in self.sections:
            if section.type == sec_type:
                return section
        return None

    def section(self, sec_type: int) -> bytes | None:
        """Assemble a section from its RS coded, compressed pages."""
        return read_data_section(self, sec_type)


def _read_system_page(
    data: bytes, pos: int, size_comp: int, size_uncomp: int, repeat_count: int
) -> tuple[bytes, int]:
    """``read_system_page`` — decode an RS coded map page.

    A *system page* (the pages map, the sections map, the R2007 file header) is
    ``repeat_count`` copies of a Reed-Solomon coded block: 239 payload bytes
    expanded to 255 codewords, padded to a multiple of 8.  It is LZ77
    compressed only when ``size_comp < size_uncomp``.

    Returns ``(decoded_bytes, bytes_consumed)``.
    """
    if not 0 <= repeat_count <= _DBG_MAX_COUNT:
        raise DwgDecodeError(
            f"invalid R2007 system page repeat_count {repeat_count}",
            section="R2007_PAGES_MAP",
        )
    if size_comp >= len(data) or size_uncomp >= len(data):
        raise DwgDecodeError(
            f"invalid R2007 system page sizes {size_comp}/{size_uncomp} for a "
            f"{len(data)}-byte file",
            section="R2007_PAGES_MAP",
        )

    pesize = ((size_comp + 7) & ~7) * repeat_count
    block_count = (pesize + _RS_K_SYSTEM - 1) // _RS_K_SYSTEM
    if not 0 < block_count <= _DBG_MAX_COUNT:
        raise DwgDecodeError(
            f"invalid R2007 system page block count {block_count}",
            section="R2007_PAGES_MAP",
        )
    page_size = (block_count * 255 + 7) & ~7
    if page_size >= _DBG_MAX_COUNT or pos + page_size > len(data):
        raise DwgDecodeError(
            f"R2007 system page of {page_size} bytes runs past the end of the "
            "file",
            section="R2007_PAGES_MAP",
        )

    pedata = decode_rs(data[pos:pos + page_size], block_count,
                       _RS_K_SYSTEM, page_size)
    pedata_size = block_count * _RS_K_SYSTEM
    if size_comp < size_uncomp:
        out = decompress_r2007(pedata, min(pedata_size, size_comp), size_uncomp)
    else:
        out = pedata[:size_uncomp] if size_uncomp <= len(pedata) else pedata
    return out, page_size


def _read_data_page(
    data: bytes, pos: int, page_size: int, size_comp: int, size_uncomp: int
) -> bytes:
    """``read_data_page`` — decode one RS coded data page.

    Data pages use (255, 251) Reed-Solomon rather than the system pages'
    (255, 239): a different codeword count for the same payload, so the two
    paths are not interchangeable.
    """
    pesize = (size_comp + 7) & ~7
    block_count = (pesize + _RS_K_DATA - 1) // _RS_K_DATA
    if not 0 < block_count <= _DBG_MAX_COUNT:
        raise DwgDecodeError(
            f"invalid R2007 data page block count {block_count}",
            section="R2007_DATA",
        )
    if pos + page_size > len(data):
        raise DwgDecodeError(
            "R2007 data page runs past the end of the file", section="R2007_DATA"
        )
    pedata = decode_rs(data[pos:pos + page_size], block_count,
                       _RS_K_DATA, page_size)
    pedata_size = block_count * _RS_K_DATA
    if size_comp < size_uncomp:
        return decompress_r2007(pedata, min(pedata_size, size_comp), size_uncomp)
    if size_uncomp > len(pedata):
        raise DwgDecodeError(
            "R2007 data page uncompressed size exceeds its decoded payload",
            section="R2007_DATA",
        )
    return pedata[:size_uncomp]


def read_data_section(container: R2007Container, sec_type: int) -> bytes | None:
    """``read_data_section`` — gather a section's pages into one buffer."""
    section = container.info_for(sec_type)
    if section is None:
        return None
    if section.data_size > _MAX_DECOMP_SECTION:
        raise DwgSectionError(
            f"section {section.name}: implausible size {section.data_size}",
            section=section.name or "R2007",
        )

    data = container.data
    decomp = bytearray(section.data_size)
    max_decomp_size = section.data_size

    for i, spage in enumerate(section.pages):
        page = container.page_by_id(spage.id)
        if page is None:
            container.problems.append(
                f"section {section.name}: page {i} id {spage.id} is not in the "
                "pages map"
            )
            return None
        if (
            spage.offset < 0
            or spage.uncomp_size < 0
            or spage.offset > max_decomp_size
            or spage.offset + spage.uncomp_size > max_decomp_size
        ):
            container.problems.append(
                f"section {section.name}: page {i} offset/size is out of bounds"
            )
            return None
        if page.offset < 0 or page.offset >= len(data):
            container.problems.append(
                f"section {section.name}: page {i} source offset "
                f"0x{page.offset:X} is out of bounds"
            )
            return None

        # RS coding is independent of compression: a page whose on-disk size
        # matches the RS coded form must go through the RS path even when it is
        # not compressed, or the caller receives payload interleaved with
        # parity bytes.
        if spage.comp_size != spage.uncomp_size or \
                page.size == page_size_if_rs_coded(spage.comp_size):
            chunk = _read_data_page(
                data, page.offset, page.size, spage.comp_size, spage.uncomp_size
            )
        else:
            if spage.uncomp_size > len(data) - page.offset:
                container.problems.append(
                    f"section {section.name}: page {i} stored data runs past "
                    "the end of the file"
                )
                return None
            chunk = data[page.offset:page.offset + spage.uncomp_size]

        decomp[spage.offset:spage.offset + len(chunk)] = chunk
    return bytes(decomp)


def _read_pages_map(
    data: bytes, pos: int, size_comp: int, size_uncomp: int, correction: int
) -> tuple[list[R2007Page], int]:
    """``read_pages_map`` — the ``{size, id}`` vector of every page on disk."""
    raw, consumed = _read_system_page(data, pos, size_comp, size_uncomp, correction)

    pages: list[R2007Page] = []
    offset = 0x480          # upstream seeds this constant, not a real offset
    p = 0
    while p + 16 <= size_uncomp and p + 16 <= len(raw):
        psize, pid = struct.unpack_from("<qq", raw, p)
        p += 16
        pages.append(R2007Page(size=psize, id=pid, offset=offset))
        offset += psize
        if len(pages) > _MAX_PAGES:
            raise DwgDecodeError("R2007 pages map is implausibly large",
                                 section="R2007_PAGES_MAP")
    return pages, consumed


def _read_sections_map(
    data: bytes, pos: int, size_comp: int, size_uncomp: int, correction: int
) -> tuple[list[R2007Section], int]:
    """``read_sections_map`` — every section descriptor and its pages."""
    raw, consumed = _read_system_page(data, pos, size_comp, size_uncomp, correction)

    sections: list[R2007Section] = []
    p = 0
    while p < size_uncomp and p + 64 <= len(raw):
        (data_size, max_size, encrypted, hashcode, name_length, unknown,
         encoded, num_pages) = struct.unpack_from("<8q", raw, p)
        p += 64

        # Sanity checks, verbatim from upstream; a failure here means this is
        # not the sections map at all, so it is fatal rather than skipped.
        if (
            data_size > 10 * len(data)
            or name_length < 0
            or name_length >= len(data)
            or name_length >= 48
        ):
            raise DwgDecodeError(
                f"invalid R2007 section descriptor (data_size {data_size}, "
                f"name_length {name_length})",
                section="R2007_SECTIONS_MAP",
            )

        section = R2007Section(
            data_size=data_size, max_size=max_size, encrypted=encrypted,
            hashcode=hashcode, name_length=name_length, unknown=unknown,
            encoded=encoded, num_pages=num_pages,
        )

        # The name length counts *bytes* of UTF-16, so it must be even.
        name_len = name_length + (name_length & 1)
        name_len = min(name_len, len(raw) - p)
        section.name = raw[p:p + name_len].decode(
            "utf-16-le", errors="replace").rstrip("\x00")
        p += name_len
        section.type = section_type_from_name(section.name)

        sections.append(section)
        if len(sections) > _MAX_SECTIONS:
            raise DwgDecodeError("R2007 sections map is implausibly large",
                                 section="R2007_SECTIONS_MAP")
        if p >= len(raw) or not 0 < num_pages <= _MAX_PAGES:
            section.num_pages = 0
            continue
        for _ in range(num_pages):
            if p + 56 > len(raw):
                break
            vals = struct.unpack_from("<7q", raw, p)
            p += 56
            section.pages.append(R2007SectionPage(
                offset=vals[0], size=vals[1], id=vals[2], uncomp_size=vals[3],
                comp_size=vals[4], checksum=vals[5], crc=vals[6],
            ))
    return sections, consumed


def read_r2007_sections(data: bytes, hdr: R2007FileHeader) -> R2007Container:
    """Read the R2007 pages map and sections map.

    ``read_r2007_meta_data`` in the C.  The pages map starts at
    ``0x80 + 0x28 + pages_map_offset``; the sections map is itself a page named
    by ``sections_map_id``, which is why the pages map has to be read first.
    """
    container = R2007Container(data=data)

    pos = (_R2007_HEADER_PAGE_ADDRESS + _R2007_HEADER_PAGE_SIZE
           + _R2007_POST_HEADER_SKIP + hdr.pages_map_offset)
    if pos < 0 or hdr.pages_map_size_comp > len(data) - pos:
        raise DwgSectionError(
            f"R2007 pages map ({hdr.pages_map_size_comp} bytes) runs past the "
            f"end of the file at 0x{pos:X}",
            section="R2007_PAGES_MAP",
        )
    pages, _ = _read_pages_map(
        data, pos, hdr.pages_map_size_comp, hdr.pages_map_size_uncomp,
        hdr.pages_map_correction,
    )
    container.pages = pages

    smap_page = container.page_by_id(hdr.sections_map_id)
    if smap_page is None:
        raise DwgSectionError(
            f"R2007 sections map page id {hdr.sections_map_id} was not found "
            "in the pages map",
            section="R2007_SECTIONS_MAP",
        )
    pos = smap_page.offset
    if pos < 0 or hdr.sections_map_size_comp > len(data) - pos:
        raise DwgSectionError(
            f"R2007 sections map ({hdr.sections_map_size_comp} bytes) runs past "
            f"the end of the file at 0x{pos:X}",
            section="R2007_SECTIONS_MAP",
        )
    sections, _ = _read_sections_map(
        data, pos, hdr.sections_map_size_comp, hdr.sections_map_size_uncomp,
        hdr.sections_map_correction,
    )
    container.sections = sections
    return container


# ---------------------------------------------------------------------------
# HANDLES — the object map
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ObjectAddress:
    """One entry of the object map.

    :attr:`address` is where the object's ``MS`` size field sits, *in the
    objects-section buffer* — the whole file image for R13–R2000, the
    decompressed ``AcDb:AcDbObjects`` section for R2004+.  :attr:`handle` is
    the UMC delta from the previous entry, i.e. how many handles were deleted
    here.
    """

    address: int = 0
    handle: int = 0
    order: int = 0


def read_object_map(
    data: bytes, *, start: int, end: int, page_limit: int = _PAGE_LIMIT_R13
) -> list[ObjectAddress]:
    """Read the HANDLES section — the object map — into a list of addresses.

    The layout is the same from R13 to R2018: a sequence of *pages*, each
    introduced by a big-endian ``RS`` page size and closed by a CRC-16.  Inside
    a page each entry is ``{UMC handle delta, MC address delta}``, and the
    running address total is where the object lives.

    Versions differ only in the page-size ceiling (2040 for R13–R2004, 2050 for
    R2007+) and in how much of upstream's recovery logic runs.  None of that
    recovery changes the bytes read — it only picks which warnings to emit — so
    one implementation covers all of them.

    ``start``/``end`` bound the scan: absolute file offsets for R13–R2000,
    where old-style sections are addressed in the whole file; ``0`` and the
    decompressed section length for R2004+.
    """
    out: list[ObjectAddress] = []
    end = min(end, len(data))
    start = max(0, start)

    # One reader for the whole map: the entries are read at the *bit* level
    # (UMC and MC are bit-packed), and the C keeps a single chain whose
    # ``byte``/``bit`` carry across entries and pages.  A fresh reader per
    # entry would silently byte-align every entry — and would also allocate
    # once per object, which on a large drawing is most of the runtime.
    rd = BitReader(data, version=DwgVersion.R_13b1, strict=False)
    rd.seek_bytes(start)

    while rd.byte + 2 <= end:
        startpos = rd.byte
        section_size = struct.unpack_from(">H", data, startpos)[0]
        rd.byte += 2
        if section_size > page_limit:
            raise DwgSectionError(
                f"object map page size {section_size} > {page_limit}",
                section="HANDLES",
            )
        # ``last_offset`` resets per page: the first MC of each page is an
        # absolute address, and the ones after it are deltas from it.
        last_offset = 0
        oldpos = 0

        while rd.byte - startpos < section_size:
            oldpos = rd.byte
            handleoff = rd.read_umc()
            offset = rd.read_mc()
            if rd.byte == oldpos:
                break                    # no progress: the page is corrupt
            last_offset += offset
            if len(out) >= _MAX_OBJECTS:
                raise DwgSectionError(
                    f"object map has more than {_MAX_OBJECTS} entries",
                    section="HANDLES",
                )
            # A nonsensical address is recorded rather than dropped: upstream
            # keeps the entry and lets the object decode fail on it, and the
            # entry count is part of what a caller compares against libredwg.
            out.append(ObjectAddress(
                address=last_offset, handle=handleoff, order=len(out)
            ))

        if rd.byte == oldpos:
            break

        # Align, then skip the page CRC-16.  The CRC covers the page's size
        # field and entries; a mismatch is advisory only — upstream warns for
        # every R14 file and decodes anyway.
        if rd.bit:
            rd.byte += 1
            rd.bit = 0
        if rd.byte + 2 > end:
            break
        rd.byte += 2                     # the RS_BE page CRC
        if section_size <= 2:
            break
    return out


# ---------------------------------------------------------------------------
# CLASSES
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class DwgClass:
    """``Dwg_Class`` — one record of the CLASSES section.

    The class number is not an object type: an object whose type code equals
    :attr:`number` is an instance of this class.  :attr:`item_class_id` says
    whether that is an entity (0x1F2) or an object (0x1F3), which is what the
    decoder consults for every type code of 500 or more.
    """

    number: int = 0
    proxyflag: int = 0
    appname: str = ""
    cppname: str = ""
    dxfname: str = ""
    is_zombie: int = 0
    item_class_id: int = 0
    num_instances: int = 0
    dwg_version: int = 0
    maint_version: int = 0
    unknown_1: int = 0
    unknown_2: int = 0

    @property
    def is_entity(self) -> bool:
        """``dwg_class_is_entity``: 0x1F2 is an entity, 0x1F3 an object."""
        return self.item_class_id == int(ObjectType.ACAD_PROXY_ENTITY)


def _has_bitsize_hi(version: DwgVersion, maint_version: int) -> bool:
    """Whether a section header carries a ``bitsize_hi`` before its ``bitsize``.

    Two upstream code paths disagree, and the difference is load-bearing:

    * The R2007 container goes through ``read_2007_section_header``, where the
      ``bitsize_hi`` read sits in a commented-out block — it is *never* read,
      whatever the maintenance version says.
    * The R2004 container (which R2010 and later also use) reads it from
      R2010.3 onward, and unconditionally for R2018.

    Getting this wrong shifts the whole string trailer by four bytes, which is
    how a bogus ``data_size`` turns into a negative seek.
    """
    if version < DwgVersion.R_2010:
        return False
    return maint_version > 3 or version >= DwgVersion.R_2018


def position_string_stream(
    rd: BitReader, *, test_position: int, data_size_position: int,
    fallback: int,
) -> bool:
    """``section_string_stream`` — position a reader on a string sub-stream.

    The trailer, read backwards from the end of a value stream, is::

        [string data][data_size (RS, maybe + hi RS)][has-strings bit]

    ``test_position`` is where that flag bit lives, ``data_size_position``
    where the length lives, and ``fallback`` where to leave the reader when
    there are no strings.  Returns whether strings are present.

    All three are absolute positions, which is how the C computes them too
    (``start`` is decremented and then handed to ``bit_set_position``) — the
    reader has no backward advance to abuse.
    """
    rd.seek_bits(test_position)
    if not rd.read_bit():
        rd.seek_bits(fallback)
        return False
    rd.seek_bits(data_size_position)
    data_size = rd.read_rs()
    if data_size & 0x8000:
        data_size_position -= 16
        rd.seek_bits(data_size_position)
        data_size = (data_size & 0x7FFF) | (rd.read_rs() << 15)
    rd.seek_bits(data_size_position - data_size)
    return True


def decode_classes(
    payload: bytes, *, version: DwgVersion, maint_version: int = 0,
    codepage: int = 0,
) -> list[DwgClass]:
    """Decode the CLASSES section (``read_2004_section_classes``).

    The section opens with the class-begin sentinel, then a size, an optional
    handle-stream size (R2010.3+ and R2018), a bit size (R2007+), the maximum
    class number, and three fixed bytes.  Class records follow; from R2007
    their three names live in the separate string stream.
    """
    start = find_sentinel(payload, SENTINEL_CLASS_BEGIN)
    if start < 0:
        raise DwgSectionError("class section sentinel not found",
                              section="CLASSES")

    rd = BitReader(payload, version=version, from_version=version,
                   codepage=codepage, strict=False)
    rd.seek_bytes(start)
    size = rd.read_rl()
    hsize_present = _has_bitsize_hi(version, maint_version)
    if hsize_present:
        rd.read_rl()                      # hsize
    bitsize = rd.read_rl() if version >= DwgVersion.R_2007 else 0
    max_num = rd.read_bs()
    rd.read_rc()                          # always 0
    rd.read_rc()                          # always 0
    rd.read_bit()                         # always 1

    if max_num < 500:
        raise DwgDecodeError(
            f"invalid maximum class number {max_num}", section="CLASSES"
        )
    num_classes = max_num - 499
    # 144 is ``sizeof(Dwg_Class)`` upstream; the ratio bounds the count against
    # the declared section size so a corrupt max_num cannot ask for a huge list.
    if num_classes > _MAX_CLASSES or num_classes > 100 + (size // 144):
        raise DwgDecodeError(
            f"implausible class count {num_classes} (max_num {max_num})",
            section="CLASSES",
        )

    str_rd: BitReader | None = None
    if version >= DwgVersion.R_2007:
        # The trailer sits just past the section header: 16 sentinel bytes plus
        # the 4-byte size, plus another 4 for ``hsize`` when one was read.
        start_bit = bitsize + (191 if hsize_present else 159)
        str_rd = BitReader(payload, version=version, from_version=version,
                           codepage=codepage, strict=False)
        position_string_stream(
            str_rd,
            test_position=start_bit,
            data_size_position=start_bit - 16,
            fallback=start_bit,
        )

    classes: list[DwgClass] = []
    for _ in range(num_classes):
        cls = DwgClass(number=rd.read_bs(), proxyflag=rd.read_bs())
        if str_rd is not None:
            cls.appname = str_rd.read_tu()
            cls.cppname = str_rd.read_tu()
            cls.dxfname = str_rd.read_tu()
        else:
            cls.appname = rd.read_tv()
            cls.cppname = rd.read_tv()
            cls.dxfname = rd.read_tv()
        cls.is_zombie = rd.read_bit()
        cls.item_class_id = rd.read_bs()
        cls.num_instances = rd.read_bl()
        cls.dwg_version = rd.read_bs()
        cls.maint_version = rd.read_bs()
        # Read twice into two fields, exactly as dwg.spec does.
        cls.unknown_1 = rd.read_bl()
        cls.unknown_2 = rd.read_bl()
        classes.append(cls)
    return classes


# ---------------------------------------------------------------------------
# HEADER section
# ---------------------------------------------------------------------------
def read_header_section(
    payload: bytes, *, version: DwgVersion, maint_version: int = 0,
    codepage: int = 0,
) -> HeaderVariables:
    """Decode the HEADER section (``read_2004_section_header`` / ``_2007_``).

    After the variable-begin sentinel come an ``RL`` size, then — for R2007+
    only, and only from R2010.3 — handle sizes, then a bit size.  From R2007 the
    header variables are split across three streams: values, handles and
    strings.  Before R2007 all three are the one stream.
    """
    start = find_sentinel(payload, SENTINEL_VARIABLE_BEGIN)
    if start < 0:
        raise DwgSectionError("header variable sentinel not found",
                              section="HEADER")

    # ``endbits`` upstream is the hardcoded absolute bit 160 — 16 sentinel bytes
    # plus a 4-byte size, doubled.  Deriving it from the sentinel position is
    # the same number for a well-formed section, and stays correct if the
    # sentinel were ever not at offset 0.
    base_bits = start * 8
    if base_bits + 32 > len(payload) * 8:
        raise DwgSectionError("header section is too short", section="HEADER")

    dat = BitReader(payload, version=version, from_version=version,
                    codepage=codepage, strict=False)
    dat.seek_bits(base_bits)
    dat.read_rl()                          # header_vars.size

    if version < DwgVersion.R_2007a:
        # One stream for everything: all three readers are the same object,
        # which is exactly what the C passes in.
        return decode_header_variables(dat, dat, dat)

    endbits = base_bits + 32
    if _has_bitsize_hi(version, maint_version):
        dat.read_rl()                      # bitsize_hi
        endbits += 32
    bitsize = dat.read_rl()

    hdl = dat.clone(payload)
    hdl.seek_bits(endbits + bitsize)
    strd = dat.clone(payload)
    position_string_stream(
        strd,
        test_position=endbits - 1 + bitsize,
        data_size_position=endbits - 17 + bitsize,
        fallback=endbits - 1 + bitsize,
    )
    return decode_header_variables(dat, hdl, strd)


# ---------------------------------------------------------------------------
# Strings
# ---------------------------------------------------------------------------
#: ``obj_has_strings`` — which types carry a separate string stream from R2007.
#: Types not listed default to *true*, matching upstream, and any type of 500 or
#: more is treated as having strings before the lookup is consulted
#: (``obj->type >= 500 || obj_has_strings (obj->type)``).
_STRINGS_BY_TYPE: Final[dict[int, bool]] = {
    int(ObjectType.TEXT): True,
    int(ObjectType.ATTRIB): True,
    int(ObjectType.ATTDEF): True,
    int(ObjectType.BLOCK): True,
    int(ObjectType.ENDBLK): False,
    int(ObjectType.SEQEND): False,
    int(ObjectType.INSERT): False,
    int(ObjectType.MINSERT): False,
    int(ObjectType.VERTEX_2D): False,
    int(ObjectType.VERTEX_3D): False,
    int(ObjectType.VERTEX_MESH): False,
    int(ObjectType.VERTEX_PFACE): False,
    int(ObjectType.VERTEX_PFACE_FACE): False,
    int(ObjectType.POLYLINE_2D): False,
    int(ObjectType.POLYLINE_3D): False,
    int(ObjectType.ARC): False,
    int(ObjectType.CIRCLE): False,
    int(ObjectType.LINE): False,
    int(ObjectType.POINT): False,
    int(ObjectType._3DFACE): False,
    int(ObjectType.POLYLINE_PFACE): False,
    int(ObjectType.POLYLINE_MESH): False,
    int(ObjectType.SOLID): False,
    int(ObjectType.TRACE): False,
    int(ObjectType.SHAPE): False,
    int(ObjectType.VIEWPORT): True,
    int(ObjectType.ELLIPSE): False,
    int(ObjectType.SPLINE): False,
    int(ObjectType.REGION): True,
    int(ObjectType._3DSOLID): True,
    int(ObjectType.BODY): True,
    int(ObjectType.RAY): False,
    int(ObjectType.XLINE): False,
    int(ObjectType.DICTIONARY): True,
    int(ObjectType.OLEFRAME): True,
    int(ObjectType.MTEXT): True,
    int(ObjectType.LEADER): True,
    int(ObjectType.TOLERANCE): True,
    int(ObjectType.MLINE): False,
    int(ObjectType.GROUP): True,
    int(ObjectType.MLINESTYLE): True,
    int(ObjectType.OLE2FRAME): True,
    int(ObjectType.DUMMY): False,
    int(ObjectType.LONG_TRANSACTION): False,
    int(ObjectType.LWPOLYLINE): False,
    int(ObjectType.HATCH): True,
    int(ObjectType.XRECORD): True,
    int(ObjectType.PLACEHOLDER): False,
    int(ObjectType.BLOCK_HEADER): True,
    int(ObjectType.LAYER): True,
    int(ObjectType.STYLE): True,
    int(ObjectType.LTYPE): True,
    int(ObjectType.VIEW): True,
    int(ObjectType.UCS): True,
    int(ObjectType.VPORT): True,
    int(ObjectType.APPID): True,
    int(ObjectType.DIMSTYLE): True,
    int(ObjectType.VX_TABLE_RECORD): True,
}

#: The ``*_CONTROL`` types (``dwg_obj_is_control``).  A control object has no
#: owner / reactors / extension-dictionary block, because it *is* the table.
_CONTROL_TYPES: Final[frozenset[int]] = frozenset({
    int(ObjectType.BLOCK_CONTROL),
    int(ObjectType.LAYER_CONTROL),
    int(ObjectType.STYLE_CONTROL),
    int(ObjectType.LTYPE_CONTROL),
    int(ObjectType.VIEW_CONTROL),
    int(ObjectType.UCS_CONTROL),
    int(ObjectType.VPORT_CONTROL),
    int(ObjectType.APPID_CONTROL),
    int(ObjectType.DIMSTYLE_CONTROL),
    int(ObjectType.VX_CONTROL),
})


def obj_has_strings(type_code: int) -> bool:
    """``obj_has_strings`` — does this type have a separate string stream?

    Optimistic above 500, exactly as upstream is: a class-defined type gets the
    benefit of the doubt, because guessing wrong there would silently shift
    every subsequent field.
    """
    if type_code >= 500:
        return True
    return _STRINGS_BY_TYPE.get(type_code, True)


# ---------------------------------------------------------------------------
# The object decoder
# ---------------------------------------------------------------------------
class ObjectState:
    """Per-object decode state: the three sub-streams and the object header.

    LibreDWG passes ``dat``, ``hdl_dat`` and ``str_dat`` as three ``Bit_Chain``
    pointers aliasing one buffer at three offsets, then moves between them with
    ``bit_reset_chain`` and pointer arithmetic.  Here they are three
    :class:`BitReader` instances over the same ``bytes`` at *absolute* bit
    offsets — the same three positions, but a mistake in one stream cannot
    silently move another.

    Attributes
    ----------
    dat:
        The value stream, positioned at the first field after the type code.
    hdl:
        The handle stream, positioned by :meth:`start_handle_stream`.
    str:
        The string stream, positioned by :meth:`start_string_stream`.
    extra:
        Values the common-data pass derives that only exist to decide which
        handles follow (``entmode``, ``num_reactors``, the flag bits).  They are
        not part of any decoded object, so they live here rather than being
        invented as fields on the dataclasses.
    """

    __slots__ = (
        "data", "version", "address", "size", "data_start", "type_code",
        "bitsize", "handlestream_size", "stringstream_size", "has_strings",
        "handle", "dat", "hdl", "str", "problems", "extra",
    )

    def __init__(self, data: bytes, version: DwgVersion, address: int,
                 size: int, data_start: int, type_code: int,
                 codepage: int = 0) -> None:
        self.data = data
        self.version = version
        self.address = address
        self.size = size
        self.data_start = data_start
        self.type_code = type_code
        self.bitsize = 0
        self.handlestream_size = 0
        self.stringstream_size = 0
        self.has_strings = 0
        self.handle = Handle()
        self.problems: list[str] = []
        self.extra: dict[str, int] = {}
        self.dat = BitReader(data, version=version, from_version=version,
                             codepage=codepage, strict=False)
        self.hdl = BitReader(data, version=version, from_version=version,
                             codepage=codepage, strict=False)
        self.str = BitReader(data, version=version, from_version=version,
                             codepage=codepage, strict=False)
        self.dat.seek_bits(data_start * 8)
        self.hdl.seek_bits(data_start * 8)
        self.str.seek_bits(data_start * 8)

    # -- stream positions, all absolute ---------------------------------
    @property
    def bitsize_abs(self) -> int:
        """Absolute bit offset where ``obj->bitsize`` points."""
        return self.data_start * 8 + self.bitsize

    def start_handle_stream(self) -> None:
        """``obj_handle_stream`` — point the handle reader at the handle stream.

        ``obj->bitsize`` counts value-stream bits from the start of the object
        data, so the handle stream begins exactly there and runs to the end of
        the declared object size.  A ``handlestream_size`` already set by the
        R2010+ object header is left alone, because it is authoritative.
        """
        self.hdl.seek_bits(self.bitsize_abs)
        if not self.handlestream_size:
            self.handlestream_size = (self.size * 8) - self.bitsize
        if self.handlestream_size < 0:
            self.problems.append(
                f"negative handle stream size {self.handlestream_size}"
            )
            self.handlestream_size = 0

    def start_string_stream(self) -> None:
        """``obj_string_stream`` — locate the string stream from the trailer.

        The trailer, backwards from the end of the value stream, is
        ``[string data][data_size RS (+hi RS)][has_strings bit]``.  Because
        ``bitsize`` counts from the object data start, every position here is
        relative to :attr:`bitsize_abs`.

        The C walks this with ``bit_advance_position`` and ``byte -= 2/4`` and
        ends with ``bit_advance_position (str, -data_size)`` — a *bit* advance
        of a value the comment calls bytes.  Whatever the unit, the CLASSES and
        HEADER readers use the same convention, so it is reproduced exactly
        rather than "corrected".
        """
        end = self.bitsize_abs
        if end < 1:
            return
        self.str.seek_bits(end - 1)
        self.has_strings = self.str.read_bit()
        if not self.has_strings:
            return

        pos = end - 17
        if pos < 0:
            self.has_strings = 0
            return
        self.str.seek_bits(pos)
        data_size = self.str.read_rs()
        data_end = pos
        if data_size & 0x8000:
            pos = end - 33
            if pos < 0:
                self.has_strings = 0
                return
            self.str.seek_bits(pos)
            data_size = (data_size & 0x7FFF) | (self.str.read_rs() << 15)
            data_end = pos

        if data_size > self.bitsize:
            self.problems.append(
                f"string stream data_size {data_size} > bitsize {self.bitsize}"
            )
            # Upstream returns here *before* the seek below, leaving the reader
            # sitting on the trailer.  For exactly R_2007 it also leaves
            # has_strings set, so the caller will go on to read strings from
            # that position; every other version starts reading inline instead.
            self.str.seek_bits(max(0, data_end))
            if self.version != DwgVersion.R_2007:
                self.has_strings = 0
            return
        self.stringstream_size = data_size
        if data_size < self.bitsize:
            self.str.seek_bits(max(0, data_end - data_size))
        else:
            self.str.seek_bits(0)

    def skip_eed(self) -> None:
        """``dwg_decode_eed`` — step over the extended entity data block.

        Each record is ``BS size``, an APPID handle, then ``size`` bytes; a zero
        size terminates.  Nothing downstream needs the payload and LibreDWG
        itself only keeps it raw, so the records are stepped over rather than
        materialised.

        The C skips with ``dat->byte = end``, which assigns the *byte* and
        leaves ``dat->bit`` untouched — so the jump is relative to the current
        bit position, not to the enclosing byte.  Advancing with ``seek_bytes``
        here would silently drop that sub-byte offset and misread everything
        after the EED, so the arithmetic is done in bits.
        """
        dat = self.dat
        limit = len(self.data) * 8
        while True:
            size = dat.read_bs()
            if not size:
                break
            if size > self.size:
                self.problems.append(
                    f"invalid EED size {size} > object size {self.size}"
                )
                break
            dat.read_h()                     # the APPID handle
            end = dat.byte * 8 + dat.bit + size * 8
            if end > limit:
                break
            dat.seek_bits(end)

    def absolute(self, handle: Handle) -> int:
        """Resolve a handle against this object's own handle.

        Translation of ``dwg_decode_handleref``: codes 0-5 carry the value
        outright, 6/8/10/12/14 are offsets from the containing object, and the
        rest are invalid and resolve to 0.
        """
        return handle.to_absolute(self.handle.value)

    def read_text(self) -> str:
        """``FIELD_T`` — a text value, from whichever stream holds it.

        Before R2007 every string is inline in the value stream.  From R2007
        the strings live in their own stream, and a type with no strings has
        none at all (``FIELD_T`` yields an empty string rather than reading).
        """
        if self.version < DwgVersion.R_2007:
            return self.dat.read_tv()
        if self.has_strings:
            return self.str.read_tu()
        return ""


class ObjectDecoder:
    """Decode the objects of one objects-section buffer.

    Parameters
    ----------
    data:
        The objects-section bytes — the whole file image for R13–R2000, or the
        decompressed ``AcDb:AcDbObjects`` section for R2004+.  The addresses
        from :func:`read_object_map` index this buffer.
    version:
        The ``from_version``, R13 through R2018.  Every branch the decoder takes
        is driven by it, exactly as ``dat->from_version`` drives the C.
    classes:
        The CLASSES section, consulted for type codes of 500 and above.
    strict:
        When true, a per-object failure propagates.  When false — the default,
        and what :func:`decode_objects` uses — a failed object becomes an
        :class:`~py_libredwg.objects.base_object.UnknownObject` carrying its raw
        bytes, so one bad object cannot cost the whole drawing.
    codepage:
        The file's ``$DWGCODEPAGE`` number, from the file header.  Only
        consulted before R2007: those files store text as single bytes, and a
        Chinese drawing read with the wrong codepage gives mojibake layer
        names that look like data corruption but are not — the bytes are
        correct, the decoding is not.  R2007+ stores UCS-2 and ignores it.
    """

    __slots__ = ("data", "version", "classes", "strict", "problems",
                 "codepage")

    def __init__(self, data: bytes, *, version: DwgVersion,
                 classes: list[DwgClass] | None = None,
                 strict: bool = False, codepage: int = 0) -> None:
        self.data = data
        self.version = version
        self.classes = classes or []
        self.strict = strict
        self.problems: list[str] = []
        self.codepage = codepage

    # -- entry point -----------------------------------------------------
    def decode(self, address: int) -> DwgObject:
        """Decode the object whose ``MS`` size field sits at ``address``."""
        try:
            return self._decode(address)
        except (DwgDecodeError, BitStreamError) as exc:
            if self.strict:
                raise
            return self._raw_fallback(address, str(exc))

    def _raw_fallback(self, address: int, why: str) -> DwgObject:
        """A placeholder for an object that could not be decoded.

        Distinct from :class:`~py_libredwg.objects.base_object.ProxyEntity` on
        purpose: a proxy's body is *undefined* to us, whereas this body is
        *defined but unreadable*.  Both keep their raw bytes, which is the
        honest thing to hand back either way.
        """
        size = 0
        if 0 <= address < len(self.data):
            probe = BitReader(self.data, strict=False)
            probe.seek_bytes(address)
            size = probe.read_ms()
        end = address + size if 0 < size <= _MAX_OBJECT_SIZE else address
        raw = self.data[address:min(end, len(self.data))]
        self.problems.append(f"object at 0x{address:X}: {why}")
        return UnknownObject(address=address, size=size, raw=raw)

    # -- object header ---------------------------------------------------
    def _decode(self, address: int) -> DwgObject:
        data = self.data
        if address < 0 or address + 2 > len(data):
            raise DwgDecodeError(
                f"object address 0x{address:X} is outside the objects buffer "
                f"({len(data)} bytes)",
                section="OBJECTS",
            )

        head = BitReader(data, version=self.version,
                         from_version=self.version, codepage=self.codepage,
                         strict=False)
        head.seek_bytes(address)
        size = head.read_ms()
        if size <= 0 or size > _MAX_OBJECT_SIZE:
            raise DwgDecodeError(
                f"invalid object size {size} at 0x{address:X}", section="OBJECTS"
            )
        if address + size > len(data):
            # Upstream tolerates a short final object by clipping; the tail is
            # padding, and refusing outright would drop the drawing's last
            # entity over a one-byte lie.
            self.problems.append(
                f"object at 0x{address:X} declares {size} bytes but only "
                f"{len(data) - address} remain"
            )

        bitsize = 0
        handlestream_size = 0
        if self.version >= DwgVersion.R_2010b:
            # R2010 moved the handle-stream size out of the value stream into
            # the object header, which is what makes `bitsize` derivable
            # rather than stored.
            handlestream_size = head.read_umc()
            bitsize = size * 8 - handlestream_size
        if head.bit:
            self.problems.append(
                f"object at 0x{address:X}: header ends mid-byte"
            )

        data_start = head.byte
        rd = BitReader(data, version=self.version,
                       from_version=self.version, codepage=self.codepage,
                       strict=False)
        rd.seek_bits(data_start * 8)
        type_code = (rd.read_bot() if self.version >= DwgVersion.R_2010b
                     else rd.read_bs())

        ctx = ObjectState(data, self.version, address, size, data_start,
                          type_code, self.codepage)
        ctx.bitsize = bitsize
        ctx.handlestream_size = handlestream_size
        ctx.dat = rd

        obj = self._decode_body(ctx)
        obj.address = address
        obj.size = size
        obj.bit_size = ctx.bitsize
        obj.type_code = type_code
        obj.handle = ctx.handle.value
        if ctx.problems:
            self.problems.extend(
                f"object 0x{address:X}: {p}" for p in ctx.problems
            )
        return obj

    # -- dispatch --------------------------------------------------------
    def _decode_body(self, ctx: ObjectState) -> DwgObject:
        """Route to the type-specific decoder (``dwg_decode_add_object``).

        The C switch has a case for every type it knows.  This one has cases for
        the MVP set plus the three types needed to *reach* them — LAYER to
        resolve layer names, and BLOCK_HEADER/BLOCK_CONTROL to get from an
        entity's owner handle to a readable block name.  Everything else keeps
        its raw bytes: a proxy or an unknown, which is what the C does for a
        type it has no handler for.
        """
        type_code = ctx.type_code

        if entity_class_for(type_code) is not None:
            return self._decode_entity(ctx)
        if type_code == int(ObjectType.LAYER):
            return self._decode_layer(ctx)
        if type_code == int(ObjectType.BLOCK_HEADER):
            return self._decode_block_header(ctx)
        if type_code in _CONTROL_TYPES:
            return self._decode_control(ctx)
        if type_code in (int(ObjectType.ACAD_PROXY_ENTITY),
                         int(ObjectType.ACAD_PROXY_OBJECT)):
            return self._decode_proxy(ctx)
        if type_code >= 500:
            cls = self._class_for(type_code)
            if cls is not None and not cls.is_entity:
                # A class-defined *object*: keep it opaque but named, which is
                # what the MVP promises for everything out of scope.
                return self._decode_opaque(ctx, cls.cppname or cls.dxfname)
            return self._decode_proxy(ctx)
        return self._decode_unknown(ctx)

    def _class_for(self, type_code: int) -> DwgClass | None:
        """The class record for a type code — ``type - 500``, as upstream."""
        index = type_code - 500
        if 0 <= index < len(self.classes):
            return self.classes[index]
        return None

    # -- entities --------------------------------------------------------
    def _decode_entity(self, ctx: ObjectState) -> DwgObject:
        """``dwg_decode_entity`` plus the type body (the ``DWG_ENTITY`` macro).

        Order, exactly as upstream: version-dependent bitsize, handle stream,
        string stream (R2007+), the object's own handle, EED, the common entity
        data, the common entity *handle* data, then the type body.

        The handle data comes before the body even though it reads a different
        stream — LibreDWG does it that way because the body lives in the macro
        wrapping ``dwg_decode_entity``, and the two streams are independent, so
        the order is observationally irrelevant either way.
        """
        dat = ctx.dat
        obj = entity_class_for(ctx.type_code)()
        assert isinstance(obj, Entity)

        self._start_entity_streams(ctx)

        handle = dat.read_h()
        if not handle.value or not handle.size:
            raise DwgDecodeError(
                f"invalid object handle (code {handle.code}, size "
                f"{handle.size}) at 0x{ctx.address:X}",
                section="OBJECTS",
            )
        ctx.handle = handle

        ctx.skip_eed()

        self._read_common_entity_data(ctx, obj)
        self._read_common_entity_handle_data(ctx, obj)
        self._tolerant(ctx, self._read_entity_body, obj)
        return obj

    def _start_entity_streams(self, ctx: ObjectState) -> None:
        """Read the version-dependent bitsize and locate the sub-streams.

        Everything between the type code and the object's own handle, shared by
        every entity-shaped type — the five the MVP decodes and the proxies it
        does not.  It has to be shared: this is what fixes where the handle
        stream *starts*, and a type that skips it does not fail, it silently
        reads its handle out of whatever field happens to be in the way.

        The three layouts are:

        ``R2000-R2007``
            ``[type][bitsize RL][handle H][EED][common data]`` — the bitsize is
            on the wire, and ``obj_handle_stream`` is only called when it is
            sane (``dwg_decode_entity``'s ``has_wrong_bitsize`` path skips it).
        ``R2007a+``
            No bitsize in the value stream: for R2010b+ the object header
            already supplied ``handlestream_size``.  The string stream follows,
            but only for types that have one.
        ``R13/R14``
            Neither: the bitsize slides in *after* the preview block, which
            ``_read_common_entity_data`` reads later.  Left alone here.
        """
        dat = ctx.dat
        v = self.version

        if DwgVersion.R_2000 <= v <= DwgVersion.R_2007:
            ctx.bitsize = dat.read_rl()
            if ctx.bitsize > ctx.size * 8:
                ctx.problems.append(
                    f"bitsize {ctx.bitsize} > object size {ctx.size * 8}"
                )
                ctx.bitsize = ctx.size * 8
            else:
                ctx.start_handle_stream()

        if v >= DwgVersion.R_2007a:
            if v >= DwgVersion.R_2010b:
                ctx.start_handle_stream()
            if ctx.bitsize > ctx.size * 8:
                ctx.bitsize = ctx.size * 8
            if obj_has_strings(ctx.type_code):
                ctx.start_string_stream()

    def _tolerant(self, ctx: ObjectState, fn, *args) -> None:
        """Run a type body, keeping whatever was decoded before it failed.

        Upstream's ``RETURN_VALUEOUTOFBOUNDS`` aborts the *type body* but leaves
        the object in the document with the fields read so far.  Letting the
        error propagate all the way to :meth:`_raw_fallback` would throw away a
        perfectly usable entity over one corrupt trailing field, so errors are
        contained here — at the body, and only at the body.  A failure in the
        common data still fails the whole object, because everything after it
        would be misaligned anyway.
        """
        try:
            fn(ctx, *args)
        except (DwgDecodeError, BitStreamError) as exc:
            ctx.problems.append(f"type body truncated: {exc}")

    def _read_common_entity_data(self, ctx: ObjectState, obj: Entity) -> None:
        """``common_entity_data.spec`` — the part every entity shares."""
        dat = ctx.dat
        v = self.version

        if v >= DwgVersion.R_13b1:
            if dat.read_bit():                       # preview_exists
                # R13b1–R2007 store the size as an RL cast to BLL; R2010+ use a
                # real BLL.  Both then carry that many raw bytes.
                preview_size = (dat.read_bll() if v >= DwgVersion.R_2010b
                                else dat.read_rl())
                bound = ctx.size or len(ctx.data)
                # ``common_entity_data.spec`` guards the raw read with
                # ``0 <= preview_size && preview_size < obj->size`` and simply
                # *skips* the bytes when the test fails — it does not abort the
                # entity.  A few erased objects in the wild carry a preview
                # size that lands in freed space, and killing the whole entity
                # over a graphics thumbnail would be the wrong trade.
                if 0 <= preview_size < bound:
                    obj.proxy_graphics_size = preview_size
                    if preview_size:
                        dat.read_bytes(preview_size)
                else:
                    ctx.problems.append(
                        f"invalid preview_size {preview_size}, max {bound}"
                    )

        if DwgVersion.R_13b1 <= v <= DwgVersion.R_14:
            # R13/R14 slide the bitsize in here, after the preview block,
            # because the value stream has no room for it earlier.
            ctx.bitsize = dat.read_rl()
            if ctx.bitsize > ctx.size * 8:
                ctx.bitsize = ctx.size * 8
            else:
                ctx.start_handle_stream()

        ctx.extra["entmode"] = dat.read_2bits()
        num_reactors = dat.read_bl()
        ctx.extra["num_reactors"] = num_reactors

        if DwgVersion.R_13b1 <= v <= DwgVersion.R_14:
            isbylayerlt = dat.read_bit()
            obj.ltype_flags = 0 if isbylayerlt else 3
            ctx.extra["isbylayerlt"] = isbylayerlt

        if v >= DwgVersion.R_2004a:
            ctx.extra["is_xdic_missing"] = dat.read_bit()
        if DwgVersion.R_13b1 <= v <= DwgVersion.R_2002:
            ctx.extra["nolinks"] = dat.read_bit()
        if v >= DwgVersion.R_2013:
            ctx.extra["has_ds_data"] = dat.read_bit()

        obj.color = self._read_color(ctx)

        if v >= DwgVersion.R_13b1:
            obj.ltype_scale = dat.read_bd()

        if v >= DwgVersion.R_2000b:
            obj.ltype_flags = dat.read_2bits()
            ctx.extra["plotstyle_flags"] = dat.read_2bits()
        if v >= DwgVersion.R_2007a:
            ctx.extra["material_flags"] = dat.read_2bits()
            ctx.extra["shadow_flags"] = dat.read_rc()
        if v >= DwgVersion.R_2010b:
            ctx.extra["has_full_visualstyle"] = dat.read_bit()
            ctx.extra["has_face_visualstyle"] = dat.read_bit()
            ctx.extra["has_edge_visualstyle"] = dat.read_bit()

        if v >= DwgVersion.R_13b1:
            obj.invisible = dat.read_bs()
        if v >= DwgVersion.R_2000b:
            obj.lineweight = dat.read_rc()

    def _read_color(self, ctx: ObjectState) -> CMColor:
        """The entity colour: the R2004+ packed form, or the classic CMC.

        The packed form is worth spelling out because one of its fields is a
        handle, and that handle is read from the handle stream.  LibreDWG reads
        it twice — inline here *and* again at the top of
        ``dwg_decode_common_entity_handle_data`` — while the *encoder* only
        writes it once, from the deferred position.  A decoder that reads two
        handles where the encoder wrote one cannot round-trip, so this reads it
        once: at the deferred position, which is where the encoder puts it.
        The flag is remembered here and the handle taken in
        :meth:`_read_common_entity_handle_data`.
        """
        dat = ctx.dat
        v = self.version

        if v < DwgVersion.R_2004a:
            if v >= DwgVersion.R_13b1:
                return dat.read_cmc(ctx.str)
            color = CMColor()
            color.index = BYLAYER
            return color

        raw = dat.read_bs()                      # FIELD_BSx (color.raw)
        color = CMColor()
        color.flag = raw >> 8
        color.index = raw & 0x1FF
        color.rgb = 0
        ctx.extra["color_flag"] = color.flag

        if color.flag & 0x20:
            alpha_raw = dat.read_bl()
            # 0 BYLAYER, 1 BYBLOCK, 3 alpha
            color.alpha_type = alpha_raw >> 24
            color.alpha = alpha_raw & 0xFF
        if color.flag & 0x40:
            # The handle itself is read from the handle stream, in
            # _read_common_entity_handle_data.  Nothing else consumes that
            # stream before then, so reading it there is the same position as
            # reading it here — and it keeps the one stream in one place.
            #
            # Note the ``elif``: upstream reads the true colour only when the
            # colour is *not* a reference, so the two flags are exclusive.
            pass
        elif color.flag & 0x80:
            color.rgb = dat.read_bl()
        if (color.flag & 0x41) == 0x41:
            color.name = dat.read_tv()
        if (color.flag & 0x42) == 0x42:
            color.book_name = dat.read_tv()
        return color

    def _read_common_entity_handle_data(self, ctx: ObjectState,
                                        obj: Entity) -> None:
        """``common_entity_handle_data.spec`` — the entity's handle stream."""
        hdl = ctx.hdl
        v = self.version
        x = ctx.extra

        # The colour handle the value-stream pass deferred (see _read_color).
        if v >= DwgVersion.R_2004a and (x.get("color_flag", 0) & 0x40):
            obj.color.handle = hdl.read_h()

        if x.get("entmode", 0) == 0 and v >= DwgVersion.R_13b1:
            obj.owner_handle = hdl.read_h().value

        num_reactors = x.get("num_reactors", 0)
        if num_reactors > 100000:
            # A count that large cannot fit the stream; upstream zeroes it
            # rather than reading garbage handles.
            ctx.problems.append(f"num_reactors {num_reactors} is too large")
            num_reactors = 0
        obj.reactors = [hdl.read_h().value for _ in range(num_reactors)]

        if v >= DwgVersion.R_2004a:
            if not x.get("is_xdic_missing"):
                obj.xdic_handle = hdl.read_h().value
        elif v >= DwgVersion.R_13b1:
            obj.xdic_handle = hdl.read_h().value

        if DwgVersion.R_13b1 <= v <= DwgVersion.R_14:
            obj.layer_handle = hdl.read_h().value
            if not x.get("isbylayerlt"):
                obj.linetype_handle = hdl.read_h().value

        if DwgVersion.R_13b1 <= v <= DwgVersion.R_2000:
            if not x.get("nolinks"):
                hdl.read_h()                         # prev_entity
                hdl.read_h()                         # next_entity

        if v >= DwgVersion.R_2000b:
            obj.layer_handle = hdl.read_h().value
            if obj.ltype_flags == 3:
                obj.linetype_handle = hdl.read_h().value

        if v >= DwgVersion.R_2007a:
            if x.get("material_flags") == 3:
                obj.material_handle = hdl.read_h().value
            if x.get("shadow_flags") == 3:
                hdl.read_h()                         # shadow

        if v >= DwgVersion.R_2000b:
            if x.get("plotstyle_flags") == 3:
                obj.plotstyle_handle = hdl.read_h().value

        if v >= DwgVersion.R_2010b:
            for key in ("has_full_visualstyle", "has_face_visualstyle",
                        "has_edge_visualstyle"):
                if x.get(key):
                    hdl.read_h()

    def _read_entity_body(self, ctx: ObjectState, obj: Entity) -> None:
        """The type-specific value stream (the ``dwg.spec`` bodies)."""
        type_code = ctx.type_code
        if type_code == int(ObjectType.LINE):
            self._read_line(ctx, obj)
        elif type_code == int(ObjectType.CIRCLE):
            self._read_circle(ctx, obj)
        elif type_code == int(ObjectType.ARC):
            self._read_arc(ctx, obj)
        elif type_code == int(ObjectType.POINT):
            self._read_point(ctx, obj)
        elif type_code == int(ObjectType.LWPOLYLINE):
            self._read_lwpolyline(ctx, obj)

    def _read_line(self, ctx: ObjectState, obj: Entity) -> None:
        """``DWG_ENTITY (LINE)``.

        R13/R14 store two full 3D points.  R2000+ switched to a delta-encoded
        form: ``x`` is absolute and ``y``/``z`` default to it, plus a
        ``z_is_zero`` flag that skips the z pair entirely for the
        (overwhelmingly common) flat line.
        """
        from .objects.line import Line

        assert isinstance(obj, Line)
        dat = ctx.dat
        if DwgVersion.R_13b1 <= self.version <= DwgVersion.R_14:
            obj.start = Vec3(dat.read_bd(), dat.read_bd(), dat.read_bd())
            obj.end = Vec3(dat.read_bd(), dat.read_bd(), dat.read_bd())
        else:
            z_is_zero = dat.read_bit()
            sx = dat.read_rd()
            ex = dat.read_dd(sx)
            sy = dat.read_rd()
            ey = dat.read_dd(sy)
            if z_is_zero:
                sz = ez = 0.0
            else:
                sz = dat.read_rd()
                ez = dat.read_dd(sz)
            obj.start = Vec3(sx, sy, sz)
            obj.end = Vec3(ex, ey, ez)

        if self.version >= DwgVersion.R_13b1:
            obj.thickness = dat.read_bt()
            obj.extrusion = Vec3(*dat.read_be())

    def _read_circle(self, ctx: ObjectState, obj: Entity) -> None:
        """``DWG_ENTITY (CIRCLE)``: a 3BD centre and a BD radius."""
        from .objects.circle import Circle

        assert isinstance(obj, Circle)
        dat = ctx.dat
        obj.center = Vec3(dat.read_bd(), dat.read_bd(), dat.read_bd())
        obj.radius = dat.read_bd()
        if self.version >= DwgVersion.R_13b1:
            obj.thickness = dat.read_bt()
            obj.extrusion = Vec3(*dat.read_be())

    def _read_arc(self, ctx: ObjectState, obj: Entity) -> None:
        """``DWG_ENTITY (ARC)`` — a circle plus a start and end angle.

        ``FIELD_BD (start_angle, 50)`` / ``FIELD_BD (end_angle, 51)``, stored
        as plain doubles and **already in radians** — LibreDWG reads them with
        no conversion and the DXF writer is what turns them into the degrees
        group 50/51 carries.  Converting here would apply the degree->radian
        factor twice over.
        """
        from .objects.arc import Arc

        assert isinstance(obj, Arc)
        dat = ctx.dat
        obj.center = Vec3(dat.read_bd(), dat.read_bd(), dat.read_bd())
        obj.radius = dat.read_bd()
        if self.version >= DwgVersion.R_13b1:
            obj.thickness = dat.read_bt()
            obj.extrusion = Vec3(*dat.read_be())
        obj.start_angle = dat.read_bd()
        obj.end_angle = dat.read_bd()

    def _read_point(self, ctx: ObjectState, obj: Entity) -> None:
        """``DWG_ENTITY (POINT)``: ``BD x``, ``BD y``, ``BD z``, then the X axis
        angle of the OCS (``FIELD_BD (x_ang, 50)``, read as stored)."""
        from .objects.point import Point

        assert isinstance(obj, Point)
        dat = ctx.dat
        obj.location = Vec3(dat.read_bd(), dat.read_bd(), dat.read_bd())
        if self.version >= DwgVersion.R_13b1:
            obj.thickness = dat.read_bt()
            obj.extrusion = Vec3(*dat.read_be())
        obj.angle = dat.read_bd()

    def _read_lwpolyline(self, ctx: ObjectState, obj: Entity) -> None:
        """``DWG_ENTITY (LWPOLYLINE)`` — a flag-driven body.

        Four optional scalars, three optional counts, then the vertex array.
        From R2000 the vertices are delta encoded *pairwise*, each seeded from
        the previous vertex, which is why the first is a plain ``2RD`` and the
        rest are ``2DD``.
        """
        from .objects.lwpolyline import LWPolyline

        assert isinstance(obj, LWPolyline)
        dat = ctx.dat
        v = self.version

        flag = dat.read_bs()
        obj.flags = flag

        if flag & 4:
            obj.constant_width = dat.read_bd()
        if flag & 8:
            obj.elevation = dat.read_bd()
        if flag & 2:
            obj.thickness = dat.read_bd()
        if flag & 1:
            obj.extrusion = Vec3(dat.read_bd(), dat.read_bd(), dat.read_bd())

        num_points = dat.read_bl()
        if num_points > 20000:
            raise DwgDecodeError(
                f"LWPOLYLINE num_points {num_points} exceeds the 20000 limit",
                section="LWPOLYLINE",
            )

        num_bulges = dat.read_bl() if flag & 16 else 0
        num_vertexids = 0
        if v >= DwgVersion.R_2010b and flag & 1024:
            num_vertexids = dat.read_bl()
        num_widths = dat.read_bl() if flag & 32 else 0

        vertices: list[LWPolylineVertex] = []  # noqa: F821 - imported below
        if DwgVersion.R_13b1 <= v <= DwgVersion.R_14:
            for _ in range(num_points):
                vertices.append(_vertex(dat.read_rd(), dat.read_rd()))
        else:
            prev_x = prev_y = 0.0
            for i in range(num_points):
                if i == 0:
                    prev_x = dat.read_rd()
                    prev_y = dat.read_rd()
                else:
                    prev_x = dat.read_dd(prev_x)
                    prev_y = dat.read_dd(prev_y)
                vertices.append(_vertex(prev_x, prev_y))

        for i in range(num_bulges):
            bulge = dat.read_bd()
            if i < len(vertices):
                vertices[i].bulge = bulge
        for _ in range(num_vertexids):
            dat.read_bl()
        for i in range(num_widths):
            start_width = dat.read_bd()
            end_width = dat.read_bd()
            if i < len(vertices):
                vertices[i].start_width = start_width
                vertices[i].end_width = end_width

        obj.vertices = vertices

    # -- objects ---------------------------------------------------------
    def _decode_object_common(self, ctx: ObjectState, obj: DwgObject) -> None:
        """``dwg_decode_object`` — the common part of a non-entity object.

        Note what is *not* here: an object is finished by
        ``common_object_handle_data.spec``, whose value-stream fields
        (``num_reactors`` and friends) are read at the end of this method, and
        whose handle-stream fields (owner, reactors, extension dictionary) are
        read immediately after — still before the type body.
        """
        dat = ctx.dat
        v = self.version

        if DwgVersion.R_2000 <= v <= DwgVersion.R_2007:
            ctx.bitsize = dat.read_rl()
            if ctx.bitsize > ctx.size * 8:
                ctx.bitsize = ctx.size * 8
            else:
                ctx.start_handle_stream()

        if v >= DwgVersion.R_2007a:
            if v >= DwgVersion.R_2010b:
                ctx.start_handle_stream()
            if ctx.bitsize > ctx.size * 8:
                ctx.bitsize = ctx.size * 8
            if obj_has_strings(ctx.type_code):
                ctx.start_string_stream()

        handle = dat.read_h()
        if not handle.value or not handle.size or handle.code:
            raise DwgDecodeError(
                f"invalid object handle (code {handle.code}, size "
                f"{handle.size}) at 0x{ctx.address:X}",
                section="OBJECTS",
            )
        ctx.handle = handle

        ctx.skip_eed()

        if DwgVersion.R_13b1 <= v <= DwgVersion.R_14:
            ctx.bitsize = dat.read_rl()
            if ctx.bitsize > ctx.size * 8:
                ctx.bitsize = ctx.size * 8
            else:
                ctx.start_handle_stream()

        # -- common_object_handle_data.spec ------------------------------
        num_reactors = dat.read_bl() if v >= DwgVersion.R_13b1 else 0
        ctx.extra["num_reactors"] = num_reactors
        if v >= DwgVersion.R_2004a:
            ctx.extra["is_xdic_missing"] = dat.read_bit()
        if v >= DwgVersion.R_2013:
            ctx.extra["has_ds_data"] = dat.read_bit()

        if ctx.type_code not in _CONTROL_TYPES and v >= DwgVersion.R_13b1:
            obj.owner_handle = ctx.hdl.read_h().value
            if num_reactors > 100000:
                ctx.problems.append(f"num_reactors {num_reactors} is too large")
                num_reactors = 0
            obj.reactors = [ctx.hdl.read_h().value for _ in range(num_reactors)]
            if v >= DwgVersion.R_2004a:
                if not ctx.extra.get("is_xdic_missing"):
                    obj.xdic_handle = ctx.hdl.read_h().value
            else:
                obj.xdic_handle = ctx.hdl.read_h().value

    def _decode_layer(self, ctx: ObjectState) -> DwgObject:
        """``DWG_TABLE (LAYER)`` — a layer definition.

        The R2000 flag split shows most clearly here: the file stores one
        ``flag0`` bitmask, while DXF stores derived flags.  Only the bitmask is
        on the wire, so that is what is decoded; the DXF-style properties are
        derived from it exactly as ``dwg.spec`` does.
        """
        layer = Layer()
        self._decode_object_common(ctx, layer)
        self._tolerant(ctx, self._read_layer_body, layer)
        return layer

    def _read_layer_body(self, ctx: ObjectState, layer: Layer) -> None:
        dat = ctx.dat
        v = self.version

        # -- COMMON_TABLE_FLAGS(Layer) -----------------------------------
        layer.name = ctx.read_text()
        if v <= DwgVersion.R_2004:
            is_xref_ref = dat.read_bit()
            is_xref_resolved = dat.read_bs()
            is_xref_dep = dat.read_bit()
        else:
            is_xref_ref = 1
            is_xref_resolved = dat.read_bs()
            is_xref_dep = 1 if is_xref_resolved == 256 else 0
        # The xref reference is a *handle*: it belongs to the handle stream even
        # though it sits between two groups of value-stream fields here.
        ctx.hdl.read_h()
        layer.flags |= (is_xref_dep << 4) | (is_xref_ref << 6)

        if DwgVersion.R_13b1 <= v <= DwgVersion.R_14:
            layer.is_frozen = bool(dat.read_bit())
            layer.is_off = bool(dat.read_bit())
            dat.read_bit()                           # frozen_in_new
            layer.is_locked = bool(dat.read_bit())
        if v >= DwgVersion.R_2000b:
            flag0 = dat.read_bs()
            layer.is_frozen = bool(flag0 & 1)
            layer.is_off = bool(flag0 & 2)
            layer.is_locked = bool(flag0 & 8)
            layer.is_plotting = bool(flag0 & 16)
            layer.lineweight = (flag0 & 0x03E0) >> 5

        if v >= DwgVersion.R_13b1:
            layer.color = dat.read_cmc(ctx.str)
            if v <= DwgVersion.R_14:
                # Historic quirk preserved upstream: a negative colour index
                # means the layer is off.
                layer.is_off = layer.color.index < 0

        hdl = ctx.hdl
        if v >= DwgVersion.R_2000b:
            layer.plotstyle_handle = hdl.read_h().value
        if v >= DwgVersion.R_2007a:
            hdl.read_h()                             # material
        if v >= DwgVersion.R_13b1:
            layer.linetype_handle = hdl.read_h().value

    def _decode_block_header(self, ctx: ObjectState) -> DwgObject:
        """``BLOCK_HEADER`` — a block's name, base point and entity handles.

        The MVP does not expand block nesting: the block's own entity handles
        are read so a caller can enumerate first-level entities, but a nested
        INSERT is left as a handle reference and never followed.
        """
        blk = BlockHeader()
        self._decode_object_common(ctx, blk)
        self._tolerant(ctx, self._read_block_header_body, blk)
        return blk

    def _read_block_header_body(self, ctx: ObjectState, blk: BlockHeader) -> None:
        dat = ctx.dat
        v = self.version

        # -- COMMON_TABLE_FLAGS(Block) + the R13b1 body -------------------
        blk.name = ctx.read_text()
        if v <= DwgVersion.R_2004:
            is_xref_ref = dat.read_bit()
            is_xref_resolved = dat.read_bs()
            is_xref_dep = dat.read_bit()
        else:
            is_xref_ref = 1
            is_xref_resolved = dat.read_bs()
            is_xref_dep = 1 if is_xref_resolved == 256 else 0
        ctx.hdl.read_h()                             # the xref reference
        blk.flags |= (is_xref_dep << 4) | (is_xref_ref << 6)

        anonymous = dat.read_bit()
        hasattrs = dat.read_bit()
        blkisxref = dat.read_bit()
        xrefoverlaid = dat.read_bit()
        blk.is_anonymous = bool(anonymous)
        blk.is_xref = bool(blkisxref or xrefoverlaid)
        blk.flags |= (anonymous | (hasattrs << 1) | (blkisxref << 2)
                      | (xrefoverlaid << 3))
        if v >= DwgVersion.R_2000b:
            dat.read_bit()                           # xref_loaded

        num_owned = 0
        if v >= DwgVersion.R_2004a and not blkisxref and not xrefoverlaid:
            num_owned = dat.read_bl()
            if num_owned > 0xF00000:
                raise DwgDecodeError(
                    f"BLOCK_HEADER num_owned {num_owned} is out of bounds",
                    section="BLOCK_HEADER",
                )

        blk.base_point = Vec3(dat.read_bd(), dat.read_bd(), dat.read_bd())
        ctx.read_text()                              # xref_pname

        num_inserts = 0
        if v >= DwgVersion.R_2000b:
            num_inserts = _read_num_inserts(dat)
            ctx.read_text()                          # description
            preview_size = dat.read_bl()
            if preview_size > 0xA00000:
                raise DwgDecodeError(
                    f"BLOCK_HEADER preview_size {preview_size} is out of bounds",
                    section="BLOCK_HEADER",
                )
            if preview_size:
                dat.read_bytes(preview_size)

        if v >= DwgVersion.R_2007a:
            dat.read_bs()                            # insert_units
            dat.read_bit()                           # explodable
            dat.read_rc()                            # block_scaling

        # -- handle stream ------------------------------------------------
        hdl = ctx.hdl
        if v >= DwgVersion.R_13b1:
            blk.block_entity_handle = hdl.read_h().value
        if DwgVersion.R_13b1 <= v <= DwgVersion.R_2000:
            if not blkisxref and not xrefoverlaid:
                blk.first_entity_handle = hdl.read_h().value
                blk.last_entity_handle = hdl.read_h().value
        if v >= DwgVersion.R_2004a and num_owned < 0xF00000:
            blk.entity_handles = [hdl.read_h().value for _ in range(num_owned)]
        if v >= DwgVersion.R_13b1:
            blk.endblk_handle = hdl.read_h().value
        if v >= DwgVersion.R_2000b:
            if 0 < num_inserts < 0xF00000:
                for _ in range(num_inserts):
                    hdl.read_h()                     # inserts
            hdl.read_h()                             # layout

    def _decode_control(self, ctx: ObjectState) -> DwgObject:
        """A ``*_CONTROL`` object — the table of symbol-record handles.

        Controls are the only objects whose owner/reactors/xdic block is
        skipped in ``common_object_handle_data.spec``, and the only ones that
        read their handles from the *value* stream before R2007
        (``CONTROL_HANDLE_STREAM`` copies the value position into the handle
        stream).  Pointing the handle reader at the value position reproduces
        that without a special path.
        """
        ctrl = ControlObject()
        self._decode_object_common(ctx, ctrl)
        self._tolerant(ctx, self._read_control_body, ctrl)
        return ctrl

    def _read_control_body(self, ctx: ObjectState, ctrl: ControlObject) -> None:
        num_entries = ctx.dat.read_bl()
        ctrl.num_entries = num_entries

        # CONTROL_HANDLE_STREAM: before R2007 the handles are inline in the
        # value stream, so the handle reader must start where the value reader
        # is.
        if self.version < DwgVersion.R_2007a:
            ctx.hdl.seek_bits(ctx.dat.bit_position())
        hdl = ctx.hdl

        ctrl.owner_handle = hdl.read_h().value
        num_reactors = ctx.extra.get("num_reactors", 0)
        if num_reactors > 100000:
            num_reactors = 0
        ctrl.reactors = [hdl.read_h().value for _ in range(num_reactors)]
        if self.version >= DwgVersion.R_2004a:
            if not ctx.extra.get("is_xdic_missing"):
                ctrl.xdic_handle = hdl.read_h().value
        else:
            ctrl.xdic_handle = hdl.read_h().value

        if 0 <= num_entries <= 100000:
            ctrl.entry_handles = [hdl.read_h().value for _ in range(num_entries)]

        if ctx.type_code == int(ObjectType.BLOCK_CONTROL):
            ctrl.model_space_handle = hdl.read_h().value
            ctrl.paper_space_handle = hdl.read_h().value

    # -- proxies and unknowns --------------------------------------------
    def _decode_proxy(self, ctx: ObjectState) -> DwgObject:
        """``ACAD_PROXY_ENTITY`` / ``ACAD_PROXY_OBJECT``.

        The *body* is defined by the third-party application that wrote it, so
        LibreDWG records the raw payload and stops; this does the same.  In
        practice these are almost always Tianzheng (天正) entities.

        Only the body is opaque, though — the object's own handle is still
        where every other type has it, and upstream reads it (``dwg_decode_entity``
        calls ``bit_read_H`` before the proxy body is dispatched).  So the
        version-dependent prologue runs here too.  Skipping it does not fail
        loudly: the R2000-R2007 ``bitsize`` ``RL`` then gets consumed *as* the
        handle, and every proxy in such a file comes back with a plausible but
        wrong number — which is exactly what the duplicate handles in
        ``example_2000.dwg`` turned out to be.
        """
        proxy = ProxyEntity(address=ctx.address, size=ctx.size,
                            type_code=ctx.type_code)
        try:
            self._start_entity_streams(ctx)
            ctx.handle = ctx.dat.read_h()
        except (DwgDecodeError, BitStreamError) as exc:
            ctx.problems.append(f"proxy handle unreadable: {exc}")
        proxy.handle = ctx.handle.value
        end = min(ctx.address + ctx.size, len(ctx.data))
        proxy.data = ctx.data[ctx.address:end]
        proxy.raw = proxy.data
        return proxy

    def _decode_opaque(self, ctx: ObjectState, name: str) -> DwgObject:
        """A recognised class-defined object that this library does not decode."""
        end = min(ctx.address + ctx.size, len(ctx.data))
        return UnknownObject(address=ctx.address, size=ctx.size,
                             type_code=ctx.type_code, class_name=name or "",
                             raw=ctx.data[ctx.address:end])

    def _decode_unknown(self, ctx: ObjectState) -> DwgObject:
        """A classic type outside the MVP scope.

        The body is *not* guessed at.  LibreDWG has a full decoder for every
        classic type; for the MVP, keeping the type code and the raw bytes is
        the same contract as a proxy — visible, byte-exact, uninvented.
        """
        end = min(ctx.address + ctx.size, len(ctx.data))
        return UnknownObject(address=ctx.address, size=ctx.size,
                             type_code=ctx.type_code,
                             raw=ctx.data[ctx.address:end])


# ---------------------------------------------------------------------------
# Decoder helpers
# ---------------------------------------------------------------------------
def _read_num_inserts(dat: BitReader) -> int:
    """``FIELD_NUM_INSERTS`` — count non-zero bytes up to a terminating zero.

    A deliberately quirky count.  The C is::

        while ((vcount = (BITCODE_RC)bit_read_RC (dat)))
          FIELD_VALUE (num_inserts)++;

    so the terminator is consumed and not counted, and — despite the name — a
    byte larger than 1 also counts.  The macro's ``type`` parameter is read
    nowhere in the decode direction.
    """
    num = 0
    while dat.read_rc():
        num += 1
    return num


def _vertex(x: float, y: float):
    """Build an :class:`~py_libredwg.objects.LWPolylineVertex` (local import)."""
    from .objects.lwpolyline import LWPolylineVertex

    return LWPolylineVertex(x=x, y=y)


def decode_objects(
    data: bytes,
    *,
    version: DwgVersion,
    addresses: list[ObjectAddress] | list[int],
    classes: list[DwgClass] | None = None,
    strict: bool = False,
    codepage: int = 0,
    problems: list[str] | None = None,
) -> Iterator[DwgObject]:
    """Yield decoded objects for each address of an object map.

    A generator, not a list, so a caller that only wants entities can stop
    early without paying for the table and dictionary records it will never
    look at — which matters, because in a real drawing those are most of the
    objects.

    Duplicate addresses are skipped: a corrupt map can repeat an entry, and
    decoding the same bytes twice would produce two objects claiming one
    handle.

    ``problems``, when given, receives the per-object diagnostics the decoder
    accumulates.  The sink is drained as the generator runs, so it must be
    iterated to completion for the list to be complete.

    ``codepage`` is the file's declared code page, used to decode pre-R2007
    single-byte text.  Leaving it at 0 decodes with latin-1, which never
    raises; pass the file header's value to get readable names.
    """
    decoder = ObjectDecoder(data, version=version, classes=classes,
                            strict=strict, codepage=codepage)
    seen: set[int] = set()
    reported = 0
    for entry in addresses:
        address = entry.address if isinstance(entry, ObjectAddress) else entry
        if address in seen:
            continue
        seen.add(address)
        yield decoder.decode(address)
        if problems is not None:
            problems.extend(decoder.problems[reported:])
            reported = len(decoder.problems)
    if problems is not None:
        problems.extend(decoder.problems[reported:])
