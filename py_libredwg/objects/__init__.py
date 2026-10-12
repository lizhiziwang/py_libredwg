# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""Decoded DWG object types.

Where LibreDWG has one tagged ``Dwg_Object`` union and a giant ``switch`` over
the type code, this package has one class per DWG type and a small registry
(:data:`ENTITY_CLASSES`) that the object decoder consults.  Adding a type is
therefore additive: write the class, add one line to the registry.

Import surface
--------------
Base classes come from :mod:`~py_libredwg.objects.base_object`; the five MVP
entity types come from their own modules, one per ``DWG_ENTITY`` block in
``src/dwg.spec``.
"""

from __future__ import annotations

from ..constants import ObjectType
from .arc import Arc
from .base_object import (
    BlockHeader,
    CMColor,
    ControlObject,
    DwgObject,
    Entity,
    Layer,
    ProxyEntity,
    TableEntry,
    UnknownObject,
)
from .circle import Circle
from .line import Line
from .lwpolyline import LWPolyline, LWPolylineVertex
from .point import Point

__all__ = [
    # base
    "CMColor",
    "DwgObject",
    "Entity",
    "ProxyEntity",
    "UnknownObject",
    "TableEntry",
    "Layer",
    "BlockHeader",
    "ControlObject",
    # MVP entities
    "Line",
    "Circle",
    "Arc",
    "Point",
    "LWPolyline",
    "LWPolylineVertex",
    # registry
    "ENTITY_CLASSES",
    "entity_class_for",
]

#: On-stream object type -> the dataclass that decodes it.
#:
#: Only the types the MVP actually reads appear here.  Everything else is
#: decoded as a :class:`~py_libredwg.objects.base_object.UnknownObject` (body
#: retained, fields not interpreted), which is exactly what LibreDWG does with
#: a type it has no spec for.
ENTITY_CLASSES: dict[int, type[DwgObject]] = {
    int(ObjectType.LINE): Line,
    int(ObjectType.CIRCLE): Circle,
    int(ObjectType.ARC): Arc,
    int(ObjectType.POINT): Point,
    int(ObjectType.LWPOLYLINE): LWPolyline,
}


def entity_class_for(type_code: int) -> type[DwgObject] | None:
    """Return the decoder class for an object type, or ``None`` if unknown.

    A ``None`` return is not an error: it means "keep the bytes, skip the
    fields", the same contract as the proxy path.
    """
    return ENTITY_CLASSES.get(type_code)
