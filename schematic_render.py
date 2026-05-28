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

COL_PITCH = 9.0     # flow: horizontal spacing between grid columns
ROW_PITCH = 11.0    # flow: vertical spacing between grid rows


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
    else:
        raise SchematicError(f"unsupported profile {profile!r} (ladder | flow)")

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
        d += elm.Line().at(pa).to((lane_x, y0))
        _series_on_segment(d, (lane_x, y0), (lane_x, y1), series, label_loc)
        d += elm.Line().at((lane_x, y1)).to(pb)
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
        cx = (min(p[0] for p in pts) + max(p[0] for p in pts)) / 2
        top = max(p[1] for p in pts)
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
