#!/usr/bin/env python3
"""Generate the SwanSong USB turnkey MacroFab manufacturing package.

The published controller source contains Gerbers but no editable PCB source.  This
script preserves the proven board outline, mounting holes, membrane contacts, and
button routing; removes the RP2040 module and SNES-output assembly; then adds a
native-USB PIC16F1459 circuit in the former module area.

Coordinates are kept in the source Gerbers' native inch system.  The output is a
complete, deterministic set of RS-274X/Excellon files plus placement/BOM CSVs.
"""

from __future__ import annotations

import csv
import hashlib
import heapq
import json
import math
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union


HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(HERE))

from gerber_geometry import component_for_point, load_layer  # noqa: E402


BASE = HERE / "base-gerbers"
OUT = PROJECT / "gerbers"

TRACE_W = 0.0050      # 0.127 mm (5 mil)
CLEARANCE = 0.0050    # 0.127 mm; MacroFab Standard minimum
VIA_PAD = 0.0260      # 0.660 mm
VIA_DRILL = 0.0120    # 0.305 mm
SMALL_VIA_PAD = 0.0200   # 0.508 mm
SMALL_VIA_DRILL = 0.0100 # 0.254 mm; MacroFab Standard minimum
MASK_EXPAND = 0.0030  # 0.076 mm per side
EDGE_COPPER_CLEARANCE = 0.0160  # 0.406 mm; margin above MacroFab's 10 mil minimum
USB_PIN_DRILL = 0.40 / 25.4
USB_PIN_PAD = 0.66 / 25.4  # 5.1 mil annular ring; above MacroFab Standard
USB_REAR_SHELL_DRILL = 2.20 / 25.4
USB_REAR_SHELL_PAD = 2.46 / 25.4
USB_FRONT_SHELL_DRILL = 1.55 / 25.4
USB_FRONT_SHELL_PAD = 1.81 / 25.4

# The WonderSwan Color exposes its accessory connector on the right side of the
# shell.  The preserved controller outline has the matching rectangular recess
# from X=4.720 in to the outer edge and Y=4.455..5.230 in.  Keep the USB-C
# receptacle centered in that opening and preserve the proven front-edge offset
# used by Rev A, which leaves the connector mouth safely inside the opening.
ACCESSORY_RECESS_INNER_X = 4.720
ACCESSORY_RECESS_Y_MIN = 4.455
ACCESSORY_RECESS_Y_MAX = 5.230
USB_FRONT_X = ACCESSORY_RECESS_INNER_X + 0.008625
USB_BODY_LENGTH = 9.17 / 25.4
USB_CX = USB_FRONT_X - USB_BODY_LENGTH / 2
USB_CY = (ACCESSORY_RECESS_Y_MIN + ACCESSORY_RECESS_Y_MAX) / 2
USB_BODY_HALF_LENGTH = USB_BODY_LENGTH / 2
USB_BODY_HALF_WIDTH = 4.47 / 25.4


@dataclass(frozen=True)
class Pad:
    ref: str
    number: str
    net: str
    x: float
    y: float
    width: float
    height: float
    paste: bool = True
    mask: bool = True
    round: bool = False
    through_hole: bool = False

    @property
    def shape(self):
        if self.round:
            return Point(self.x, self.y).buffer(self.width / 2, resolution=24)
        return box(
            self.x - self.width / 2,
            self.y - self.height / 2,
            self.x + self.width / 2,
            self.y + self.height / 2,
        )


@dataclass(frozen=True)
class Placement:
    ref: str
    value: str
    mpn: str
    x: float
    y: float
    rotation: float
    side: str = "Bottom"
    package: str = ""


def rect_pad(ref, number, net, x, y, width_mm, height_mm, **kwargs):
    return Pad(ref, str(number), net, x, y, width_mm / 25.4, height_mm / 25.4, **kwargs)


def round_pad(ref, number, net, x, y, diameter_mm, **kwargs):
    diameter = diameter_mm / 25.4
    return Pad(ref, str(number), net, x, y, diameter, diameter, round=True, **kwargs)


def line_shape(points: list[tuple[float, float]], width=TRACE_W):
    return LineString(points).buffer(width / 2, resolution=12, cap_style=1, join_style=1)


def circle_shape(x, y, diameter):
    return Point(x, y).buffer(diameter / 2, resolution=24)


def xy(value: float) -> int:
    return round(value * 1_000_000)


def gerber_header(layer_name: str) -> str:
    return (
        f"G04 SwanSong USB - {layer_name}*\n"
        "G04 Generated from the published SwanTroller Gerbers; units inches*\n"
        "%FSLAX36Y36*%\n"
        "%MOIN*%\n"
        "%ADD10C,0.001*%\n"
        "%LPD*%\n"
    )


def polygon_region(poly: Polygon) -> str:
    coords = list(poly.exterior.coords)
    if len(coords) < 4:
        return ""
    out = ["G36*", "G01*", f"X{xy(coords[0][0])}Y{xy(coords[0][1])}D02*"]
    out.extend(f"X{xy(x)}Y{xy(y)}D01*" for x, y in coords[1:])
    out.append("G37*")
    return "\n".join(out) + "\n"


def geometry_regions(geometry, polarity: str) -> str:
    if geometry.is_empty:
        return ""
    geoms = list(geometry.geoms) if geometry.geom_type in {"MultiPolygon", "GeometryCollection"} else [geometry]
    out = [f"%LP{'D' if polarity == 'dark' else 'C'}*%\n"]
    for geom in geoms:
        if geom.is_empty:
            continue
        if geom.geom_type != "Polygon":
            geom = geom.buffer(0)
        polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
        for poly in polys:
            out.append(polygon_region(poly))
            # New geometries in this design do not intentionally contain holes,
            # but preserve them correctly if a union operation creates one.
            for interior in poly.interiors:
                out.append(f"%LP{'C' if polarity == 'dark' else 'D'}*%\n")
                out.append(polygon_region(Polygon(interior)))
                out.append(f"%LP{'D' if polarity == 'dark' else 'C'}*%\n")
    return "".join(out)


def append_overlay(base_path: Path, output_path: Path, clear_shape, dark_shape):
    text = base_path.read_text()
    marker = text.rfind("M02*")
    if marker < 0:
        raise ValueError(f"No Gerber terminator in {base_path}")
    text = text[:marker]
    text += "\nG04 SwanSong USB replacement circuitry*\n"
    text += geometry_regions(clear_shape, "clear")
    text += geometry_regions(dark_shape, "dark")
    text += "M02*\n"
    output_path.write_text(text)


def write_positive_layer(output_path: Path, layer_name: str, dark_shape):
    output_path.write_text(
        gerber_header(layer_name) + geometry_regions(dark_shape, "dark") + "M02*\n"
    )


def endpoint_pads():
    """Return reused RP2040 perimeter pads keyed by their former module number."""
    pads: dict[int, Pad] = {}
    # Former pads 1..9: top row, right-to-left numbering.
    for number in range(1, 10):
        x = 4.98256 - (number - 1) * 0.1
        pads[number] = Pad("TP", str(number), "", x, 5.92433, 0.049646, 0.156063, False, False)
    # Former pads 10..14: left row, bottom-to-top.
    for number in range(10, 15):
        y = 5.37079 + (number - 10) * 0.1
        pads[number] = Pad("TP", str(number), "", 4.127439, y, 0.156063, 0.049646, False, False)
    # Former pads 15..23: bottom row, left-to-right.
    for number in range(15, 24):
        x = 4.18256 + (number - 15) * 0.1
        pads[number] = Pad("TP", str(number), "", x, 5.21567, 0.049646, 0.156063, False, False)
    return pads


def pic_pads(cx=4.50, cy=5.52):
    pads: dict[int, Pad] = {}
    # SOIC-20 uses a generous 1.27 mm pitch and is substantially easier for a
    # 5 mil Standard process than the earlier 0.65 mm SSOP footprint.
    x_left = cx - 4.85 / 25.4
    x_right = cx + 4.85 / 25.4
    for pin in range(1, 11):
        y = cy + (-5.715 + (pin - 1) * 1.27) / 25.4
        pads[pin] = rect_pad("U1", pin, "", x_left, y, 2.0, 0.60)
    for pin in range(11, 21):
        y = cy + (5.715 - (pin - 11) * 1.27) / 25.4
        pads[pin] = rect_pad("U1", pin, "", x_right, y, 2.0, 0.60)
    return pads


def connector_pads(cy=USB_CY):
    """GCT USB4085-GF-A PTH USB-C facing the accessory-port recess.

    GCT's component-side land pattern uses two 0.85 mm-pitch rows separated by
    1.35 mm. The drawing is for a top-mounted part. Here the receptacle is
    bottom-mounted and faces +X, so flipping it around that +X mating axis puts
    A1/B12 at +Y; the descending column order below is the intentional mirror.
    The four rectangular shell stakes are deliberately placed in close-fitting
    round plated holes: MacroFab documents this as electrically valid, and it
    avoids the extra fabrication cost of routed plated slots.
    """
    columns = [2.975, 2.125, 1.275, 0.425, -0.425, -1.275, -2.125, -2.975]
    a_pins = [
        ("A1", "GND"), ("A4", "VBUS"), ("A5", "CC1"), ("A6", "USB_DP"),
        ("A7", "USB_DM"), ("A8", "NC_A8"), ("A9", "VBUS"), ("A12", "GND"),
    ]
    b_pins = [
        ("B12", "GND"), ("B9", "VBUS"), ("B8", "NC_B8"), ("B7", "USB_DM"),
        ("B6", "USB_DP"), ("B5", "CC2"), ("B4", "VBUS"), ("B1", "GND"),
    ]
    result = []
    for depth_mm, pins in ((6.65, a_pins), (5.30, b_pins)):
        x = USB_FRONT_X - depth_mm / 25.4
        for (number, net), column_mm in zip(pins, columns):
            result.append(round_pad(
                "J1", number, net, x, cy + column_mm / 25.4, 0.66,
                paste=False, mask=True, through_hole=True,
            ))

    for row, depth_mm, pad_mm in (
        ("R", 5.12, 2.46),
        ("F", 1.74, 1.81),
    ):
        x = USB_FRONT_X - depth_mm / 25.4
        for side, column_mm in (("U", 4.325), ("L", -4.325)):
            result.append(round_pad(
                "J1", f"SH{row}{side}", "GND", x, cy + column_mm / 25.4, pad_mm,
                paste=False, mask=True, through_hole=True,
            ))
    return result


def resistor_array_pads(ref="RN1", cx=3.93, cy=5.52):
    """CTS 746X101, rotated 90 degrees, using the vendor land pattern.

    Pins 5 and 10 are the duplicated common bus terminals. The body is
    3.2 x 1.6 mm; recommended lands are 0.35 x 0.80 mm on a 0.64 mm pitch
    with 2.60 mm between rows (CTS 74x Series, rev. T, pages 4 and 6).
    """
    pads = []
    # Unrotated numbering is counter-clockwise: pins 1..5 on the lower row
    # left-to-right, then 6..10 on the upper row right-to-left. Rotate +90.
    for pin in range(1, 6):
        local_x = (-1.28 + (pin - 1) * 0.64) / 25.4
        local_y = -1.30 / 25.4
        pads.append(rect_pad(ref, pin, "", cx - local_y, cy + local_x, 0.80, 0.35))
    for pin in range(6, 11):
        local_x = (1.28 - (pin - 6) * 0.64) / 25.4
        local_y = 1.30 / 25.4
        pads.append(rect_pad(ref, pin, "", cx - local_y, cy + local_x, 0.80, 0.35))
    return pads


def passive_pads(ref, net1, net2, cx, cy, rotation=0, capacitor=False):
    offset = (0.48 if capacitor else 0.51) / 25.4
    w = (0.56 if capacitor else 0.54)
    h = (0.62 if capacitor else 0.64)
    if rotation == 90:
        return [
            rect_pad(ref, 1, net1, cx, cy + offset, h, w),
            rect_pad(ref, 2, net2, cx, cy - offset, h, w),
        ]
    return [
        rect_pad(ref, 1, net1, cx - offset, cy, w, h),
        rect_pad(ref, 2, net2, cx + offset, cy, w, h),
    ]


def passive_0201_pads(ref, net1, net2, cx, cy, rotation=0):
    """Compact IPC-style 0201 pads for the USB-regulator capacitor."""
    offset = 0.35 / 25.4
    if rotation == 90:
        return [
            rect_pad(ref, 1, net1, cx, cy + offset, 0.40, 0.35),
            rect_pad(ref, 2, net2, cx, cy - offset, 0.40, 0.35),
        ]
    return [
        rect_pad(ref, 1, net1, cx - offset, cy, 0.35, 0.40),
        rect_pad(ref, 2, net2, cx + offset, cy, 0.35, 0.40),
    ]


def passive_0603_pads(ref, net1, net2, cx, cy, rotation=0):
    offset = 0.875 / 25.4
    if rotation == 90:
        return [
            rect_pad(ref, 1, net1, cx, cy + offset, 0.95, 0.95),
            rect_pad(ref, 2, net2, cx, cy - offset, 0.95, 0.95),
        ]
    return [
        rect_pad(ref, 1, net1, cx - offset, cy, 0.95, 0.95),
        rect_pad(ref, 2, net2, cx + offset, cy, 0.95, 0.95),
    ]


def nearest_grid(value, step):
    # Keep grid nodes on the same six-decimal representation used by the
    # router's neighbor expansion. Without this normalization, values such as
    # 5.6450000000000005 can never compare equal to the generated 5.645 node.
    return round(round(value / step) * step, 6)


def astar_path(start, goal, blocked, bounds, step=0.005):
    """Route a short top-layer trace through ground while avoiding signal copper."""
    minx, miny, maxx, maxy = bounds
    start = (nearest_grid(start[0], step), nearest_grid(start[1], step))
    goal = (nearest_grid(goal[0], step), nearest_grid(goal[1], step))

    def valid(node):
        x, y = node
        if x < minx or x > maxx or y < miny or y > maxy:
            return False
        if node in {start, goal}:
            return True
        return not blocked.intersects(Point(node))

    queue = [(0.0, 0.0, start)]
    came = {}
    cost = {start: 0.0}
    directions = [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)]
    while queue:
        _, current_cost, current = heapq.heappop(queue)
        if current == goal:
            break
        if current_cost != cost.get(current):
            continue
        for dx, dy in directions:
            nxt = (round(current[0] + dx * step, 6), round(current[1] + dy * step, 6))
            if not valid(nxt):
                continue
            segment = LineString([current, nxt])
            if blocked.intersects(segment):
                continue
            new_cost = current_cost + (math.sqrt(2) if dx and dy else 1)
            # Small bend penalty keeps the path manufacturing-friendly.
            if current in came:
                px, py = came[current]
                if (round(current[0] - px, 6), round(current[1] - py, 6)) != (round(dx * step, 6), round(dy * step, 6)):
                    new_cost += 0.15
            if new_cost < cost.get(nxt, float("inf")):
                cost[nxt] = new_cost
                came[nxt] = current
                heuristic = math.hypot(goal[0] - nxt[0], goal[1] - nxt[1]) / step
                heapq.heappush(queue, (new_cost + heuristic, new_cost, nxt))
    if goal not in came and goal != start:
        raise RuntimeError(f"No route from {start} to {goal}")
    path = [goal]
    while path[-1] != start:
        path.append(came[path[-1]])
    path.reverse()

    # Collapse collinear grid points.
    simplified = [path[0]]
    last_direction = None
    for index in range(1, len(path)):
        direction = (
            round(path[index][0] - path[index - 1][0], 6),
            round(path[index][1] - path[index - 1][1], 6),
        )
        if last_direction is not None and direction != last_direction:
            simplified.append(path[index - 1])
        last_direction = direction
    simplified.append(path[-1])
    return simplified


def bitmap_font():
    return {
        " ": ["00000"] * 7,
        "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
        ".": ["00000", "00000", "00000", "00000", "00000", "00100", "00100"],
        "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
        "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
        "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
        "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
        "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
        "5": ["11111", "10000", "10000", "11110", "00001", "00001", "11110"],
        "6": ["01110", "10000", "10000", "11110", "10001", "10001", "01110"],
        "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
        "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
        "9": ["01110", "10001", "10001", "01111", "00001", "00001", "01110"],
        "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
        "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
        "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
        "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
        "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
        "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
        "G": ["01111", "10000", "10000", "10111", "10001", "10001", "01111"],
        "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
        "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
        "J": ["00111", "00010", "00010", "00010", "10010", "10010", "01100"],
        "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
        "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
        "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
        "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
        "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
        "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
        "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
        "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
        "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
        "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
        "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
        "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
        "W": ["10001", "10001", "10001", "10101", "10101", "10101", "01010"],
        "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
        "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
        "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    }


FONT = bitmap_font()


def text_shape(text: str, x: float, y: float, pixel_mm: float, align="left"):
    pixel = pixel_mm / 25.4
    advance = 6 * pixel
    width = max(0, len(text) * advance - pixel)
    if align == "center":
        x -= width / 2
    elif align == "right":
        x -= width
    shapes = []
    for char_index, char in enumerate(text.upper()):
        glyph = FONT.get(char, FONT[" "])
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit == "1":
                    px = x + char_index * advance + col * pixel
                    py = y - row * pixel
                    shapes.append(box(px, py - pixel, px + pixel * 0.82, py - pixel * 0.18))
    return unary_union(shapes) if shapes else Polygon()


def assign_pad_nets(pads, assignments):
    result = []
    for pad in pads:
        key = (pad.ref, pad.number)
        net = assignments.get(key, pad.net)
        result.append(Pad(
            pad.ref, pad.number, net, pad.x, pad.y, pad.width, pad.height,
            pad.paste, pad.mask, pad.round, pad.through_hole,
        ))
    return result


def build_design():
    endpoints = endpoint_pads()
    pic = pic_pads(cx=4.38, cy=5.52)
    connector = connector_pads()
    resistor_array_1 = resistor_array_pads("RN1", 3.93, 5.60)
    resistor_array_2 = resistor_array_pads("RN2", 4.28, 4.96)

    # Assign each contact to the nearest available GPIO while reserving the
    # PIC's native USB/power pins.  The four bottom-right controls escape under
    # the SOIC body, leaving the connector side free for D+/D-.
    controls = {
        6: ("Y2", 11),
        7: ("Y1", 12),
        10: ("Y4", 3),
        11: ("Y3", 5),
        12: ("X1", 7),
        13: ("X2", 9),
        14: ("X4", 10),
        15: ("X3", 8),
        16: ("SOUND", 6),
        17: ("START", 13),
        18: ("POWER", 14),
        19: ("B", 15),
        20: ("A", 16),
    }
    # The original RP2040 module used castellated surface pads, so its top
    # button traces and our new bottom routing are not inherently connected.
    # A 10-mil plated hit through each reused landing makes that connection
    # explicit while remaining inside the original copper pad.
    endpoint_vias = [(endpoints[number].x, endpoints[number].y) for number in controls]
    pic_fixed = {1: "VBUS", 4: "MCLR", 17: "VUSB3V3", 18: "USB_DM", 19: "USB_DP", 20: "GND"}

    pad_assignments = {}
    for endpoint_number, (net, pic_pin) in controls.items():
        pad_assignments[("TP", str(endpoint_number))] = net
        pad_assignments[("U1", str(pic_pin))] = net
    for pin, net in pic_fixed.items():
        pad_assignments[("U1", str(pin))] = net

    # Port C has no internal weak pull-ups on the PIC16F1459. Two identical
    # bussed arrays keep the pull-up branches local and preserve 5 mil spacing;
    # pins 5/10 are the duplicated internal common bus.
    resistor_array_assignments = {
        "RN1": {
            1: "VBUS", 2: "SOUND", 3: "Y3", 4: "X1", 5: "X2",
            6: "UNUSED_RN1_6", 7: "UNUSED_RN1_7", 8: "UNUSED_RN1_8", 9: "UNUSED_RN1_9", 10: "VBUS",
        },
        "RN2": {
            1: "VBUS", 2: "A", 3: "B", 4: "POWER", 5: "UNUSED_RN2_5",
            6: "X3", 7: "UNUSED_RN2_7", 8: "UNUSED_RN2_8", 9: "UNUSED_RN2_9", 10: "VBUS",
        },
    }
    for ref, assignments in resistor_array_assignments.items():
        for pin, net in assignments.items():
            pad_assignments[(ref, str(pin))] = net

    all_pads = []
    all_pads.extend(assign_pad_nets(list(pic.values()), pad_assignments))
    all_pads.extend(assign_pad_nets(connector, pad_assignments))
    all_pads.extend(assign_pad_nets(resistor_array_1, pad_assignments))
    all_pads.extend(assign_pad_nets(resistor_array_2, pad_assignments))

    # 0402 passives; all are ordinary distributor-stock parts.  The USB-C CC
    # pull-downs live above/below the connector so the data pair stays short.
    # Put the Type-C configuration resistors immediately behind the relocated
    # connector.  This also keeps them clear of the receptacle's metal body.
    all_pads.extend(passive_pads("R1", "GND", "CC1", 4.22, 4.700, 0))
    all_pads.extend(passive_pads("R2", "GND", "CC2", 4.22, 4.620, 0))
    all_pads.extend(passive_pads("C1", "VBUS", "GND", 4.05, 5.02, 0, True))
    # Rev A placed C2 beneath the connector's new mechanical envelope. Move it
    # left beside C1/RN2 so no assembled part sits below the USB-C shell.
    all_pads.extend(passive_pads("C2", "VBUS", "GND", 4.12, 4.90, 90, True))
    # The VUSB capacitor sits immediately outside pin 17. Reversing its
    # non-polarized pad numbering puts the VUSB pad at the lower end, beside
    # the controller lead, while leaving a clean USB-routing channel to its
    # right.
    all_pads.extend(passive_pads("C3", "GND", "VUSB3V3", 4.63, 5.465, 90, True))
    all_pads.append(Pad("TP", "VPP", "MCLR", 4.04, 5.42, 0.050, 0.050, False, True))

    # Re-add endpoint copper after clearing the old module interior. B/A remain
    # exposed for functional probing; U1 is factory-programmed before placement.
    for number, pad in endpoints.items():
        net = pad_assignments.get(("TP", str(number)))
        if not net:
            continue
        all_pads.append(Pad("TP", str(number), net, pad.x, pad.y, 0.032, 0.032, False, number in {19, 20}))

    pad_map = {(pad.ref, pad.number): pad for pad in all_pads}
    nets: dict[str, list] = {}
    for pad in all_pads:
        if pad.net and pad.net not in {"NC", "SPARE"}:
            nets.setdefault(pad.net, []).append(pad.shape)

    def route(net, points, width=TRACE_W):
        nets.setdefault(net, []).append(line_shape(points, width))

    route("MCLR", [(pic[4].x, pic[4].y), (4.10, pic[4].y), (4.075, 5.42), (4.04, 5.42)])

    # The USB4085 contacts are plated through-holes. Join each reversible D+/D-
    # pair on top, using only one ordinary via beside the PIC for each signal.
    dm_pads = sorted([p for p in all_pads if p.ref == "J1" and p.net == "USB_DM"], key=lambda p: p.y)
    dp_pads = sorted([p for p in all_pads if p.ref == "J1" and p.net == "USB_DP"], key=lambda p: p.y)
    dm_vias = [
        (4.65, pic[18].y),
        (dm_pads[0].x, dm_pads[0].y),
        (dm_pads[1].x, dm_pads[1].y),
    ]
    dp_vias = [
        (4.63, pic[19].y),
        (dp_pads[0].x, dp_pads[0].y),
        (dp_pads[1].x, dp_pads[1].y),
    ]
    nets.setdefault("USB_DM", []).append(circle_shape(*dm_vias[0], SMALL_VIA_PAD))
    route("USB_DM", [(pic[18].x, pic[18].y), dm_vias[0]])
    # The reversible D-/D+ contacts form opposing diagonals in the 16-pin
    # Type-C footprint. Join D- on the bottom and D+ on the top so those two
    # diagonals never cross on the same copper layer. The plated contacts make
    # the layer transition without adding another via or fabrication feature.
    route("USB_DM", [dm_vias[1], dm_vias[2]])
    nets.setdefault("USB_DP", []).append(circle_shape(*dp_vias[0], SMALL_VIA_PAD))
    route("USB_DP", [(pic[19].x, pic[19].y), dp_vias[0]])

    # Pair the four VBUS contacts by row, then escape behind the receptacle on
    # a left-side spine. The PTH grid leaves a full Standard-rule corridor.
    vbus_pads = [p for p in all_pads if p.ref == "J1" and p.net == "VBUS"]
    vbus_rows = {}
    for pad in vbus_pads:
        vbus_rows.setdefault(round(pad.y, 6), []).append(pad)
    connector_vbus_spine_x = 4.40
    for row_y, row_pads in sorted(vbus_rows.items()):
        row_pads = sorted(row_pads, key=lambda p: p.x)
        route("VBUS", [(row_pads[0].x, row_y), (row_pads[-1].x, row_y)], 0.008)
        route("VBUS", [(row_pads[0].x, row_y), (connector_vbus_spine_x, row_y)], 0.008)
    lower_vbus_y = min(vbus_rows)
    upper_vbus_y = max(vbus_rows)
    route("VBUS", [
        (connector_vbus_spine_x, lower_vbus_y),
        (connector_vbus_spine_x, upper_vbus_y),
    ], 0.008)
    signal_vias = [(4.56, 5.11), (4.24, pic[1].y)]
    for x, y in signal_vias:
        nets.setdefault("VBUS", []).append(circle_shape(x, y, VIA_PAD))
    route("VBUS", [
        (connector_vbus_spine_x, upper_vbus_y),
        (connector_vbus_spine_x, 5.08),
        (signal_vias[0][0], 5.08), signal_vias[0],
    ], 0.008)
    route("VBUS", [signal_vias[1], (4.32, pic[1].y), (pic[1].x, pic[1].y)], 0.012)

    # The two array commons join below the controller. Their common bus then
    # feeds the nearby PIC VBUS landing; the existing top VBUS bridge carries
    # power onward to the USB-C connector without crossing the A/B controls.
    rn1_common = pad_map[("RN1", "1")]
    rn2_common = pad_map[("RN2", "1")]
    # Run the supply spine through the empty center beneath RN2's package. The
    # right-hand signal pads can then escape outward with a full 5 mil gap.
    connector_side_power_x = 4.30
    route("VBUS", [
        (rn1_common.x, rn1_common.y), (3.75, rn1_common.y),
        (3.75, 4.82), (connector_side_power_x, 4.82),
        (connector_side_power_x, rn2_common.y), (rn2_common.x, rn2_common.y),
    ], 0.008)
    rn_power_path = [
        (rn2_common.x, rn2_common.y), (connector_side_power_x, rn2_common.y),
        (connector_side_power_x, 5.15), (4.33, 5.18),
        (4.33, 5.25), signal_vias[1],
    ]
    route("VBUS", rn_power_path, 0.008)

    # Tie both bulk/decoupling capacitors into the low, unobstructed portion
    # of the array supply bus.
    c1_vbus = next(p for p in all_pads if p.ref == "C1" and p.net == "VBUS")
    c2_vbus = next(p for p in all_pads if p.ref == "C2" and p.net == "VBUS")
    route("VBUS", [(c1_vbus.x, c1_vbus.y), (3.75, c1_vbus.y)], 0.008)
    # Escape C2 to the left before dropping to the low supply trunk.  Going
    # straight down would cross C2's ground pad, while approaching RN2 from
    # above would cross the array's unused pins 8/9.
    route("VBUS", [
        (c2_vbus.x, c2_vbus.y), (4.08, c2_vbus.y), (4.08, 4.82),
    ], 0.008)

    vusb_cap = next(p for p in all_pads if p.ref == "C3" and p.net == "VUSB3V3")
    route("VUSB3V3", [(pic[17].x, pic[17].y), (4.612, pic[17].y), (vusb_cap.x, vusb_cap.y)])

    # X3 escapes upward on the top layer. SOUND can remain on the bottom; its
    # original top button trace is joined at the endpoint by the plated hit.
    x3_vias = [(endpoints[15].x, endpoints[15].y), (4.24, pic[8].y)]
    for net, endpoint_number, pin, via_points in (
        ("X3", 15, 8, x3_vias),
    ):
        for x, y in via_points:
            nets.setdefault(net, []).append(circle_shape(x, y, SMALL_VIA_PAD))
        route(net, [(pic[pin].x, pic[pin].y), via_points[1]])

    # Shortest-path control fanout. The nearest-pin assignment gives the
    # router broad independent corridors instead of the old nested crossover.
    control_by_net = {
        net: (endpoints[endpoint_number], pic[pin])
        for endpoint_number, (net, pin) in controls.items()
        if net != "X3"
    }
    fixed_shapes = [
        shape
        for net, shapes in nets.items()
        if net not in control_by_net
        for shape in shapes
    ]
    fixed_blocked = unary_union(fixed_shapes).buffer(CLEARANCE + TRACE_W / 2)
    fixed_blocked = fixed_blocked.union(box(4.735, 5.315, 5.035, 5.825))
    pad_obstacles = {
        net: unary_union([
            p.shape for p in all_pads
            if p.net != net and p.net not in {"NC", "SPARE"}
        ]).buffer(CLEARANCE + TRACE_W / 2)
        for net in control_by_net
    }
    groups = {
        "top": ["Y2", "Y1"],
        "left": ["SOUND", "X4", "X2", "X1", "Y3", "Y4"],
        "bottom": ["START", "POWER", "B", "A"],
    }
    trial_orders = [
        list(reversed(groups["left"])) + groups["top"] + groups["bottom"],
        ["SOUND", "X1", "Y3", "X2", "Y4", "X4"] + groups["top"] + groups["bottom"],
        ["POWER", "START", "B", "A"] + list(reversed(groups["left"])) + groups["top"],
        ["START", "POWER", "B", "A"] + list(reversed(groups["left"])) + groups["top"],
        groups["top"] + groups["left"] + groups["bottom"],
        list(reversed(groups["bottom"])) + list(reversed(groups["left"])) + list(reversed(groups["top"])),
        groups["bottom"] + groups["left"] + groups["top"],
        groups["left"] + groups["top"] + groups["bottom"],
    ]
    control_paths = None
    control_failures = []
    for order in trial_orders:
        dynamic = fixed_blocked
        trial = {}
        try:
            for net in order:
                start_pad, goal_pad = control_by_net[net]
                start = (start_pad.x, start_pad.y)
                goal = (goal_pad.x, goal_pad.y)
                blocked = dynamic.union(pad_obstacles[net])
                direct = LineString([start, goal])
                path = None
                candidates = [
                    [start, goal],
                    [start, (start[0], goal[1]), goal],
                    [start, (goal[0], start[1]), goal],
                    [start, (start[0] - 0.020, start[1]), (start[0] - 0.020, goal[1]), goal],
                    [start, (start[0] + 0.020, start[1]), (start[0] + 0.020, goal[1]), goal],
                ]
                for candidate in candidates:
                    if not blocked.intersects(LineString(candidate)):
                        path = candidate
                        break
                if path is None:
                    path = astar_path(
                        start, goal, blocked,
                        (4.105, 5.195, 4.735, 5.945), step=0.0025,
                    )
                shape = line_shape(path)
                trial[net] = path
                dynamic = dynamic.union(shape.buffer(CLEARANCE + TRACE_W / 2))
        except RuntimeError as exc:
            control_failures.append(f"{net}: {exc}")
            continue
        control_paths = trial
        break
    if control_paths is None:
        raise RuntimeError(
            "Unable to complete nearest-pin control fanout; "
            + "; ".join(control_failures)
        )
    for net, path in control_paths.items():
        route(net, path)

    # Add the resistor-array branches only after the core control fanout is
    # fixed. Each branch treats every other completed net as an obstacle, while
    # it may harmlessly join its own control trace anywhere along the route.
    endpoint_for_net = {net: endpoints[number] for number, (net, _pin) in controls.items()}
    rn_pin_for_net = {
        "SOUND": ("RN1", 2), "Y3": ("RN1", 3),
        "X1": ("RN1", 4), "X2": ("RN1", 5),
        "A": ("RN2", 2), "B": ("RN2", 3),
        "POWER": ("RN2", 4), "X3": ("RN2", 6),
    }
    pullup_by_net = {
        net: (pad_map[(ref, str(pin))], endpoint_for_net[net])
        for net, (ref, pin) in rn_pin_for_net.items()
    }
    # The three rightmost branches fan outward below the endpoint row, then
    # rise in separate lanes. Their source and destination ordering matches,
    # so these doglegs never cross one another.
    manual_pullup_paths = {}
    manual_pullup_shapes = {net: line_shape(path) for net, path in manual_pullup_paths.items()}
    auto_pullup_by_net = {
        net: endpoints for net, endpoints in pullup_by_net.items() if net not in manual_pullup_paths
    }
    pullup_blocked_by_net = {
        net: unary_union([
            shape
            for other_net, shapes in nets.items()
            if other_net != net
            for shape in shapes
        ]).buffer(CLEARANCE + TRACE_W / 2)
        for net in auto_pullup_by_net
    }
    pullup_pad_obstacles = {
        net: unary_union([
            p.shape for p in all_pads if p.net != net and p.net != "NC"
        ]).buffer(CLEARANCE + TRACE_W / 2)
        for net in auto_pullup_by_net
    }
    pullup_orders = [
        ["POWER", "B", "A", "Y3", "X1", "X2", "SOUND", "X3"],
        ["A", "B", "POWER", "X3", "SOUND", "X2", "X1", "Y3"],
        ["Y3", "X1", "X2", "SOUND", "X3", "POWER", "B", "A"],
        ["X3", "SOUND", "Y3", "X1", "X2", "POWER", "B", "A"],
    ]
    pullup_paths = None
    pullup_failures = []
    for order in pullup_orders:
        dynamic = unary_union([
            shape.buffer(CLEARANCE + TRACE_W / 2)
            for shape in manual_pullup_shapes.values()
        ])
        trial = {}
        try:
            for net in order:
                start_pad, goal_pad = auto_pullup_by_net[net]
                start = (start_pad.x, start_pad.y)
                goal = (goal_pad.x, goal_pad.y)
                blocked = dynamic.union(pullup_blocked_by_net[net]).union(pullup_pad_obstacles[net])
                direct = LineString([start, goal])
                path = None
                candidates = [
                    [start, goal],
                    [start, (start[0], goal[1]), goal],
                    [start, (goal[0], start[1]), goal],
                ]
                for candidate in candidates:
                    if not blocked.intersects(LineString(candidate)):
                        path = candidate
                        break
                if path is None:
                    path = astar_path(
                        start, goal, blocked,
                        (3.60, 4.60, 4.72, 5.74), step=0.0025,
                    )
                shape = line_shape(path)
                trial[net] = path
                dynamic = dynamic.union(shape.buffer(CLEARANCE + TRACE_W / 2))
        except RuntimeError as exc:
            pullup_failures.append(f"{net}: {exc}")
            continue
        pullup_paths = {**trial, **manual_pullup_paths}
        break
    if pullup_paths is None:
        raise RuntimeError(
            "Unable to complete resistor-array pull-up fanout; "
            + "; ".join(pullup_failures)
        )
    for net, path in pullup_paths.items():
        route(net, path)

    # The original A-button trace occupies the USB connector's shell-stake
    # locations. Preserve the membrane contact below the connector, add one via
    # in that retained trace, and carry A to its existing bottom network. This
    # removes the long top-side barrier without changing the button circuitry.
    a_button_via = (4.67, 4.50)
    nets.setdefault("A", []).append(circle_shape(*a_button_via, SMALL_VIA_PAD))
    a_button_goal = pad_map[("RN2", "2")]
    a_button_blocked = unary_union([
        shape
        for other_net, shapes in nets.items()
        if other_net != "A"
        for shape in shapes
    ]).buffer(CLEARANCE + TRACE_W / 2)
    a_button_path = astar_path(
        a_button_via, (a_button_goal.x, a_button_goal.y),
        a_button_blocked, (4.05, 4.40, 4.72, 5.15), step=0.0025,
    )
    route("A", a_button_path)

    # Each Type-C configuration net changes layers once in the open channel to
    # the right of the connector contacts. The bottom half then reaches its
    # resistor below the VBUS trunk without crossing any power copper.
    cc1 = next(p for p in all_pads if p.ref == "J1" and p.net == "CC1")
    cc2 = next(p for p in all_pads if p.ref == "J1" and p.net == "CC2")
    r1_cc = pad_map[("R1", "2")]
    # Separate the two transitions vertically: CC2 passes below the relocated
    # A trace while CC1 passes above it. Their bottom traces then run in parallel
    # lanes to the two resistors.
    cc1_vias = [(cc1.x, cc1.y), (4.635, 5.065)]
    cc2_vias = [(cc2.x, cc2.y), (4.635, 4.600)]
    nets.setdefault("CC1", []).append(circle_shape(*cc1_vias[1], SMALL_VIA_PAD))
    nets.setdefault("CC2", []).append(circle_shape(*cc2_vias[1], SMALL_VIA_PAD))
    cc_bottom_info = {
        "CC1": (cc1_vias[1], (r1_cc.x, r1_cc.y)),
        "CC2": (
            cc2_vias[1],
            (pad_map[("R2", "2")].x, pad_map[("R2", "2")].y),
        ),
    }
    cc_bottom_paths = None
    cc_bottom_failures = []
    for order in (("CC2", "CC1"), ("CC1", "CC2")):
        dynamic = Polygon()
        trial = {}
        try:
            for net in order:
                start, goal = cc_bottom_info[net]
                fixed = unary_union([
                    shape
                    for other_net, shapes in nets.items()
                    if other_net != net
                    for shape in shapes
                ]).buffer(CLEARANCE + TRACE_W / 2)
                blocked = fixed.union(dynamic)
                path = astar_path(
                    start, goal, blocked,
                    (4.05, 4.45, 4.72, 5.08), step=0.0025,
                )
                shape = line_shape(path)
                trial[net] = path
                dynamic = dynamic.union(shape.buffer(CLEARANCE + TRACE_W / 2))
        except RuntimeError as exc:
            cc_bottom_failures.append(str(exc))
            continue
        cc_bottom_paths = trial
        break
    if cc_bottom_paths is None:
        raise RuntimeError(
            "Unable to complete bottom-layer USB-C configuration fanout; "
            + "; ".join(cc_bottom_failures)
        )
    for net, path in cc_bottom_paths.items():
        route(net, path)

    # Stitch the two remote capacitor returns and the controller-side ground
    # pocket into the original top ground plane. The vias sit beyond the paste
    # pads, preventing solder wicking during bottom-side assembly.
    ground_vias = [
        (4.035, 5.055),  # C1 return
        (4.155, 4.92),   # C2 return
        (4.05, 4.60),    # CC-resistor return in the original top ground plane
        (4.60, 5.30),    # controller pin-20 ground pocket
        (4.45, 5.80),
        (4.44, 5.60),    # main controller-side ground pour
    ]
    c1_gnd = next(p for p in all_pads if p.ref == "C1" and p.net == "GND")
    c2_gnd = next(p for p in all_pads if p.ref == "C2" and p.net == "GND")
    route("GND", [(c1_gnd.x, c1_gnd.y), ground_vias[0]])
    route("GND", [
        (c2_gnd.x, c2_gnd.y), (c2_gnd.x, 4.85),
        (ground_vias[1][0], 4.85), ground_vias[1],
    ])

    # Both Type-C configuration resistors return to the nearby ground stitch.
    r1_gnd = pad_map[("R1", "1")]
    r2_gnd = pad_map[("R2", "1")]
    route("GND", [
        (r1_gnd.x, r1_gnd.y), (ground_vias[2][0], r1_gnd.y), ground_vias[2],
    ])
    route("GND", [
        (r2_gnd.x, r2_gnd.y), (ground_vias[2][0], r2_gnd.y), ground_vias[2],
    ])

    # Re-pour the cleared module bay as bottom ground after every signal is
    # fixed.  Only pour components that touch a real GND pad are retained, so
    # no floating copper islands are emitted. Plated ground stitching vias tie
    # every retained pocket into the controller's original top ground plane.
    non_ground_copper = unary_union([
        shape for net, shapes in nets.items() if net != "GND" for shape in shapes
    ]).buffer(CLEARANCE + 0.0002)
    pour_area = box(4.205, 5.292, 5.015, 5.845).difference(non_ground_copper).buffer(0)
    pour_components = list(pour_area.geoms) if pour_area.geom_type == "MultiPolygon" else [pour_area]
    gnd_pads = unary_union([p.shape for p in all_pads if p.net == "GND"])
    retained_pour = unary_union([
        component for component in pour_components
        if component.buffer(1e-6).intersects(gnd_pads)
    ]).buffer(0)
    nets.setdefault("GND", []).append(retained_pour)

    # Each isolated local capacitor-ground pocket is stitched to the original
    # top ground plane.  This keeps every ground pad electrically continuous
    # without adding any parts or a second assembly operation.
    for x, y in ground_vias:
        nets.setdefault("GND", []).append(circle_shape(x, y, VIA_PAD))

    bottom_nets = {name: unary_union(shapes).buffer(0) for name, shapes in nets.items()}

    # Only ground-via annuli are added on top; all signals remain on the bottom.
    base_top = load_layer(BASE / "Gerber_TopLayer.GTL")
    components = list(base_top.geoms) if base_top.geom_type == "MultiPolygon" else [base_top]
    ground_component = max(components, key=lambda g: g.area)
    top_repurpose_clear = box(4.205, 5.292, 5.015, 5.845)
    existing_x3 = component_for_point(base_top, x3_vias[0], tolerance=0.001)
    if existing_x3 is None:
        raise RuntimeError("Unable to identify original X3 top-copper component")
    # The original A-button trace runs vertically through the accessory-port
    # bay. Retain the membrane contact below the connector, then retire the
    # upper trace that would otherwise be bridged by the grounded shell stakes.
    existing_a = component_for_point(base_top, (4.67, 5.18), tolerance=0.001)
    if existing_a is None:
        raise RuntimeError("Unable to identify original A-button top-copper component")
    a_keep_box = box(4.60, 4.00, 5.05, 4.52)
    retained_a = existing_a.intersection(a_keep_box).buffer(0)
    a_repurpose_clear = existing_a.difference(retained_a).buffer(0.0010)
    signal_copper = unary_union([
        component for component in components
        if component is not ground_component
        and not component.equals(existing_x3)
        and not component.equals(existing_a)
    ]).difference(top_repurpose_clear).buffer(0)
    retained_x3 = existing_x3.difference(top_repurpose_clear).buffer(0)
    a_top_shape = unary_union([
        retained_a,
        circle_shape(*a_button_via, SMALL_VIA_PAD),
        circle_shape(endpoints[20].x, endpoints[20].y, SMALL_VIA_PAD),
    ]).buffer(0)
    vbus_top_blocked = unary_union([
        signal_copper.buffer(CLEARANCE + VIA_PAD / 2),
        retained_x3.buffer(CLEARANCE + TRACE_W / 2),
        a_top_shape.buffer(CLEARANCE + TRACE_W / 2),
    ])
    top_vbus_path = astar_path(
        signal_vias[0], signal_vias[1], vbus_top_blocked,
        (3.80, 4.90, 4.72, 5.90), step=0.0025,
    )
    j1_top_pads = {}
    for pad in all_pads:
        if pad.ref == "J1" and pad.through_hole and pad.net != "GND":
            j1_top_pads.setdefault(pad.net, []).append(pad.shape)
    top_nets = {
        "A": a_top_shape,
        "VBUS": unary_union([
            line_shape(top_vbus_path, 0.010),
            *[circle_shape(x, y, VIA_PAD) for x, y in signal_vias],
            *j1_top_pads["VBUS"],
        ]).buffer(0),
        "GND": unary_union([
            pad.shape for pad in all_pads
            if pad.ref == "J1" and pad.through_hole and pad.net == "GND"
        ]).buffer(0),
    }
    top_nets["NC_A8"] = unary_union(j1_top_pads["NC_A8"]).buffer(0)
    top_nets["NC_B8"] = unary_union(j1_top_pads["NC_B8"]).buffer(0)

    cc_top_info = {
        "CC1": {"contact": cc1_vias[0], "via": cc1_vias[1]},
        "CC2": {"contact": cc2_vias[0], "via": cc2_vias[1]},
    }
    cc_top_paths = None
    cc_top_shapes = None
    cc_top_failures = []
    for order in (("CC2", "CC1"), ("CC1", "CC2")):
        dynamic = unary_union([
            signal_copper.buffer(CLEARANCE + TRACE_W / 2),
            *[shape.buffer(CLEARANCE + TRACE_W / 2) for shape in top_nets.values()],
        ])
        trial_paths = {}
        trial_shapes = {}
        try:
            for net in order:
                info = cc_top_info[net]
                other_pads = unary_union([
                    pad.shape.buffer(CLEARANCE + TRACE_W / 2)
                    for pad in all_pads
                    if pad.ref == "J1" and pad.net != net
                ])
                path = astar_path(
                    info["contact"], info["via"], dynamic.union(other_pads),
                    (4.30, 4.45, 4.72, 5.30), step=0.0025,
                )
                shape = unary_union([
                    line_shape(path),
                    circle_shape(*info["via"], SMALL_VIA_PAD),
                    *j1_top_pads[net],
                ]).buffer(0)
                trial_paths[net] = path
                trial_shapes[net] = shape
                dynamic = dynamic.union(shape.buffer(CLEARANCE + TRACE_W / 2))
        except RuntimeError as exc:
            cc_top_failures.append(str(exc))
            continue
        cc_top_paths = trial_paths
        cc_top_shapes = trial_shapes
        break
    if cc_top_paths is None or cc_top_shapes is None:
        raise RuntimeError(
            "Unable to complete top-layer USB-C configuration fanout; "
            + "; ".join(cc_top_failures)
        )
    top_nets.update(cc_top_shapes)

    # Route the relocated USB pair through the existing top-side ground plane
    # without cutting any preserved button trace.  A* is used here instead of
    # the Rev-A straight bridges because the accessory-port recess lies below
    # several original A/B fanout traces.
    usb_top_bounds = (3.80, 4.60, 4.72, 5.90)
    usb_fixed_blocked = unary_union([
        signal_copper.buffer(CLEARANCE + TRACE_W / 2),
        *[shape.buffer(CLEARANCE + TRACE_W / 2) for shape in top_nets.values()],
        *[
            pad.shape.buffer(CLEARANCE + TRACE_W / 2)
            for pad in all_pads
            if pad.ref == "J1" and pad.net == "GND"
        ],
    ])
    usb_data = {
        "USB_DM": {
            "via": dm_vias[0],
            "contacts": dm_vias[1:],
            "pair_on_top": False,
        },
        "USB_DP": {
            "via": dp_vias[0],
            "contacts": dp_vias[1:],
            "pair_on_top": True,
        },
    }
    usb_data_paths = None
    usb_data_shapes = None
    usb_data_failures = []
    # Try both net orders and both possible connector targets. The footprint is
    # dense enough that choosing the wrong first escape can close the only
    # Standard-rule corridor for the second signal.
    for order in (("USB_DP", "USB_DM"), ("USB_DM", "USB_DP")):
        for first_target in (0, 1):
            for second_target in (0, 1):
                target_by_net = {
                    order[0]: first_target,
                    order[1]: second_target,
                }
                trial_paths = {}
                trial_shapes = {}
                dynamic = usb_fixed_blocked
                try:
                    for net in order:
                        info = usb_data[net]
                        other_net = "USB_DP" if net == "USB_DM" else "USB_DM"
                        other_pad_blocked = unary_union([
                            shape.buffer(CLEARANCE + TRACE_W / 2)
                            for shape in j1_top_pads[other_net]
                        ])
                        blocked = dynamic.union(other_pad_blocked)
                        target = info["contacts"][target_by_net[net]]
                        path = astar_path(
                            info["via"], target, blocked,
                            usb_top_bounds, step=0.0025,
                        )
                        shapes = [
                            line_shape(path),
                            circle_shape(*info["via"], SMALL_VIA_PAD),
                            *j1_top_pads[net],
                        ]
                        if info["pair_on_top"]:
                            pair_shape = line_shape(info["contacts"])
                            if blocked.intersects(pair_shape):
                                raise RuntimeError(f"No clear top-layer contact pair for {net}")
                            shapes.append(pair_shape)
                        net_shape = unary_union(shapes).buffer(0)
                        trial_paths[net] = path
                        trial_shapes[net] = net_shape
                        dynamic = dynamic.union(
                            net_shape.buffer(CLEARANCE + TRACE_W / 2)
                        )
                except RuntimeError as exc:
                    usb_data_failures.append(str(exc))
                    continue
                usb_data_paths = trial_paths
                usb_data_shapes = trial_shapes
                break
            if usb_data_paths is not None:
                break
        if usb_data_paths is not None:
            break
    if usb_data_paths is None or usb_data_shapes is None:
        raise RuntimeError(
            "Unable to complete USB-C top-layer fanout; "
            + "; ".join(usb_data_failures)
        )
    top_dm_path = usb_data_paths["USB_DM"]
    top_dp_path = usb_data_paths["USB_DP"]
    top_nets.update(usb_data_shapes)
    retained_x3 = component_for_point(
        retained_x3.difference(top_nets["VBUS"].buffer(CLEARANCE + 0.0003)).buffer(0),
        x3_vias[0],
        tolerance=0.001,
    )
    if retained_x3 is None:
        raise RuntimeError("VBUS clearance unexpectedly severed the retained X3 button trace")
    top_blocked = unary_union([
        signal_copper.buffer(CLEARANCE + TRACE_W / 2),
        *[shape.buffer(CLEARANCE + TRACE_W / 2) for shape in top_nets.values()],
    ])
    top_x3_path = astar_path(
        x3_vias[0], x3_vias[1], top_blocked,
        (3.80, 4.90, 4.72, 5.90), step=0.0025,
    )
    top_nets["X3"] = unary_union([
        retained_x3,
        line_shape(top_x3_path),
        *[circle_shape(x, y, SMALL_VIA_PAD) for x, y in x3_vias],
    ]).buffer(0)
    top_paths = {
        "VBUS": top_vbus_path,
        "CC1": cc_top_paths["CC1"],
        "CC2": cc_top_paths["CC2"],
        "USB_DM": top_dm_path,
        "USB_DP": [*top_dp_path, *dp_vias[1:]],
        "X3": top_x3_path,
    }

    placements = [
        Placement("U1", "PIC16F1459", "PIC16F1459-I/SO", 4.38, 5.52, 180, package="SOIC-20"),
        Placement("J1", "USB-C 2.0 receptacle", "USB4085-GF-A", USB_CX, USB_CY, 0, package="GCT USB4085 PTH"),
        Placement("RN1", "8x10k bussed resistor array", "746X101103JP", 3.93, 5.60, 90, package="1206 10-pin bussed array"),
        Placement("RN2", "8x10k bussed resistor array", "746X101103JP", 4.28, 4.96, 90, package="1206 10-pin bussed array"),
        Placement("R1", "5.1k 1%", "RC0402FR-075K1L", 4.22, 4.700, 0, package="0402"),
        Placement("R2", "5.1k 1%", "RC0402FR-075K1L", 4.22, 4.620, 0, package="0402"),
        Placement("C1", "0.1uF 16V X7R", "CC0402KRX7R7BB104", 4.05, 5.02, 0, package="0402"),
        Placement("C2", "1uF 10V X7R", "CC0402KRX7R6BB105", 4.12, 4.90, 90, package="0402"),
        Placement("C3", "0.47uF 6.3V X5R", "CC0402KRX5R5BB474", 4.63, 5.465, 90, package="0402"),
    ]
    all_vias = signal_vias + ground_vias
    small_vias = [
        dm_vias[0], dp_vias[0], cc1_vias[1], cc2_vias[1],
        a_button_via, *x3_vias,
    ]
    j1_pin_holes = [
        (pad.x, pad.y) for pad in all_pads
        if pad.ref == "J1" and not pad.number.startswith("SH")
    ]
    j1_rear_shell_holes = [
        (pad.x, pad.y) for pad in all_pads
        if pad.ref == "J1" and pad.number.startswith("SHR")
    ]
    j1_front_shell_holes = [
        (pad.x, pad.y) for pad in all_pads
        if pad.ref == "J1" and pad.number.startswith("SHF")
    ]
    return {
        "pads": all_pads,
        "bottom_nets": bottom_nets,
        "top_nets": top_nets,
        "placements": placements,
        "j1_pin_holes": j1_pin_holes,
        "j1_rear_shell_holes": j1_rear_shell_holes,
        "j1_front_shell_holes": j1_front_shell_holes,
        "vias": all_vias,
        "small_vias": small_vias,
        "dm_vias": dm_vias,
        "dp_vias": dp_vias,
        "cc_bottom_paths": cc_bottom_paths,
        "a_button_path": a_button_path,
        "a_button_via": a_button_via,
        "cc1_vias": cc1_vias,
        "cc2_vias": cc2_vias,
        "signal_vias": signal_vias,
        "x3_vias": x3_vias,
        "endpoint_vias": endpoint_vias,
        "ground_vias": ground_vias,
        "controls": controls,
        "control_paths": control_paths,
        "pullup_paths": pullup_paths,
        "top_paths": top_paths,
        "ground_component": ground_component,
        "signal_copper": signal_copper,
        "top_repurpose_clear": top_repurpose_clear,
        "a_repurpose_clear": a_repurpose_clear,
    }


def validate_design(design):
    errors = []

    # The USB4085 body is 9.17 x 8.94 mm. Its mating face projects 0.22 mm into
    # the preserved accessory-port recess and the deeper PTH body stays clear
    # of every neighboring component.
    connector_front_x = USB_CX + USB_BODY_HALF_LENGTH
    connector_body_min_y = USB_CY - USB_BODY_HALF_WIDTH
    connector_body_max_y = USB_CY + USB_BODY_HALF_WIDTH
    if not ACCESSORY_RECESS_INNER_X <= connector_front_x <= ACCESSORY_RECESS_INNER_X + 0.020:
        errors.append(
            f"J1 mating face is not aligned to the accessory recess: X={connector_front_x:.4f}"
        )
    if connector_body_min_y < ACCESSORY_RECESS_Y_MIN or connector_body_max_y > ACCESSORY_RECESS_Y_MAX:
        errors.append("J1 body does not fit inside the accessory-port recess")

    # The vendor footprint is drawn for a top-mounted connector. SwanSong USB
    # mounts J1 on the bottom while preserving the +X mating direction, which
    # mirrors the contact columns: A1/B12 must be above A12/B1 in board view.
    j1_pads = {
        pad.number: pad for pad in design["pads"] if pad.ref == "J1"
    }
    if not (j1_pads["A1"].y > j1_pads["A12"].y and
            j1_pads["B12"].y > j1_pads["B1"].y):
        errors.append("J1 contact pattern is not mirrored for bottom-side assembly")
    j1_placement = next(p for p in design["placements"] if p.ref == "J1")
    if j1_placement.side != "Bottom":
        errors.append("J1 must remain a bottom-side placement")

    connector_body = box(
        USB_CX - USB_BODY_HALF_LENGTH,
        connector_body_min_y,
        USB_CX + USB_BODY_HALF_LENGTH,
        connector_body_max_y,
    )
    for pad in design["pads"]:
        if pad.ref != "J1" and pad.ref != "TP" and connector_body.intersects(pad.shape):
            errors.append(f"J1 mechanical body overlaps {pad.ref} pad {pad.number}")

    for layer_name in ("bottom_nets", "top_nets"):
        nets = design[layer_name]
        names = sorted(nets)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                distance = nets[a].distance(nets[b])
                if distance + 1e-6 < CLEARANCE:
                    errors.append(f"{layer_name}: {a} to {b} clearance {distance * 25.4:.3f} mm")

    # Every connector hole has at least a 5 mil copper annulus. The shell tabs
    # use close-fitting round holes rather than routed slots to keep fabrication
    # on MacroFab's least-expensive Standard process.
    for pad in [p for p in design["pads"] if p.ref == "J1"]:
        if pad.number.startswith("SHR"):
            drill = USB_REAR_SHELL_DRILL
        elif pad.number.startswith("SHF"):
            drill = USB_FRONT_SHELL_DRILL
        else:
            drill = USB_PIN_DRILL
        annular_ring = (pad.width - drill) / 2
        if annular_ring + 1e-6 < 0.005:
            errors.append(f"J1 {pad.number} annular ring is {annular_ring * 1000:.2f} mil")

    # Every bottom-only net must be a single connected copper component.
    # VBUS is the one deliberate exception: its two bottom components are
    # joined by the validated top trace and plated through-vias.
    for net, shape in design["bottom_nets"].items():
        if net in {"VBUS", "USB_DM", "USB_DP", "CC1", "CC2", "X3", "GND"}:
            continue
        components = list(shape.geoms) if shape.geom_type == "MultiPolygon" else [shape]
        if len([component for component in components if not component.is_empty]) != 1:
            errors.append(f"bottom_nets: {net} is electrically disconnected ({len(components)} copper islands)")

    # The power nets deliberately change layers once.  Every bottom island
    # must terminate at one via, and each top bridge must be continuous.
    for net, via_points, via_diameter in (
        ("VBUS", design["signal_vias"], VIA_PAD),
        ("USB_DM", design["dm_vias"], SMALL_VIA_PAD),
        ("USB_DP", design["dp_vias"], SMALL_VIA_PAD),
        ("CC1", design["cc1_vias"], SMALL_VIA_PAD),
        ("CC2", design["cc2_vias"], SMALL_VIA_PAD),
        ("X3", design["x3_vias"], SMALL_VIA_PAD),
    ):
        shape = design["bottom_nets"][net]
        components = list(shape.geoms) if shape.geom_type == "MultiPolygon" else [shape]
        for component in components:
            # Pins 1 and 10 are the duplicated ends of each array's internal
            # VBUS bus. A landing may therefore be electrically joined through
            # the component even when that particular end has no PCB trace.
            internal_common_pads = [
                p for p in design["pads"]
                if p.ref in {"RN1", "RN2"} and p.number in {"1", "10"}
            ]
            hits = sum(component.intersects(circle_shape(x, y, via_diameter)) for x, y in via_points)
            if net == "VBUS" and hits == 0 and any(
                component.intersects(pad.shape) for pad in internal_common_pads
            ):
                continue
            # D- is intentionally paired on the bottom: the controller island
            # terminates at one via while the short connector island terminates
            # at both plated Type-C contacts.
            allowed_hits = {1, 2} if net == "USB_DM" else {1}
            if hits not in allowed_hits:
                errors.append(f"bottom_nets: {net} island terminates at {hits} vias instead of one")
        top_shape = design["top_nets"][net]
        top_components = list(top_shape.geoms) if top_shape.geom_type == "MultiPolygon" else [top_shape]
        component_count = len([component for component in top_components if not component.is_empty])
        if net == "VBUS":
            # The four isolated top annuli connect through their plated barrels
            # to the single bottom connector bus; the PIC bridge is the fifth.
            allowed = 1 + len([p for p in design["pads"] if p.ref == "J1" and p.net == "VBUS"])
        elif net == "USB_DM":
            # One top component reaches the controller; the other contact annulus
            # joins through the validated bottom-layer D- pair.
            allowed = 2
        else:
            allowed = 1
        if component_count != allowed:
            errors.append(f"top_nets: {net} bridge is not continuous")

    # Every local ground pocket must contain a stitching via to the original
    # top ground plane.
    gnd_shape = design["bottom_nets"]["GND"]
    gnd_components = list(gnd_shape.geoms) if gnd_shape.geom_type == "MultiPolygon" else [gnd_shape]
    connector_ground_pads = [
        p for p in design["pads"]
        if p.ref == "J1" and p.net == "GND" and p.through_hole
    ]
    for component in gnd_components:
        via_hit = any(component.intersects(circle_shape(x, y, VIA_PAD)) for x, y in design["ground_vias"])
        connector_pth = any(component.intersects(pad.shape) for pad in connector_ground_pads)
        if not via_hit and not connector_pth:
            errors.append("bottom_nets: unstitched ground island")
    for pad in connector_ground_pads:
        if not design["ground_component"].intersects(pad.shape):
            errors.append(f"J1 {pad.number} misses the original top ground plane")
    for x, y in design["ground_vias"]:
        if not design["ground_component"].contains(Point(x, y)):
            errors.append(f"ground via misses original top ground plane at {x:.4f},{y:.4f}")

    # New top signals must not cut any original signal island.
    for net, shape in design["top_nets"].items():
        if shape.buffer(CLEARANCE).intersects(design["signal_copper"]):
            errors.append(f"top_nets: {net} intersects original signal copper")

    # Ground and signal vias may only touch their intended copper.
    for x, y in design["vias"]:
        if not (4.20 <= x <= 4.76 and 5.30 <= y <= 5.84):
            continue
        if circle_shape(x, y, VIA_PAD).intersects(design["signal_copper"]):
            # USB/VBUS signal vias intentionally clear ground but must not land
            # on an existing signal.  Ground vias are held to the same rule.
            errors.append(f"new via intersects original signal copper at {x:.4f},{y:.4f}")
    for x, y in design["small_vias"]:
        if circle_shape(x, y, SMALL_VIA_PAD).intersects(design["signal_copper"]):
            errors.append(f"new small via intersects original signal copper at {x:.4f},{y:.4f}")

    if errors:
        raise RuntimeError("Design validation failed:\n  " + "\n  ".join(errors))


def modify_drill(base_path: Path, output_path: Path, points: list[tuple[float, float]], tool="T01"):
    text = base_path.read_text()
    marker = text.rfind("M30")
    insertion = [f"\n{tool}\n"]
    insertion.extend(f"X{round(x * 10000):06d}Y{round(y * 10000):06d}\n" for x, y in points)
    output_path.write_text(text[:marker] + "".join(insertion) + text[marker:])


def read_excellon_holes(path: Path):
    """Read the simple 2:4-inch Excellon form used by the source board."""
    tools = {}
    current_tool = None
    holes = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        tool_definition = re.fullmatch(r"(T\d+)C([0-9.]+)", line)
        if tool_definition:
            tools[tool_definition.group(1)] = float(tool_definition.group(2))
            continue
        if re.fullmatch(r"T\d+", line):
            current_tool = line
            continue
        coordinate = re.fullmatch(r"X(\d+)Y(\d+)", line)
        if coordinate and current_tool in tools:
            holes.append((
                int(coordinate.group(1)) / 10000,
                int(coordinate.group(2)) / 10000,
                tools[current_tool],
            ))
    return holes


def write_outputs(design):
    OUT.mkdir(parents=True, exist_ok=True)
    for path in OUT.iterdir():
        if path.is_file():
            path.unlink()

    # Copy the mechanical layer, then use a thin plot line for the route.
    # MacroFab's DRC treats the plotted outline aperture as part of the edge
    # keepout.  The source's 10 mil outline therefore consumed almost the
    # entire nominal copper setback even though the actual route follows the
    # line center.  A 1 mil plot line is unambiguous to the importer.
    for name in ["Gerber_BoardOutlineLayer.GKO"]:
        shutil.copy2(BASE / name, OUT / name)
    outline_path = OUT / "Gerber_BoardOutlineLayer.GKO"
    outline_text = outline_path.read_text().replace("%ADD10C,0.0100*%", "%ADD10C,0.0010*%")
    outline_path.write_text(outline_text)

    # The original outline is a 10 mil stroke (5 mil radius). Buffer it by
    # another 11 mil to create a true 16 mil centerline-to-copper keepout.
    # This is intentionally more conservative than MacroFab Standard so
    # importer rounding cannot drop the reported clearance below 10 mil.
    source_outline_stroke = load_layer(BASE / "Gerber_BoardOutlineLayer.GKO")
    outer_edge_keepout = source_outline_stroke.buffer(EDGE_COPPER_CLEARANCE - 0.0050)
    # MacroFab may treat NPTH drills as internal routed edges. Clear copper from
    # every preserved mechanical hole as well as from the outside route so the
    # uploaded Gerbers satisfy the same Standard copper-to-edge rule everywhere.
    npth_holes = read_excellon_holes(BASE / "Drill_NPTH_Through.DRL")
    npth_edge_keepout = unary_union([
        circle_shape(x, y, diameter + 2 * EDGE_COPPER_CLEARANCE)
        for x, y, diameter in npth_holes
    ]).buffer(0)
    edge_keepout = outer_edge_keepout.union(npth_edge_keepout).buffer(0)

    bottom_dark = unary_union(list(design["bottom_nets"].values())).difference(edge_keepout).buffer(0)
    top_dark = unary_union(list(design["top_nets"].values())).difference(edge_keepout).buffer(0)

    # The former module interior becomes a solder-mask-covered routing bay.
    # Remove every old module landing before re-adding only the endpoints used
    # by SwanSong USB. Leaving the unused pad slivers in place produced a
    # 1.3-mil gap and copper that crossed the curved board cutout.
    old_endpoint_copper = unary_union([pad.shape for pad in endpoint_pads().values()]).buffer(0.002)
    central_clear = box(4.205, 5.292, 5.015, 5.845).union(old_endpoint_copper).buffer(0)
    bottom_non_ground = unary_union([
        shape for net, shape in design["bottom_nets"].items() if net != "GND"
    ])
    bottom_clear = central_clear.union(bottom_non_ground.buffer(CLEARANCE + 0.0002)).union(edge_keepout).buffer(0)
    append_overlay(BASE / "Gerber_BottomLayer.GBL", OUT / "Gerber_BottomLayer.GBL", bottom_clear, bottom_dark)

    top_clear = unary_union([
        edge_keepout,
        design["top_repurpose_clear"],
        design["a_repurpose_clear"],
        *[
            shape.buffer(CLEARANCE + 0.0002)
            for net, shape in design["top_nets"].items()
            if net != "GND"
        ],
    ]).buffer(0)
    append_overlay(BASE / "Gerber_TopLayer.GTL", OUT / "Gerber_TopLayer.GTL", top_clear, top_dark)

    # Solder masks: retire all old RP2040/module pads and the SNES level shifter.
    mask_shapes = []
    paste_shapes = []
    for pad in design["pads"]:
        if pad.mask:
            mask_shapes.append(pad.shape.buffer(MASK_EXPAND, resolution=16))
        if pad.paste and pad.ref not in {"TP"}:
            paste_shapes.append(pad.shape)
    bottom_mask_dark = unary_union(mask_shapes).buffer(0)
    bottom_mask_clear = box(4.025, 5.095, 5.035, 6.025)
    append_overlay(BASE / "Gerber_BottomSolderMaskLayer.GBS", OUT / "Gerber_BottomSolderMaskLayer.GBS", bottom_mask_clear, bottom_mask_dark)

    top_mask_clear = box(2.12, 4.13, 2.58, 4.75)
    top_mask_dark = unary_union([
        pad.shape.buffer(MASK_EXPAND, resolution=16)
        for pad in design["pads"]
        if pad.ref == "J1" and pad.through_hole and pad.mask
    ]).buffer(0)
    append_overlay(BASE / "Gerber_TopSolderMaskLayer.GTS", OUT / "Gerber_TopSolderMaskLayer.GTS", top_mask_clear, top_mask_dark)

    write_positive_layer(OUT / "Gerber_BottomPasteMaskLayer.GBP", "Bottom Paste", unary_union(paste_shapes).buffer(0))
    write_positive_layer(OUT / "Gerber_TopPasteMaskLayer.GTP", "Top Paste (intentionally empty)", Polygon())

    # New top silkscreen: intentionally sparse, high-contrast, and pixel-clean.
    top_silk = []
    top_silk.append(text_shape("SWANSONG USB", 2.67, 5.08, 0.62, "center"))
    top_silk.append(text_shape("USB GAMEPAD - REV C", 2.67, 5.97, 0.22, "center"))
    labels = [
        ("Y1", 0.845, 5.86), ("Y2", 1.12, 5.59), ("Y3", 0.845, 5.31), ("Y4", 0.565, 5.59),
        ("X1", 0.845, 4.50), ("X2", 1.12, 4.23), ("X3", 0.845, 3.95), ("X4", 0.565, 4.23),
        ("A", 4.82, 4.35), ("B", 4.56, 4.10),
        ("SOUND", 1.915, 3.82), ("START", 2.35, 3.82), ("POWER", 2.79, 3.82),
    ]
    for label, x, y in labels:
        top_silk.append(text_shape(label, x, y, 0.20 if len(label) <= 2 else 0.14, "center"))
    write_positive_layer(OUT / "Gerber_TopSilkscreenLayer.GTO", "Top Silkscreen", unary_union(top_silk).buffer(0))

    bottom_silk = [
        text_shape("SWANSONG USB", 2.60, 5.95, 0.26, "center"),
        text_shape("REV C", 2.60, 5.78, 0.18, "center"),
        text_shape("FACTORY PROGRAMMED", 3.18, 5.45, 0.12, "center"),
        text_shape("USB HID GAMEPAD", 3.18, 5.30, 0.12, "center"),
        text_shape("USB-C", 4.82, 5.30, 0.12, "center"),
        text_shape("VPP", 3.98, 5.42, 0.08, "right"),
    ]
    write_positive_layer(OUT / "Gerber_BottomSilkscreenLayer.GBO", "Bottom Silkscreen", unary_union(bottom_silk).buffer(0))

    # Keep the source board's true non-plated mechanical holes unchanged. The
    # USB4085 has no plastic locating pegs; all 20 connector holes are plated.
    shutil.copy2(BASE / "Drill_NPTH_Through.DRL", OUT / "Drill_NPTH_Through.DRL")
    # EasyEDA exported two byte-identical plated drill files with different
    # comments. Emit one canonical file so importers do not show duplicate hits.
    for source_name in ("Drill_PTH_Through.DRL",):
        output_path = OUT / source_name
        modify_drill(BASE / source_name, output_path, design["vias"], "T01")
        small_drills = list(dict.fromkeys(design["small_vias"] + design["endpoint_vias"]))
        modify_drill(output_path, output_path, small_drills, "T02")
        drill_text = output_path.read_text()
        drill_text = drill_text.replace(
            "%\nG05",
            f";Holesize 2 = {SMALL_VIA_DRILL:.4f} inch\nT02C{SMALL_VIA_DRILL:.4f}\n%\nG05",
        )
        output_path.write_text(drill_text)
        modify_drill(output_path, output_path, design["j1_pin_holes"], "T04")
        modify_drill(output_path, output_path, design["j1_rear_shell_holes"], "T05")
        modify_drill(output_path, output_path, design["j1_front_shell_holes"], "T06")
        drill_text = output_path.read_text()
        drill_text = drill_text.replace(
            "%\nG05",
            f";Holesize 4 = {USB_PIN_DRILL:.4f} inch\n"
            f"T04C{USB_PIN_DRILL:.4f}\n"
            f";Holesize 5 = {USB_REAR_SHELL_DRILL:.4f} inch\n"
            f"T05C{USB_REAR_SHELL_DRILL:.4f}\n"
            f";Holesize 6 = {USB_FRONT_SHELL_DRILL:.4f} inch\n"
            f"T06C{USB_FRONT_SHELL_DRILL:.4f}\n%\nG05",
        )
        output_path.write_text(drill_text)

    # MacroFab XYRS, coordinates in mil relative to the lower-left board extent.
    x0, y0 = 0.300, 3.520
    with (PROJECT / "swansong-usb.XYRS").open("w", newline="") as handle:
        handle.write("#MacroFab, INC. XYRS data for SwanSong USB.\n")
        handle.write("#Board Size is 4745.00 x 2550.00 mil.\n")
        handle.write("#Coordinates are relative to the lower-left board extent (0.300, 3.520 inch).\n")
        handle.write("#Designator\tX-Loc\tY-Loc\tRotation\tSide\tType\tX-Size\tY-Size\tValue\tFootprint\tPopulate\tMPN\n")
        for p in design["placements"]:
            handle.write(
                f"{p.ref}\t{(p.x - x0) * 1000:.2f}\t{(p.y - y0) * 1000:.2f}\t{p.rotation:g}\t2\t1\t0\t0\t{p.value}\t{p.package}\t1\t{p.mpn}\n"
            )

    # Flat BOM for importer fallback and audit.
    quantities = {}
    for placement in design["placements"]:
        key = (placement.mpn, placement.value, placement.package)
        quantities.setdefault(key, []).append(placement.ref)
    with (PROJECT / "swansong-usb-bom.csv").open("w", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["Quantity", "Designators", "Manufacturer Part Number", "Description", "Package", "Manufacturer"])
        manufacturer = {
            "PIC16F1459-I/SO": "Microchip Technology",
            "USB4085-GF-A": "GCT",
            "746X101103JP": "CTS Resistor Products",
            "CC0402KRX5R5BB474": "Yageo",
        }
        for (mpn, value, package), refs in quantities.items():
            mfr = manufacturer.get(mpn, "Yageo")
            writer.writerow([len(refs), ", ".join(refs), mpn, value, package, mfr])

    manifest = {
        "product": "SwanSong USB",
        "revision": "C",
        "architecture": "PIC16F1459 crystal-free native USB, USB-C device",
        "placements": len(design["placements"]),
        "assembly_side": "Bottom SMT plus one PTH USB-C connector",
        "button_inputs": {net: {"former_module_pad": endpoint, "pic_pin": pin} for endpoint, (net, pin) in design["controls"].items()},
        "top_routes": design["top_paths"],
    }
    (PROJECT / "design-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    # Zip only the layers MacroFab needs.
    archive = shutil.make_archive(str(PROJECT / "swansong-usb-gerbers"), "zip", OUT)
    checksums = {}
    for path in sorted([*OUT.iterdir(), PROJECT / "swansong-usb.XYRS", PROJECT / "swansong-usb-bom.csv", Path(archive)]):
        checksums[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (PROJECT / "SHA256SUMS.json").write_text(json.dumps(checksums, indent=2) + "\n")


def main():
    design = build_design()
    validate_design(design)
    write_outputs(design)
    print(f"Generated {len(list(OUT.iterdir()))} manufacturing layers in {OUT}")
    print(f"Placements: {len(design['placements'])}; controls: {len(design['controls'])}")


if __name__ == "__main__":
    main()
