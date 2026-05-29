#!/usr/bin/env python3
"""Deterministic schematic geometry engine — renders a declarative spec to SVG/PNG.

The split this implements: the *caller* (an LLM) supplies semantic placement — which
nets are rails, how pins group into branches / which grid cell a block sits in, the
order of series elements. This engine owns all geometry: lane allocation, orthogonal
routing, symbol selection, rail lines and label sides.

Two profiles:

* ``ladder`` — a central IC block with two power rails and a fan of series branches
  (DIN-rail relay / safety control sheets). Spec::

    {
      "title": "...", "profile": "ladder",
      "rails":  [{"id": "P24", "label": "+24 V", "side": "top"},
                 {"id": "P0",  "label": "0 V",   "side": "bottom"}],
      "blocks": [{"id": "PNOZ", "type": "ic", "label": "PNOZ 8",
                  "pins_left": [...], "pins_right": [...]}],   # exactly one block
      "branches": [{"from": "PNOZ.Y36", "to": "PNOZ.S12",
                    "series": [{"type": "switch_nc", "label": "E-stop NC1"}]}]
    }

* ``flow`` — several blocks placed on a grid, wired pin-to-pin (power / signal-flow
  sheets: mains → contactors → VFD → load). Spec::

    {
      "title": "...", "profile": "flow",
      "blocks": [{"id": "K1", "label": "K1\\nLC1D18", "col": 1, "row": 0,
                  "pins_left": ["L1", "L2"], "pins_right": ["T1", "T2"]}, ...],
      "connections": [{"from": "MAINS.L", "to": "K1.L1", "label": "L"}, ...]
    }

  Blocks sit at (col, row) on a grid (the caller's placement); each connection is
  routed as an orthogonal H–V–H path through a per-connection lane so parallel buses
  don't merge. Place series-flow blocks left→right and off-chain blocks (supplies,
  loads, earth) on a lower row for a clean read.

An anchor is ``"BLOCK.pin"`` (or, in ladder, a rail id). Run::

    python schematic_render.py spec.json out.svg

writes ``out.svg`` and a sibling ``out.png`` (PNG best-effort).
"""
from __future__ import annotations

import json
import math
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
LANE_PITCH = 2.0    # ladder: horizontal spacing between adjacent branch lanes
RAIL_GAP = 2.0      # ladder: vertical gap between outermost pins and the rails
RAIL_MARGIN = 1.5   # ladder: how far the rails extend past the outermost lane
MIN_ELEM_PITCH = 2.0  # ladder: min vertical run per series element, so a multi-
                      # element branch between adjacent pins (e.g. a start +
                      # EDM feedback loop on Y1-Y2) doesn't crowd its symbols.

COL_PITCH = 9.0     # flow: horizontal spacing between grid columns
ROW_PITCH = 11.0    # flow: vertical spacing between grid rows

MODULE_W = 1.0      # panel: drawing units per 18 mm DIN module
DEV_H = 4.0         # panel: height of a rail-mounted device
DEV_GAP = 0.3       # panel: gap between adjacent devices on a rail
RAIL_PITCH = 9.0    # panel: vertical spacing between DIN rails
PANEL_MARGIN = 2.0  # panel: enclosure margin around everything


class SchematicError(ValueError):
    """Raised on a malformed spec — surfaced to the caller, never silently fudged."""


def _pin_anchor(block, name):
    """Return the (x, y) anchor of a pin, tolerating numeric pin names like '13'."""
    try:
        return getattr(block, name)
    except AttributeError:
        return block[name]


def _ic(bspec, size_w=3.0, label_inside=True):
    """Build an elm.Ic from a block spec's pins_left / pins_right. With
    label_inside=False the block is drawn bare (caller adds a designator above it,
    so a long part-number label can't collide with the pin names in a narrow box)."""
    pins_left = bspec.get("pins_left", [])
    pins_right = bspec.get("pins_right", [])
    npins = max(len(pins_left), len(pins_right), 1)
    kwargs = {"label": bspec.get("label", bspec["id"])} if label_inside else {}
    return elm.Ic(
        pins=[elm.IcPin(name=n, side="left", pin=str(i + 1))
              for i, n in enumerate(pins_left)]
             + [elm.IcPin(name=n, side="right", pin=str(i + 1))
                for i, n in enumerate(pins_right)],
        size=(size_w, max(4.0, 1.6 * npins)),
        **kwargs,
    )


def render(spec: dict, svg_path: Path) -> dict:
    profile = spec.get("profile", "ladder")
    d = schemdraw.Drawing()
    d.config(unit=UNIT, fontsize=10)

    if profile == "ladder":
        meta = _render_ladder(d, spec)
    elif profile == "flow":
        meta = _render_flow(d, spec)
    elif profile == "panel":
        meta = _render_panel(d, spec)
    else:
        raise SchematicError(f"unsupported profile {profile!r} (ladder | flow | panel)")

    if spec.get("title"):
        cx = (meta["xmin"] + meta["xmax"]) / 2
        d += elm.Label().label(spec["title"]).at((cx, meta["ymin"] - 1.5))

    d.draw(show=False)
    svg_path = Path(svg_path)
    d.save(str(svg_path))
    png_path = _svg_to_png(svg_path)
    return {"svg": str(svg_path), "png": str(png_path) if png_path else "", **meta["counts"]}


# ── ladder profile ───────────────────────────────────────────────────────────
def _render_ladder(d, spec) -> dict:
    blocks_spec = spec.get("blocks", [])
    if len(blocks_spec) != 1:
        raise SchematicError("ladder profile requires exactly one block (the IC)")
    bspec = blocks_spec[0]

    pins_left = bspec.get("pins_left", [])
    pins_right = bspec.get("pins_right", [])
    ic = _ic(bspec, size_w=5.0)
    d += ic
    blocks = {bspec["id"]: ic}

    left_x = _pin_anchor(ic, pins_left[0])[0] if pins_left else 0.0
    right_x = _pin_anchor(ic, pins_right[0])[0] if pins_right else 0.0
    all_pin_y = [_pin_anchor(ic, n)[1] for n in pins_left + pins_right]
    top_y = max(all_pin_y) + RAIL_GAP
    bot_y = min(all_pin_y) - RAIL_GAP

    rails = {r["id"]: r for r in spec.get("rails", [])}
    rail_y = {rid: (top_y if r.get("side") == "top" else bot_y)
              for rid, r in rails.items()}

    def anchor_side(ref):
        if "." in ref:
            _, pin = ref.split(".", 1)
            if pin in pins_left:
                return "left"
            if pin in pins_right:
                return "right"
            raise SchematicError(f"unknown pin {ref!r}")
        if ref in rails:
            return "rail"
        raise SchematicError(f"unknown anchor {ref!r}")

    def resolve(ref):
        if "." in ref:
            blk, pin = ref.split(".", 1)
            if blk not in blocks:
                raise SchematicError(f"unknown block {blk!r} in {ref!r}")
            return _pin_anchor(blocks[blk], pin)
        return None

    left_lane = right_lane = 0

    def lane_x_for(side):
        nonlocal left_lane, right_lane
        if side == "left":
            left_lane += 1
            return left_x - LANE_PITCH * left_lane
        right_lane += 1
        return right_x + LANE_PITCH * right_lane

    lane_xs = []
    branches = spec.get("branches", [])
    for br in branches:
        a, b = br["from"], br["to"]
        sa, sb = anchor_side(a), anchor_side(b)
        side = sa if sa != "rail" else sb
        if side == "rail":
            raise SchematicError(f"branch {a!r}->{b!r} has no pin endpoint")
        lane = lane_x_for(side)
        lane_xs.append(lane)
        _draw_branch(d, a, b, sa, sb, side, lane, br.get("series", []), resolve, rail_y)

    span_left = min([left_x] + lane_xs) - RAIL_MARGIN
    span_right = max([right_x] + lane_xs) + RAIL_MARGIN
    for rid, r in rails.items():
        y = rail_y[rid]
        d += elm.Line().at((span_left, y)).to((span_right, y))
        d += elm.Label().label(r.get("label", rid), loc="left").at((span_left, y))

    return {"xmin": span_left, "xmax": span_right, "ymin": bot_y,
            "counts": {"branches": len(branches), "rails": len(rails)}}


def _draw_branch(d, a, b, sa, sb, side, lane_x, series, resolve, rail_y):
    """Route one ladder branch: escape to its lane, run vertically through its series
    elements, meet the far endpoint."""
    pa, pb = resolve(a), resolve(b)
    label_loc = "left" if side == "left" else "right"

    if sa == "rail":
        pin_pt, pin_y, ry = pb, pb[1], rail_y[a]
        rail_end = True
    elif sb == "rail":
        pin_pt, pin_y, ry = pa, pa[1], rail_y[b]
        rail_end = True
    else:
        rail_end = False

    if not rail_end:
        y0, y1 = pa[1], pb[1]
        # Extend the vertical run so N series elements each get >= MIN_ELEM_PITCH.
        # When the two pins are far apart the natural span already wins (half is
        # half the gap); when they're adjacent the run grows symmetrically about
        # their midpoint, and the two horizontal stubs T into it at y0 / y1.
        ymid = (y0 + y1) / 2
        half = max(abs(y1 - y0) / 2, len(series) * MIN_ELEM_PITCH / 2)
        ytop, ybot = ymid + half, ymid - half
        d += elm.Line().at(pa).to((lane_x, y0))
        d += elm.Line().at(pb).to((lane_x, y1))
        _series_on_segment(d, (lane_x, ytop), (lane_x, ybot), series, label_loc)
    else:
        d += elm.Line().at(pin_pt).to((lane_x, pin_y))
        _series_on_segment(d, (lane_x, pin_y), (lane_x, ry), series, label_loc)


# ── flow profile ──────────────────────────────────────────────────────────────
def _render_flow(d, spec) -> dict:
    blocks_spec = spec.get("blocks", [])
    if not blocks_spec:
        raise SchematicError("flow profile requires at least one block")

    blocks = {}
    sides = {}  # block id -> {pin: 'left'|'right'}
    bbox = {}   # block id -> (xmin, xmax, ymin, ymax) over its pins
    for b in blocks_spec:
        ic = _ic(b, size_w=3.5, label_inside=False)
        x = b.get("col", 0) * COL_PITCH
        y = -b.get("row", 0) * ROW_PITCH
        d += ic.at((x, y))
        blocks[b["id"]] = ic
        sides[b["id"]] = ({p: "left" for p in b.get("pins_left", [])}
                          | {p: "right" for p in b.get("pins_right", [])})
        # designator above the block — keeps long labels clear of pin names.
        pts = [_pin_anchor(ic, p) for p in
               b.get("pins_left", []) + b.get("pins_right", [])]
        bx = [p[0] for p in pts]
        by = [p[1] for p in pts]
        bbox[b["id"]] = (min(bx), max(bx), min(by), max(by))
        cx = (min(bx) + max(bx)) / 2
        top = max(by)
        d += elm.Label().label(b.get("label", b["id"]), fontsize=9).at((cx, top + 1.2))

    def resolve(ref):
        if "." not in ref:
            raise SchematicError(f"flow anchor must be BLOCK.pin, got {ref!r}")
        blk, pin = ref.split(".", 1)
        if blk not in blocks:
            raise SchematicError(f"unknown block {blk!r} in {ref!r}")
        if pin not in sides[blk]:
            raise SchematicError(f"unknown pin {ref!r}")
        return _pin_anchor(blocks[blk], pin), sides[blk][pin]

    conns = spec.get("connections", [])
    for i, cn in enumerate(conns):
        (pa, side_a), (pb, _side_b) = resolve(cn["from"]), resolve(cn["to"])
        _route_flow(d, pa, side_a, pb, i, cn.get("label"))

    # mechanical links: dashed connector between two whole blocks (e.g. a
    # contactor coil and its power poles), drawn between their facing edges.
    for ml in spec.get("mech_links", []):
        a, b = ml["from"], ml["to"]
        if a not in bbox or b not in bbox:
            raise SchematicError(f"mech_link references unknown block {a!r}/{b!r}")
        axm = (bbox[a][0] + bbox[a][1]) / 2
        bxm = (bbox[b][0] + bbox[b][1]) / 2
        aym = (bbox[a][2] + bbox[a][3]) / 2
        bym = (bbox[b][2] + bbox[b][3]) / 2
        if bbox[a][2] > bbox[b][3]:        # a sits above b
            p0, p1 = (axm, bbox[a][2]), (bxm, bbox[b][3])
        elif bbox[b][2] > bbox[a][3]:      # b sits above a
            p0, p1 = (axm, bbox[a][3]), (bxm, bbox[b][2])
        elif bbox[a][0] > bbox[b][1]:      # a is to the right of b
            p0, p1 = (bbox[a][0], aym), (bbox[b][1], bym)
        else:                              # a is to the left of b
            p0, p1 = (bbox[a][1], aym), (bbox[b][0], bym)
        _dashed_line(d, p0, p1)
        if ml.get("label"):
            d += elm.Label().label(ml["label"], fontsize=7) \
                            .at(((p0[0] + p1[0]) / 2 + 0.8, (p0[1] + p1[1]) / 2))

    # extents from every pin of every block
    xs, ys = [], []
    for b in blocks_spec:
        ic = blocks[b["id"]]
        for p in b.get("pins_left", []) + b.get("pins_right", []):
            ax, ay = _pin_anchor(ic, p)
            xs.append(ax)
            ys.append(ay)
    return {"xmin": min(xs), "xmax": max(xs), "ymin": min(ys) - 2.0,
            "counts": {"blocks": len(blocks_spec), "connections": len(conns)}}


def _route_flow(d, pa, side_a, pb, idx, label):
    """Route one flow connection as an orthogonal H–V–H path. Aligned pins (same y)
    get a straight horizontal bus; otherwise a vertical lane in the gutter, nudged
    per-connection so parallel buses stay separate."""
    (x0, y0), (x1, y1) = pa, pb
    if abs(y0 - y1) < 1e-6:
        d += elm.Line().at(pa).to(pb)
        # only a straight horizontal bus carries a label — it sits cleanly on the
        # wire; a label on a bent route floats in empty space, so we skip those.
        if label:
            d += elm.Label().label(label, fontsize=9, loc="top").at(((x0 + x1) / 2, y0))
    else:
        # vertical lane sits in the gutter, biased toward the source's exit side.
        bias = 1.0 if side_a == "right" else -1.0
        base = x0 + bias * abs(x1 - x0) * 0.4
        lane = base + (idx % 5 - 2) * 0.5
        d += elm.Line().at(pa).to((lane, y0))
        d += elm.Line().at((lane, y0)).to((lane, y1))
        d += elm.Line().at((lane, y1)).to(pb)


def _dashed_line(d, p0, p1, dash=0.4, gap=0.28):
    """Dashed straight line — used for a contactor's mechanical (coil↔poles) link."""
    (x0, y0), (x1, y1) = p0, p1
    length = math.hypot(x1 - x0, y1 - y0)
    if length < 1e-9:
        return
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    t = 0.0
    while t < length:
        e = min(t + dash, length)
        d += elm.Line().at((x0 + ux * t, y0 + uy * t)).to((x0 + ux * e, y0 + uy * e))
        t += dash + gap


# ── panel profile ──────────────────────────────────────────────────────────────
def _rect(d, x0, y0, x1, y1):
    """Draw an axis-aligned rectangle as four lines (no fill, version-proof)."""
    d += elm.Line().at((x0, y0)).to((x1, y0))
    d += elm.Line().at((x1, y0)).to((x1, y1))
    d += elm.Line().at((x1, y1)).to((x0, y1))
    d += elm.Line().at((x0, y1)).to((x0, y0))


def _hatch_rect(d, x0, y0, x1, y1, n=7):
    """Rectangle filled with diagonal hatch lines — marks a wire duct / trunking so
    it reads differently from a (plain-outline) device box."""
    _rect(d, x0, y0, x1, y1)
    lo, hi = min(y0, y1), max(y0, y1)
    xl, xr = min(x0, x1), max(x0, x1)
    h = hi - lo
    step = (xr - xl) / (n + 1)
    for i in range(1, n + 1):
        sx = xl + i * step
        ex = min(sx + h, xr)          # clip the diagonal to the box
        d += elm.Line().at((sx, lo)).to((ex, lo + (ex - sx)))


def _render_panel(d, spec) -> dict:
    """Physical back-panel layout: DIN-rail-mounted devices placed left-to-right on
    horizontal rails (sized by DIN-module width), plus free back-panel components
    (VFD, brake resistor, earth bar) placed by absolute (x, y). The caller owns
    which rail/where and the module widths; the engine owns the geometry — rail
    lines, device boxes, labels, enclosure outline.

    Spec::

        {"title": "...", "profile": "panel",
         "enclosure": {"label": "Enclosure 600x400"},
         "rails": [{"id": "RAIL1", "label": "power",
                    "devices": [{"id": "QF1", "label": "QF1\\nMCB", "width": 2}, ...]}],
         "panel_devices": [{"id": "U1", "label": "VFD", "x": 14, "y": 3,
                            "w": 7, "h": 11}]}

    ``width`` is in 18 mm DIN modules; ``x,y`` is a panel device's top-left corner
    and ``w,h`` its size, both in the same drawing units as the rails (rail i sits
    at y = -i*RAIL_PITCH, devices start at x = 0)."""
    rails = spec.get("rails", [])
    panel_devices = spec.get("panel_devices", [])
    xs, ys = [], []
    n_dev = 0

    for i, rail in enumerate(rails):
        yc = -i * RAIL_PITCH
        x = 0.0
        for dv in rail.get("devices", []):
            w = float(dv.get("width", 1)) * MODULE_W
            x0, x1 = x, x + w
            y0, y1 = yc + DEV_H / 2, yc - DEV_H / 2
            _rect(d, x0, y1, x1, y0)
            d += elm.Label().label(dv.get("label", dv["id"]), fontsize=8).at(((x0 + x1) / 2, yc))
            xs += [x0, x1]
            ys += [y0, y1]
            x = x1 + DEV_GAP
            n_dev += 1
        rail_end = max(x - DEV_GAP, 0.0)
        # the DIN rail itself, drawn just behind the foot of the device boxes
        rail_y = yc - DEV_H / 2 - 0.4
        d += elm.Line().at((-0.5, rail_y)).to((rail_end + 0.5, rail_y))
        d += elm.Label().label(rail.get("label", rail["id"]), fontsize=9, loc="left") \
                        .at((0.0, yc + DEV_H / 2 + 0.9))
        ys += [rail_y]

    for pd in panel_devices:
        x0 = float(pd["x"])
        y0 = float(pd["y"])
        x1 = x0 + float(pd["w"])
        y1 = y0 - float(pd["h"])
        _rect(d, x0, y1, x1, y0)
        d += elm.Label().label(pd.get("label", pd["id"]), fontsize=8) \
                        .at(((x0 + x1) / 2, (y0 + y1) / 2))
        xs += [x0, x1]
        ys += [y0, y1]
        n_dev += 1

    for du in spec.get("ducts", []):
        x0 = float(du["x"])
        y0 = float(du["y"])
        x1 = x0 + float(du["w"])
        y1 = y0 - float(du["h"])
        _hatch_rect(d, x0, y1, x1, y0)
        if du.get("label"):
            d += elm.Label().label(du["label"], fontsize=7).at(((x0 + x1) / 2, (y0 + y1) / 2))
        xs += [x0, x1]
        ys += [y0, y1]

    if not xs:
        raise SchematicError("panel profile requires at least one rail or panel device")

    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    enc = spec.get("enclosure")
    if enc:
        ex0, ey1 = xmin - PANEL_MARGIN * 3.0, ymin - PANEL_MARGIN
        ex1, ey0 = xmax + PANEL_MARGIN, ymax + PANEL_MARGIN
        _rect(d, ex0, ey1, ex1, ey0)
        if enc.get("label"):
            d += elm.Label().label(enc["label"], fontsize=10).at(((ex0 + ex1) / 2, ey0 + 0.9))
        xmin, xmax, ymin = ex0, ex1, ey1

    return {"xmin": xmin, "xmax": xmax, "ymin": ymin,
            "counts": {"rails": len(rails), "devices": n_dev}}


def _series_on_segment(d, p0, p1, series, label_loc):
    """Lay `series` elements evenly along the straight segment p0→p1, filling the
    rest with wire. With no series elements the whole segment is a single wire."""
    if not series:
        d += elm.Line().at(p0).to(p1)
        return
    (x0, y0), (x1, y1) = p0, p1
    n = len(series)
    for i, el in enumerate(series):
        t0, t1 = i / n, (i + 1) / n
        pad = (t1 - t0) * 0.15
        ea = (x0 + (x1 - x0) * (t0 + pad), y0 + (y1 - y0) * (t0 + pad))
        eb = (x0 + (x1 - x0) * (t1 - pad), y0 + (y1 - y0) * (t1 - pad))
        sa = (x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0)
        sb = (x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1)
        d += elm.Line().at(sa).to(ea)
        factory = SYMBOLS.get(el["type"])
        if factory is None:
            raise SchematicError(f"unknown symbol type {el['type']!r}")
        e = factory().at(ea).to(eb)
        if el.get("label"):
            e = e.label(el["label"], loc=label_loc)
        d += e
        d += elm.Line().at(eb).to(sb)


def _svg_to_png(svg_path: Path):
    """Convert SVG→PNG via cairosvg or rsvg-convert if present; else return None.

    Renders on a solid white background: the schemdraw SVG has a transparent
    background, so a PNG without one shows as a transparency checkerboard in
    image viewers (e.g. swayimg in Haven's present_app), washing out the thin
    schematic lines. (view_file of the SVG is unaffected.)"""
    png_path = svg_path.with_suffix(".png")
    try:
        import cairosvg  # type: ignore
        cairosvg.svg2png(url=str(svg_path), write_to=str(png_path),
                         output_width=1600, background_color="white")
        return png_path
    except Exception:
        pass
    import shutil
    import subprocess
    if shutil.which("rsvg-convert"):
        try:
            subprocess.run(["rsvg-convert", "-w", "1600", "--background-color", "white",
                            "-o", str(png_path), str(svg_path)],
                           check=True, capture_output=True)
            return png_path
        except Exception:
            pass
    return None


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
