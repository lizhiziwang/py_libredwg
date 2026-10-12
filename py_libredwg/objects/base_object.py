# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""Base classes for decoded DWG objects.

C's object model is replaced here in three ways:

* **No unions, no pointers.**  LibreDWG stores an object as a tagged union
  (``obj->tio.entity->tio.LINE``) and hands out raw pointers into it.  Here
  each object is a dataclass of its own type, and the tag is replaced by the
  Python class itself.
* **No manual lifetime.**  The ``malloc``/``free`` pairs disappear; a decoded
  document owns its objects in a plain ``dict`` keyed by handle, and normal
  reference counting does the rest.
* **Handles are resolved, not followed.**  Every reference (layer, linetype,
  owner, ...) is stored as a handle *number* plus an optional resolved name.
  Nothing in the object graph holds a reference to another object, so cycles
  in a malformed file cannot produce a cycle in memory.

Everything decoded keeps its :attr:`DwgObject.address` (byte offset in the
OBJECTS section) and :attr:`DwgObject.raw` bytes when available, so a value
that this library does not yet understand can still be inspected by hand.

.. note::
   Every ``to_dict`` below calls ``super(Class, self)`` rather than the
   zero-argument ``super()``.  ``@dataclass(slots=True)`` rebuilds the class
   object to install ``__slots__``, which leaves the compiler's ``__class__``
   cell pointing at the discarded original; the bare ``super()`` form then
   raises ``TypeError: obj is not an instance or subtype of type`` on CPython
   3.13.  The two-argument form resolves the name at call time and is correct.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..constants import (
    BYLAYER,
    LWEIGHT_BYLAYER,
    ColorMethod,
    lineweight_value,
    object_type_name,
)
from ..geometry import Vec3

__all__ = [
    "CMColor",
    "DwgObject",
    "Entity",
    "ProxyEntity",
    "UnknownObject",
    "TableEntry",
    "Layer",
    "BlockHeader",
    "ControlObject",
]

# The concrete MVP entity types live in sibling modules — one per DWG type,
# matching the layout of ``src/dwg.spec``.  They are re-exported from
# ``py_libredwg.objects`` so callers rarely import them directly:
#
#     line.py         Line        (0x13)
#     circle.py       Circle      (0x12)
#     arc.py          Arc         (0x11)   <- derives from Circle
#     point.py        Point       (0x1B)
#     lwpolyline.py   LWPolyline  (0x4D)   (+ LWPolylineVertex)


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class CMColor:
    """A DWG colour (``Dwg_Color`` / ``BITCODE_CMC``).

    Pre-R2004 this is only :attr:`index`, a slot in the AutoCAD Color Index
    table; ``256`` means *ByLayer* and ``0`` means *ByBlock*.  From R2004 the
    entity colour carries a true-colour value whose top byte selects how to
    interpret it — see :class:`~py_libredwg.constants.ColorMethod`.
    """

    index: int = BYLAYER
    rgb: int = 0
    method: int = int(ColorMethod.BYLAYER)
    flag: int = 0
    name: str | None = None
    book_name: str | None = None
    handle: Any = None          # local Handle; typed loosely to avoid a cycle
    alpha: int = 0
    alpha_type: int = 0

    @property
    def true_color(self) -> tuple[int, int, int] | None:
        """The 24-bit true colour as ``(r, g, b)``, or ``None`` if not set."""
        if self.method not in (int(ColorMethod.RGB), int(ColorMethod.ACAD_INDEX)):
            return None
        return ((self.rgb >> 16) & 0xFF, (self.rgb >> 8) & 0xFF, self.rgb & 0xFF)

    def describe(self) -> str:
        """A short human description, used by the JSON exporter."""
        if self.index == BYLAYER:
            return "ByLayer"
        if self.index == 0:
            return "ByBlock"
        tc = self.true_color
        if tc is not None:
            return f"#{tc[0]:02X}{tc[1]:02X}{tc[2]:02X}"
        return str(self.index)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"index": self.index, "description": self.describe()}
        if self.rgb:
            out["rgb"] = f"#{(self.rgb & 0xFFFFFF):06X}"
        if self.method:
            try:
                out["method"] = ColorMethod(self.method).name
            except ValueError:
                out["method"] = self.method
        if self.name:
            out["name"] = self.name
        if self.book_name:
            out["book_name"] = self.book_name
        if self.alpha or self.alpha_type:
            out["alpha"] = self.alpha
            out["alpha_type"] = self.alpha_type
        return out


# ---------------------------------------------------------------------------
# Object base
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class DwgObject:
    """Anything stored in the OBJECTS section, addressed by handle.

    The fields every DWG object carries regardless of type.  Subclasses add
    whatever their type needs; the class itself is the type tag.
    """

    handle: int = 0
    """The object's own handle number."""

    type_code: int = 0
    """Raw on-stream object type number (see :class:`ObjectType`)."""

    owner_handle: int = 0
    """Handle of the owning object — the block or table this belongs to."""

    address: int = 0
    """Byte offset of this object inside the OBJECTS section, for diagnostics."""

    size: int = 0
    """Size in bytes as declared by the object header."""

    bit_size: int = 0
    """Size in bits for R2004+ objects, which are bit-addressed."""

    is_erased: bool = False
    """Soft-delete flag; erased objects are normally skipped on export."""

    reactors: list[int] = field(default_factory=list)
    """Handles of objects that receive notification when this one changes."""

    xdic_handle: int = 0
    """Extension dictionary handle, 0 when absent."""

    raw: bytes | None = None
    """The undecoded object body.  Always populated for proxies."""

    @property
    def type_name(self) -> str:
        return object_type_name(self.type_code)

    @property
    def is_proxy(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle": f"{self.handle:X}",
            "type": self.type_name,
            "type_code": self.type_code,
            "owner_handle": f"{self.owner_handle:X}" if self.owner_handle else None,
        }


@dataclass(slots=True)
class UnknownObject(DwgObject):
    """An object this library recognises but does not yet decode.

    LibreDWG decodes every type it knows; for the MVP scope we keep the type
    code and the raw body instead of guessing at fields.  The object is
    visible by its address, type code and type name, and its body is
    byte-exact.

    :attr:`handle` is usually **0** here, and that is not an oversight.  The
    object's own handle lives at the head of the handle stream, whose position
    is derived from where the value stream ends — and for a type we cannot lay
    out, "where the value stream ends" is exactly what we do not know.  Upstream
    has the same hole: ``dwg_decode_add_object`` comments its handle map insert
    with "empty only with UNKNOWN", and ``UNKNOWN_ENT``/``UNKNOWN_OBJ`` in
    ``dwg2.spec`` read their remaining bits *without* the common handle data.
    Guessing a position would put a plausible wrong number on every undecoded
    object, which is worse than an honest zero.
    """

    #: For an object whose type code is in the file's class range (500+), the
    #: class name from the CLASSES section — ``"AcDbTable"`` and the like.
    #: Empty for a type this library simply has no handler for, where there is
    #: no class record to name it from.  A real field rather than a stashed
    #: attribute: ``slots=True`` leaves no ``__dict__`` to stash into.
    class_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = super(UnknownObject, self).to_dict()
        if self.class_name:
            d["class_name"] = self.class_name
        if self.raw:
            d["raw_size"] = len(self.raw)
        return d


@dataclass(slots=True)
class ProxyEntity(DwgObject):
    """A proxy entity — typically a custom object from a vertical product.

    In practice almost always a Tianzheng (天正) entity.  LibreDWG does not
    decode the body: it records the raw payload and moves on, because the
    meaning of those bytes is defined solely by the third-party application
    that wrote them.

    This library matches that behaviour exactly.  :attr:`raw` holds the
    undecoded blob so a caller can hand it to the originating application.

    Attributes
    ----------
    proxy_class_id:
        The class id from the CLASSES section identifying the authoring
        application's class.
    version:
        The proxy's own format version counter.
    data:
        The opaque payload (same bytes as :attr:`DwgObject.raw`).
    """

    proxy_class_id: int = 0
    version: int = 0
    data: bytes = b""

    @property
    def is_proxy(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        d = super(ProxyEntity, self).to_dict()
        d["proxy"] = True
        d["proxy_class_id"] = self.proxy_class_id
        d["data_size"] = len(self.data or self.raw or b"")
        return d


# ---------------------------------------------------------------------------
# Entity base
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Entity(DwgObject):
    """Common entity data (``common_entity_data.spec``).

    Field order here does **not** mirror the on-stream order — that lives in
    :mod:`py_libredwg.sections`.  These are just the decoded values, named.

    Layer, linetype, material and plotstyle are stored as handles.  The
    :attr:`layer` name is filled in by the decoder once the TABLES section has
    been read, so that a consumer never has to chase handles itself.
    """

    layer_handle: int = 0
    layer: str | None = None

    linetype_handle: int = 0
    linetype: str | None = None

    plotstyle_handle: int = 0
    material_handle: int = 0

    color: CMColor = field(default_factory=CMColor)

    ltype_scale: float = 1.0
    ltype_flags: int = 0

    invisible: int = 0

    #: The **raw DWG lineweight index**, 0..31 — not the DXF value.  The two
    #: differ: the stream stores a small index (``FIELD_RC (linewt, 370)`` in
    #: ``common_entity_data.spec``) and only ``dxf_cvt_lweight`` turns it into
    #: hundredths of a millimetre.  Index 29 is *ByLayer*, which is why a raw
    #: byte of 29 shows up as ``-1`` in DXF.  Use
    #: :attr:`lineweight_value` for anything user-facing.
    #:
    #: The default is *ByLayer*: files older than R2000 do not carry the field
    #: at all, and AutoCAD treats such an entity as inheriting its layer.
    lineweight: int = LWEIGHT_BYLAYER

    extrusion: Vec3 = field(default_factory=lambda: Vec3(0.0, 0.0, 1.0))

    #: Set when the entity carries an extension dictionary or proxy graphics;
    #: recorded so the raw stream can be replayed, not decoded here.
    proxy_graphics_size: int = 0

    @property
    def lineweight_value(self) -> int:
        """The lineweight as DXF group 370 reports it.

        ``-1`` ByLayer, ``-2`` ByBlock, ``-3`` ByDefault, otherwise the width
        in hundredths of a millimetre.  Translation of ``dxf_cvt_lweight``.
        """
        return lineweight_value(self.lineweight)

    @property
    def geometry_type(self) -> str:
        """GeoJSON geometry family this entity maps to.

        Mirrors ``out_geojson.c``: lines are ``LineString``, closed rings and
        circles/arcs are ``Polygon``, points are ``Point``.
        """
        return "Unknown"

    def to_dict(self) -> dict[str, Any]:
        d = super(Entity, self).to_dict()
        d["layer"] = self.layer
        d["color"] = self.color.to_dict()
        if self.linetype:
            d["linetype"] = self.linetype
        # Report the converted value; the raw index is an implementation
        # detail of the DWG stream and means nothing to a consumer.
        if self.lineweight_value:
            d["lineweight"] = self.lineweight_value
        if self.extrusion != Vec3(0.0, 0.0, 1.0):
            d["extrusion"] = list(self.extrusion)
        return d


# ---------------------------------------------------------------------------
# Table entries and block structures
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class TableEntry(DwgObject):
    """An entry in a symbol table (LAYER, LTYPE, STYLE, ...).

    Table entries are symbol records; they share a name and a set of flags.
    The MVP only resolves LAYER names, but the shape is general so extending
    to linetypes and text styles is additive.
    """

    name: str = ""
    flags: int = 0

    @property
    def is_xref_dependent(self) -> bool:
        return bool(self.flags & 16)

    def to_dict(self) -> dict[str, Any]:
        d = super(TableEntry, self).to_dict()
        d["name"] = self.name
        return d


@dataclass(slots=True)
class Layer(TableEntry):
    """``LAYER`` (type 0x33) — a layer definition.

    :attr:`color` is the layer's own colour; entities set to *ByLayer* resolve
    to it.  :attr:`is_frozen` and :attr:`is_off` matter for presentation but
    do not affect geometry.
    """

    color: CMColor = field(default_factory=CMColor)
    linetype_handle: int = 0
    plotstyle_handle: int = 0

    is_frozen: bool = False
    is_off: bool = False
    is_locked: bool = False
    is_plotting: bool = True

    #: Raw packed lineweight index from bits 5..9 of the R2000+ ``flag0``
    #: field — the same encoding as :attr:`Entity.lineweight`, so the same
    #: conversion applies.  Zero when the file predates R2000.
    lineweight: int = 0

    @property
    def lineweight_value(self) -> int:
        """The layer's lineweight as DXF group 370 reports it."""
        return lineweight_value(self.lineweight)

    def to_dict(self) -> dict[str, Any]:
        d = super(Layer, self).to_dict()
        d["color"] = self.color.to_dict()
        if self.lineweight_value:
            d["lineweight"] = self.lineweight_value
        if self.is_frozen:
            d["frozen"] = True
        if self.is_off:
            d["off"] = True
        if self.is_locked:
            d["locked"] = True
        return d


@dataclass(slots=True)
class BlockHeader(TableEntry):
    """``BLOCK_HEADER`` (type 0x31) — the name record for a block.

    The MVP keeps the block's identity and its entity handles but does not
    expand nested block references; :attr:`entity_handles` is populated for
    first-level blocks and left empty for nested ones.
    """

    base_point: Vec3 = field(default_factory=Vec3)
    is_anonymous: bool = False
    is_xref: bool = False

    entity_handles: list[int] = field(default_factory=list)
    endblk_handle: int = 0

    block_entity_handle: int = 0       # the BLOCK begin marker
    first_entity_handle: int = 0
    last_entity_handle: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = super(BlockHeader, self).to_dict()
        d["base_point"] = list(self.base_point)
        d["entity_count"] = len(self.entity_handles)
        if self.is_xref:
            d["xref"] = True
        if self.is_anonymous:
            d["anonymous"] = True
        return d


@dataclass(slots=True)
class ControlObject(DwgObject):
    """A ``*_CONTROL`` object (BLOCK_CONTROL, LAYER_CONTROL, ...).

    Controls are the per-table containers that own the symbol records.  The
    MVP needs them because the handle of the block control is the root that
    walks to every block, and because the *model space* block header is
    identified through it.
    """

    num_entries: int = 0
    entry_handles: list[int] = field(default_factory=list)
    owner_handle_of_entries: int = 0

    #: ``BLOCK_CONTROL`` only.  Every control object ends with its own handle
    #: list, but only the block control carries two extra handles: the block
    #: headers for model space (``*MODEL_SPACE``) and paper space
    #: (``*PAPER_SPACE``).  Those two are how an entity's ``entmode`` (1 or 2)
    #: gets turned into the block it actually belongs to, so the exporter needs
    #: them.  Left at 0 for every other control, which is what the file says.
    model_space_handle: int = 0
    paper_space_handle: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = super(ControlObject, self).to_dict()
        d["num_entries"] = self.num_entries
        return d
