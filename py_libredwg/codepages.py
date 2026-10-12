# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""DWG codepage identifiers and their Python codec names.

A pre-R2007 DWG stores text as single bytes plus a ``$DWGCODEPAGE`` number in
the HEADER section naming the byte-to-character mapping to use.  R2007 and
later abandon this and store UCS-2 instead.

The numbers come from ``src/codepages.h`` (``Dwg_Codepage``).  Upstream maps
them through libiconv; Python's own codec registry covers the same ground, so
the table below maps each DWG number to a Python codec name.  Where DWG has
two identifiers for one mapping (for example ``CP_CP932`` and ``CP_ANSI_932``
are both Shift-JIS variants) the more specific Python name is used.
"""

from __future__ import annotations

from typing import Final

__all__ = ["DWGPAGE_TO_CODEC", "codepage_to_python", "CP_UNDEFINED"]

#: ``CP_UNDEFINED`` — used mostly by R11 files, treated as ASCII.
CP_UNDEFINED: Final[int] = 0xFF

#: DWG codepage number -> Python codec name (``src/codepages.h``).
DWGPAGE_TO_CODEC: Final[dict[int, str]] = {
    0: "utf-8",            # CP_UTF8
    1: "ascii",            # CP_US_ASCII
    2: "iso8859-1",        # CP_ISO_8859_1
    3: "iso8859-2",        # CP_ISO_8859_2
    4: "iso8859-3",        # CP_ISO_8859_3
    5: "iso8859-4",        # CP_ISO_8859_4
    6: "iso8859-5",        # CP_ISO_8859_5
    7: "iso8859-6",        # CP_ISO_8859_6
    8: "iso8859-7",        # CP_ISO_8859_7
    9: "iso8859-8",        # CP_ISO_8859_8
    10: "iso8859-9",       # CP_ISO_8859_9
    11: "cp437",           # CP_CP437  DOS English
    12: "cp850",           # CP_CP850  DOS Latin-1
    13: "cp852",           # CP_CP852  DOS Central European
    14: "cp855",           # CP_CP855  DOS Cyrillic
    15: "cp857",           # CP_CP857  DOS Turkish
    16: "cp860",           # CP_CP860  DOS Portuguese
    17: "cp861",           # CP_CP861  DOS Icelandic
    18: "cp863",           # CP_CP863  DOS Hebrew
    19: "cp864",           # CP_CP864  DOS Arabic (IBM)
    20: "cp865",           # CP_CP865  DOS Nordic
    21: "cp869",           # CP_CP869  DOS Greek
    22: "cp932",           # CP_CP932  DOS Japanese (shift-jis)
    23: "mac-roman",       # CP_MACINTOSH
    24: "big5",            # CP_BIG5
    25: "cp949",           # CP_CP949  Korean (Wansung + Johab)
    26: "johab",           # CP_JOHAB
    27: "cp866",           # CP_CP866  Russian
    28: "cp1250",          # CP_ANSI_1250 Central + Eastern European
    29: "cp1251",          # CP_ANSI_1251 Cyrillic
    30: "cp1252",          # CP_ANSI_1252 Western European  (the common default)
    31: "gb2312",          # CP_GB2312     EUC-CN Chinese
    32: "cp1253",          # CP_ANSI_1253  Greek
    33: "cp1254",          # CP_ANSI_1254  Turkish
    34: "cp1255",          # CP_ANSI_1255  Hebrew
    35: "cp1256",          # CP_ANSI_1256  Arabic
    36: "cp1257",          # CP_ANSI_1257  Baltic
    37: "cp874",           # CP_ANSI_874   Thai
    38: "cp932",           # CP_ANSI_932   Japanese (windows-31j)
    39: "cp936",           # CP_ANSI_936   Simplified Chinese
    40: "cp949",           # CP_ANSI_949   Korean Wansung
    41: "cp950",           # CP_ANSI_950   Traditional Chinese
    42: "johab",           # CP_ANSI_1361  Korean Wansung
    43: "utf-16",          # CP_UTF16
    44: "cp1258",          # CP_ANSI_1258  Vietnamese
    CP_UNDEFINED: "ascii",
}


def codepage_to_python(codepage: int) -> str:
    """Return the Python codec for a DWG codepage number.

    Unknown numbers fall back to ``cp1252``, which is what AutoCAD uses by
    default and what LibreDWG effectively assumes for western drawings.
    The caller is expected to handle ``UnicodeDecodeError`` regardless, since
    real files mislabel their text more often than one would hope.
    """
    return DWGPAGE_TO_CODEC.get(codepage, "cp1252")
