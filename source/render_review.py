#!/usr/bin/env python3
"""Render deterministic top/bottom review images from the generated Gerbers."""

from __future__ import annotations

import builtins
from pathlib import Path

import cairocffi as cairo


# pcb-tools still opens Gerbers with the removed Python universal-newline flag.
_open = builtins.open


def _compat_open(file, mode="r", *args, **kwargs):
    return _open(file, mode.replace("U", ""), *args, **kwargs)


builtins.open = _compat_open

from gerber.layers import load_layer  # noqa: E402
from gerber.render.cairo_backend import GerberCairoContext  # noqa: E402
from gerber.render.theme import THEMES  # noqa: E402


PROJECT = Path(__file__).resolve().parent.parent
GERBERS = PROJECT / "gerbers"
OUTPUT = PROJECT / "review-renders"

ACCESSORY_RECESS_INNER_X = 4.720
ACCESSORY_RECESS_Y_MIN = 4.455
ACCESSORY_RECESS_Y_MAX = 5.230
USB_CX = 4.58394
USB_CY = (ACCESSORY_RECESS_Y_MIN + ACCESSORY_RECESS_Y_MAX) / 2
USB_BODY_HALF_LENGTH = 3.675 / 25.4
USB_BODY_HALF_WIDTH = 4.47 / 25.4


def render(name: str, filenames: list[str]) -> None:
    layers = [load_layer(str(GERBERS / filename)) for filename in filenames]
    output = OUTPUT / name
    GerberCairoContext().render_layers(
        layers,
        str(output),
        theme=THEMES["OSH Park"],
        max_width=2400,
        max_height=1400,
    )
    print(output)


def render_alignment() -> None:
    """Draw the connector against the exact rectangular board recess."""
    width, height = 1200, 820
    margin = 80
    x_min, x_max = 4.35, 5.08
    y_min, y_max = 4.35, 5.33

    def px(x: float) -> float:
        return margin + (x - x_min) / (x_max - x_min) * (width - 2 * margin)

    def py(y: float) -> float:
        return height - margin - (y - y_min) / (y_max - y_min) * (height - 2 * margin)

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    ctx = cairo.Context(surface)
    ctx.set_source_rgb(0.97, 0.98, 0.99)
    ctx.paint()

    # Board area, including the two tabs above and below the cut-in recess.
    board = [
        (x_min, y_min), (5.045, y_min), (5.045, ACCESSORY_RECESS_Y_MIN),
        (ACCESSORY_RECESS_INNER_X, ACCESSORY_RECESS_Y_MIN),
        (ACCESSORY_RECESS_INNER_X, ACCESSORY_RECESS_Y_MAX),
        (5.045, ACCESSORY_RECESS_Y_MAX), (5.045, y_max), (x_min, y_max),
    ]
    ctx.move_to(px(board[0][0]), py(board[0][1]))
    for x, y in board[1:]:
        ctx.line_to(px(x), py(y))
    ctx.close_path()
    ctx.set_source_rgb(0.22, 0.08, 0.31)
    ctx.fill_preserve()
    ctx.set_source_rgb(0.10, 0.03, 0.16)
    ctx.set_line_width(4)
    ctx.stroke()

    body_x0 = USB_CX - USB_BODY_HALF_LENGTH
    body_x1 = USB_CX + USB_BODY_HALF_LENGTH
    body_y0 = USB_CY - USB_BODY_HALF_WIDTH
    body_y1 = USB_CY + USB_BODY_HALF_WIDTH
    ctx.rectangle(px(body_x0), py(body_y1), px(body_x1) - px(body_x0), py(body_y0) - py(body_y1))
    ctx.set_source_rgba(0.68, 0.74, 0.79, 0.92)
    ctx.fill_preserve()
    ctx.set_source_rgb(0.10, 0.18, 0.24)
    ctx.set_line_width(4)
    ctx.stroke()

    # Mating opening and a cable centerline continuing through the recess.
    ctx.move_to(px(body_x1), py(body_y0))
    ctx.line_to(px(body_x1), py(body_y1))
    ctx.set_source_rgb(0.00, 0.55, 0.72)
    ctx.set_line_width(8)
    ctx.stroke()
    ctx.move_to(px(body_x1), py(USB_CY))
    ctx.line_to(px(5.04), py(USB_CY))
    ctx.set_line_width(5)
    ctx.stroke()

    ctx.select_font_face("Arial", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_source_rgb(0.08, 0.12, 0.16)
    ctx.set_font_size(34)
    ctx.move_to(80, 48)
    ctx.show_text("SwanSong USB — right-side accessory recess")
    ctx.set_font_size(25)
    ctx.move_to(px(4.405), py(5.28))
    ctx.show_text("PCB")
    ctx.move_to(px(4.47), py(USB_CY) - 10)
    ctx.show_text("USB-C")
    ctx.set_font_size(22)
    ctx.move_to(px(4.735), py(USB_CY) - 18)
    ctx.show_text("cable path")

    projection_mm = (body_x1 - ACCESSORY_RECESS_INNER_X) * 25.4
    clearance_mm = (body_y0 - ACCESSORY_RECESS_Y_MIN) * 25.4
    ctx.select_font_face("Arial", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    ctx.set_font_size(23)
    ctx.move_to(80, height - 34)
    ctx.show_text(
        f"Mating face projects {projection_mm:.2f} mm into opening; centered clearance is {clearance_mm:.2f} mm above and below."
    )

    output = OUTPUT / "usb-accessory-recess-alignment.png"
    surface.write_to_png(str(output))
    print(output)


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    common = ["Drill_PTH_Through.DRL", "Drill_NPTH_Through.DRL", "Gerber_BoardOutlineLayer.GKO"]
    render(
        "top-rev-c.png",
        ["Gerber_TopLayer.GTL", "Gerber_TopSolderMaskLayer.GTS", "Gerber_TopSilkscreenLayer.GTO", *common],
    )
    render(
        "bottom-rev-c.png",
        ["Gerber_BottomLayer.GBL", "Gerber_BottomSolderMaskLayer.GBS", "Gerber_BottomSilkscreenLayer.GBO", *common],
    )
    render_alignment()


if __name__ == "__main__":
    main()
