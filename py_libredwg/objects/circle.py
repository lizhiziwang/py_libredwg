# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""``CIRCLE`` (type 0x12).

Upstream: ``DWG_ENTITY (CIRCLE)`` in ``src/dwg.spec``.  For R13+ the body is
just ``3BD center``, ``BD radius``, then the two common optional fields
``BT0 thickness`` and ``BE extrusion`` — the same four fields ``ARC`` reads
before its own angles, which is why :class:`~py_libredwg.objects.arc.Arc`
derives from this class rather than repeating them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..geometry import Vec3
from .base_object import Entity

__all__ = ["Circle"]


@dataclass(slots=True)
class Circle(Entity):
    """``CIRCLE`` (type 0x12) — centre plus radius in the extrusion plane."""

    center: Vec3 = field(default_factory=Vec3)
    radius: float = 0.0
    thickness: float = 0.0

    @property
    def diameter(self) -> float:
        return self.radius * 2.0

    @property
    def geometry_type(self) -> str:
        return "Polygon"

    def to_dict(self) -> dict[str, Any]:
        d = super(Circle, self).to_dict()
        d.update({
            "center": list(self.center),
            "radius": self.radius,
        })
        if self.thickness:
            d["thickness"] = self.thickness
        return d
