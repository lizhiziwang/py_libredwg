# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""``LWPOLYLINE`` (type 0x4D) and its vertices.

Upstream: ``DWG_ENTITY (LWPOLYLINE)`` in ``src/dwg.spec``.  This is a
*data-only* object: unlike the other MVP entities it carries no handles of its
own beyond the common entity handle stream, so it is decoded entirely from the
value stream.

The body is flag-driven, which is why the decoder in
:mod:`py_libredwg.sections` has to read ``flag`` first and then conditionally
read four optional scalars, three optional counts, and the vertex array.  The
flag bits, from the upstream comment::

    1    extrusion present
    2    thickness present
    4    const_width present
    8    elevation present
    16   num_bulges present
    32   num_widths present
    512  closed
    1024 num_vertexids present (R2010+ only)

Vertices themselves are ``2DD`` pairs from R2000 (``2RD`` before that) and are
always x/y — the elevation lives once on the polyline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base_object import Entity

__all__ = ["LWPolyline", "LWPolylineVertex"]


@dataclass(slots=True)
class LWPolylineVertex:
    """One vertex of an ``LWPOLYLINE``.

    LWPOLYLINE vertices carry only x/y — the elevation is on the polyline
    itself — plus optional width and a bulge factor.  The bulge is
    ``tan(sweep/4)`` and describes the arc segment *starting* at this vertex;
    a bulge of 0 means a straight segment follows.
    """

    x: float = 0.0
    y: float = 0.0
    start_width: float = 0.0
    end_width: float = 0.0
    bulge: float = 0.0

    def as_xy(self) -> tuple[float, float]:
        return (self.x, self.y)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"x": self.x, "y": self.y}
        if self.bulge:
            d["bulge"] = self.bulge
        if self.start_width or self.end_width:
            d["start_width"] = self.start_width
            d["end_width"] = self.end_width
        return d


@dataclass(slots=True)
class LWPolyline(Entity):
    """``LWPOLYLINE`` (type 0x4D) — a lightweight 2D polyline.

    Vertices are 2D in the polyline's own plane at :attr:`elevation`; the
    :attr:`extrusion` maps that plane into world space.  This is the most
    common entity in modern drawings by a wide margin.
    """

    vertices: list[LWPolylineVertex] = field(default_factory=list)
    elevation: float = 0.0
    thickness: float = 0.0
    constant_width: float = 0.0

    #: Raw bit flags.  Bit 0 = closed, bit 7 = plinegen, bit 8 = continuous
    #: linetype generated.
    flags: int = 0

    @property
    def closed(self) -> bool:
        """True when the polyline forms a closed loop (flag bit 0)."""
        return bool(self.flags & 1)

    @property
    def point_count(self) -> int:
        return len(self.vertices)

    @property
    def has_bulges(self) -> bool:
        return any(v.bulge for v in self.vertices)

    @property
    def geometry_type(self) -> str:
        """``Polygon`` for closed rings, otherwise ``LineString``.

        LibreDWG applies the same rule in ``dwg_geojson_LWPOLYLINE``: a closed
        polyline with more than three points is a polygon, everything else is
        a line string.
        """
        if self.closed and len(self.vertices) > 3:
            return "Polygon"
        return "LineString"

    def to_dict(self) -> dict[str, Any]:
        d = super(LWPolyline, self).to_dict()
        d.update({
            "vertices": [v.to_dict() for v in self.vertices],
            "closed": self.closed,
            "elevation": self.elevation,
        })
        if self.constant_width:
            d["constant_width"] = self.constant_width
        if self.thickness:
            d["thickness"] = self.thickness
        return d
