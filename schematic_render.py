#!/usr/bin/env python3
"""Deterministic schematic geometry engine — renders a declarative spec to SVG/PNG.

The split this implements: the *caller* (an LLM) supplies semantic placement — which
nets are rails, how pins group into branches, the left-to-right order of series
elements. This engine owns all geometry: lane allocation, orthogonal routing, symbol
selection, rail lines and label sides. It is intentionally narrow: the `ladder`
control profile (a central IC block with two power rails and a fan of series
branches), which covers DIN-rail relay/safety panels.

Spec format (JSON)::

    {
      "title": "...",                 # drawn under the diagram
      "profile": "ladder",            # only profile supported today
      "rails": [
        {"id": "P24", "label": "+24 V", "side": "top"},
        {"id": "P0",  "label": "0 V",   "side": "bottom"}
      ],
      "blocks": [
        {"id": "PNOZ", "type": "ic", "label": "PNOZ 8\\nPilz 774760",
         "pins_left":  ["Y37", "Y2", ...],     # top-to-bottom on the left edge
         "pins_right": ["13", "14", ...]}       # top-to-bottom on the right edge
      ],
      "branches": [
        {"from": "PNOZ.Y36", "to": "PNOZ.S12",
         "series": [{"type": "switch_nc", "label": "E-stop NC1"}],
         "comment": "..."}            # optional, ignored by the renderer
      ]
    }

An anchor is either ``"BLOCK.pin"`` or a rail id. A branch with an empty ``series``
is a plain wire. Run::

    python schematic_render.py spec.json out.svg

writes ``out.svg`` and a sibling ``out.png``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import schemdraw
import schemdraw.elements as elm

# ── symbol registry: spec "type" → (schemdraw element factory) ──────────────
# Every entry is a 2-terminal element so it can be placed with .at(a).to(b).
SYMBOLS = {
    "wire":       lambda: elm.Line(),
    "switch_nc":  lambda: elm.Switch(nc=True),
    "switch_no":  lambda: elm.Switch(nc=False),
    "button":     lambda: elm.Button(nc=False),
    "button_nc":  lambda: elm.Button(nc=True),
    "fuse":       lambda: elm.Fuse(),
    "coil":       lambda: elm.Inductor2(),   # relay/contactor coil
    "lamp":       lambda: elm.Lamp(),
    "resistor":   lambda: elm.Resistor(),
}

UNIT = 2.0          # schemdraw drawing unit
LANE_PITCH = 2.0    # horizontal spacing between adjacent branch lanes
RAIL_GAP = 2.0      # vertical gap between outermost pins and the rails
RAIL_MARGIN = 1.5   # how far the rails extend past the outermost lane


class SchematicError(ValueError):
    """Raised on a malformed spec — surfaced to the caller, never silently fudged."""


def _pin_anchor(block, name):
    """Return the (x, y) anchor of a pin, tolerating numeric pin names like '13'."""
    try:
        return getattr(block, name)
    except AttributeError:
        return block[name]


def render(spec: dict, svg_path: Path) -> dict:
    profile = spec.get("profile", "ladder")
    if profile != "ladder":
        raise SchematicError(f"unsupported profile {profile!r} (only 'ladder' today)")

    blocks_spec = spec.get("blocks", [])
    if len(blocks_spec) != 1:
        raise SchematicError("ladder profile requires exactly one block (the IC)")
    bspec = blocks_spec[0]

    d = schemdraw.Drawing()
    d.config(unit=UNIT, fontsize=10)

    # ── place the central IC block ──────────────────────────────────────────
    pins_left = bspec.get("pins_left", [])
    pins_right = bspec.get("pins_right", [])
    npins = max(len(pins_left), len(pins_right))
    ic = elm.Ic(
        pins=[elm.IcPin(name=n, side="left", pin=str(i + 1))
              for i, n in enumerate(pins_left)]
             + [elm.IcPin(name=n, side="right", pin=str(i + 1))
                for i, n in enumerate(pins_right)],
        size=(5, max(8, 1.6 * npins)),
        label=bspec.get("label", bspec["id"]),
    )
    d += ic
    block_id = bspec["id"]
    blocks = {block_id: ic}

    # pin extents — every left pin shares one x, every right pin another.
    left_x = _pin_anchor(ic, pins_left[0])[0] if pins_left else 0.0
    right_x = _pin_anchor(ic, pins_right[0])[0] if pins_right else 0.0
    all_pin_y = [_pin_anchor(ic, n)[1] for n in pins_left + pins_right]
    top_y = max(all_pin_y) + RAIL_GAP
    bot_y = min(all_pin_y) - RAIL_GAP

    rails = {r["id"]: r for r in spec.get("rails", [])}
    rail_y = {}
    for rid, r in rails.items():
        rail_y[rid] = top_y if r.get("side") == "top" else bot_y

    def anchor_side(ref: str) -> str:
        """Which side ('left'/'right') a branch endpoint sits on."""
        if "." in ref:
            blk, pin = ref.split(".", 1)
            if pin in pins_left:
                return "left"
            if pin in pins_right:
                return "right"
            raise SchematicError(f"unknown pin {ref!r}")
        if ref in rails:
            return "rail"
        raise SchematicError(f"unknown anchor {ref!r}")

    def resolve(ref: str):
        if "." in ref:
            blk, pin = ref.split(".", 1)
            if blk not in blocks:
                raise SchematicError(f"unknown block {blk!r} in {ref!r}")
            return _pin_anchor(blocks[blk], pin)
        return None  # rail — resolved against a lane x later

    # ── allocate one lane per branch, per side ────────────────────────────────
    left_lane = 0
    right_lane = 0
    branches = spec.get("branches", [])

    def lane_x_for(side: str) -> float:
        nonlocal left_lane, right_lane
        if side == "left":
            left_lane += 1
            return left_x - LANE_PITCH * left_lane
        right_lane += 1
        return right_x + LANE_PITCH * right_lane

    lane_xs = []  # track for rail extents
    for br in branches:
        a, b = br["from"], br["to"]
        sa, sb = anchor_side(a), anchor_side(b)
        # the working side is whichever endpoint is a pin (rails span all x).
        side = sa if sa != "rail" else sb
        if side == "rail":
            raise SchematicError(f"branch {a!r}->{b!r} has no pin endpoint")
        lane = lane_x_for(side)
        lane_xs.append(lane)
        _draw_branch(d, a, b, sa, sb, side, lane, br.get("series", []),
                     resolve, rail_y)

    # ── draw the rails last, spanning all lanes ───────────────────────────────
    span_left = min([left_x] + [x for x in lane_xs]) - RAIL_MARGIN
    span_right = max([right_x] + [x for x in lane_xs]) + RAIL_MARGIN
    for rid, r in rails.items():
        y = rail_y[rid]
        d += elm.Line().at((span_left, y)).to((span_right, y))
        d += elm.Label().label(r.get("label", rid), loc="left").at((span_left, y))

    if spec.get("title"):
        d += elm.Label().label(spec["title"]).at(((span_left + span_right) / 2,
                                                   bot_y - 1.5))

    d.draw(show=False)
    svg_path = Path(svg_path)
    d.save(str(svg_path))
    # PNG is best-effort: schemdraw's SVG backend can't rasterize, so convert the
    # SVG if a converter is available. SVG remains the primary, view_file-able output.
    png_path = _svg_to_png(svg_path)
    return {"svg": str(svg_path), "png": str(png_path) if png_path else "",
            "branches": len(branches), "rails": len(rails)}


def _svg_to_png(svg_path: Path):
    """Convert SVG→PNG via cairosvg or rsvg-convert if present; else return None."""
    png_path = svg_path.with_suffix(".png")
    try:
        import cairosvg  # type: ignore
        cairosvg.svg2png(url=str(svg_path), write_to=str(png_path), output_width=1400)
        return png_path
    except Exception:
        pass
    import shutil
    import subprocess
    if shutil.which("rsvg-convert"):
        try:
            subprocess.run(["rsvg-convert", "-w", "1400", "-o", str(png_path),
                            str(svg_path)], check=True, capture_output=True)
            return png_path
        except Exception:
            pass
    return None


def _draw_branch(d, a, b, sa, sb, side, lane_x, series, resolve, rail_y):
    """Route one branch: escape to its lane, run vertically through its series
    elements, meet the far endpoint. The vertical lane run carries the symbols."""
    pa, pb = resolve(a), resolve(b)
    label_loc = "left" if side == "left" else "right"

    # y at each end of the vertical lane run.
    if sa == "rail":
        ya = rail_y[a]
        pin_pt, pin_y = pb, pb[1]
        rail_end, pin_end = "from", "to"
    elif sb == "rail":
        yb_rail = rail_y[b]
        pin_pt, pin_y = pa, pa[1]
        rail_end, pin_end = "to", "from"
    else:
        rail_end = None

    if rail_end is None:
        # pin ↔ pin (same side): escape both pins horizontally to the lane,
        # vertical run between their y's carries the series elements.
        y0, y1 = pa[1], pb[1]
        d += elm.Line().at(pa).to((lane_x, y0))
        _series_on_segment(d, (lane_x, y0), (lane_x, y1), series, label_loc)
        d += elm.Line().at((lane_x, y1)).to(pb)
    else:
        # pin ↔ rail: escape the pin horizontally to the lane, vertical run to
        # the rail carries the series elements, meet the rail at lane_x.
        ry = rail_y[a] if rail_end == "from" else rail_y[b]
        d += elm.Line().at(pin_pt).to((lane_x, pin_y))
        _series_on_segment(d, (lane_x, pin_y), (lane_x, ry), series, label_loc)


def _series_on_segment(d, p0, p1, series, label_loc):
    """Lay `series` elements evenly along the straight segment p0→p1, filling the
    rest with wire. With no series elements the whole segment is a single wire."""
    if not series:
        d += elm.Line().at(p0).to(p1)
        return
    (x0, y0), (x1, y1) = p0, p1
    n = len(series)
    # element occupies the middle ~70% of its slot, wire pads either side.
    for i, el in enumerate(series):
        t0 = i / n
        t1 = (i + 1) / n
        pad = (t1 - t0) * 0.15
        ea = (x0 + (x1 - x0) * (t0 + pad), y0 + (y1 - y0) * (t0 + pad))
        eb = (x0 + (x1 - x0) * (t1 - pad), y0 + (y1 - y0) * (t1 - pad))
        sa = (x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0)
        sb = (x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1)
        d += elm.Line().at(sa).to(ea)              # lead-in wire
        factory = SYMBOLS.get(el["type"])
        if factory is None:
            raise SchematicError(f"unknown symbol type {el['type']!r}")
        e = factory().at(ea).to(eb)
        if el.get("label"):
            e = e.label(el["label"], loc=label_loc)
        d += e
        d += elm.Line().at(eb).to(sb)              # lead-out wire


def main(argv):
    if len(argv) != 3:
        print("usage: schematic_render.py spec.json out.svg", file=sys.stderr)
        return 2
    spec = json.loads(Path(argv[1]).read_text())
    try:
        result = render(spec, Path(argv[2]))
    except SchematicError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    print(json.dumps({"ok": True, **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
