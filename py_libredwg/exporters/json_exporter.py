# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""Plain structured JSON export.

Where the GeoJSON exporter imposes a geometry standard, this one imposes
nothing: it writes what the file said, with the decoded values in named fields
and the geometry flattened to plain numbers.  It is the export to reach for
when a parse looks wrong, because it is the only place the intermediate values
— handles, owner handles, sizes, addresses — are visible.

Two deliberate differences from LibreDWG's ``out_json.c``:

* **No ``objects`` array mirroring the raw object list.**  ``dwgread --json``
  emits one entry per *object*, including the tables, dictionaries and every
  type this library does not decode.  That output is enormous and its shape is
  tied to the C struct layout, so matching it byte-for-byte would be a
  maintenance liability for no gain.  This exporter emits *entities* — the
  things that have geometry — plus a summary of the rest.
* **Handles are hexadecimal strings.**  DWG handles are conceptually
  hex-labelled (``2F``, ``1A4``); ``dwgread`` agrees in its text output.
  Writing decimal integers would make cross-checking against the C tool
  unpleasant, so handles, owners and references are all ``str`` of the hex
  value.

What is *not* flattened: ``raw`` bytes.  An undecoded object's payload is
reported as a length, never as a base64 blob — the JSON should stay
diffable.
"""

from __future__ import annotations

import json
import os
from typing import IO, Any

from ..geometry import Vec3
from ..objects.arc import Arc
from ..objects.base_object import DwgObject, Entity, ProxyEntity
from ..objects.circle import Circle
from ..objects.line import Line
from ..objects.lwpolyline import LWPolyline
from ..objects.point import Point

__all__ = [
    "to_json_dict",
    "entity_to_dict",
    "export_json",
    "write_json",
]


def _hex(handle: int) -> str | None:
    """A handle as uppercase hex, or ``None`` for the null handle.

    ``None`` rather than ``"0"`` on purpose: JSON ``null`` reads as "no
    reference" and a consumer can test it directly, whereas ``0`` is a legal
    handle value in a corrupt file and the two would be confusable.
    """
    return f"{handle:X}" if handle else None


def _v3(v: Vec3) -> list[float]:
    """A 3D point as ``[x, y, z]``, rounded to LibreDWG's print precision."""
    return [round(v.x, 6), round(v.y, 6), round(v.z, 6)]


def _v2(v: Vec3) -> list[float]:
    """A 2D point as ``[x, y]`` — ``z`` is dropped, not zeroed.

    Layers and lwpolylines are planar by definition in DWG; writing ``z`` would
    suggest a coordinate that the format does not store.
    """
    return [round(v.x, 6), round(v.y, 6)]


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------
def _geometry_fields(entity: Entity) -> dict[str, Any]:
    """The type-specific fields, flattened to lists of numbers.

    Each branch mirrors the field set of the corresponding
    :mod:`py_libredwg.objects` dataclass.  Returning ``{}`` for an unknown
    entity is correct rather than lazy: there is nothing decoded to report,
    and the caller still has the type code and the raw length.
    """
    if isinstance(entity, Line):
        return {"start": _v3(entity.start), "end": _v3(entity.end),
                "thickness": entity.thickness}

    if isinstance(entity, Point):
        out = {"location": _v3(entity.location), "thickness": entity.thickness}
        # ``angle`` is only meaningful as a non-zero value (it is the point's
        # own rotation); upstream omits it in the DXF output when zero.
        if entity.angle:
            out["angle"] = entity.angle
        return out

    if isinstance(entity, Arc):
        # Before Circle, because Arc subclasses it.
        return {
            "center": _v3(entity.center),
            "radius": entity.radius,
            "start_angle": entity.start_angle,
            "end_angle": entity.end_angle,
            "thickness": entity.thickness,
        }

    if isinstance(entity, Circle):
        return {"center": _v3(entity.center), "radius": entity.radius,
                "thickness": entity.thickness}

    if isinstance(entity, LWPolyline):
        return {
            "vertices": [[round(v.x, 6), round(v.y, 6)] for v in entity.vertices],
            "num_points": entity.point_count,
            "elevation": entity.elevation,
            "thickness": entity.thickness,
            "constant_width": entity.constant_width,
            "flags": entity.flags,
            "closed": entity.closed,
        }

    return {}


def entity_to_dict(entity: DwgObject) -> dict[str, Any]:
    """One entity as a JSON-ready dict.

    Built on :meth:`DwgObject.to_dict` — the object half — with the geometry
    half added under ``geometry``.  Keeping the two separable means a caller
    that only wants the common fields can ignore ``geometry``, and it keeps the
    handle/type/owner fields identical across every entity type.
    """
    out = entity.to_dict()
    if isinstance(entity, Entity):
        out["geometry"] = _geometry_fields(entity)
        out["geometry_type"] = entity.geometry_type
    if isinstance(entity, ProxyEntity):
        # The proxy's payload is deliberately opaque, so the only honest
        # summary is its size.  ``raw`` itself is never serialised.
        out["data_size"] = len(entity.data or entity.raw or b"")
    return out


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------
def to_json_dict(decoder, *, include_objects: bool = True,
                 include_erased: bool = True) -> dict[str, Any]:
    """The whole document as one JSON-ready dict.

    ``include_erased`` defaults to ``True`` here, unlike in the GeoJSON
    exporter: this view is for inspecting the parse, and an erased entity is
    exactly the kind of thing one wants to see while doing that.  It is
    reported with ``"erased": true`` so it can still be filtered.
    """
    doc = decoder.to_dict(include_objects=False)

    entities = []
    for entity in decoder.entities:
        if entity.is_erased and not include_erased:
            continue
        item = entity_to_dict(entity)
        if entity.is_erased:
            item["erased"] = True
        entities.append(item)
    doc["entities"] = entities

    # Everything the MVP did not decode, summarised by type.  A consumer
    # checking coverage gets the census without wading through a raw blob per
    # object; a caller that needs the bytes has the object in memory.
    undecoded: dict[str, int] = {}
    for obj in decoder.objects:
        if isinstance(obj, Entity):
            continue
        name = obj.type_name
        undecoded[name] = undecoded.get(name, 0) + 1
    if undecoded:
        doc["undecoded_by_type"] = dict(sorted(undecoded.items()))

    if include_objects:
        doc["objects"] = [
            {
                "handle": _hex(obj.handle),
                "type": obj.type_name,
                "type_code": obj.type_code,
                "owner": _hex(obj.owner_handle),
                "address": obj.address,
                "size": obj.size,
                "raw_size": len(obj.raw) if obj.raw else 0,
            }
            for obj in decoder.objects
        ]
    return doc


def write_json(decoder, fh: IO[str], *, indent: int | None = 2,
               **kwargs: Any) -> int:
    """Write the document to an open text stream; return the entity count."""
    doc = to_json_dict(decoder, **kwargs)
    json.dump(doc, fh, indent=indent, ensure_ascii=False)
    fh.write("\n")
    return len(doc["entities"])


def export_json(decoder, path: str | os.PathLike[str], *,
                indent: int | None = 2, **kwargs: Any) -> int:
    """Write the document to ``path`` (UTF-8); return the entity count.

    UTF-8 and ``ensure_ascii=False`` for the same reason as the GeoJSON
    exporter: layer, block and class names in a Chinese drawing are Chinese,
    and escaping them helps nobody.
    """
    with open(path, "w", encoding="utf-8") as fh:
        return write_json(decoder, fh, indent=indent, **kwargs)
