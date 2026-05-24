"""Place footprints on a grid + add a board outline + export Specctra DSN, via pcbnew.

Usage: python3 place_and_dsn.py <project_dir> [target] [pitch_mm]
atopile leaves footprints at the origin; this lays them on a square-ish grid,
draws an Edge.Cuts rectangle sized to enclose them with a margin, saves, and
exports a .dsn for freerouting.

NOTE: run with the SYSTEM python that can `import pcbnew` (KiCad's bundled module).
A naive grid placer — fine for small boards (a dozen-ish parts); for anything
larger, swap in a real autoplacer.
"""
import math
import sys

import pcbnew

proj = sys.argv[1]
target = sys.argv[2] if len(sys.argv) > 2 else "default"
pitch = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0  # mm between grid cells
pcb_path = f"{proj}/layouts/{target}/{target}.kicad_pcb"
board = pcbnew.LoadBoard(pcb_path)


def mm(x):
    return pcbnew.FromMM(x)


fps = list(board.GetFootprints())
n = len(fps)
cols = max(1, math.ceil(math.sqrt(n)))
x0, y0 = 100.0, 100.0

# Place on a grid (sorted by reference for stable, readable layout).
for i, fp in enumerate(sorted(fps, key=lambda f: f.GetReference())):
    row, col = divmod(i, cols)
    fp.SetPosition(pcbnew.VECTOR2I(mm(x0 + col * pitch), mm(y0 + row * pitch)))

# Edge.Cuts rectangle enclosing the grid with a margin.
rows = math.ceil(n / cols)
margin = pitch * 0.7
bx0, by0 = x0 - margin, y0 - margin
bx1, by1 = x0 + (cols - 1) * pitch + margin, y0 + (rows - 1) * pitch + margin
edges = [(bx0, by0, bx1, by0), (bx1, by0, bx1, by1),
         (bx1, by1, bx0, by1), (bx0, by1, bx0, by0)]
for ax, ay, bx, by in edges:
    seg = pcbnew.PCB_SHAPE(board)
    seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
    seg.SetStart(pcbnew.VECTOR2I(mm(ax), mm(ay)))
    seg.SetEnd(pcbnew.VECTOR2I(mm(bx), mm(by)))
    seg.SetLayer(pcbnew.Edge_Cuts)
    seg.SetWidth(mm(0.15))
    board.Add(seg)

pcbnew.SaveBoard(pcb_path, board)
print("PLACED", n, "parts in", cols, "cols:", [f.GetReference() for f in fps])

dsn = f"{proj}/build/{target}.dsn"
try:
    ok = pcbnew.ExportSpecctraDSN(board, dsn)
except TypeError:
    ok = pcbnew.ExportSpecctraDSN(dsn)
print("DSN", ok, dsn)
