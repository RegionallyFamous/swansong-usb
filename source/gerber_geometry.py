#!/usr/bin/env python3
"""Convert the subset of RS-274X primitives used by the SwanSong board to Shapely.

The source controller was published as Gerbers only.  This helper is intentionally
small and deterministic: it supports the flashes, stroked lines/arcs, regions, and
aperture-macro outlines present in those files so geometry can be audited without
depending on a GUI Gerber viewer.
"""

from __future__ import annotations

import builtins
import math
from pathlib import Path
from typing import Iterable

from shapely.affinity import rotate, translate
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union


_real_open = builtins.open


def _compat_open(file, mode="r", *args, **kwargs):
    """pcb-tools still asks Python for the removed ``rU`` mode."""
    return _real_open(file, mode.replace("U", ""), *args, **kwargs)


builtins.open = _compat_open

import gerber  # noqa: E402  (must follow the rU compatibility patch)


def _circle(position: tuple[float, float], diameter: float):
    return Point(position).buffer(diameter / 2.0, resolution=48)


def _obround(position: tuple[float, float], width: float, height: float):
    x, y = position
    if abs(width - height) < 1e-9:
        return _circle(position, width)
    if width > height:
        half = (width - height) / 2.0
        return LineString([(x - half, y), (x + half, y)]).buffer(
            height / 2.0, resolution=32
        )
    half = (height - width) / 2.0
    return LineString([(x, y - half), (x, y + half)]).buffer(
        width / 2.0, resolution=32
    )


def _rectangle(position: tuple[float, float], width: float, height: float, rotation=0):
    x, y = position
    shape = box(x - width / 2.0, y - height / 2.0, x + width / 2.0, y + height / 2.0)
    return rotate(shape, rotation or 0, origin=position, use_radians=False)


def _aperture_diameter(aperture) -> float:
    name = type(aperture).__name__
    if name == "Circle":
        return aperture.diameter
    if name in {"Rectangle", "Obround"}:
        return min(aperture.width, aperture.height)
    raise NotImplementedError(f"Unsupported stroke aperture: {name}")


def _arc_points(arc, steps_per_circle: int = 256):
    cx, cy = arc.center
    sx, sy = arc.start
    ex, ey = arc.end
    start = math.atan2(sy - cy, sx - cx)
    end = math.atan2(ey - cy, ex - cx)
    direction = getattr(arc, "direction", "counterclockwise")
    if direction == "clockwise":
        while end >= start:
            end -= 2 * math.pi
    else:
        while end <= start:
            end += 2 * math.pi
    span = end - start
    count = max(8, int(abs(span) / (2 * math.pi) * steps_per_circle))
    radius = math.hypot(sx - cx, sy - cy)
    return [
        (cx + radius * math.cos(start + span * i / count),
         cy + radius * math.sin(start + span * i / count))
        for i in range(count + 1)
    ]


def _region_points(region) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for segment in region.primitives:
        name = type(segment).__name__
        if name == "Line":
            segment_points = [segment.start, segment.end]
        elif name == "Arc":
            segment_points = _arc_points(segment)
        else:
            raise NotImplementedError(f"Unsupported region segment: {name}")
        if points and Point(points[-1]).distance(Point(segment_points[0])) < 1e-7:
            points.extend(segment_points[1:])
        else:
            points.extend(segment_points)
    if points and points[0] != points[-1]:
        points.append(points[0])
    return points


def primitive_shape(primitive):
    name = type(primitive).__name__
    if name == "Circle":
        return _circle(primitive.position, primitive.diameter)
    if name == "Obround":
        return _obround(primitive.position, primitive.width, primitive.height)
    if name == "Rectangle":
        return _rectangle(
            primitive.position,
            primitive.width,
            primitive.height,
            getattr(primitive, "rotation", 0),
        )
    if name == "Line":
        width = _aperture_diameter(primitive.aperture)
        return LineString([primitive.start, primitive.end]).buffer(
            width / 2.0, resolution=24, cap_style=1, join_style=1
        )
    if name == "Arc":
        width = _aperture_diameter(primitive.aperture)
        return LineString(_arc_points(primitive)).buffer(
            width / 2.0, resolution=24, cap_style=1, join_style=1
        )
    if name == "Region":
        shape = Polygon(_region_points(primitive))
        return shape.buffer(0)
    if name == "AMGroup":
        nested = []
        for item in primitive.primitives:
            item_name = type(item).__name__
            if item_name == "Outline":
                # pcb-tools represents an aperture-macro outline as rays from
                # the first vertex to each following vertex, not as a Region.
                points = [item.primitives[0].start]
                points.extend(segment.end for segment in item.primitives)
                shape = Polygon(points).buffer(0)
            else:
                shape = primitive_shape(item)
            nested.append(shape)
        shape = unary_union(nested)
        return shape
    raise NotImplementedError(f"Unsupported Gerber primitive: {name}")


def load_layer(path: str | Path):
    """Return the final positive geometry after sequential dark/clear operations."""
    layer = gerber.read(str(path))
    result = Polygon()
    for primitive in layer.primitives:
        shape = primitive_shape(primitive)
        if primitive.level_polarity == "clear":
            result = result.difference(shape)
        else:
            result = result.union(shape)
    return result.buffer(0)


def component_for_point(layer_shape, point: tuple[float, float], tolerance=1e-5):
    """Return the connected copper island containing ``point``, if one exists."""
    probe = Point(point).buffer(tolerance)
    for geometry in getattr(layer_shape, "geoms", [layer_shape]):
        if geometry.intersects(probe):
            return geometry
    return None
