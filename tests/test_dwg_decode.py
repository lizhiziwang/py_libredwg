# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""Tests for py-libredwg.

Three kinds of test live here, in increasing order of how much they prove:

**Unit tests** for the pieces that can be pinned down without a file — the bit
reader's encodings, the CRC-16 table, the colour palette and the lineweight
table.  These encode the *specification* of the format, taken from LibreDWG's
``bits.c`` / ``dwg.c``, so a regression shows up as a wrong number rather than
as a mysterious geometry change three layers down.

**Fixture tests** over ``tests/data/``.  Each ``<version>/<Entity>.dwg`` is
paired with an AutoCAD ``.txt`` — a VLA property dump, exported by AutoCAD
itself from the very same drawing — which is the strongest ground truth
available here: it is the vendor's own reading of the file, not another
parser's.  Handles, layer names, coordinates, radii, angles and lineweights are
compared field by field, for every version from R2000 to R2018.

**Fixture tests over the two ``example_*.dwg`` drawings**, which carry real
Chinese layer names, several entity types and proxy objects.

Ground truth can also be produced with LibreDWG's own tools::

    dwgread --json drawing.dwg > drawing.json

and compared against :func:`py_libredwg.exporters.to_geojson`.  That is the
comparison the README recommends; ``.txt`` dumps are used here because they
need no C build.

The fixture tests are skipped, not failed, when ``tests/data/`` is absent: the
data files are not small, and a checkout that drops them should still be able
to run the unit tests.
"""

from __future__ import annotations

import json
import math
import re
import struct
from pathlib import Path

import pytest

from py_libredwg import (
    Arc,
    BitReader,
    Circle,
    DwgDecodeError,
    DwgDecoder,
    DwgVersion,
    Line,
    LWPolyline,
    Point,
    ProxyEntity,
    UnsupportedVersionError,
    read_dwg,
)
from py_libredwg.cli import main as cli_main
from py_libredwg.constants import (
    BYLAYER,
    LWEIGHT_BYBLOCK,
    LWEIGHT_BYLAYER,
    LWEIGHT_DEFAULT,
    LWEIGHTS,
    RGB_PALETTE,
    find_color_index,
    lineweight_value,
    object_type_name,
    rgb_palette_index,
)
from py_libredwg.dwg_parser import parser, render, render_summary
from py_libredwg.errors import BitStreamError
from py_libredwg.exporters import to_geojson, to_json_dict
from py_libredwg.header import crc16

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
DATA = Path(__file__).resolve().parent / "data"

#: The versions for which ``tests/data/<version>/<Entity>.dwg`` exists.
VERSIONS = ("2000", "2004", "2007", "2010", "2013", "2018")

#: ``<file stem> -> (version tags that must share one geometry)``.
ENTITY_FILES = ("Line", "circle", "Arc", "Point", "Polyline")

needs_data = pytest.mark.skipif(
    not DATA.is_dir(), reason="tests/data is not present in this checkout"
)


def load(relative: str) -> DwgDecoder:
    """Parse ``tests/data/<relative>`` and return the decoder."""
    return DwgDecoder(DATA / relative).parse()


# ---------------------------------------------------------------------------
# The VLA dump the AutoCAD fixtures ship with
# ---------------------------------------------------------------------------
_VLA_LINE = re.compile(r"^;\s*(?P<key>[A-Za-z() ]+?)\s*=\s*(?P<value>.*?)\s*$")
_NUMBER = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")


class VlaDump:
    """An AutoCAD ``.txt`` property dump.

    AutoCAD writes one ``;   Property = value`` line per VLA property.  Only
    four shapes matter to these tests: a plain number, a quoted string, a
    parenthesised list of numbers, and the ``(RO)`` suffix that marks a
    read-only property (the name is otherwise the same).
    """

    def __init__(self, text: str) -> None:
        self.props: dict[str, str] = {}
        for line in text.splitlines():
            m = _VLA_LINE.match(line)
            if not m:
                continue
            key = m.group("key").replace("(RO)", "").strip()
            self.props[key] = m.group("value")

    def number(self, key: str) -> float:
        return float(self.props[key])

    def string(self, key: str) -> str:
        return self.props[key].strip('"')

    def numbers(self, key: str) -> list[float]:
        """Every number in the value, so a ``(... )`` list flattens.

        The Polyline dump abbreviates long lists with a trailing ``...``, so
        the result may be shorter than the entity's real vertex count; compare
        against a prefix, not for equality.
        """
        return [float(m.group()) for m in _NUMBER.finditer(self.props[key])]

    def __contains__(self, key: str) -> bool:
        return key in self.props


def vla(relative: str) -> VlaDump:
    return VlaDump((DATA / relative).read_text(encoding="utf-8", errors="replace"))


def close(a: float, b: float, tol: float = 5e-5) -> bool:
    """Compare a decoded value with a printed one.

    The dumps print about six significant digits, so an exact comparison would
    fail on the last bits of a double for no useful reason.
    """
    return abs(a - b) <= tol * max(1.0, abs(b))


# ===========================================================================
# Bit reader
# ===========================================================================
class TestBitReaderBasics:
    def test_read_bits_spans_bytes(self):
        # 0b1010_1100 0b0011_0101 ; a 12-bit window is 0xAC3
        r = BitReader(b"\xac\x35")
        assert r.read_bits(12) == 0xAC3
        assert (r.byte, r.bit) == (1, 4)

    def test_bits_are_msb_first(self):
        r = BitReader(b"\x80")
        assert r.read_bit() == 1
        assert r.read_bits(7) == 0

    def test_read_bits_zero_and_full_width_are_identity(self):
        r = BitReader(b"\x12\x34")
        assert r.read_bits(0) == 0
        assert r.read_bits(16) == 0x1234

    def test_signed_bits_are_twos_complement(self):
        r = BitReader(b"\xff\x00")
        assert r.read_signed_bits(12) == -16
        r = BitReader(b"\x7f\xf0")
        assert r.read_signed_bits(12) == 0x7FF

    def test_skip_and_align(self):
        r = BitReader(b"\xab\xcd")
        r.skip_bits(3)
        assert r.bit == 3
        r.align_to_byte()
        assert (r.byte, r.bit) == (1, 0)
        assert r.read_rc() == 0xCD

    def test_seek(self):
        r = BitReader(b"\x01\x02\x03\x04")
        r.seek_bits(20)
        assert (r.byte, r.bit) == (2, 4)
        assert r.read_bits(4) == 0x3
        # A negative position is the one seek the guard rejects: the decoder
        # never needs it, and it is how a corrupt section offset announces
        # itself.
        with pytest.raises(BitStreamError):
            r.seek_bits(-1)

    def test_truncated_stream_raises_when_strict(self):
        r = BitReader(b"\x01", strict=True)
        with pytest.raises(BitStreamError):
            r.read_bits(32)

    def test_truncated_stream_is_noted_when_lenient(self):
        r = BitReader(b"\x01", strict=False)
        assert r.read_bits(32) == 0x01000000
        assert r.problems

    def test_read_ahead_to_the_end_is_safe(self):
        r = BitReader(b"\xff\xff", strict=False)
        assert r.read_bits(9) == 0x1FF
        assert (r.byte, r.bit) == (1, 1)
        r.read_bits(9)                 # would run past the end
        assert r.problems
        assert r.byte >= 2


class TestBitReaderEncodings:
    """``bit_read_BS`` / ``BL`` / ``MS`` / ``H`` and the 63-bit guard."""

    @staticmethod
    def _reader(bits: str) -> BitReader:
        """A reader over ``bits``, padded on the right to a whole byte."""
        pad = -len(bits) % 8
        data = int(bits + "0" * pad, 2).to_bytes((len(bits) + pad) // 8, "big")
        return BitReader(data)

    @staticmethod
    def _bs(bits: str) -> int:
        return TestBitReaderEncodings._reader(bits).read_bs()

    def test_bs_selectors(self):
        # 00 -> a 16-bit RS follows (18 bits in all)
        assert self._bs("00" + format(0xBEEF, "016b")) == 0xEFBE
        # 01 -> an 8-bit RC follows (10 bits in all)
        assert self._bs("01" + format(0x5A, "08b")) == 0x5A
        # 10 -> the literal 0
        assert self._bs("10") == 0
        # 11 -> the literal 256
        assert self._bs("11") == 256

    def test_bs_can_exceed_a_byte(self):
        """A handle's low byte done as ``BS`` still yields 0..511."""
        assert self._bs("11") == 256

    def test_raw_shorts_and_longs_are_little_endian(self):
        """``bit_read_RS`` / ``RL`` store the *least significant byte first*.

        This is not an accident of the host: LibreDWG's readers are written as
        ``byte1 = bit_read_RC(); byte2 = bit_read_RC(); (byte2 << 8) | byte1``,
        so the byte order on the stream is little-endian while every
        bit-granular field around it is most-significant-bit first.  Reading
        ``RS``/``RL`` big-endian is a classic way to get plausible-looking
        garbage out of a handle or a section size.
        """
        assert BitReader(b"\xbe\xef").read_rs() == 0xEFBE
        assert BitReader(b"\xde\xad\xbe\xef").read_rl() == 0xEFBEADDE
        assert BitReader(b"\x5a").read_rc() == 0x5A

    def test_bl_is_the_long_form(self):
        # The payload bytes go in little-endian order, and the 2-bit selector
        # means the reader is no longer byte-aligned, so the value is built as
        # a bit string rather than by concatenating bytes.
        r = self._reader("00" + format(0xEFBEADDE, "032b"))
        assert r.read_bl() == 0xDEADBEEF
        r = BitReader(b"\xc0")                     # 11 -> the literal 256
        assert r.read_bl() == 256

    def test_rl_is_a_bare_32_bit_word(self):
        r = BitReader(struct.pack("<I", 0x89ABCDEF))
        assert r.read_rl() == 0x89ABCDEF

    def test_ms_is_15_value_bits_plus_a_continuation_flag(self):
        # One word, bit 15 clear: the value is the word itself.
        r = BitReader(struct.pack("<H", 0x2A00))
        assert r.read_ms() == 0x2A00
        assert r.byte == 2                          # nothing extra consumed
        # Bit 15 set: a second word carries bits 15..29.
        r = BitReader(struct.pack("<H", 0x8000) + struct.pack("<H", 0x0001))
        assert r.read_ms() == 0x8000
        assert r.byte == 4

    def test_handle_byte_packs_code_and_size(self):
        # 0x52 -> code 5 (hard pointer), size 2 -> two big-endian bytes
        r = BitReader(b"\x52\x09\xba", version=DwgVersion.R_2000)
        h = r.read_h()
        assert (h.code, h.size, h.value) == (5, 2, 0x9BA)

    def test_handle_relative_codes(self):
        r = BitReader(b"\x60", version=DwgVersion.R_2000)   # code 6, size 0
        h = r.read_h()
        assert (h.code, h.size) == (6, 0)
        assert h.is_relative
        assert h.to_absolute(0x100) == 0x101                 # code 6 -> +1

    def test_raw_double_is_two_words_low_word_first(self):
        """``bit_read_RD`` is not a plain big-endian double.

        LibreDWG assembles a raw double from two 32-bit words read
        least-significant first (``bit_read_RLL``), so the byte order on the
        stream is word-swapped.  Reading it the obvious way — as one
        big-endian 64-bit float — gives a plausible-looking but wrong number,
        which is exactly the kind of bug that only surfaces as a stray vertex.
        """
        value = -2680430.068109198
        raw = struct.unpack("<Q", struct.pack("<d", value))[0]
        stream = struct.pack("<II", raw & 0xFFFFFFFF, raw >> 32)

        assert BitReader(stream).read_rd() == value
        # The same bits read as a leading 2-bit code plus 64 bits of double,
        # which is the form the header variables use.  The 2-bit code takes
        # the reader off a byte boundary, so the payload is fed as bits.
        payload = "".join(format(byte, "08b") for byte in stream)
        assert self._reader("00" + payload).read_bd() == value
        # ... and the naive big-endian reading really is something else, so
        # this test would fail if read_rd ever regressed to it.
        assert struct.unpack(">d", stream)[0] != value


# ===========================================================================
# CRC-16 and colours — the two tables transcribed from the C sources
# ===========================================================================
class TestCRC16:
    def test_table_prefix_matches_upstream(self):
        """The first entries of LibreDWG's ``crc_table`` in ``decode.c``."""
        from py_libredwg.header import _CRC16_TABLE

        assert tuple(_CRC16_TABLE[:8]) == (
            0x0000, 0xC0C1, 0xC181, 0x0140, 0xC301, 0x03C0, 0x0280, 0xC241,
        )

    def test_seeded_run_is_stable(self):
        # A fixed vector, so a change to the table or the loop order is caught
        # even though there is nothing special about the input.
        assert crc16(0xC0C1, b"123456789") == 0x7E6C

    def test_offset_and_length_select_a_window(self):
        whole = crc16(0, b"ABCDEF")
        assert crc16(0, b"xxabcdefyy", 2, 6) != whole


class TestColours:
    def test_palette_is_the_full_256_entries(self):
        assert len(RGB_PALETTE) == 256
        # The ACI values AutoCAD fixes by convention.
        assert RGB_PALETTE[0] == (0, 0, 0)
        assert RGB_PALETTE[1] == (255, 0, 0)
        assert RGB_PALETTE[7] == (255, 255, 255)
        assert RGB_PALETTE[255] == (255, 255, 255)

    def test_find_color_index_is_an_exact_search(self):
        assert find_color_index(0xFF0000) == 1
        assert find_color_index(0x000000) == 0

    def test_find_color_index_returns_bylayer_on_a_miss(self):
        # Upstream returns 256 rather than the nearest entry: a colour that
        # came out of the stream is always a palette colour, so a miss means
        # something else is wrong and guessing would hide it.
        assert find_color_index(0x123456) == BYLAYER

    def test_palette_index_and_find_color_index_are_inverse_where_unique(self):
        for index in (0, 1, 7, 8):
            assert find_color_index(rgb_palette_index(index)) == index
        assert rgb_palette_index(256) == 0          # out of range -> 0

    def test_duplicate_palette_entries_resolve_to_the_first(self):
        """Indices 1 and 10 share red; the search returns the lower one.

        Worth pinning down because it is observable in the output: a
        true-colour red written from ACI 10 comes back as ACI 1.
        """
        assert rgb_palette_index(10) == rgb_palette_index(1)
        assert find_color_index(rgb_palette_index(10)) == 1
        assert rgb_palette_index(255) == 0xFFFFFF
        assert find_color_index(0xFFFFFF) == 7

    def test_color_description(self):
        from py_libredwg.constants import ColorMethod
        from py_libredwg.objects import CMColor

        assert CMColor(index=BYLAYER).describe() == "ByLayer"
        assert CMColor(index=0).describe() == "ByBlock"
        cm = CMColor(index=1, rgb=0xC2FF0000, method=int(ColorMethod.RGB))
        assert cm.describe() == "#FF0000"
        assert cm.true_color == (0xFF, 0, 0)


class TestLineweight:
    def test_table_is_the_dwg_c_one(self):
        assert len(LWEIGHTS) == 32
        assert LWEIGHTS[0] == 0
        assert LWEIGHTS[23] == 211

    def test_sentinels(self):
        assert lineweight_value(LWEIGHT_BYLAYER) == -1
        assert lineweight_value(LWEIGHT_BYBLOCK) == -2
        assert lineweight_value(LWEIGHT_DEFAULT) == -3

    def test_index_is_taken_modulo_32(self):
        """The packed layer-flag form shares the table with the raw byte."""
        assert lineweight_value(LWEIGHT_BYLAYER + 32) == -1
        assert lineweight_value(3) == lineweight_value(35) == 13

    def test_reserved_slots_read_as_zero(self):
        for index in range(24, 29):
            assert lineweight_value(index) == 0


class TestObjectTypeNames:
    def test_known_type(self):
        assert object_type_name(0x13) == "LINE"

    def test_class_range_and_fixed_range_are_labelled(self):
        assert object_type_name(505) == "FIXED_505"
        assert object_type_name(0x84) == "CLASS_132"
        assert object_type_name(0x200) == "CLASS_512"


class TestCodepage:
    """Pre-R2007 text is single bytes plus a declared code page.

    A Chinese drawing read as latin-1 gives perfectly plausible-looking
    mojibake — ``¶ÁÈ¡`` where ``读取`` belongs — so the failure is easy to miss
    and the code page has to be threaded all the way down to the string
    readers.  These pin that down without needing a Chinese fixture.
    """

    #: ``读`` ``取`` in GBK (what a cp936 drawing stores), and the same two
    #: code points in UTF-8, so the expectation is not a hand-typed string.
    GBK = "读取".encode("gbk")
    UTF8 = "读取".encode()

    @staticmethod
    def _bitstring_to_bytes(bits: str) -> bytes:
        padded = bits + "0" * (-len(bits) % 8)
        return bytes(
            int(padded[i:i + 8], 2) for i in range(0, len(padded), 8)
        )

    @classmethod
    def _pack(cls, payload_bits: str, count: int | None = None) -> bytes:
        """A ``BS`` count over ``payload_bits``, then those bits.

        The payload is spliced in *as bits*, not appended as bytes: the 2-bit
        selector plus the 8-bit value leave the reader six bits short of a byte
        boundary, so a byte-appended payload would be read six bits out of
        phase and come back as plausible garbage.

        ``count`` defaults to the byte length — right for a ``TV``, whose
        prefix counts bytes — and must be given for a ``TU``, whose prefix
        counts 16-bit characters instead.
        """
        if count is None:
            count = len(payload_bits) // 8
        return cls._bitstring_to_bytes(
            "01" + format(count, "08b") + payload_bits
        )

    @classmethod
    def _bytes_bits(cls, payload: bytes) -> str:
        return "".join(format(b, "08b") for b in payload)

    def _tv(self, payload: bytes, *, codepage: int) -> str:
        data = self._pack(self._bytes_bits(payload))
        return BitReader(data, codepage=codepage).read_tv()

    def test_declared_codepage_decodes_the_text(self):
        assert self._tv(self.GBK, codepage=39) == "读取"

    def test_codepage_zero_falls_back_to_latin1(self):
        # Not a wrong answer exactly — the bytes are returned as characters —
        # but it is why a missing code page shows up as mojibake rather than
        # as an exception, and it is the behaviour the defaults preserve.
        assert self._tv(self.GBK, codepage=0) == self.GBK.decode("latin-1")

    def test_unknown_codepage_does_not_raise(self):
        # codepage_to_python falls back to cp1252, which cannot decode every
        # byte; the reader then falls back again to latin-1 rather than dying
        # on a mislabelled file.  Only the no-exception part is contractual.
        text = self._tv(self.UTF8, codepage=9999)
        assert isinstance(text, str) and text

    def test_tu_ignores_the_codepage(self):
        # R2007+ text is UCS-2 and must not be run through the code page: the
        # same bytes read with the file's cp936 would come back as 12 random
        # CJK ideographs rather than the 12 characters that were written.
        text = "新建长沙至赣州高速铁路"
        data = self._pack(self._bytes_bits(text.encode("utf-16-le")),
                          count=len(text))
        reader = BitReader(data, version=DwgVersion.R_2018,
                           from_version=DwgVersion.R_2018, codepage=39)
        assert reader.read_tu() == text

    def test_object_decoder_accepts_and_propagates_the_codepage(self):
        from py_libredwg.sections import ObjectDecoder

        decoder = ObjectDecoder(b"\x00" * 8, version=DwgVersion.R_2004,
                                codepage=39)
        assert decoder.codepage == 39

        from py_libredwg.sections import ObjectState

        state = ObjectState(b"\x00" * 8, DwgVersion.R_2004, 0, 8, 2, 1,
                            codepage=39)
        for reader in (state.dat, state.hdl, state.str):
            assert reader.codepage == 39

    def test_clone_carries_the_codepage(self):
        original = BitReader(b"\x00" * 8, codepage=39)
        assert original.clone(b"\x00" * 8).codepage == 39

    def test_a_real_drawing_reports_its_code_page(self):
        # The fixture drawings are western, but the number still has to be
        # read out of the header for the plumbing above to have anything to
        # pass down.
        decoder = load("2004/Line.dwg")
        assert decoder.header is not None
        assert decoder.header.codepage in (0, 30)
        # ... and the entity's own reader must have received it, so that its
        # layer name would be decoded with the right code page.
        assert decoder.entities[0].layer == "0"


# ===========================================================================
# Fixture tests: one entity per file, checked against AutoCAD's own dump
# ===========================================================================
@needs_data
@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("stem", ENTITY_FILES)
def test_entity_matches_autocad_dump(version: str, stem: str):
    """Every MVP entity, in every version, against the vendor's reading."""
    decoder = load(f"{version}/{stem}.dwg")
    entities = decoder.entities
    assert len(entities) == 1, f"expected exactly one entity, got {len(entities)}"
    entity = entities[0]
    assert not decoder.problems, decoder.problems

    dump = vla(f"{version}/{stem}.txt")

    # Identity and the common entity data, for every type.
    assert f"{entity.handle:X}" == dump.string("Handle")
    assert entity.layer == dump.string("Layer")
    # The dump prints the *converted* lineweight, which is why the raw index
    # has to go through lineweight_value() before it can be compared.
    assert entity.lineweight_value == dump.number("Lineweight")
    assert close(entity.ltype_scale, dump.number("LinetypeScale"))
    assert list(entity.extrusion) == dump.numbers("Extrusion")

    # Type-specific geometry.
    if isinstance(entity, Line):
        assert entity.type_name == "LINE"
        for attr, key in (("start", "StartPoint"), ("end", "EndPoint")):
            want = dump.numbers(key)
            got = getattr(entity, attr)
            assert close(got.x, want[0]) and close(got.y, want[1])
            assert got.z == want[2]
    elif isinstance(entity, Circle) and not isinstance(entity, Arc):
        want = dump.numbers("Center")
        assert close(entity.center.x, want[0])
        assert close(entity.center.y, want[1])
        assert close(entity.radius, dump.number("Radius"))
        assert close(entity.diameter, dump.number("Diameter"))
    elif isinstance(entity, Arc):
        want = dump.numbers("StartPoint")
        # The angles are stored in radians in the DWG; the dump shows the
        # endpoints, so the angles are *derived* and checked against them.
        start = (
            entity.center.x + entity.radius * math.cos(entity.start_angle),
            entity.center.y + entity.radius * math.sin(entity.start_angle),
        )
        assert close(start[0], want[0]) and close(start[1], want[1])
        end = dump.numbers("EndPoint")
        got = (
            entity.center.x + entity.radius * math.cos(entity.end_angle),
            entity.center.y + entity.radius * math.sin(entity.end_angle),
        )
        assert close(got[0], end[0]) and close(got[1], end[1])
    elif isinstance(entity, Point):
        want = dump.numbers("Coordinates")
        assert close(entity.location.x, want[0])
        assert close(entity.location.y, want[1])
        assert entity.location.z == want[2]
    elif isinstance(entity, LWPolyline):
        want = dump.numbers("Coordinates")
        assert len(entity.vertices) >= len(want) // 2
        for i in range(len(want) // 2):
            # The dump abbreviates long lists, so only the prefix is compared.
            assert close(entity.vertices[i].x, want[2 * i])
            assert close(entity.vertices[i].y, want[2 * i + 1])
        assert entity.closed == bool(dump.number("Closed"))
        assert close(entity.elevation, dump.number("Elevation"))
    else:                                       # pragma: no cover - scope guard
        pytest.fail(f"unexpected entity type {type(entity).__name__}")


@needs_data
@pytest.mark.parametrize("stem", ENTITY_FILES)
def test_same_geometry_across_all_versions(stem: str):
    """One drawing saved as six versions must decode to one geometry.

    This is the cross-version check the per-version fixtures cannot make on
    their own: a bit reader that is right for R2000 and wrong for R2018 still
    passes the dump comparison one version at a time only if the error happens
    to cancel out — and it does not, it shifts the value.  Comparing the six
    results against each other catches the version-conditional paths
    (``SINCE (R_2004)``, the R2007 string stream, the R2010 bit sizes).
    """
    first = load(f"{VERSIONS[0]}/{stem}.dwg").entities[0]
    for version in VERSIONS[1:]:
        other = load(f"{version}/{stem}.dwg").entities[0]
        assert type(other) is type(first)
        assert other.handle == first.handle
        assert other.layer == first.layer
        assert other.lineweight == first.lineweight
        if isinstance(first, Line):
            assert list(other.start) == list(first.start)
            assert list(other.end) == list(first.end)
        elif isinstance(first, Arc):
            assert other.start_angle == first.start_angle
            assert other.end_angle == first.end_angle
            assert other.radius == first.radius
        elif isinstance(first, Circle):
            assert other.radius == first.radius
            assert list(other.center) == list(first.center)
        elif isinstance(first, Point):
            assert list(other.location) == list(first.location)
        elif isinstance(first, LWPolyline):
            assert [(v.x, v.y) for v in other.vertices] == \
                   [(v.x, v.y) for v in first.vertices]


@needs_data
@pytest.mark.parametrize("version", VERSIONS)
def test_entity_file_overhead_is_constant(version: str):
    """Every version declares the same tables for the same drawing.

    A parse that silently mis-reads a section header usually shows up as a
    wildly different object count; these drawings are all the same template,
    so the counts sit in a narrow band across versions.
    """
    decoder = load(f"{version}/Line.dwg")
    assert decoder.version_tag.startswith("AC10")
    assert 140 <= len(decoder.objects) <= 240
    assert len(decoder.layers) == 1
    assert decoder.layers[0].name == "0"
    assert decoder.entities[0].layer_handle == decoder.layers[0].handle


@needs_data
@pytest.mark.parametrize("version", ["2000", "2018"])
def test_example_drawing(version: str):
    """The two multi-entity drawings, with Chinese layers and proxies."""
    decoder = load(f"example_{version}.dwg")
    assert decoder.entities, "no entity was decoded"
    assert len(decoder.layers) == 5

    names = {layer.name for layer in decoder.layers}
    assert "0" in names
    assert "Tavolo 3" in names                 # a layer name with a space

    # Both files must decode clean.  The R2000 one used to report "handle 0x500
    # claimed by both FIXED_506 and LINE", which looked like a duplicate-handle
    # anomaly in the file; it was the proxy decoder reading the R2000-R2007
    # ``bitsize`` RL as if it were the handle, so several proxies landed on
    # numbers that were already taken.  With that fixed the problem is gone and
    # this can assert the strong form.
    assert decoder.problems == [], decoder.problems

    # Proxies are the types the MVP deliberately does not decode, so they are
    # the ones most likely to silently lose their identity — see above.  Every
    # one of them still has to be addressable.
    assert decoder.proxies, "this drawing has proxies; check them"
    for proxy in decoder.proxies:
        assert proxy.handle, f"proxy at 0x{proxy.address:X} lost its handle"
    handles = [p.handle for p in decoder.proxies]
    assert len(set(handles)) == len(handles), "proxies share a handle"

    # Handle 0x8A is $CLAYER in the header; resolving it through the layer
    # table is how a consumer turns the header into a name.
    assert decoder.layer_name(0x8A) == "Tavolo 3"

    # Every decoded entity must resolve its layer.
    for entity in decoder.entities:
        assert entity.layer is not None, f"unresolved layer on {entity.handle:X}"

    geometry = decoder.geometry_entities
    assert len(geometry) == len(decoder.entities)
    kinds = {e.geometry_type for e in geometry}
    assert kinds == {"LineString", "Polygon", "Point"}


@needs_data
def test_proxy_entities_keep_their_bytes():
    """A proxy is opaque, but not dropped.

    Tianzheng (天正) drawings are full of these; the payload is the only thing
    a consumer can act on, so it has to survive the parse intact and be
    reported by size rather than guessed at.
    """
    decoder = load("example_2000.dwg")
    proxies = decoder.proxies
    assert proxies, "expected proxy entities in this drawing"
    for proxy in proxies:
        assert isinstance(proxy, ProxyEntity)
        assert proxy.is_proxy
        assert proxy.raw is not None and len(proxy.raw) > 0
        assert proxy.raw == proxy.data
        # The JSON view reports the size, never the contents.
        assert proxy.to_dict()["data_size"] == len(proxy.raw)


@needs_data
def test_erased_entities_are_flagged_not_dropped():
    """An erased object is still returned; the exporter is what filters it."""
    decoder = load("example_2018.dwg")
    for obj in decoder.objects:
        assert isinstance(obj.is_erased, bool)


# ===========================================================================
# Exporters
# ===========================================================================
@needs_data
def test_geojson_structure():
    decoder = load("example_2018.dwg")
    doc = to_geojson(decoder)

    assert doc["type"] == "FeatureCollection"
    features = doc["features"]
    assert len(features) == len(decoder.geometry_entities)

    ids = [f["id"] for f in features]
    assert len(ids) == len(set(ids)), "feature ids must be unique"

    for feature in features:
        assert feature["type"] == "Feature"
        props = feature["properties"]
        # The property names are LibreDWG's own, from out_geojson.c — kept so
        # that output here and output from dwgread are diffable.
        assert props["Layer"] in {layer.name for layer in decoder.layers}
        assert props["EntityHandle"] == feature["id"]
        assert "SubClasses" in props
        geometry = feature["geometry"]
        assert geometry["type"] in ("LineString", "Polygon", "Point")
        if geometry["type"] == "LineString":
            assert len(geometry["coordinates"]) >= 2
        elif geometry["type"] == "Polygon":
            # A GeoJSON ring repeats its first position to close.
            for ring in geometry["coordinates"]:
                assert ring[0] == ring[-1]
        else:
            assert len(geometry["coordinates"]) in (2, 3)


@needs_data
def test_geojson_is_json_serialisable_and_utf8_friendly():
    decoder = load("example_2000.dwg")
    text = json.dumps(to_geojson(decoder), ensure_ascii=False)
    assert "Tavolo" in text
    # ensure_ascii=False must leave the Chinese readable rather than escaped.
    assert "\\u" not in text or "Tavolo" in text


@needs_data
def test_json_view_reports_the_parse():
    decoder = load("example_2018.dwg")
    doc = to_json_dict(decoder)

    assert doc["file"]["version"] == "AC1032"
    assert doc["file"]["version_name"] == "R_2018"
    assert doc["counts"]["entities"] == len(decoder.entities)
    assert doc["counts"]["layers"] == 5
    # Proxies and undecoded objects are counted, not silently dropped: a
    # drawing that is 60 % proxies by count is normal and the numbers should
    # say so.
    assert doc["counts"]["objects"] == len(decoder.objects)
    assert doc["counts"]["proxies"] == len(decoder.proxies)

    assert [layer["name"] for layer in doc["layers"]] == \
           [layer.name for layer in decoder.layers]
    assert len(doc["entities"]) == len(decoder.entities)

    # The round trip through JSON must preserve the numbers exactly.
    round_tripped = json.loads(json.dumps(doc))
    assert round_tripped["entities"][0]["handle"] == doc["entities"][0]["handle"]


@needs_data
def test_export_writes_a_file(tmp_path):
    from py_libredwg.exporters import export_geojson, export_json

    decoder = load("example_2018.dwg")
    gj = tmp_path / "out.geojson"
    js = tmp_path / "out.json"

    assert export_geojson(decoder, gj) == len(decoder.geometry_entities)
    assert export_json(decoder, js) == len(decoder.entities)

    loaded = json.loads(gj.read_text(encoding="utf-8"))
    assert loaded["type"] == "FeatureCollection"
    assert len(loaded["features"]) == len(decoder.geometry_entities)


def test_entity_to_feature_survives_a_bare_decoder():
    """``entity_to_feature`` only needs version and extents from the decoder.

    Keeping that dependency honest means the geometry mapping can be tested
    without a DWG file at all — which is the whole point of the synthetic
    test below.
    """
    from py_libredwg.exporters import entity_to_feature

    class Fake:
        version = DwgVersion.R_2018
        version_tag = "AC1032"

        class header_vars:                       # noqa: N801 - a stand-in
            EXTMIN = None
            EXTMAX = None

    line = Line(handle=0x10, layer="0")
    line.start = type(line.start)(1.0, 2.0, 0.0)
    line.end = type(line.end)(3.0, 4.0, 0.0)
    feature = entity_to_feature(Fake(), line)
    assert feature["geometry"] == {"type": "LineString",
                                   "coordinates": [[1.0, 2.0, 0.0],
                                                   [3.0, 4.0, 0.0]]}
    assert feature["id"] == "10"
    assert feature["properties"]["Layer"] == "0"


# ===========================================================================
# Errors
# ===========================================================================
class TestErrors:
    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            DwgDecoder(tmp_path / "nope.dwg").parse()

    def test_empty_file(self, tmp_path):
        path = tmp_path / "empty.dwg"
        path.write_bytes(b"")
        with pytest.raises(DwgDecodeError):
            DwgDecoder(path).parse()

    def test_unsupported_version(self, tmp_path):
        """A plausible-looking tag from a version outside the MVP range."""
        path = tmp_path / "old.dwg"
        # AC1014 is R14: real, but before the R13..R2018 window this MVP
        # implements... it is the boundary, so use something well outside.
        path.write_bytes(b"AC1009" + b"\x00" * 64)
        with pytest.raises((UnsupportedVersionError, DwgDecodeError)):
            DwgDecoder(path).parse()

    def test_garbage_tag(self, tmp_path):
        path = tmp_path / "junk.dwg"
        path.write_bytes(b"not a dwg at all")
        with pytest.raises(DwgDecodeError):
            DwgDecoder(path).parse()

    def test_truncated_real_file(self, tmp_path):
        """A file that starts right and stops early must not hang or crash."""
        source = DATA / "2018" / "Line.dwg"
        if not source.is_file():
            pytest.skip("tests/data is not present in this checkout")
        path = tmp_path / "half.dwg"
        path.write_bytes(source.read_bytes()[: source.stat().st_size // 3])
        with pytest.raises(DwgDecodeError):
            DwgDecoder(path, strict=True).parse()

    def test_every_error_is_a_dwg_error(self):
        from py_libredwg.errors import (
            BitStreamError,
            DwgCRCError,
            DwgError,
            DwgFormatError,
            DwgSectionError,
            UnsupportedObjectError,
        )

        for exc in (DwgDecodeError, DwgFormatError, DwgCRCError,
                    DwgSectionError, BitStreamError, UnsupportedVersionError,
                    UnsupportedObjectError):
            assert issubclass(exc, DwgError)
        assert issubclass(DwgFormatError, DwgDecodeError)


# ===========================================================================
# The module's public surface
# ===========================================================================
class TestPublicApi:
    def test_read_dwg_returns_a_parsed_decoder(self):
        if not (DATA / "2018" / "Line.dwg").is_file():
            pytest.skip("tests/data is not present in this checkout")
        decoder = read_dwg(DATA / "2018" / "Line.dwg")
        assert decoder.header is not None
        assert decoder.version is DwgVersion.R_2018

    def test_parse_is_idempotent(self):
        if not (DATA / "2018" / "Line.dwg").is_file():
            pytest.skip("tests/data is not present in this checkout")
        decoder = DwgDecoder(DATA / "2018" / "Line.dwg").parse()
        again = decoder.parse()
        assert again is decoder

    def test_from_bytes_matches_from_path(self):
        source = DATA / "2018" / "Line.dwg"
        if not source.is_file():
            pytest.skip("tests/data is not present in this checkout")
        a = DwgDecoder(source).parse()
        b = DwgDecoder.from_bytes(source.read_bytes()).parse()
        assert len(a.objects) == len(b.objects)
        assert a.entities[0].handle == b.entities[0].handle


# ===========================================================================
# Command line
# ===========================================================================
class TestCli:
    """The console script.  ``main`` takes argv as an argument, so no
    subprocess and no installed entry point are needed to drive it."""

    @staticmethod
    def _fixture() -> str:
        path = DATA / "2018" / "Line.dwg"
        if not path.is_file():
            pytest.skip("tests/data is not present in this checkout")
        return str(path)

    def test_summary_is_the_default(self, capsys):
        assert cli_main([self._fixture()]) == 0
        out = capsys.readouterr().out
        assert "AC1032" in out
        assert "entities" in out
        assert "problems    : none" in out

    def test_layers_section_is_opt_in(self, capsys):
        cli_main([self._fixture(), "--layers"])
        assert "layers" in capsys.readouterr().out

    def test_output_suffix_picks_the_format(self, tmp_path, capsys):
        target = tmp_path / "out.geojson"
        assert cli_main([self._fixture(), "-o", str(target)]) == 0
        doc = json.loads(target.read_text(encoding="utf-8"))
        assert doc["type"] == "FeatureCollection"
        assert len(doc["features"]) == 1
        # The summary goes to stderr as a one-line ack, never polluting the
        # file it just wrote.
        assert "wrote" in capsys.readouterr().err

    def test_json_to_stdout(self, capsys):
        assert cli_main([self._fixture(), "--format", "json", "-o", "-"]) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["file"]["version"] == "AC1032"

    def test_problems_only_is_silent_on_a_clean_file(self, capsys):
        assert cli_main([self._fixture(), "--problems-only"]) == 0
        assert capsys.readouterr().out == ""

    def test_missing_file_exits_2(self, capsys):
        assert cli_main(["no-such-drawing.dwg"]) == 2
        assert "no such DWG file" in capsys.readouterr().err

    def test_undecodable_file_exits_1_without_a_traceback(self, tmp_path,
                                                        capsys):
        bad = tmp_path / "bad.dwg"
        bad.write_bytes(b"AC1032" + b"\x00" * 200)
        assert cli_main([str(bad)]) == 1
        err = capsys.readouterr().err
        assert "cannot decode" in err
        assert "Traceback" not in err

    def test_version_flag_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            cli_main(["--version"])
        assert excinfo.value.code == 0
        assert "py-libredwg" in capsys.readouterr().out


class TestParser:
    """``dwg_parser.parser`` — the path-in, format-out front end.

    It holds no decoding logic of its own, so these are mostly contract tests:
    which argument spellings are accepted, what shape comes back, and that the
    two side-effect-free promises hold (nothing written unless ``out`` is given,
    no format silently substituted for an unknown one).
    """

    @staticmethod
    def _fixture() -> str:
        path = DATA / "2018" / "Line.dwg"
        if not path.is_file():
            pytest.skip("tests/data is not present in this checkout")
        return str(path)

    def test_summary_returns_the_census_text(self):
        text = parser(self._fixture(), "summary")
        assert isinstance(text, str)
        assert "AC1032" in text
        assert "problems    : none" in text

    def test_geojson_returns_a_feature_collection(self):
        geo = parser(self._fixture(), "geojson")
        assert geo["type"] == "FeatureCollection"
        assert len(geo["features"]) == 1

    def test_json_returns_the_structured_view(self):
        doc = parser(self._fixture(), "json")
        assert "entities" in doc
        assert "header" in doc
        # No GeoJSON envelope on the plain view, which is what makes the two
        # dict-returning formats distinguishable at all.
        assert "type" not in doc

    def test_the_default_format_is_geojson(self):
        assert parser(self._fixture())["type"] == "FeatureCollection"

    @pytest.mark.parametrize("spelling", ["geojson", "GeoJSON", "GEOJSON",
                                          " geo "])
    def test_geojson_spellings(self, spelling):
        assert parser(self._fixture(), spelling)["type"] == "FeatureCollection"

    @pytest.mark.parametrize("spelling", ["json", "JSON", " dict "])
    def test_json_spellings(self, spelling):
        assert "entities" in parser(self._fixture(), spelling)

    @pytest.mark.parametrize("spelling", ["summary", "text", " Summary "])
    def test_summary_spellings(self, spelling):
        assert isinstance(parser(self._fixture(), spelling), str)

    def test_an_unknown_format_is_a_caller_error(self):
        # ``pytest.raises(ValueError)`` also pins down that this is *not* a
        # DwgError: a bad argument is a bug in the caller, and it must not be
        # swallowed by the handler a caller has wrapped around a bad drawing.
        with pytest.raises(ValueError) as excinfo:
            parser(self._fixture(), "dxf")
        assert "dxf" in str(excinfo.value)

    def test_a_missing_file_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            parser("no-such-drawing.dwg", "json")

    def test_out_writes_the_payload_and_still_returns_it(self, tmp_path):
        target = tmp_path / "out.geojson"
        payload = parser(self._fixture(), "geojson", out=target)
        written = json.loads(target.read_text(encoding="utf-8"))
        assert written == payload
        assert written["type"] == "FeatureCollection"

    def test_out_writes_a_summary_as_plain_text(self, tmp_path):
        target = tmp_path / "out.txt"
        text = parser(self._fixture(), "summary", out=target)
        assert target.read_text(encoding="utf-8") == text + chr(10)

    def test_render_reuses_an_already_decoded_document(self):
        doc = DwgDecoder(self._fixture()).parse()
        assert render(doc, "geojson")["type"] == "FeatureCollection"
        assert render(doc, "summary").startswith("file")

    def test_render_summary_is_public_and_agrees_with_parser(self):
        doc = DwgDecoder(self._fixture()).parse()
        assert render_summary(doc) == parser(self._fixture(), "summary")

    def test_the_summary_is_the_command_lines_text(self, capsys):
        # The command line and the library must not drift apart, and both
        # render through dwg_parser.render_summary.  This is what holds that in
        # place: if the two ever stop agreeing, one of them moved.
        path = self._fixture()
        assert cli_main([path]) == 0
        assert capsys.readouterr().out.strip() == parser(path, "summary")

    def test_out_dir_names_the_file_after_the_drawing(self, tmp_path):
        payload = parser(self._fixture(), "geojson", out_dir=tmp_path)
        # Line.dwg -> Line.geojson: the name follows the *input*, not the
        # format, which is what lets a batch keep track of which file is which.
        written = tmp_path / "Line.geojson"
        assert written.is_file()
        assert json.loads(written.read_text(encoding="utf-8")) == payload

    def test_out_dir_is_created_and_each_format_has_its_own_name(self,
                                                                tmp_path):
        target = tmp_path / "build" / "exports"
        assert not target.exists()          # cold tree, including the parents
        parser(self._fixture(), "geojson", out_dir=target)
        parser(self._fixture(), "json", out_dir=target)
        parser(self._fixture(), "summary", out_dir=target)
        # Three formats into one directory must not collide on a filename.
        assert sorted(p.name for p in target.iterdir()) == [
            "Line.geojson", "Line.json", "Line.txt",
        ]

    def test_out_and_out_dir_together_are_a_caller_error(self, tmp_path):
        # Refused before the drawing is opened: the two disagree about where the
        # output goes, so which one wins is not something to guess at, and the
        # refusal must not depend on the file being readable.
        with pytest.raises(ValueError) as excinfo:
            parser("no-such-drawing.dwg", "json",
                   out=tmp_path / "x.json", out_dir=tmp_path)
        assert "out_dir" in str(excinfo.value)

    def test_out_naming_a_directory_is_a_caller_error(self, tmp_path):
        # The out=/out_dir= mix-up.  Left to open() this is a bare
        # PermissionError on Windows and an IsADirectoryError on POSIX, neither
        # of which names the parameter that would have worked.
        with pytest.raises(ValueError) as excinfo:
            parser(self._fixture(), "geojson", out=tmp_path)
        message = str(excinfo.value)
        assert "out_dir" in message
        assert "directory" in message

