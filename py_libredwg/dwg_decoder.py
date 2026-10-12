# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""The entry point: :class:`DwgDecoder`.

This is the module a caller actually imports.  It owns the *main flow* that
LibreDWG spreads across ``dwg.c`` (``dwg_read_file``), ``decode.c``
(``decode_R13_R2000``, ``decode_R2004``, ``decode_R2007``) and
``decode_r2007.c`` (``read_r2007_meta_data``):

1. read the file image once,
2. detect the version from the six-byte tag,
3. read the fixed file header,
4. open the *container* — which is the only part that differs between
   R13-R2000, R2004 and R2007+ — and pull out the sections,
5. decode HEADER, CLASSES, HANDLES (the object map) and OBJECTS,
6. resolve the cross-references the objects hold as handles: layer names,
   block names, and which block each entity belongs to.

Steps 1-5 are mechanical.  Step 6 is the one thing LibreDWG leaves to its
callers (its ``dwg`` struct keeps handle *numbers* everywhere and ``dwgread``
only prints them).  Doing it here means an exported entity already carries a
readable ``layer`` name, which is what a consumer of GeoJSON wants.

Where the C's pointers went
---------------------------

``dwg_read_file`` fills a caller-provided ``Dwg_Data`` and returns an error
bitmask; every subsequent call takes that struct again.  Here the state lives
on the decoder, :meth:`DwgDecoder.parse` is the only mutating method, and
errors are exceptions (:class:`~py_libredwg.errors.DwgDecodeError` and
friends) rather than accumulated bits.  The one thing kept from the bitmask
model is the *tolerance*: like ``dwgread``, a document that is 95% readable is
returned with the remaining 5% recorded in :attr:`problems`, not refused.

Nothing here writes.  This library parses DWG and nothing else.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .constants import (
    MVP_ENTITY_TYPES,
    DwgVersion,
    SectionType,
)
from .errors import DwgDecodeError
from .header import FileHeader, HeaderVariables, read_file_header
from .objects.base_object import (
    BlockHeader,
    ControlObject,
    DwgObject,
    Entity,
    Layer,
    ProxyEntity,
    TableEntry,
    UnknownObject,
)
from .sections import (
    _PAGE_LIMIT_R13,
    _PAGE_LIMIT_R2007,
    DwgClass,
    ObjectAddress,
    R2004Container,
    R2007Container,
    decode_classes,
    decode_objects,
    read_2004_section,
    read_data_section,
    read_header_section,
    read_object_map,
    read_r2004_sections,
    read_r2007_sections,
    section_type_from_name,
)

__all__ = ["DwgDecoder", "read_dwg"]


#: Section names LibreDWG gives the R13-R2000 locator records, in order.
#: A file may carry fewer than six; the names are positional, which is why the
#: list is indexed rather than looked up.
_R13_SECTION_NAMES: tuple[str, ...] = (
    "AcDb:Header",
    "AcDb:Classes",
    "AcDb:Handles",
    "AcDb:ObjFreeSpace",
    "AcDb:Template",
    "AcDb:AuxHeader",
)

#: The entity types this MVP decodes; anything else keeps its raw bytes.
_MVP_ENTITIES: frozenset[int] = frozenset(int(t) for t in MVP_ENTITY_TYPES)


def _is_opaque(obj: DwgObject) -> bool:
    """True for an object whose fields this library did not interpret.

    A proxy (undefined payload) and an :class:`UnknownObject` (defined but
    unreadable body) both keep their raw bytes and nothing else.  Used only to
    break ties when two records claim one handle.
    """
    return isinstance(obj, (ProxyEntity, UnknownObject))


class DwgDecoder:
    """Read one DWG file and hand back its objects.

    Parameters
    ----------
    source:
        A path (``str`` or :class:`~pathlib.Path`) or the file's bytes.
    strict:
        When true, the first unreadable object raises instead of being
        recorded as a placeholder.  The default mirrors ``dwgread``, which
        reports and carries on — on a real drawing a single bad object is
        almost always one entity, and losing the other 200 000 to it would be
        the wrong trade.

    Examples
    --------
    >>> decoder = DwgDecoder("drawing.dwg").parse()      # doctest: +SKIP
    >>> decoder.version_tag                              # doctest: +SKIP
    'AC1032'
    >>> len(decoder.entities)                            # doctest: +SKIP
    1573
    >>> decoder.entities[0].layer                        # doctest: +SKIP
    '0'
    """

    __slots__ = (
        "source", "strict", "data", "header", "version", "version_tag",
        "header_vars", "classes", "object_map", "objects", "problems",
        "_container", "_sections", "_objects_buffer", "_hdr_dat_error",
        "_by_handle", "_layers", "_blocks", "_controls", "model_space_handle",
        "paper_space_handle", "_entities", "_no_owner",
    )

    def __init__(self, source: str | os.PathLike[str] | bytes,
                 *, strict: bool = False) -> None:
        self.source = source
        self.strict = strict

        self.data: bytes = b""
        self.header: FileHeader | None = None
        self.version: DwgVersion = DwgVersion.R_INVALID
        self.version_tag: str = ""

        self.header_vars: HeaderVariables | None = None
        self.classes: list[DwgClass] = []
        self.object_map: list[ObjectAddress] = []
        self.objects: list[DwgObject] = []

        #: Everything that went wrong but did not stop the parse — the same
        #: role ``dwgread``'s stderr log plays.  Empty is the good case, not
        #: the expected one: a real drawing from a third-party writer almost
        #: always trips at least one of LibreDWG's sanity checks, and this
        #: library inherits them.
        self.problems: list[str] = []

        self._container: R2004Container | R2007Container | None = None
        self._sections: dict[int, bytes] = {}
        self._objects_buffer: bytes = b""
        self._hdr_dat_error: str | None = None

        self._by_handle: dict[int, DwgObject] = {}
        self._layers: dict[int, Layer] = {}
        self._blocks: dict[int, BlockHeader] = {}
        self._controls: dict[int, ControlObject] = {}
        self.model_space_handle: int = 0
        self.paper_space_handle: int = 0

        self._entities: list[DwgObject] | None = None
        self._no_owner: int = 0

    # -- construction ----------------------------------------------------
    @classmethod
    def from_bytes(cls, data: bytes, **kwargs: Any) -> DwgDecoder:
        """Build a decoder over an in-memory file image."""
        return cls(data, **kwargs)

    def _load(self) -> bytes:
        """Read the file exactly once and keep the one immutable image.

        Every reader in this library works off this single ``bytes`` object —
        the bit reader takes a view of it rather than a copy, and the R2004 /
        R2007 page readers slice it.  Re-reading or re-allocating the image per
        section would be the single biggest cost on a large drawing, so it
        happens here and nowhere else.
        """
        if isinstance(self.source, bytes):
            data = self.source
        elif isinstance(self.source, (bytearray, memoryview)):
            data = bytes(self.source)
        else:
            path = Path(self.source)
            if not path.is_file():
                raise FileNotFoundError(f"no such DWG file: {path}")
            data = path.read_bytes()
        if not data:
            raise DwgDecodeError("empty file")
        return data

    # -- the main flow ---------------------------------------------------
    def parse(self) -> DwgDecoder:
        """Decode the whole file.  Idempotent: parsing twice is a no-op."""
        if self.header is not None:
            return self

        self.data = self._load()
        self.header = read_file_header(self.data)
        self.version = self.header.version
        self.version_tag = self.header.version_tag

        self._open_container()
        self._read_classes()
        self._read_header_variables()
        self._read_object_map()
        self._decode_all_objects()
        self._index_objects()
        self._resolve_references()
        return self

    # -- container -------------------------------------------------------
    def _open_container(self) -> None:
        """Open whichever container this version uses.

        Three layouts, one outcome: a ``{section type: bytes}`` map plus an
        objects buffer the object map addresses.  Keeping the branch here —
        rather than inside the object decoder — is what lets
        :class:`~py_libredwg.sections.ObjectDecoder` be version-agnostic.
        """
        hdr = self.header
        assert hdr is not None

        if self.version < DwgVersion.R_2004a:
            self._open_r13_r2000(hdr)
        elif hdr.is_2007:
            assert hdr.r2007_header is not None
            container = read_r2007_sections(self.data, hdr.r2007_header)
            self._container = container
            self.problems.extend(container.problems)
            for sec_type in SectionType:
                payload = read_data_section(container, int(sec_type))
                if payload:
                    self._sections[int(sec_type)] = payload
            self._objects_buffer = self._sections.get(int(SectionType.OBJECTS), b"")
        else:
            assert hdr.r2004_header is not None
            container = read_r2004_sections(
                self.data, hdr.r2004_header, self.version)
            self._container = container
            self.problems.extend(container.problems)
            for sec_type in SectionType:
                payload = read_2004_section(container, int(sec_type))
                if payload:
                    self._sections[int(sec_type)] = payload
            self._objects_buffer = self._sections.get(int(SectionType.OBJECTS), b"")

    def _open_r13_r2000(self, hdr: FileHeader) -> None:
        """R13-R2000: the whole file is the objects buffer.

        There is no object section in these versions — objects are scattered
        through the file and the object map records *absolute file offsets*.
        So the "objects buffer" is the entire image, and the section records
        only say where the four named sections begin.
        """
        for i, rec in enumerate(hdr.sections):
            name = rec.name or (_R13_SECTION_NAMES[i]
                                if i < len(_R13_SECTION_NAMES) else "")
            sec_type = section_type_from_name(name)
            if sec_type == int(SectionType.UNKNOWN):
                continue
            end = min(rec.address + rec.size, len(self.data))
            if rec.address < 0 or rec.address > end:
                self.problems.append(
                    f"section {name} at 0x{rec.address:X} is out of bounds"
                )
                continue
            self._sections[sec_type] = self.data[rec.address:end]
        self._objects_buffer = self.data

    # -- sections --------------------------------------------------------
    def _read_classes(self) -> None:
        payload = self._sections.get(int(SectionType.CLASSES))
        if not payload:
            return
        assert self.header is not None
        try:
            self.classes = decode_classes(
                payload,
                version=self.version,
                maint_version=self.header.maint_version,
                codepage=self.header.codepage,
            )
        except DwgDecodeError as exc:
            if self.strict:
                raise
            self.problems.append(f"CLASSES section not decoded: {exc}")

    def _read_header_variables(self) -> None:
        payload = self._sections.get(int(SectionType.HEADER))
        if not payload:
            return
        assert self.header is not None
        try:
            self.header_vars = read_header_section(
                payload,
                version=self.version,
                maint_version=self.header.maint_version,
                codepage=self.header.codepage,
            )
        except DwgDecodeError as exc:
            # Not fatal.  The header variables carry the drawing's extents and
            # default layer, which the exporters treat as optional decoration;
            # the objects are what matter and are read from a different
            # section entirely.
            if self.strict:
                raise
            self.problems.append(f"HEADER section not decoded: {exc}")

    def _read_object_map(self) -> None:
        payload = self._sections.get(int(SectionType.HANDLES))
        if not payload:
            self.problems.append("no HANDLES section: the file has no objects")
            return

        if self.version >= DwgVersion.R_2007a:
            page_limit = _PAGE_LIMIT_R2007
        else:
            page_limit = _PAGE_LIMIT_R13

        # ``start``/``end`` are offsets into the buffer the map addresses are
        # relative to.  For R2004+ that is the decompressed objects section,
        # so the whole section is scanned from zero; for R13-R2000 the
        # addresses are absolute file offsets and ``_objects_buffer`` is the
        # whole file, so again the section is scanned from its own zero.  The
        # two cases agree, which is why there is no branch.
        try:
            self.object_map = read_object_map(
                payload, start=0, end=len(payload), page_limit=page_limit)
        except DwgDecodeError as exc:
            if self.strict:
                raise
            self.problems.append(f"HANDLES section not decoded: {exc}")

    def _decode_all_objects(self) -> None:
        if not self.object_map:
            return
        buffer = self._objects_buffer
        if not buffer:
            self.problems.append("no objects section to decode")
            return
        # The file's code page goes in with the objects: pre-R2007 layer,
        # block and style names are single-byte text, and decoding a cp936
        # drawing as latin-1 turns every Chinese name into mojibake.  R2007+
        # stores UCS-2 and this is ignored there.
        assert self.header is not None
        self.objects = list(decode_objects(
            buffer,
            version=self.version,
            addresses=self.object_map,
            classes=self.classes,
            strict=self.strict,
            codepage=self.header.codepage,
            problems=self.problems,
        ))

    # -- cross-reference resolution --------------------------------------
    def _index_objects(self) -> None:
        """Build the handle indices the reference pass looks things up in.

        One pass over the object list, three dicts: everything by handle (for
        arbitrary references), layers, block headers, and controls.

        A handle that appears twice is a corrupt map.  When the two records are
        of different types the clash is reported, and the *decoded* object is
        preferred over an opaque one — a proxy or an unreadable body claiming a
        handle that a real entity owns should not be what a reference resolves
        to.  A same-type repeat just overwrites, which is what a map keyed by
        handle does anyway.
        """
        for obj in self.objects:
            if not obj.handle:
                continue
            prev = self._by_handle.get(obj.handle)
            if prev is None:
                self._by_handle[obj.handle] = obj
            elif type(prev) is not type(obj):
                self.problems.append(
                    f"handle {obj.handle:X} claimed by both "
                    f"{prev.type_name} and {obj.type_name}"
                )
                if _is_opaque(prev) and not _is_opaque(obj):
                    self._by_handle[obj.handle] = obj
            else:
                self._by_handle[obj.handle] = obj

            if isinstance(obj, Layer):
                self._layers[obj.handle] = obj
            elif isinstance(obj, BlockHeader):
                self._blocks[obj.handle] = obj
            elif isinstance(obj, ControlObject):
                self._controls[obj.handle] = obj

    def _resolve_references(self) -> None:
        """Fill in the names that the objects only know as handles.

        Three things happen, all of them pure lookups over the index built by
        :meth:`_index_objects`:

        * every entity gets its ``layer`` name (and linetype, when present),
        * the block control gives up model/paper space,
        * the layer control gives the layer objects their owning table back.
        """
        for obj in self.objects:
            if isinstance(obj, Entity):
                if obj.layer_handle:
                    layer = self._layers.get(obj.layer_handle)
                    if layer is not None:
                        obj.layer = layer.name
                if obj.linetype_handle:
                    ltype = self._by_handle.get(obj.linetype_handle)
                    if isinstance(ltype, TableEntry) and ltype.name:
                        obj.linetype = ltype.name

        for ctrl in self._controls.values():
            if ctrl.model_space_handle:
                self.model_space_handle = ctrl.model_space_handle
            if ctrl.paper_space_handle:
                self.paper_space_handle = ctrl.paper_space_handle
            for handle in ctrl.entry_handles:
                entry = self._by_handle.get(handle)
                if isinstance(entry, TableEntry):
                    entry.owner_handle = ctrl.handle

    # -- public views ----------------------------------------------------
    @property
    def entities(self) -> list[DwgObject]:
        """Every decoded entity, in object-map order.

        Includes the MVP types, and also the proxies and unknown entities —
        they are entities in the file, and hiding them would make the count
        disagree with ``dwgread``.  Filter by type when you want only
        geometry; see :attr:`geometry_entities`.
        """
        if self._entities is None:
            self._entities = [o for o in self.objects if isinstance(o, Entity)]
        return self._entities

    @property
    def geometry_entities(self) -> list[Entity]:
        """The entities this library can actually turn into geometry."""
        return [o for o in self.entities if o.type_code in _MVP_ENTITIES]

    @property
    def layers(self) -> list[Layer]:
        """The LAYER table, sorted by name for stable output."""
        return sorted(self._layers.values(), key=lambda x: x.name)

    @property
    def blocks(self) -> list[BlockHeader]:
        """The BLOCK_HEADER table, in handle order."""
        return sorted(self._blocks.values(), key=lambda x: x.handle)

    @property
    def proxies(self) -> list[ProxyEntity]:
        """Proxy entities — in practice Tianzheng (天正) entities."""
        return [o for o in self.objects if isinstance(o, ProxyEntity)]

    @property
    def unknown(self) -> list[UnknownObject]:
        """Objects this MVP recognises but does not decode."""
        return [o for o in self.objects if isinstance(o, UnknownObject)]

    def object_by_handle(self, handle: int) -> DwgObject | None:
        return self._by_handle.get(handle)

    def layer_name(self, handle: int) -> str | None:
        """The name of a layer by handle, or ``None`` if it is not in the file."""
        layer = self._layers.get(handle)
        return layer.name if layer is not None else None

    def block_name(self, handle: int) -> str | None:
        """The name of a block by its block-record handle."""
        blk = self._blocks.get(handle)
        return blk.name if blk is not None else None

    def entities_in_block(self, block_handle: int) -> list[Entity]:
        """Entities whose owner is ``block_handle``.

        An entity's owner *is* its block record, so this is a plain filter —
        no graph walk, and no cycle risk from a malformed file.
        """
        return [e for e in self.entities if e.owner_handle == block_handle]

    def iter_blocks(self) -> Iterator[tuple[str, list[Entity]]]:
        """``(block name, entities)`` for every block that owns entities."""
        grouped: dict[int, list[Entity]] = {}
        for entity in self.entities:
            grouped.setdefault(entity.owner_handle, []).append(entity)
        for handle, ents in grouped.items():
            name = self.block_name(handle)
            yield (name if name is not None else f"*handle {handle:X}"), ents

    # -- output ----------------------------------------------------------
    def to_dict(self, *, include_objects: bool = True) -> dict[str, Any]:
        """A plain nested structure suitable for ``json.dump``.

        Deliberately *not* GeoJSON — that is
        :mod:`py_libredwg.exporters.geojson_exporter`'s job.  This is the
        "simple structured JSON" view: what the file declared, and what
        happened while reading it.
        """
        out: dict[str, Any] = {
            "file": {
                "version": self.version_tag,
                "version_name": self.version.name,
                "codepage": self.header.codepage if self.header else 0,
            },
            "counts": {
                "objects": len(self.objects),
                "entities": len(self.entities),
                "geometry_entities": len(self.geometry_entities),
                "layers": len(self._layers),
                "blocks": len(self._blocks),
                "classes": len(self.classes),
                "proxies": len(self.proxies),
                "unknown": len(self.unknown),
            },
        }
        if self.header_vars is not None:
            out["header"] = self.header_vars.to_dict()
            clayer = self.layer_name(self.header_vars.CLAYER)
            if clayer:
                out["header"]["CLAYER_name"] = clayer

        out["layers"] = [layer.to_dict() for layer in self.layers]
        out["blocks"] = [
            {**blk.to_dict(), "entities": len(self.entities_in_block(blk.handle))}
            for blk in self.blocks
        ]
        out["classes"] = [
            {
                "number": cls.number,
                "name": cls.cppname or cls.dxfname or cls.appname,
                "is_entity": cls.is_entity,
                "num_instances": cls.num_instances,
            }
            for cls in self.classes
        ]
        if include_objects:
            out["entities"] = [obj.to_dict() for obj in self.entities]
        if self.problems:
            out["problems"] = list(self.problems)
        return out

    def __repr__(self) -> str:
        return (
            f"<DwgDecoder {self.version_tag or 'unparsed'} "
            f"objects={len(self.objects)} entities={len(self.entities)}>"
        )


def read_dwg(source: str | os.PathLike[str] | bytes,
             **kwargs: Any) -> DwgDecoder:
    """Parse a DWG file and return the decoder.

    The one-liner form of ``DwgDecoder(source, **kwargs).parse()``.  Kept as a
    function because that is how LibreDWG's own ``dwg_read_file`` reads, and
    because most callers want the result, not the object that produced it.

    >>> doc = read_dwg("drawing.dwg")       # doctest: +SKIP
    >>> doc.entities[0].layer               # doctest: +SKIP
    'WALLS'
    """
    return DwgDecoder(source, **kwargs).parse()
