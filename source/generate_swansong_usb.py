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

# The WonderSwan Color exposes its accessory connector on the right side of the
# shell.  The preserved controller outline has the matching rectangular recess
# from X=4.720 in to the outer edge and Y=4.455..5.230 in.  Keep the USB-C
# receptacle centered in that opening and preserve the proven front-edge offset
# used by Rev A, which leaves the signal lands safely on the PCB while the plug
# mouth projects into the shell opening.
ACCESSORY_RECESS_INNER_X = 4.720
ACCESSORY_RECESS_Y_MIN = 4.455
ACCESSORY_RECESS_Y_MAX = 5.230
USB_CX = 4.58394
USB_CY = (ACCESSORY_RECESS_Y_MIN + ACCESSORY_RECESS_Y_MAX) / 2
USB_BODY_HALF_LENGTH = 3.675 / 25.4
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

    @property
    def shape(self):
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


def connector_pads(cx=USB_CX, cy=USB_CY):
    """GCT USB4110-GF-A facing the right-side accessory-port recess."""
    local = [
        ("A1", "GND", -3.2, -3.68, 0.6, 1.15),
        ("A4", "VBUS", -2.4, -3.68, 0.6, 1.15),
        ("A5", "CC1", -1.25, -3.68, 0.3, 1.15),
        ("A6", "USB_DP", -0.25, -3.68, 0.3, 1.15),
        ("A7", "USB_DM", 0.25, -3.68, 0.3, 1.15),
        ("A8", "NC", 1.25, -3.68, 0.3, 1.15),
        ("A9", "VBUS", 2.4, -3.68, 0.6, 1.15),
        ("A12", "GND", 3.2, -3.68, 0.6, 1.15),
        ("B1", "GND", 3.2, -3.68, 0.6, 1.15),
        ("B4", "VBUS", 2.4, -3.68, 0.6, 1.15),
        ("B5", "CC2", 1.75, -3.68, 0.3, 1.15),
        ("B6", "USB_DP", 0.75, -3.68, 0.3, 1.15),
        ("B7", "USB_DM", -0.75, -3.68, 0.3, 1.15),
        ("B8", "NC", -1.75, -3.68, 0.3, 1.15),
        ("B9", "VBUS", -2.4, -3.68, 0.6, 1.15),
        ("B12", "GND", -3.2, -3.68, 0.6, 1.15),
        ("SH1", "GND", -5.11, -3.105, 2.18, 2.0),
        ("SH2", "GND", -5.11, 0.825, 2.18, 2.0),
        ("SH3", "GND", 5.11, -3.105, 2.18, 2.0),
        ("SH4", "GND", 5.11, 0.825, 2.18, 2.0),
    ]
    result = []
    for number, net, lx, ly, w, h in local:
        # -90 degree rotation: (x, y) -> (y, -x); pad dimensions swap.
        result.append(rect_pad("J1", number, net, cx + ly / 25.4, cy - lx / 25.4, h, w))
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
        result.append(Pad(pad.ref, pad.number, net, pad.x, pad.y, pad.width, pad.height, pad.paste, pad.mask))
    return result


def build_design():
    endpoints = endpoint_pads()
    pic = pic_pads(cx=4.38, cy=5.52)
    connector = connector_pads()
    resistor_array_1 = resistor_array_pads("RN1", 3.93, 5.60)
    resistor_array_2 = resistor_array_pads("RN2", 4.34, 4.96)

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
    all_pads.extend(passive_pads("R1", "GND", "CC1", 4.22, 4.860, 0))
    all_pads.extend(passive_pads("R2", "GND", "CC2", 4.22, 4.765, 0))
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

    # USB-C duplicate pins. D+ stays on the bottom and joins behind the
    # connector. D- changes layers through two 10-mil vias, avoiding the
    # interleaved reversible-connector pads without a zero-ohm jumper.
    dm_pads = sorted([p for p in all_pads if p.ref == "J1" and p.net == "USB_DM"], key=lambda p: p.y)
    dp_pads = sorted([p for p in all_pads if p.ref == "J1" and p.net == "USB_DP"], key=lambda p: p.y)
    # Each reversible USB-C data contact gets its own via. Joining the duplicate
    # A/B contacts on the top layer prevents the interleaved bottom pads from
    # forcing a crossover beneath the receptacle.
    dm_vias = [
        (4.65, pic[18].y),
        (4.50, dm_pads[0].y),
        (4.50, dm_pads[1].y),
    ]
    dp_vias = [
        (4.63, pic[19].y),
        (4.52, dp_pads[0].y),
        (4.52, dp_pads[1].y),
    ]
    for x, y in dm_vias:
        nets.setdefault("USB_DM", []).append(circle_shape(x, y, SMALL_VIA_PAD))
    route("USB_DM", [(pic[18].x, pic[18].y), dm_vias[0]])
    route("USB_DM", [dm_vias[1], (dm_pads[0].x, dm_pads[0].y)])
    route("USB_DM", [dm_vias[2], (dm_pads[1].x, dm_pads[1].y)])
    for x, y in dp_vias:
        nets.setdefault("USB_DP", []).append(circle_shape(x, y, SMALL_VIA_PAD))
    route("USB_DP", [(pic[19].x, pic[19].y), dp_vias[0]])
    route("USB_DP", [dp_vias[1], (dp_pads[0].x, dp_pads[0].y)])
    route("USB_DP", [dp_vias[2], (dp_pads[1].x, dp_pads[1].y)])

    # Tie the two physical VBUS positions behind the connector pin row.  VBUS
    # then uses a short top-layer bridge between two vias, avoiding the control
    # fanout without adding a purchased jumper or a second assembly side.
    vbus_pads = sorted({(p.x, p.y) for p in all_pads if p.ref == "J1" and p.net == "VBUS"}, key=lambda p: p[1])
    connector_vbus_spine_x = 4.56
    route("VBUS", [vbus_pads[0], (connector_vbus_spine_x, vbus_pads[0][1]), (connector_vbus_spine_x, vbus_pads[-1][1]), vbus_pads[-1]], 0.012)
    signal_vias = [(4.56, 5.11), (4.24, pic[1].y)]
    for x, y in signal_vias:
        nets.setdefault("VBUS", []).append(circle_shape(x, y, VIA_PAD))
    route("VBUS", [
        vbus_pads[-1], (connector_vbus_spine_x, vbus_pads[-1][1]),
        (signal_vias[0][0], vbus_pads[-1][1]), signal_vias[0],
    ], 0.008)
    route("VBUS", [signal_vias[1], (4.32, pic[1].y), (pic[1].x, pic[1].y)], 0.012)

    # The two array commons join below the controller. Their common bus then
    # feeds the nearby PIC VBUS landing; the existing top VBUS bridge carries
    # power onward to the USB-C connector without crossing the A/B controls.
    rn1_common = pad_map[("RN1", "1")]
    rn2_common_low = pad_map[("RN2", "10")]
    route("VBUS", [
        (rn1_common.x, rn1_common.y), (3.75, rn1_common.y),
        (3.75, 4.82), (rn2_common_low.x, 4.82),
        (rn2_common_low.x, rn2_common_low.y),
    ], 0.008)
    rn2_common = pad_map[("RN2", "1")]
    rn_power_path = [
        (rn2_common.x, rn2_common.y), (4.33, rn2_common.y),
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

    # CC and VUSB branches are added after the button fanout so their narrow
    # service corridors do not bias the control router.
    cc1 = next(p for p in all_pads if p.ref == "J1" and p.net == "CC1")
    cc2 = next(p for p in all_pads if p.ref == "J1" and p.net == "CC2")
    r1_cc = pad_map[("R1", "2")]
    # Hop CC1 over RN2's VBUS feed on the top layer.  Both 10 mil vias use the
    # same standard drill already required by the USB pair, so this adds no
    # special fabrication operation or purchased part.
    cc1_vias = [(4.32, 4.88), (4.26, 4.88)]
    for x, y in cc1_vias:
        nets.setdefault("CC1", []).append(circle_shape(x, y, SMALL_VIA_PAD))
    route("CC1", [(cc1.x, cc1.y), (4.34, 4.88), cc1_vias[0]])
    route("CC1", [cc1_vias[1], (r1_cc.x, r1_cc.y)])
    route("CC2", [(cc2.x, cc2.y), (pad_map[("R2", "2")].x, pad_map[("R2", "2")].y)])

    # Stitch the two remote capacitor returns and the controller-side ground
    # pocket into the original top ground plane. The vias sit beyond the paste
    # pads, preventing solder wicking during bottom-side assembly.
    ground_vias = [
        (4.035, 5.055),  # C1 return
        (4.155, 4.92),   # C2 and CC-resistor returns
        (4.63, 5.10),    # USB-C signal-ground and shield return
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

    # R1 returns to the nearby stitching via above the VBUS trunk. R2 remains
    # below that trunk and joins the connector's lower ground contact through
    # the open corridor between the signal row and lower shell stake.
    r1_gnd = pad_map[("R1", "1")]
    r2_gnd = pad_map[("R2", "1")]
    route("GND", [(r1_gnd.x, r1_gnd.y), (r1_gnd.x, 4.92), ground_vias[1]])
    lower_j1_ground = min(
        [p for p in all_pads if p.ref == "J1" and p.net == "GND" and not p.number.startswith("SH")],
        key=lambda p: p.y,
    )
    route("GND", [
        (r2_gnd.x, r2_gnd.y), (4.18, 4.73),
        (4.36, 4.695), (4.40, 4.695),
        (lower_j1_ground.x, lower_j1_ground.y),
    ])

    # The connector now sits outside the former-module ground pour. Tie every
    # GND contact and all four shield stakes together along the front side of
    # the receptacle, then stitch that local group into the original top plane.
    j1_ground_pads = [p for p in all_pads if p.ref == "J1" and p.net == "GND"]
    j1_pin_ground = sorted(
        [p for p in j1_ground_pads if not p.number.startswith("SH")],
        key=lambda p: p.y,
    )
    j1_shell_ground = [p for p in j1_ground_pads if p.number.startswith("SH")]
    connector_ground_spine_x = 4.66
    left_shell_by_y = sorted(
        [p for p in j1_shell_ground if p.x < USB_CX],
        key=lambda p: p.y,
    )
    for pad in j1_pin_ground:
        nearest_shell = min(left_shell_by_y, key=lambda p: abs(p.y - pad.y))
        route("GND", [(pad.x, pad.y), (nearest_shell.x, nearest_shell.y)])
    for shell_y in sorted({p.y for p in j1_shell_ground}):
        shell_row = sorted([p for p in j1_shell_ground if abs(p.y - shell_y) < 1e-6], key=lambda p: p.x)
        # All four stakes are one continuous metal shell. Ground the right-hand
        # stakes directly; the left stakes are electrically common through the
        # connector body and remain free of the button-routing escape channel.
        route("GND", [(shell_row[-1].x, shell_y), (connector_ground_spine_x, shell_y)])
    route("GND", [
        (connector_ground_spine_x, min(p.y for p in j1_shell_ground)),
        (connector_ground_spine_x, max(p.y for p in j1_shell_ground)),
        ground_vias[2],
    ], 0.012)

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
    signal_copper = unary_union([
        component for component in components
        if component is not ground_component
        and not component.equals(existing_x3)
    ]).difference(top_repurpose_clear).buffer(0)
    retained_x3 = existing_x3.difference(top_repurpose_clear).buffer(0)
    vbus_top_blocked = unary_union([
        signal_copper.buffer(CLEARANCE + VIA_PAD / 2),
        retained_x3.buffer(CLEARANCE + TRACE_W / 2),
    ])
    top_vbus_path = astar_path(
        signal_vias[0], signal_vias[1], vbus_top_blocked,
        (3.80, 4.90, 4.72, 5.90), step=0.0025,
    )
    top_nets = {
        "VBUS": unary_union([
            line_shape(top_vbus_path, 0.010),
            *[circle_shape(x, y, VIA_PAD) for x, y in signal_vias],
        ]).buffer(0),
    }
    top_nets["CC1"] = unary_union([
        line_shape(cc1_vias),
        *[circle_shape(x, y, SMALL_VIA_PAD) for x, y in cc1_vias],
    ]).buffer(0)

    # Route the relocated USB pair through the existing top-side ground plane
    # without cutting any preserved button trace.  A* is used here instead of
    # the Rev-A straight bridges because the accessory-port recess lies below
    # several original A/B fanout traces.
    usb_top_bounds = (3.80, 4.60, 4.72, 5.90)
    usb_dm_blocked = unary_union([
        signal_copper.buffer(CLEARANCE + TRACE_W / 2),
        top_nets["VBUS"].buffer(CLEARANCE + TRACE_W / 2),
    ])
    top_dm_path = astar_path(dm_vias[0], dm_vias[2], usb_dm_blocked, usb_top_bounds, step=0.0025)
    top_nets["USB_DM"] = unary_union([
        line_shape(top_dm_path),
        line_shape([dm_vias[1], dm_vias[2]]),
        *[circle_shape(x, y, SMALL_VIA_PAD) for x, y in dm_vias],
    ]).buffer(0)

    usb_dp_blocked = unary_union([
        usb_dm_blocked,
        top_nets["USB_DM"].buffer(CLEARANCE + TRACE_W / 2),
    ])
    top_dp_path = astar_path(dp_vias[0], dp_vias[1], usb_dp_blocked, usb_top_bounds, step=0.0025)
    top_nets["USB_DP"] = unary_union([
        line_shape(top_dp_path),
        line_shape([dp_vias[1], dp_vias[2]]),
        *[circle_shape(x, y, SMALL_VIA_PAD) for x, y in dp_vias],
    ]).buffer(0)
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
        "CC1": cc1_vias,
        "USB_DM": [*top_dm_path, dm_vias[2]],
        "USB_DP": [*top_dp_path, dp_vias[2]],
        "X3": top_x3_path,
    }

    placements = [
        Placement("U1", "PIC16F1459", "PIC16F1459-I/SO", 4.38, 5.52, 180, package="SOIC-20"),
        Placement("J1", "USB-C 2.0 receptacle", "USB4110-GF-A", USB_CX, USB_CY, 0, package="GCT USB4110"),
        Placement("RN1", "8x10k bussed resistor array", "746X101103JP", 3.93, 5.60, 90, package="1206 10-pin bussed array"),
        Placement("RN2", "8x10k bussed resistor array", "746X101103JP", 4.34, 4.96, 90, package="1206 10-pin bussed array"),
        Placement("R1", "5.1k 1%", "RC0402FR-075K1L", 4.22, 4.860, 0, package="0402"),
        Placement("R2", "5.1k 1%", "RC0402FR-075K1L", 4.22, 4.765, 0, package="0402"),
        Placement("C1", "0.1uF 16V X7R", "CC0402KRX7R7BB104", 4.05, 5.02, 0, package="0402"),
        Placement("C2", "1uF 10V X7R", "CC0402KRX7R6BB105", 4.12, 4.90, 90, package="0402"),
        Placement("C3", "0.47uF 6.3V X5R", "CC0402KRX5R5BB474", 4.63, 5.465, 90, package="0402"),
    ]
    npth = [
        (USB_CX - 2.605 / 25.4, USB_CY + 2.89 / 25.4),
        (USB_CX - 2.605 / 25.4, USB_CY - 2.89 / 25.4),
    ]
    all_vias = signal_vias + ground_vias
    small_vias = dm_vias + dp_vias + cc1_vias + x3_vias
    return {
        "pads": all_pads,
        "bottom_nets": bottom_nets,
        "top_nets": top_nets,
        "placements": placements,
        "npth": npth,
        "vias": all_vias,
        "small_vias": small_vias,
        "dm_vias": dm_vias,
        "dp_vias": dp_vias,
        "cc1_vias": cc1_vias,
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
    }


def validate_design(design):
    errors = []

    # GCT's verified USB4110 footprint marks the PCB-edge datum 3.675 mm in
    # front of the footprint origin.  Rev B intentionally projects the mating
    # face 0.22 mm into the shell opening, while centering the 8.94 mm body in
    # the much taller preserved accessory-port recess.
    connector_front_x = USB_CX + USB_BODY_HALF_LENGTH
    connector_body_min_y = USB_CY - USB_BODY_HALF_WIDTH
    connector_body_max_y = USB_CY + USB_BODY_HALF_WIDTH
    if not ACCESSORY_RECESS_INNER_X <= connector_front_x <= ACCESSORY_RECESS_INNER_X + 0.020:
        errors.append(
            f"J1 mating face is not aligned to the accessory recess: X={connector_front_x:.4f}"
        )
    if connector_body_min_y < ACCESSORY_RECESS_Y_MIN or connector_body_max_y > ACCESSORY_RECESS_Y_MAX:
        errors.append("J1 body does not fit inside the accessory-port recess")

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

    # Every bottom-only net must be a single connected copper component.
    # VBUS is the one deliberate exception: its two bottom components are
    # joined by the validated top trace and plated through-vias.
    for net, shape in design["bottom_nets"].items():
        if net in {"VBUS", "USB_DM", "USB_DP", "CC1", "X3", "GND"}:
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
            if hits != 1:
                errors.append(f"bottom_nets: {net} island terminates at {hits} vias instead of one")
        top_shape = design["top_nets"][net]
        top_components = list(top_shape.geoms) if top_shape.geom_type == "MultiPolygon" else [top_shape]
        if len([component for component in top_components if not component.is_empty]) != 1:
            errors.append(f"top_nets: {net} bridge is not continuous")

    # Every local ground pocket must contain a stitching via to the original
    # top ground plane.
    gnd_shape = design["bottom_nets"]["GND"]
    gnd_components = list(gnd_shape.geoms) if gnd_shape.geom_type == "MultiPolygon" else [gnd_shape]
    connector_shell_pads = [
        p for p in design["pads"]
        if p.ref == "J1" and p.number.startswith("SH")
    ]
    for component in gnd_components:
        via_hit = any(component.intersects(circle_shape(x, y, VIA_PAD)) for x, y in design["ground_vias"])
        shell_common = any(component.intersects(pad.shape) for pad in connector_shell_pads)
        if not via_hit and not shell_common:
            errors.append("bottom_nets: unstitched ground island")
    for x, y in design["ground_vias"]:
        if not design["ground_component"].contains(Point(x, y)):
            errors.append(f"ground via misses original top ground plane at {x:.4f},{y:.4f}")

    # New top signals must not cut any original signal island.
    for net, shape in design["top_nets"].items():
        if shape.buffer(CLEARANCE).intersects(design["signal_copper"]):
            errors.append(f"top_nets: {net} intersects original signal copper")

    # Locating holes and ground vias may only touch the original ground plane.
    for x, y in design["npth"]:
        hole = circle_shape(x, y, 0.65 / 25.4)
        if hole.intersects(design["signal_copper"]):
            errors.append(f"J1 locating hole intersects signal copper at {x:.4f},{y:.4f}")
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
    edge_keepout = source_outline_stroke.buffer(EDGE_COPPER_CLEARANCE - 0.0050)

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
        *[shape.buffer(CLEARANCE + 0.0002) for shape in design["top_nets"].values()],
    ]).buffer(0)
    append_overlay(BASE / "Gerber_TopLayer.GTL", OUT / "Gerber_TopLayer.GTL", top_clear, top_dark)

    # Solder masks: retire all old RP2040/module pads and the SNES level shifter.
    mask_shapes = []
    paste_shapes = []
    for pad in design["pads"]:
        if pad.mask:
            mask_shapes.append(box(
                pad.x - pad.width / 2 - MASK_EXPAND,
                pad.y - pad.height / 2 - MASK_EXPAND,
                pad.x + pad.width / 2 + MASK_EXPAND,
                pad.y + pad.height / 2 + MASK_EXPAND,
            ))
        if pad.paste and pad.ref not in {"TP"}:
            paste_shapes.append(pad.shape)
    bottom_mask_dark = unary_union(mask_shapes).buffer(0)
    bottom_mask_clear = box(4.025, 5.095, 5.035, 6.025)
    append_overlay(BASE / "Gerber_BottomSolderMaskLayer.GBS", OUT / "Gerber_BottomSolderMaskLayer.GBS", bottom_mask_clear, bottom_mask_dark)

    top_mask_clear = box(2.12, 4.13, 2.58, 4.75)
    append_overlay(BASE / "Gerber_TopSolderMaskLayer.GTS", OUT / "Gerber_TopSolderMaskLayer.GTS", top_mask_clear, Polygon())

    write_positive_layer(OUT / "Gerber_BottomPasteMaskLayer.GBP", "Bottom Paste", unary_union(paste_shapes).buffer(0))
    write_positive_layer(OUT / "Gerber_TopPasteMaskLayer.GTP", "Top Paste (intentionally empty)", Polygon())

    # New top silkscreen: intentionally sparse, high-contrast, and pixel-clean.
    top_silk = []
    top_silk.append(text_shape("SWANSONG USB", 2.67, 5.08, 0.62, "center"))
    top_silk.append(text_shape("USB GAMEPAD - REV B", 2.67, 5.97, 0.22, "center"))
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
        text_shape("REV B", 2.60, 5.78, 0.18, "center"),
        text_shape("FACTORY PROGRAMMED", 3.18, 5.45, 0.12, "center"),
        text_shape("USB HID GAMEPAD", 3.18, 5.30, 0.12, "center"),
        text_shape("USB-C", 4.82, 5.30, 0.12, "center"),
        text_shape("VPP", 3.98, 5.42, 0.08, "right"),
    ]
    write_positive_layer(OUT / "Gerber_BottomSilkscreenLayer.GBO", "Bottom Silkscreen", unary_union(bottom_silk).buffer(0))

    # Add the connector's two non-plated locating pegs and all new 0.30 mm vias.
    modify_drill(BASE / "Drill_NPTH_Through.DRL", OUT / "Drill_NPTH_Through.DRL", design["npth"], "T04")
    # The new NPTH diameter needs its own tool declaration.
    npth_text = (OUT / "Drill_NPTH_Through.DRL").read_text()
    npth_text = npth_text.replace("%\nG05", ";Holesize 4 = 0.0256 inch\nT04C0.0256\n%\nG05")
    (OUT / "Drill_NPTH_Through.DRL").write_text(npth_text)
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
        writer = csv.writer(handle)
        writer.writerow(["Quantity", "Designators", "Manufacturer Part Number", "Description", "Package", "Manufacturer"])
        manufacturer = {
            "PIC16F1459-I/SO": "Microchip Technology",
            "USB4110-GF-A": "GCT",
            "746X101103JP": "CTS Resistor Products",
            "CC0402KRX5R5BB474": "Yageo",
        }
        for (mpn, value, package), refs in quantities.items():
            mfr = manufacturer.get(mpn, "Yageo")
            writer.writerow([len(refs), ", ".join(refs), mpn, value, package, mfr])

    manifest = {
        "product": "SwanSong USB",
        "revision": "B",
        "architecture": "PIC16F1459 crystal-free native USB, USB-C device",
        "placements": len(design["placements"]),
        "assembly_side": "Bottom only",
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
