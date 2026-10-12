# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""``ARC`` (type 0x11).

Upstream: ``DWG_ENTITY (ARC)`` in ``src/dwg.spec``.  On stream an arc is a
circle's fields (center, radius, thickness, extrusion) followed by
``BD start_angle`` and ``BD end_angle``, both in radians.  The inheritance
below mirrors that shared prefix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .circle import Circle

__all__ = ["Arc"]


@dataclass(slots=True)
class Arc(Circle):
    """``ARC`` (type 0x11) — a circle with a start and end angle.

    DWG models an arc as exactly a circle plus two angles, so this inherits
    :class:`~py_libredwg.objects.circle.Circle` rather than repeating its
    fields.  Angles are radians in the entity's own coordinate system (i.e.
    in the extrusion plane).
    """

    start_angle: float = 0.0
    end_angle: float = 0.0

    @property
    def sweep(self) -> float:
        """Signed angular span in radians, always positive.

        DWG stores the two angles independently and does not guarantee
        ``end >= start``; the arc runs counter-clockwise from start to end, so
        a wrapped end angle is corrected the same way ``arc_split`` does it.
        """
        from ..geometry import TWO_PI, angle_normalize

        start = angle_normalize(self.start_angle)
        end = angle_normalize(self.end_angle)
        if end < start:
            end += TWO_PI
        return end - start

    @property
    def geometry_type(self) -> str:
        return "Polygon"

    def to_dict(self) -> dict[str, Any]:
        d = super(Arc, self).to_dict()
        d.update({
            "center": list(self.center),
            "radius": self.radius,
            "start_angle": self.start_angle,
            "end_angle": self.end_angle,
        })
        if self.thickness:
            d["thickness"] = self.thickness
        return d
