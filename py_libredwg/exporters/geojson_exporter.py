# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""GeoJSON export — a translation of LibreDWG's ``out_geojson.c``.

The point of following ``out_geojson.c`` rather than inventing a mapping is
comparability: ``dwgread -O geojson`` and :func:`to_geojson` should produce the
same features, the same coordinates and the same properties for the same file.
Anywhere they differ, either this library or the test is wrong — which is
exactly the signal the test suite is built to catch.

The mapping, as upstream defines it
-----------------------------------

===========================  ==================================================
Entity                       Geometry
===========================  ==================================================
``LINE``                     ``LineString`` — start, end, 3D
``LWPOLYLINE`` open           ``LineString`` — the vertices, 2D
``LWPOLYLINE`` closed         ``Polygon`` — a single ring, first point repeated
``CIRCLE``                   ``Polygon`` — ring closed by repeating the first
``ARC``                      ``Polygon`` — up to and including the closing point
``POINT``                    ``Point`` — 3D when ``z`` is non-zero
===========================  ==================================================

Two upstream choices are worth flagging because they surprise people:

* An **arc is a ``Polygon``**, not a ``LineString``.  The ring is closed across
  the chord, which is what ``out_geojson.c`` does.  It is not what a GIS
  consumer expects from an arc, but matching it is the whole point.
* A **closed ``LWPOLYLINE`` is a ``Polygon`` only when it has more than three
  points**; three or fewer falls through to ``LineString``.

Both are reproduced faithfully.  Where this module deliberately diverges, it is
noted at the divergence.

Known limitations (inherited from upstream, not introduced here)
----------------------------------------------------------------

* **Bulges are ignored.**  ``LWPOLYLINE`` vertices carry a bulge that makes the
  segment between the vertex and the next one an arc; ``out_geojson.c`` has
  ``// TODO bulges, splines, ...`` on that line, so the segment is emitted
  straight.  A drawing that uses bulges for curves will export as polylines
  with slightly cut corners.  :attr:`~py_libredwg.objects.lwpolyline.LWPolyline.has_bulges`
  tells you whether a given entity is affected.
* **Curve tessellation is resolution-limited.**  A circle becomes a 16- to
  256-sided polygon, the count chosen by :func:`~py_libredwg.geometry.curve_segments`
  from the radius and the drawing extents.  The algorithm is upstream's, so the
  vertex counts match, but a very large or very small drawing still gets the
  same 16/256 floor and ceiling.
* **Only the MVP entity types produce geometry.**  Everything else — including
  every proxy — is skipped, which mirrors ``dwg_geojson_object``'s ``default:``
  branch returning 0.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from typing import IO, Any

from ..constants import ColorMethod, DwgVersion, ObjectType
from ..geometry import TWO_PI, Vec3, arc_split, curve_segments
from ..objects.arc import Arc
from ..objects.base_object import Entity, ProxyEntity
from ..objects.circle import Circle
from ..objects.line import Line
from ..objects.lwpolyline import LWPolyline
from ..objects.point import Point

__all__ = [
    "to_geojson",
    "entity_to_feature",
    "export_geojson",
    "write_geojson",
    "SUBCLASSES",
]

#: The ``SubClasses`` property, keyed by object type.  Upstream writes these
#: as literal strings at each call site; the strings are the same ones
#: LibreDWG's DXF classes use, so a consumer that already understands DXF
#: subclass names can read them.
SUBCLASSES: dict[int, str] = {
    int(ObjectType.LINE): "AcDbEntity : AcDbLine",
    int(ObjectType.CIRCLE): "AcDbEntity : AcDbCircle",
    int(ObjectType.ARC): "AcDbEntity : AcDbArc",
    int(ObjectType.POINT): "AcDbEntity : AcDbPoint",
    int(ObjectType.LWPOLYLINE): "AcDbEntity : AcDbLwPolyline",
}

#: These two colour methods only make sense from R2004 on, which is why the
#: ``Color`` property's *shape* depends on the file version.
_TRUE_COLOR_METHODS = frozenset(
    (int(ColorMethod.RGB), int(ColorMethod.ACAD_INDEX)))
_UNSET_COLOR_METHODS = frozenset(
    (int(ColorMethod.BYLAYER), int(ColorMethod.BYBLOCK), int(ColorMethod.NONE)))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _xy(z: Vec3) -> list[float]:
    """A 2D coordinate pair, rounded just enough to keep the JSON readable.

    Upstream prints ``%f`` — six decimals — for every coordinate.  Rounding to
    the same precision here means an equality test against ``dwgread`` output
    is meaningful rather than a float-formatting lottery.
    """
    return [round(z.x, 6), round(z.y, 6)]


def _xyz(z: Vec3) -> list[float]:
    return [round(z.x, 6), round(z.y, 6), round(z.z, 6)]


def _point_coord(z: Vec3) -> list[float]:
    """A ``POINT`` coordinate: 3D only when ``z`` is genuinely non-zero.

    Upstream tests ``fabs (z) > 0.000001``; the threshold is a
    floating-point-noise guard, not a tolerance, so it is kept verbatim.
    """
    if abs(z.z) > 1e-6:
        return _xyz(z)
    return _xy(z)


def _polygon_ring(points: Iterable[list[float]]) -> list[list[list[float]]]:
    """Wrap a closing-point-repeated point list as a GeoJSON ``Polygon`` ring.

    GeoJSON nests coordinates one level deeper for polygons — a ring is a list
    of positions, a polygon is a list of rings — and the ring must be closed.
    Callers pass the ring's points *without* the closing repetition; this adds
    it.  An empty input returns an empty polygon rather than raising, matching
    upstream's tolerance for a degenerate entity.
    """
    positions = list(points)
    if not positions:
        return [[]]
    return [positions + [positions[0]]]


def _normalize_polygon_orient(positions: list[list[float]]) -> list[list[float]]:
    """Force counter-clockwise winding, as RFC 7946 requires.

    Translation of ``normalize_polygon_orient``: the shoelace *sum*
    ``Σ (x[i+1]-x[i])*(y[i+1]+y[i])`` is positive for a clockwise ring, in
    which case the list is reversed.  Note the comparison is against the
    original (unclosed) point list and the loop stops one short of the last
    point, exactly as upstream — the closing point is implicit here and added
    later by :func:`_polygon_ring`.
    """
    numpts = len(positions)
    if numpts < 2:
        return positions
    total = 0.0
    for i in range(numpts - 1):
        total += ((positions[i + 1][0] - positions[i][0])
                  * (positions[i + 1][1] + positions[i][1]))
    if total > 0.0:                      # clockwise: reverse
        return positions[::-1]
    return positions


def _circle_points(center: Vec3, radius: float, start_angle: float,
                   end_angle: float, extent: float) -> list[list[float]]:
    """Tessellate a circular span, then map to 2D coordinate pairs."""
    span = end_angle - start_angle
    num_pts = curve_segments(radius, span, extent)
    pts = arc_split(num_pts, center, start_angle, end_angle, radius)
    return [_xy(p) for p in pts]


def _arc_points(center: Vec3, radius: float, start_angle: float,
                end_angle: float, extent: float) -> list[list[float]]:
    """As :func:`_circle_points`, kept separate for the call sites to read."""
    span = end_angle - start_angle
    num_pts = curve_segments(radius, span, extent)
    pts = arc_split(num_pts, center, start_angle, end_angle, radius)
    return [_xy(p) for p in pts]


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------
def _color_property(entity: Entity, version: DwgVersion) -> Any:
    """The ``Color`` property — a ``"#RRGGBB"`` string or an ACI index.

    Translation of the ``dwg_geojson_feature`` colour branch.  The rule is
    fiddly and reproduced exactly:

    * from R2004, a *true* colour on a ByLayer entity is written as hex;
    * otherwise the ACAD index is written as an integer, unless the colour is
      an unset ByLayer/ByBlock/none — in which case the property is omitted.
      Pre-R2004 that means "only when the index isn't 256".

    Returns ``None`` when the property should be omitted.
    """
    color = entity.color
    if color is None:
        return None
    if (version >= DwgVersion.R_2004
            and color.method in _TRUE_COLOR_METHODS
            and color.index == 256):
        return f"#{(color.rgb & 0xFFFFFF):06X}"
    if color.index != 256:
        return color.index
    if version >= DwgVersion.R_2004 and color.method not in _UNSET_COLOR_METHODS:
        return color.index
    return None


def _properties(decoder, entity: Entity) -> dict[str, Any]:
    """The ``properties`` object of one feature.

    Field order follows upstream's ``dwg_geojson_feature``, and omitting a
    field when upstream omits it matters: a diff against ``dwgread`` output
    then lines up, and a downstream consumer that keys off presence behaves
    identically.
    """
    props: dict[str, Any] = {
        "SubClasses": SUBCLASSES.get(entity.type_code, "AcDbEntity"),
    }

    # ``Layer`` is written only when the layer actually resolved.  Upstream
    # also accepts a DICTIONARY here, which is how a layer that never made it
    # into the LAYER table still gets a name; the MVP resolves against LAYER
    # records only, so a dangling handle simply yields no property.
    if entity.layer:
        props["Layer"] = entity.layer

    color = _color_property(entity, decoder.version)
    if color is not None:
        props["Color"] = color

    # Linetype only when it is not the default, and only when known.
    if entity.linetype and entity.linetype != "ByLayer":
        props["Linetype"] = entity.linetype

    props["EntityHandle"] = f"{entity.handle:X}"
    return props


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def _geometry(decoder, entity: Entity) -> dict[str, Any] | None:
    """The ``geometry`` object, or ``None`` for an entity with no geometry.

    Returns ``None`` rather than raising for the out-of-scope types: upstream's
    dispatch returns 0 and the feature is skipped entirely (or, for the last
    object, replaced by a null placeholder).  The caller decides which.
    """
    extent = _extent(decoder)

    if isinstance(entity, Line):
        return {"type": "LineString",
                "coordinates": [_xyz(entity.start), _xyz(entity.end)]}

    if isinstance(entity, Point):
        return {"type": "Point", "coordinates": _point_coord(entity.location)}

    if isinstance(entity, LWPolyline):
        return _lwpolyline_geometry(entity)

    if isinstance(entity, Arc):
        # Arc before Circle: Arc subclasses Circle, and the two need
        # different angles.  Order matters here, unlike in a C switch.
        positions = _arc_points(entity.center, entity.radius,
                                entity.start_angle, entity.end_angle, extent)
        return {"type": "Polygon", "coordinates": _polygon_ring(positions)}

    if isinstance(entity, Circle):
        positions = _circle_points(entity.center, entity.radius,
                                   0.0, TWO_PI, extent)
        return {"type": "Polygon", "coordinates": _polygon_ring(positions)}

    return None


def _lwpolyline_geometry(entity: LWPolyline) -> dict[str, Any]:
    """``dwg_geojson_LWPOLYLINE`` — closed rings become polygons.

    The closed test is ``flag & 512`` (the DWG "closed" bit) *and* more than
    three vertices, both upstream.  A closed polyline of three or fewer points
    is emitted as a ``LineString``, which is odd but is what the C does.

    Winding is normalised for the polygon case only — upstream calls
    ``normalize_polygon_orient`` there and nowhere else.
    """
    positions = [[round(v.x, 6), round(v.y, 6)] for v in entity.vertices]

    if (entity.flags & 512) and len(positions) > 3:
        positions = _normalize_polygon_orient(positions)
        return {"type": "Polygon", "coordinates": _polygon_ring(positions)}

    return {"type": "LineString", "coordinates": positions}


def _extent(decoder) -> float:
    """The drawing extent used to pick tessellation density.

    ``calc_curve_segments`` takes the larger of the header's ``LIMMAX-LIMMIN``
    spans, with a floor of 1.0 when the drawing declares no limits — as a
    fresh drawing often does.  Without the floor, ``sqrt(radius / 0)`` would
    be infinite.
    """
    extents = None
    if decoder.header_vars is not None:
        try:
            extents = decoder.header_vars.extents
        except Exception:                     # pragma: no cover - defensive
            extents = None
    if not extents:
        return 1.0
    low, high = extents
    span = max(abs(high.x - low.x), abs(high.y - low.y))
    return span if span >= 1e-12 else 1.0


# ---------------------------------------------------------------------------
# Features and documents
# ---------------------------------------------------------------------------
def entity_to_feature(decoder, entity: Entity) -> dict[str, Any] | None:
    """One GeoJSON ``Feature``, or ``None`` if the entity has no geometry.

    ``decoder`` is only read for the file version and the header extents, so a
    bare object with those two attributes will do — which is what makes this
    function testable without a DWG file.
    """
    geometry = _geometry(decoder, entity)
    if geometry is None:
        return None
    return {
        "type": "Feature",
        "id": f"{entity.handle:X}",
        "properties": _properties(decoder, entity),
        "geometry": geometry,
    }


def to_geojson(decoder, *, include_erased: bool = False,
               include_proxies: bool = False) -> dict[str, Any]:
    """Build the whole ``FeatureCollection``.

    Parameters
    ----------
    include_erased:
        Export soft-deleted entities too.  Off by default: an erased entity is
        not part of the drawing, and ``dwgread``'s own object writer skips
        them.
    include_proxies:
        Emit a geometry-less placeholder feature for each proxy entity.  Off by
        default, because a proxy has no geometry we can state — the feature
        would carry an identity and nothing else.  Turn it on to get a census
        of the proxies without having to look at :attr:`DwgDecoder.proxies`.
    """
    features: list[dict[str, Any]] = []

    for entity in decoder.entities:
        if entity.is_erased and not include_erased:
            continue
        if isinstance(entity, ProxyEntity):
            if include_proxies:
                features.append({
                    "type": "Feature",
                    "id": f"{entity.handle:X}",
                    "properties": {
                        "SubClasses": "AcDbEntity : AcDbProxyEntity",
                        "Layer": entity.layer,
                        "ProxyClassId": entity.proxy_class_id,
                        "EntityHandle": f"{entity.handle:X}",
                    },
                    "geometry": None,
                })
            continue
        feature = entity_to_feature(decoder, entity)
        if feature is not None:
            features.append(feature)

    return {
        "type": "FeatureCollection",
        "features": features,
        "geocoding": {
            "creation_date": time.strftime("%Y-%m-%d", time.localtime()),
            "generator": {
                "author": {"name": "py-libredwg"},
                "package": "py-libredwg",
                "version": _version(),
            },
        },
    }


def _version() -> str:
    """The package version, or ``"unknown"`` when running from a source tree."""
    try:
        from .. import __version__
        return __version__
    except Exception:                         # pragma: no cover - defensive
        return "unknown"


def write_geojson(decoder, fh: IO[str], *, indent: int | None = 2,
                  **kwargs: Any) -> int:
    """Write the FeatureCollection to an open text stream; return the count.

    Streaming the dump rather than pre-serialising keeps peak memory at one
    document plus one string instead of two documents — which matters once a
    drawing has a few hundred thousand entities.
    """
    doc = to_geojson(decoder, **kwargs)
    json.dump(doc, fh, indent=indent, ensure_ascii=False)
    fh.write("\n")
    return len(doc["features"])


def export_geojson(decoder, path: str | os.PathLike[str], *,
                   indent: int | None = 2, **kwargs: Any) -> int:
    """Write the FeatureCollection to ``path`` (UTF-8); return the count.

    ``ensure_ascii=False`` throughout: DWG layer and block names are frequently
    Chinese, and escaping them to ``\\uXXXX`` makes the output unreadable for
    no benefit — the file is explicitly UTF-8.
    """
    with open(path, "w", encoding="utf-8") as fh:
        return write_geojson(decoder, fh, indent=indent, **kwargs)
