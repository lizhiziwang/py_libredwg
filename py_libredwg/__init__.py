# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""py-libredwg — read DWG files in pure Python.

**This is a derivative work of GNU LibreDWG.**  LibreDWG is Copyright (C)
2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc. and is licensed
under the GNU General Public License, version 3 or later.  The decoding logic
here is translated from LibreDWG's C source — principally ``bit.c``, ``dwg.c``,
``decode.c``, ``decode_r2007.c``, ``dwg_objects.c`` and the ``*.spec`` field
descriptions — so this package is distributed under **the same licence,
GPL-3.0-or-later**.  See ``license_notice.md`` next to this file for the full
statement, and the repository ``LICENSE`` for the licence text.

This is not an independent implementation of the DWG format.

Quick start
-----------

::

    from py_libredwg import DwgDecoder
    from py_libredwg.exporters import export_geojson, export_json

    doc = DwgDecoder("drawing.dwg").parse()

    print(doc.version_tag)              # 'AC1032'
    print(len(doc.entities))            # 1573
    print(doc.entities[0].layer)        # 'WALLS'

    export_geojson(doc, "drawing.geojson")
    export_json(doc, "drawing.json")

What is decoded
---------------

Read-only, versions R13 through R2018 (``AC1015`` .. ``AC1032``).  The sections
HEADER, CLASSES, TABLES, BLOCKS and ENTITIES are processed; five entity types
are decoded to typed objects — ``LINE``, ``LWPOLYLINE``, ``CIRCLE``, ``ARC``
and ``POINT`` — and everything else is retained with its raw bytes and its type
name.  Proxy entities (in practice Tianzheng / 天正 entities) are never decoded
internally: their payload is preserved verbatim, which is what LibreDWG does
too.

There is no writer.  This library cannot modify, re-save or convert to DWG.

Module map
----------

===============================================  ==========================
:mod:`~py_libredwg.dwg_decoder`                    :class:`DwgDecoder`, the entry point
:mod:`~py_libredwg.bitreader`                      the bit-level reader
:mod:`~py_libredwg.header`                         file header, CRC, R2004/R2007 headers
:mod:`~py_libredwg.sections`                       sections, containers, the object decoder
:mod:`~py_libredwg.objects`                        one dataclass per DWG type
:mod:`~py_libredwg.exporters`                      GeoJSON and JSON writers
:mod:`~py_libredwg.dwg_parser`                     :func:`parser` — path and format in, result out
:mod:`~py_libredwg.constants`                      version and type enumerations
:mod:`~py_libredwg.errors`                         the exception hierarchy
===============================================  ==========================
"""

from __future__ import annotations

from . import constants, errors, geometry, header, sections
from .bitreader import BitReader, Handle
from .constants import DwgVersion, ObjectType, SectionType
from .dwg_decoder import DwgDecoder, read_dwg
from .errors import (
    BitStreamError,
    DwgCRCError,
    DwgDecodeError,
    DwgError,
    DwgFormatError,
    DwgSectionError,
    UnsupportedObjectError,
    UnsupportedVersionError,
)
from .header import FileHeader, HeaderVariables
from .objects import (
    Arc,
    BlockHeader,
    Circle,
    CMColor,
    ControlObject,
    DwgObject,
    Entity,
    Layer,
    Line,
    LWPolyline,
    LWPolylineVertex,
    Point,
    ProxyEntity,
    TableEntry,
    UnknownObject,
)
from .sections import DwgClass

__version__ = "0.1.0"

__all__ = [
    # entry point
    "DwgDecoder",
    "read_dwg",
    # bit level
    "BitReader",
    "Handle",
    # header
    "FileHeader",
    "HeaderVariables",
    # objects
    "DwgObject",
    "Entity",
    "ProxyEntity",
    "UnknownObject",
    "TableEntry",
    "Layer",
    "BlockHeader",
    "ControlObject",
    "CMColor",
    "Line",
    "Circle",
    "Arc",
    "Point",
    "LWPolyline",
    "LWPolylineVertex",
    # classes
    "DwgClass",
    # enums
    "DwgVersion",
    "ObjectType",
    "SectionType",
    # errors
    "DwgError",
    "DwgDecodeError",
    "DwgFormatError",
    "DwgCRCError",
    "DwgSectionError",
    "BitStreamError",
    "UnsupportedVersionError",
    "UnsupportedObjectError",
    # submodules
    "constants",
    "errors",
    "geometry",
    "header",
    "sections",
    "__version__",
]
