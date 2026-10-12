# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""Geometry primitives and curve helpers shared by the parser and exporters.

LibreDWG stores coordinates in flat C structs (``BITCODE_3BD``,
``BITCODE_2RD`` ...).  Here they are a small immutable :class:`Vec3` value
type: cheap, hashable, and free of the aliasing that C's pointers allowed.

The curve helpers (:func:`angle_normalize`, :func:`arc_split`,
:func:`curve_segments`) are translations of ``geom.c`` and of
``calc_curve_segments`` in ``out_geojson.c``.  They are reproduced faithfully
*including* the magic constants, so that tessellated circles and arcs come out
with the same vertex count and the same coordinates as LibreDWG's GeoJSON
writer.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

__all__ = [
    "Vec3",
    "angle_normalize",
    "angle_vector_2d",
    "arc_split",
    "curve_segments",
    "TWO_PI",
]

TWO_PI = 2.0 * math.pi


@dataclass(frozen=True, slots=True)
class Vec3:
    """A 3D point / vector, mirroring ``BITCODE_3BD``.

    Missing ``z`` defaults to ``0.0``, which is how DWG encodes 2D points
    (``BITCODE_2RD``, ``BITCODE_2BD``) promoted to 3D.
    """

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    # -- construction -----------------------------------------------------
    @classmethod
    def from_2d(cls, x: float, y: float) -> Vec3:
        return cls(float(x), float(y), 0.0)

    @classmethod
    def from_iterable(cls, values: Iterable[float]) -> Vec3:
        vals = list(values)
        if len(vals) == 2:
            return cls(float(vals[0]), float(vals[1]), 0.0)
        if len(vals) >= 3:
            return cls(float(vals[0]), float(vals[1]), float(vals[2]))
        raise ValueError(f"need 2 or 3 coordinates, got {len(vals)}")

    # -- conversion -------------------------------------------------------
    def as_tuple(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)

    def as_xy(self) -> tuple[float, float]:
        return (self.x, self.y)

    def __iter__(self) -> Iterator[float]:
        yield self.x
        yield self.y
        yield self.z

    def is_zero(self, tol: float = 0.0) -> bool:
        return abs(self.x) <= tol and abs(self.y) <= tol and abs(self.z) <= tol

    # -- vector algebra ---------------------------------------------------
    def __add__(self, other: Vec3) -> Vec3:
        return Vec3(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other: Vec3) -> Vec3:
        return Vec3(self.x - other.x, self.y - other.y, self.z - other.z)

    def __mul__(self, scalar: float) -> Vec3:
        return Vec3(self.x * scalar, self.y * scalar, self.z * scalar)

    __rmul__ = __mul__

    def length(self) -> float:
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z)


# ---------------------------------------------------------------------------
# Curve helpers (translated from src/geom.c and src/out_geojson.c)
# ---------------------------------------------------------------------------


def angle_normalize(angle: float) -> float:
    """Normalize an angle to ``[0, 2*pi)``.

    Translation of ``angle_normalize`` in ``geom.c``.  Non-finite input maps
    to ``0.0`` rather than propagating ``nan`` — LibreDWG made the same choice
    so that a corrupt angle cannot poison an entire geometry.
    """
    if not math.isfinite(angle):
        return 0.0
    angle = math.fmod(angle, TWO_PI)
    if angle < 0.0:
        angle += TWO_PI
    return angle


def angle_vector_2d(center: Vec3, angle: float, length: float) -> Vec3:
    """Shoot a vector of ``length`` from ``center`` at ``angle``.

    Translation of ``angle_vector_2d`` in ``geom.c``.
    """
    return Vec3(
        center.x + (length * math.cos(angle)),
        center.y + (length * math.sin(angle)),
        0.0,
    )


def curve_segments(radius: float, angle_span: float,
                   extent: float, *, min_pts: int = 16,
                   max_pts: int = 256, base: float = 128.0) -> int:
    """Number of segments to tessellate a curve into.

    Translation of ``calc_curve_segments`` in ``out_geojson.c``::

        extent   = max(|LIMMAX.x - LIMMIN.x|, |LIMMAX.y - LIMMIN.y|)
        scale    = sqrt(|radius| / extent)
        num_pts  = (int)(128.0 * scale * (angle_span / (2 * PI)))
        clamp to [16, 256]

    ``extent`` is the drawing's limit diagonal from the HEADER section
    (``LIMMAX``/``LIMMIN``).  When the limits are degenerate LibreDWG falls
    back to ``extent = 1.0``; we do the same so the result stays defined.
    """
    if not math.isfinite(extent) or abs(extent) < 1e-12:
        extent = 1.0
    scale = math.sqrt(abs(radius) / abs(extent)) if extent else 0.0
    num_pts = int(base * scale * (angle_span / TWO_PI))
    if num_pts < min_pts:
        num_pts = min_pts
    if num_pts > max_pts:
        num_pts = max_pts
    return num_pts


def arc_split(num_pts: int, center: Vec3, start_angle: float,
              end_angle: float, length: float) -> list[Vec3]:
    """Tessellate an arc into ``num_pts`` points (exclusive of the closing one).

    Translation of ``arc_split`` in ``geom.c``.  The returned list is *open*:
    it contains ``num_pts`` points spanning ``[start_angle, end_angle)``.
    Callers that need a closed ring append the first point themselves, which
    is what LibreDWG's GeoJSON writer does.
    """
    if num_pts <= 0:
        return []
    start_angle = angle_normalize(start_angle)
    end_angle = angle_normalize(end_angle)
    if end_angle < start_angle:
        end_angle += TWO_PI

    angle = start_angle
    step = (end_angle - start_angle) / num_pts
    pts: list[Vec3] = []
    for _ in range(num_pts):
        pts.append(angle_vector_2d(center, angle, length))
        angle += step
    return pts
