# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""``POINT`` (type 0x1B).

Upstream: ``DWG_ENTITY (POINT)`` in ``src/dwg.spec``.  R13+ stores
``BD x``, ``BD y``, ``BD z``, ``BT0 thickness``, ``BE extrusion``,
``BD x_ang``.  ``x_ang`` is the angle of the point's own X axis in the OCS and
is 0 for an ordinary point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..geometry import Vec3
from .base_object import Entity

__all__ = ["Point"]


@dataclass(slots=True)
class Point(Entity):
    """``POINT`` (type 0x1B) — a single location."""

    location: Vec3 = field(default_factory=Vec3)
    thickness: float = 0.0
    #: Angle of the point's X axis, in radians.
    angle: float = 0.0

    @property
    def geometry_type(self) -> str:
        return "Point"

    def to_dict(self) -> dict[str, Any]:
        d = super(Point, self).to_dict()
        d["location"] = list(self.location)
        if self.thickness:
            d["thickness"] = self.thickness
        if self.angle:
            d["angle"] = self.angle
        return d
