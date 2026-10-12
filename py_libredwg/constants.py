# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""Constants transcribed from the LibreDWG headers.

Every table here has a counterpart in the C sources; the comment above each
block names the file it came from so the values can be re-verified against
upstream.

Sources:
  ``include/dwg.h``   version enum, object type enum, section enums, handle codes
  ``src/common.c``    version tag string <-> version mapping
"""

from __future__ import annotations

import enum
from typing import Final

__all__ = [
    "DwgVersion",
    "ObjectType",
    "SectionType",
    "SectionTypeR13",
    "HandleCode",
    "ColorMethod",
    "VERSION_TAGS",
    "TAG_TO_VERSION",
    "SUPPORTED_VERSIONS",
    "BYLAYER",
    "BYBLOCK",
    "LWEIGHTS",
    "LWEIGHT_BYLAYER",
    "LWEIGHT_BYBLOCK",
    "LWEIGHT_DEFAULT",
    "lineweight_value",
    "RGB_PALETTE",
    "rgb_palette_index",
    "find_color_index",
    "MIN_SUPPORTED",
    "MAX_SUPPORTED",
    "object_type_name",
]


# ---------------------------------------------------------------------------
# Dwg_Version_Type  (include/dwg.h)
#
# The *numeric order is load-bearing*: LibreDWG compares versions with
# ``>=`` / ``<`` throughout the decoder (e.g. ``from_version >= R_2004``),
# so these ordinals are reproduced exactly, including the holes where
# Autodesk releases LibreDWG never observed are commented out.
# ---------------------------------------------------------------------------
class DwgVersion(enum.IntEnum):
    R_INVALID = 0
    R_1_1 = 1
    R_1_2 = 2
    R_1_3 = 3
    R_1_4 = 4
    R_2_0b = 5
    R_2_0 = 6
    R_2_10 = 7
    R_2_21 = 8
    R_2_22 = 9
    R_2_4 = 10
    R_2_5 = 11
    R_2_6 = 12
    R_9 = 13
    R_9c1 = 14
    R_10 = 15
    R_11b1 = 16
    R_11b2 = 17
    R_11 = 18
    R_12 = 18          # R_12 = R_11, same release
    R_13b1 = 19
    R_13b2 = 20
    R_13 = 21
    R_13c3 = 22
    R_14 = 23
    R_2000b = 24
    R_2000 = 25
    R_2000i = 26
    R_2002 = 27
    R_2004a = 28
    R_2004b = 29
    R_2004c = 30
    R_2004 = 31
    R_2007a = 32
    R_2007b = 33
    R_2007 = 34
    R_2010b = 35
    R_2010 = 36
    R_2013b = 37
    R_2013 = 38
    R_2018b = 39
    R_2018 = 40
    R_2022b = 41
    R_AFTER = 42

    @property
    def is_2004_plus(self) -> bool:
        """True when the file uses the R2004+ container (second header + RS)."""
        return self >= DwgVersion.R_2004

    @property
    def is_2007_plus(self) -> bool:
        """True when strings are stored as UCS-2 (``bit_read_T`` -> ``TU``)."""
        return self >= DwgVersion.R_2007

    @property
    def is_2000_plus(self) -> bool:
        """True for the R2000+ field encodings (BT/BE read a flag bit first)."""
        return self >= DwgVersion.R_2000

    @property
    def is_r13_plus(self) -> bool:
        return self >= DwgVersion.R_13b1

    @property
    def tag(self) -> str:
        """The 6-byte ``AC10xx`` tag written at offset 0 of a DWG file."""
        return VERSION_TAGS.get(self, "AC0000")


# The raw 6-character version tags.  A DWG file starts with one of these at
# offset 0; ``src/common.c`` holds the same strings in ``dwg_versions``.
VERSION_TAGS: Final[dict[DwgVersion, str]] = {
    DwgVersion.R_1_2: "AC1.2",
    DwgVersion.R_1_3: "AC1.3",
    DwgVersion.R_1_4: "AC1.40",
    DwgVersion.R_2_0: "AC1.50",
    DwgVersion.R_2_10: "AC2.10",
    DwgVersion.R_2_21: "AC2.21",
    DwgVersion.R_2_22: "AC2.22",
    DwgVersion.R_2_4: "AC1001",
    DwgVersion.R_2_5: "AC1002",
    DwgVersion.R_2_6: "AC1003",
    DwgVersion.R_9: "AC1004",
    DwgVersion.R_9c1: "AC1005",
    DwgVersion.R_10: "AC1006",
    DwgVersion.R_11b1: "AC1007",
    DwgVersion.R_11b2: "AC1008",
    DwgVersion.R_11: "AC1009",
    DwgVersion.R_13b1: "AC1010",
    DwgVersion.R_13b2: "AC1011",
    DwgVersion.R_13: "AC1012",
    DwgVersion.R_13c3: "AC1013",
    DwgVersion.R_14: "AC1014",
    DwgVersion.R_2000b: "AC1500",
    DwgVersion.R_2000: "AC1015",
    DwgVersion.R_2000i: "AC1016",
    DwgVersion.R_2002: "AC1017",
    DwgVersion.R_2004a: "AC402a",
    DwgVersion.R_2004b: "AC402b",
    DwgVersion.R_2004c: "AC1018",
    DwgVersion.R_2004: "AC1018",
    DwgVersion.R_2007b: "AC1021",
    DwgVersion.R_2007: "AC1021",
    DwgVersion.R_2010b: "AC1024",
    DwgVersion.R_2010: "AC1024",
    DwgVersion.R_2013b: "AC1027",
    DwgVersion.R_2013: "AC1027",
    DwgVersion.R_2018b: "AC1032",
    DwgVersion.R_2018: "AC1032",
}

# A tag maps to several enum members; these are the *canonical release* choices,
# matching what LibreDWG's ``dwg_version_hdr_type`` resolves to for a plain
# (non-alpha/beta) file.  Betas share a tag with their release.
TAG_TO_VERSION: Final[dict[str, DwgVersion]] = {
    "AC1.2": DwgVersion.R_1_2,
    "AC1.3": DwgVersion.R_1_3,
    "AC1.40": DwgVersion.R_1_4,
    "AC1.50": DwgVersion.R_2_0,
    "AC2.10": DwgVersion.R_2_10,
    "AC2.21": DwgVersion.R_2_21,
    "AC2.22": DwgVersion.R_2_22,
    "AC1001": DwgVersion.R_2_4,
    "AC1002": DwgVersion.R_2_5,
    "AC1003": DwgVersion.R_2_6,
    "AC1004": DwgVersion.R_9,
    "AC1005": DwgVersion.R_9c1,
    "AC1006": DwgVersion.R_10,
    "AC1007": DwgVersion.R_11b1,
    "AC1008": DwgVersion.R_11b2,
    "AC1009": DwgVersion.R_11,
    "AC1010": DwgVersion.R_13b1,
    "AC1011": DwgVersion.R_13b2,
    "AC1012": DwgVersion.R_13,
    "AC1013": DwgVersion.R_13c3,
    "AC1014": DwgVersion.R_14,
    "AC1500": DwgVersion.R_2000b,
    "AC1015": DwgVersion.R_2000,
    "AC1016": DwgVersion.R_2000i,
    "AC1017": DwgVersion.R_2002,
    "AC402a": DwgVersion.R_2004a,
    "AC402b": DwgVersion.R_2004b,
    "AC1018": DwgVersion.R_2004,
    "AC1021": DwgVersion.R_2007,
    "AC1024": DwgVersion.R_2010,
    "AC1027": DwgVersion.R_2013,
    "AC1032": DwgVersion.R_2018,
}

#: The version range this library implements (the MVP scope).
MIN_SUPPORTED: Final[DwgVersion] = DwgVersion.R_13
MAX_SUPPORTED: Final[DwgVersion] = DwgVersion.R_2018
SUPPORTED_VERSIONS: Final[tuple[DwgVersion, ...]] = tuple(
    v for v in (
        DwgVersion.R_13,
        DwgVersion.R_13c3,
        DwgVersion.R_14,
        DwgVersion.R_2000,
        DwgVersion.R_2004,
        DwgVersion.R_2007,
        DwgVersion.R_2010,
        DwgVersion.R_2013,
        DwgVersion.R_2018,
    )
)


# ---------------------------------------------------------------------------
# DWG_OBJECT_TYPE  (include/dwg.h)
#
# Values are the on-stream object type numbers.  0x01..0x51 are the classic
# types; anything >= 500 is a "fixed" R13+ type and anything else at/above
# 0x82 is a CLASS-defined type (see classes.py).
# ---------------------------------------------------------------------------
class ObjectType(enum.IntEnum):
    UNUSED = 0x00
    TEXT = 0x01
    ATTRIB = 0x02
    ATTDEF = 0x03
    BLOCK = 0x04
    ENDBLK = 0x05
    SEQEND = 0x06
    INSERT = 0x07
    MINSERT = 0x08
    VERTEX_2D = 0x0A
    VERTEX_3D = 0x0B
    VERTEX_MESH = 0x0C
    VERTEX_PFACE = 0x0D
    VERTEX_PFACE_FACE = 0x0E
    POLYLINE_2D = 0x0F
    POLYLINE_3D = 0x10
    ARC = 0x11
    CIRCLE = 0x12
    LINE = 0x13
    DIMENSION_ORDINATE = 0x14
    DIMENSION_LINEAR = 0x15
    DIMENSION_ALIGNED = 0x16
    DIMENSION_ANG3PT = 0x17
    DIMENSION_ANG2LN = 0x18
    DIMENSION_RADIUS = 0x19
    DIMENSION_DIAMETER = 0x1A
    POINT = 0x1B
    _3DFACE = 0x1C
    POLYLINE_PFACE = 0x1D
    POLYLINE_MESH = 0x1E
    SOLID = 0x1F
    TRACE = 0x20
    SHAPE = 0x21
    VIEWPORT = 0x22
    ELLIPSE = 0x23
    SPLINE = 0x24
    REGION = 0x25
    _3DSOLID = 0x26
    BODY = 0x27
    RAY = 0x28
    XLINE = 0x29
    DICTIONARY = 0x2A
    OLEFRAME = 0x2B
    MTEXT = 0x2C
    LEADER = 0x2D
    TOLERANCE = 0x2E
    MLINE = 0x2F
    BLOCK_CONTROL = 0x30
    BLOCK_HEADER = 0x31
    LAYER_CONTROL = 0x32
    LAYER = 0x33
    STYLE_CONTROL = 0x34
    STYLE = 0x35
    LTYPE_CONTROL = 0x38
    LTYPE = 0x39
    VIEW_CONTROL = 0x3C
    VIEW = 0x3D
    UCS_CONTROL = 0x3E
    UCS = 0x3F
    VPORT_CONTROL = 0x40
    VPORT = 0x41
    APPID_CONTROL = 0x42
    APPID = 0x43
    DIMSTYLE_CONTROL = 0x44
    DIMSTYLE = 0x45
    VX_CONTROL = 0x46
    VX_TABLE_RECORD = 0x47
    GROUP = 0x48
    MLINESTYLE = 0x49
    OLE2FRAME = 0x4A
    DUMMY = 0x4B
    LONG_TRANSACTION = 0x4C
    LWPOLYLINE = 0x4D
    HATCH = 0x4E
    XRECORD = 0x4F
    PLACEHOLDER = 0x50
    VBA_PROJECT = 0x51
    LAYOUT = 0x52
    # The R2000+ "new class" range.  Objects 0x53..0x81 are reserved;
    # 500+ are the fixed R13+ control/entity types (see decode.c).
    ACAD_PROXY_ENTITY = 0x1F2   # 498 — see ProxyEntity handling in sections.py
    ACAD_PROXY_OBJECT = 0x1F3   # 499


#: Object types whose body this library decodes into a typed dataclass.
#: Everything else is retained as an opaque record (LibreDWG: "skipped").
MVP_ENTITY_TYPES: Final[frozenset[ObjectType]] = frozenset({
    ObjectType.LINE,
    ObjectType.CIRCLE,
    ObjectType.ARC,
    ObjectType.POINT,
    ObjectType.LWPOLYLINE,
})

#: The proxy types LibreDWG records without decoding the body.
PROXY_TYPES: Final[frozenset[int]] = frozenset({
    int(ObjectType.ACAD_PROXY_ENTITY),   # 498
    int(ObjectType.ACAD_PROXY_OBJECT),   # 499
})


def object_type_name(code: int) -> str:
    """Best-effort human name for an object type code.

    Mirrors LibreDWG's ``dwg_type_name`` / ``dwg_object_name``: known classic
    types get their enum name, the class range is labelled, and anything else
    is reported numerically rather than guessed at.
    """
    try:
        return ObjectType(code).name
    except ValueError:
        pass
    if 500 <= code <= 509:
        return f"FIXED_{code}"          # R13+ fixed types, no symbol in the enum
    if code >= 0x82:
        return f"CLASS_{code}"          # defined by the CLASSES section
    return f"UNKNOWN_{code}"


# ---------------------------------------------------------------------------
# DWG_SECTION_TYPE  (include/dwg.h)
# ---------------------------------------------------------------------------
class SectionType(enum.IntEnum):
    """Section numbers for the R2004+ container (section *page map*)."""

    UNKNOWN = 0            # the very first 160-byte file header
    HEADER = 1             # AcDb:Header
    AUXHEADER = 2          # AcDb:AuxHeader
    CLASSES = 3            # AcDb:Classes
    HANDLES = 4            # AcDb:Handles  (the object map)
    TEMPLATE = 5           # AcDb:Template
    OBJFREESPACE = 6       # AcDb:ObjFreeSpace
    OBJECTS = 7            # AcDb:AcDbObjects
    REVHISTORY = 8         # AcDb:RevHistory
    SUMMARYINFO = 9        # AcDb:SummaryInfo
    PREVIEW = 10           # AcDb:Preview
    APPINFO = 11           # AcDb:AppInfo
    APPINFOHISTORY = 12    # AcDb:AppInfoHistory
    FILEDEPLIST = 13       # AcDb:FileDepList
    SECURITY = 14
    VBAPROJECT = 15
    SIGNATURE = 16
    ACDS = 17
    INFO = 18              # section page map (the "Data Section")
    SYSTEM_MAP = 19


class SectionTypeR13(enum.IntEnum):
    """Section numbers for the R13-R2000 container (section *locator records*)."""

    HEADER = 0
    CLASSES = 1
    HANDLES = 2             # the object map
    OBJFREESPACE = 3        # includes the 2nd header
    TEMPLATE = 4
    AUXHEADER = 5           # R2000 only
    THUMBNAIL = 6


#: Friendly names used in errors and the JSON exporter.
SECTION_NAMES: Final[dict[int, str]] = {
    int(SectionType.HEADER): "HEADER",
    int(SectionType.AUXHEADER): "AUXHEADER",
    int(SectionType.CLASSES): "CLASSES",
    int(SectionType.HANDLES): "HANDLES",
    int(SectionType.TEMPLATE): "TEMPLATE",
    int(SectionType.OBJFREESPACE): "OBJFREESPACE",
    int(SectionType.OBJECTS): "OBJECTS",
    int(SectionType.REVHISTORY): "REVHISTORY",
    int(SectionType.SUMMARYINFO): "SUMMARYINFO",
    int(SectionType.PREVIEW): "PREVIEW",
    int(SectionType.APPINFO): "APPINFO",
    int(SectionType.APPINFOHISTORY): "APPINFOHISTORY",
    int(SectionType.FILEDEPLIST): "FILEDEPLIST",
    int(SectionType.INFO): "INFO",
}


# ---------------------------------------------------------------------------
# Handle codes  (Dwg_Hdl_Code, include/dwg.h)
# ---------------------------------------------------------------------------
class HandleCode(enum.IntEnum):
    """The 4-bit reference code packed into a handle's leading byte.

    Codes above :data:`OFFSET_MAX` are *relative* references: the stored value
    is a delta from the current handle rather than an absolute one.
    """

    OWNER = 0
    SOFTOWN = 2
    HARDOWN = 3
    SOFTPTR = 4       # may be relative
    HARDPTR = 5
    # 6 is the first OFFSETOBJHANDLE code; anything > 6 is relative.
    OFFSET_BASE = 6


#: Values >= this are relative (offsets from the current handle).
OFFSET_MIN: Final[int] = 6


class ColorMethod(enum.IntEnum):
    """``rgb >> 24`` of a R2004+ true-colour value (``bit_read_CMC``)."""

    BYLAYER = 0xC0
    BYBLOCK = 0xC1
    RGB = 0xC2
    ACAD_INDEX = 0xC3
    NONE = 0xC8
    DEFAULT = 0xC2


#: AutoCAD colour index sentinels (``Dwg_Color.index``).
BYLAYER: Final[int] = 256
BYBLOCK: Final[int] = 0


# ---------------------------------------------------------------------------
# Lineweight
# ---------------------------------------------------------------------------
#: ``lweights`` from ``dwg.c`` — the DWG lineweight *index* (0..31) to the DXF
#: group-370 value.  The two are not the same number: DWG stores a small index
#: and DXF writes the value in hundredths of a millimetre, with the last three
#: entries as the ByLayer/ByBlock/ByDefault sentinels.  Indices 24..28 are
#: reserved and read back as 0.
#:
#: This is why a raw byte of ``29`` means "ByLayer" and must be run through
#: :func:`lineweight_value` before it is shown to anyone.
LWEIGHTS: Final[tuple[int, ...]] = (
    0, 5, 9, 13, 15, 18, 20, 25, 30, 35,          # 0..9
    40, 50, 53, 60, 70, 80, 90, 100, 106, 120,     # 10..19
    140, 158, 200, 211,                            # 20..23
    0, 0, 0, 0, 0,                                 # 24..28 reserved
    -1, -2, -3,                                    # 29 ByLayer, 30 ByBlock, 31 ByDefault
)

#: Raw DWG lineweight index for *ByLayer* — the value a decoded entity carries
#: when it inherits its layer's lineweight.  Not ``-1``: that is the *converted*
#: DXF value, which is what :func:`lineweight_value` produces.
LWEIGHT_BYLAYER: Final[int] = 29
LWEIGHT_BYBLOCK: Final[int] = 30
LWEIGHT_DEFAULT: Final[int] = 31


# ---------------------------------------------------------------------------
# AutoCAD Color Index palette  (``rgb_palette``, src/dwg.c)
# ---------------------------------------------------------------------------
#: The 256 ACI entries exactly as LibreDWG ships them in ``src/dwg.c``.
#: Duplicates are intentional: the palette is not a bijection, several indices
#: share an RGB triple, and :func:`find_color_index` therefore returns the
#: *first* index that matches a colour (which is what upstream's linear search
#: does), falling back to 256 with no match.
RGB_PALETTE: Final[tuple[tuple[int, int, int], ...]] = (
    (0x00, 0x00, 0x00), (0xFF, 0x00, 0x00), (0xFF, 0xFF, 0x00), (0x00, 0xFF, 0x00),
    (0x00, 0xFF, 0xFF), (0x00, 0x00, 0xFF), (0xFF, 0x00, 0xFF), (0xFF, 0xFF, 0xFF),
    (0x41, 0x41, 0x41), (0x80, 0x80, 0x80), (0xFF, 0x00, 0x00), (0xFF, 0xAA, 0xAA),
    (0xBD, 0x00, 0x00), (0xBD, 0x7E, 0x7E), (0x81, 0x00, 0x00), (0x81, 0x56, 0x56),
    (0x68, 0x00, 0x00), (0x68, 0x45, 0x45), (0x4F, 0x00, 0x00), (0x4F, 0x35, 0x35),
    (0xFF, 0x3F, 0x00), (0xFF, 0xBF, 0xAA), (0xBD, 0x2E, 0x00), (0xBD, 0x8D, 0x7E),
    (0x81, 0x1F, 0x00), (0x81, 0x60, 0x56), (0x68, 0x19, 0x00), (0x68, 0x4E, 0x45),
    (0x4F, 0x13, 0x00), (0x4F, 0x3B, 0x35), (0xFF, 0x7F, 0x00), (0xFF, 0xD4, 0xAA),
    (0xBD, 0x5E, 0x00), (0xBD, 0x9D, 0x7E), (0x81, 0x40, 0x00), (0x81, 0x6B, 0x56),
    (0x68, 0x34, 0x00), (0x68, 0x56, 0x45), (0x4F, 0x27, 0x00), (0x4F, 0x42, 0x35),
    (0xFF, 0xBF, 0x00), (0xFF, 0xEA, 0xAA), (0xBD, 0x8D, 0x00), (0xBD, 0xAD, 0x7E),
    (0x81, 0x60, 0x00), (0x81, 0x76, 0x56), (0x68, 0x4E, 0x00), (0x68, 0x5F, 0x45),
    (0x4F, 0x3B, 0x00), (0x4F, 0x49, 0x35), (0xFF, 0xFF, 0x00), (0xFF, 0xFF, 0xAA),
    (0xBD, 0xBD, 0x00), (0xBD, 0xBD, 0x7E), (0x81, 0x81, 0x00), (0x81, 0x81, 0x56),
    (0x68, 0x68, 0x00), (0x68, 0x68, 0x45), (0x4F, 0x4F, 0x00), (0x4F, 0x4F, 0x35),
    (0xBF, 0xFF, 0x00), (0xEA, 0xFF, 0xAA), (0x8D, 0xBD, 0x00), (0xAD, 0xBD, 0x7E),
    (0x60, 0x81, 0x00), (0x76, 0x81, 0x56), (0x4E, 0x68, 0x00), (0x5F, 0x68, 0x45),
    (0x3B, 0x4F, 0x00), (0x49, 0x4F, 0x35), (0x7F, 0xFF, 0x00), (0xD4, 0xFF, 0xAA),
    (0x5E, 0xBD, 0x00), (0x9D, 0xBD, 0x7E), (0x40, 0x81, 0x00), (0x6B, 0x81, 0x56),
    (0x34, 0x68, 0x00), (0x56, 0x68, 0x45), (0x27, 0x4F, 0x00), (0x42, 0x4F, 0x35),
    (0x3F, 0xFF, 0x00), (0xBF, 0xFF, 0xAA), (0x2E, 0xBD, 0x00), (0x8D, 0xBD, 0x7E),
    (0x1F, 0x81, 0x00), (0x60, 0x81, 0x56), (0x19, 0x68, 0x00), (0x4E, 0x68, 0x45),
    (0x13, 0x4F, 0x00), (0x3B, 0x4F, 0x35), (0x00, 0xFF, 0x00), (0xAA, 0xFF, 0xAA),
    (0x00, 0xBD, 0x00), (0x7E, 0xBD, 0x7E), (0x00, 0x81, 0x00), (0x56, 0x81, 0x56),
    (0x00, 0x68, 0x00), (0x45, 0x68, 0x45), (0x00, 0x4F, 0x00), (0x35, 0x4F, 0x35),
    (0x00, 0xFF, 0x3F), (0xAA, 0xFF, 0xBF), (0x00, 0xBD, 0x2E), (0x7E, 0xBD, 0x8D),
    (0x00, 0x81, 0x1F), (0x56, 0x81, 0x60), (0x00, 0x68, 0x19), (0x45, 0x68, 0x4E),
    (0x00, 0x4F, 0x13), (0x35, 0x4F, 0x3B), (0x00, 0xFF, 0x7F), (0xAA, 0xFF, 0xD4),
    (0x00, 0xBD, 0x5E), (0x7E, 0xBD, 0x9D), (0x00, 0x81, 0x40), (0x56, 0x81, 0x6B),
    (0x00, 0x68, 0x34), (0x45, 0x68, 0x56), (0x00, 0x4F, 0x27), (0x35, 0x4F, 0x42),
    (0x00, 0xFF, 0xBF), (0xAA, 0xFF, 0xEA), (0x00, 0xBD, 0x8D), (0x7E, 0xBD, 0xAD),
    (0x00, 0x81, 0x60), (0x56, 0x81, 0x76), (0x00, 0x68, 0x4E), (0x45, 0x68, 0x5F),
    (0x00, 0x4F, 0x3B), (0x35, 0x4F, 0x49), (0x00, 0xFF, 0xFF), (0xAA, 0xFF, 0xFF),
    (0x00, 0xBD, 0xBD), (0x7E, 0xBD, 0xBD), (0x00, 0x81, 0x81), (0x56, 0x81, 0x81),
    (0x00, 0x68, 0x68), (0x45, 0x68, 0x68), (0x00, 0x4F, 0x4F), (0x35, 0x4F, 0x4F),
    (0x00, 0xBF, 0xFF), (0xAA, 0xEA, 0xFF), (0x00, 0x8D, 0xBD), (0x7E, 0xAD, 0xBD),
    (0x00, 0x60, 0x81), (0x56, 0x76, 0x81), (0x00, 0x4E, 0x68), (0x45, 0x5F, 0x68),
    (0x00, 0x3B, 0x4F), (0x35, 0x49, 0x4F), (0x00, 0x7F, 0xFF), (0xAA, 0xD4, 0xFF),
    (0x00, 0x5E, 0xBD), (0x7E, 0x9D, 0xBD), (0x00, 0x40, 0x81), (0x56, 0x6B, 0x81),
    (0x00, 0x34, 0x68), (0x45, 0x56, 0x68), (0x00, 0x27, 0x4F), (0x35, 0x42, 0x4F),
    (0x00, 0x3F, 0xFF), (0xAA, 0xBF, 0xFF), (0x00, 0x2E, 0xBD), (0x7E, 0x8D, 0xBD),
    (0x00, 0x1F, 0x81), (0x56, 0x60, 0x81), (0x00, 0x19, 0x68), (0x45, 0x4E, 0x68),
    (0x00, 0x13, 0x4F), (0x35, 0x3B, 0x4F), (0x00, 0x00, 0xFF), (0xAA, 0xAA, 0xFF),
    (0x00, 0x00, 0xBD), (0x7E, 0x7E, 0xBD), (0x00, 0x00, 0x81), (0x56, 0x56, 0x81),
    (0x00, 0x00, 0x68), (0x45, 0x45, 0x68), (0x00, 0x00, 0x4F), (0x35, 0x35, 0x4F),
    (0x3F, 0x00, 0xFF), (0xBF, 0xAA, 0xFF), (0x2E, 0x00, 0xBD), (0x8D, 0x7E, 0xBD),
    (0x1F, 0x00, 0x81), (0x60, 0x56, 0x81), (0x19, 0x00, 0x68), (0x4E, 0x45, 0x68),
    (0x13, 0x00, 0x4F), (0x3B, 0x35, 0x4F), (0x7F, 0x00, 0xFF), (0xD4, 0xAA, 0xFF),
    (0x5E, 0x00, 0xBD), (0x9D, 0x7E, 0xBD), (0x40, 0x00, 0x81), (0x6B, 0x56, 0x81),
    (0x34, 0x00, 0x68), (0x56, 0x45, 0x68), (0x27, 0x00, 0x4F), (0x42, 0x35, 0x4F),
    (0xBF, 0x00, 0xFF), (0xEA, 0xAA, 0xFF), (0x8D, 0x00, 0xBD), (0xAD, 0x7E, 0xBD),
    (0x60, 0x00, 0x81), (0x76, 0x56, 0x81), (0x4E, 0x00, 0x68), (0x5F, 0x45, 0x68),
    (0x3B, 0x00, 0x4F), (0x49, 0x35, 0x4F), (0xFF, 0x00, 0xFF), (0xFF, 0xAA, 0xFF),
    (0xBD, 0x00, 0xBD), (0xBD, 0x7E, 0xBD), (0x81, 0x00, 0x81), (0x81, 0x56, 0x81),
    (0x68, 0x00, 0x68), (0x68, 0x45, 0x68), (0x4F, 0x00, 0x4F), (0x4F, 0x35, 0x4F),
    (0xFF, 0x00, 0xBF), (0xFF, 0xAA, 0xEA), (0xBD, 0x00, 0x8D), (0xBD, 0x7E, 0xAD),
    (0x81, 0x00, 0x60), (0x81, 0x56, 0x76), (0x68, 0x00, 0x4E), (0x68, 0x45, 0x5F),
    (0x4F, 0x00, 0x3B), (0x4F, 0x35, 0x49), (0xFF, 0x00, 0x7F), (0xFF, 0xAA, 0xD4),
    (0xBD, 0x00, 0x5E), (0xBD, 0x7E, 0x9D), (0x81, 0x00, 0x40), (0x81, 0x56, 0x6B),
    (0x68, 0x00, 0x34), (0x68, 0x45, 0x56), (0x4F, 0x00, 0x27), (0x4F, 0x35, 0x42),
    (0xFF, 0x00, 0x3F), (0xFF, 0xAA, 0xBF), (0xBD, 0x00, 0x2E), (0xBD, 0x7E, 0x8D),
    (0x81, 0x00, 0x1F), (0x81, 0x56, 0x60), (0x68, 0x00, 0x19), (0x68, 0x45, 0x4E),
    (0x4F, 0x00, 0x13), (0x4F, 0x35, 0x3B), (0x33, 0x33, 0x33), (0x50, 0x50, 0x50),
    (0x69, 0x69, 0x69), (0x82, 0x82, 0x82), (0xBE, 0xBE, 0xBE), (0xFF, 0xFF, 0xFF),
)


def rgb_palette_index(index: int) -> int:
    """``dwg_rgb_palette_index`` — ACI index to a packed ``0xRRGGBB`` value.

    Indices at or above 256 are out of range and yield 0, matching upstream.
    Used to give a pre-R2004 colour the true-colour word it would have been
    written with from R2004 on.
    """
    if index >= 256:
        return 0
    r, g, b = RGB_PALETTE[index]
    return (r << 16) | (g << 8) | b


def find_color_index(rgb: int) -> int:
    """``dwg_find_color_index`` — packed ``0xRRGGBB`` to an ACI index.

    A linear search over the palette for the identical triple, returning the
    first hit; 256 (*ByLayer*) when the colour is not in the palette at all.
    ``bit_read_CMC`` calls this from R2004 on to overwrite the stream's own
    index with one derived from the true colour.
    """
    r = (rgb >> 16) & 0xFF
    g = (rgb >> 8) & 0xFF
    b = rgb & 0xFF
    for i, entry in enumerate(RGB_PALETTE):
        if entry == (r, g, b):
            return i
    return BYLAYER


def lineweight_value(index: int) -> int:
    """Convert a raw DWG lineweight index to the DXF group-370 value.

    Translation of ``dxf_cvt_lweight`` in ``dwg.c``: the index is taken modulo
    32, so the packed layer-flag form (``(flag0 & 0x03E0) >> 5``) and the raw
    entity byte share one conversion.  Returns ``-1`` for ByLayer, ``-2`` for
    ByBlock, ``-3`` for ByDefault, otherwise hundredths of a millimetre.
    """
    return LWEIGHTS[index % 32]
