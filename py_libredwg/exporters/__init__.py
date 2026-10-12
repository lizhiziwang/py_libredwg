# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""Output writers.

Two, matching the two formats the MVP promises:

:mod:`~py_libredwg.exporters.geojson_exporter`
    A GeoJSON ``FeatureCollection``, one ``Feature`` per entity.  The geometry
    mapping — which entity becomes a ``LineString``, which a ``Polygon``, how
    many segments a curve is tessellated into — follows LibreDWG's
    ``out_geojson.c`` so that the output can be diffed against ``dwgread``.

:mod:`~py_libredwg.exporters.json_exporter`
    A plain nested structure: what the file declared, and what was decoded,
    with no geometry standard imposed.  This is the one to reach for when
    debugging a parse.

Neither writes DWG.  Neither needs the ``numpy`` used elsewhere — the
exporters are pure ``dict``/``json`` code, which keeps them usable from a
minimal install.
"""

from __future__ import annotations

from .geojson_exporter import (
    entity_to_feature,
    export_geojson,
    to_geojson,
    write_geojson,
)
from .json_exporter import (
    entity_to_dict,
    export_json,
    to_json_dict,
    write_json,
)

__all__ = [
    # geojson
    "to_geojson",
    "entity_to_feature",
    "export_geojson",
    "write_geojson",
    # json
    "to_json_dict",
    "entity_to_dict",
    "export_json",
    "write_json",
]
