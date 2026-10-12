# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""``LINE`` (type 0x13).

Upstream: ``DWG_ENTITY (LINE)`` in ``src/dwg.spec``, with the common entity
prefix from ``src/common_entity_data.spec``.

The on-stream encoding differs by version in a way worth remembering when
reading the decoder in :mod:`py_libredwg.sections`:

* R13–R14 store two plain ``3BD`` points (``start``, ``end``).
* R2000+ store a ``z_is_zero`` bit; when it is set both z values are zero and
  are not written at all, otherwise they follow as ``RD`` + ``DD``.  The x and
  y are ``RD`` for the start point and *differential* ``DD`` for the end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..geometry import Vec3
from .base_object import Entity

__all__ = ["Line"]


@dataclass(slots=True)
class Line(Entity):
    """``LINE`` (type 0x13) — a straight segment.

    DWG keeps ``start``/``end`` plus a thickness along the extrusion; the
    extrusion plus the two endpoints fully defines the 3D geometry.
    """

    start: Vec3 = field(default_factory=Vec3)
    end: Vec3 = field(default_factory=Vec3)
    thickness: float = 0.0

    @property
    def length(self) -> float:
        return (self.end - self.start).length()

    @property
    def geometry_type(self) -> str:
        return "LineString"

    def to_dict(self) -> dict[str, Any]:
        d = super(Line, self).to_dict()
        d.update({
            "start": list(self.start),
            "end": list(self.end),
            "length": self.length,
        })
        if self.thickness:
            d["thickness"] = self.thickness
        return d
