"""Place footprints + add a board outline + export Specctra DSN, via pcbnew.

Usage: python3 place_and_dsn.py <project_dir> [target]
atopile leaves footprints at the origin; this spreads them, draws an Edge.Cuts
rectangle, saves, and exports a .dsn for freerouting.

NOTE: must run with the SYSTEM python that can `import pcbnew` (KiCad's bundled
module), not a venv that lacks it. Placement is a naive fixed grid for the LDO
reference design (U1/C1/C2); replace with a real auto-placer for larger boards.
"""
import sys
import pcbnew

proj = sys.argv[1]
target = sys.argv[2] if len(sys.argv) > 2 else "default"
pcb_path = f"{proj}/layouts/{target}/{target}.kicad_pcb"
board = pcbnew.LoadBoard(pcb_path)


def mm(x):
    return pcbnew.FromMM(x)


fps = list(board.GetFootprints())
by_ref = {fp.GetReference(): fp for fp in fps}

# LDO centred, input cap left, output cap right (~8 mm pitch).
layout = {"U1": (100, 100), "C1": (92, 100), "C2": (108, 100)}
spare_x = 100
for fp in fps:
    ref = fp.GetReference()
    if ref in layout:
        x, y = layout[ref]
        fp.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
    else:
        fp.SetPosition(pcbnew.VECTOR2I(mm(spare_x), mm(112)))
        spare_x += 8

# Edge.Cuts rectangle around the parts.
x0, y0, x1, y1 = 86, 92, 114, 108
edges = [(x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)]
for ax, ay, bx, by in edges:
    seg = pcbnew.PCB_SHAPE(board)
    seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
    seg.SetStart(pcbnew.VECTOR2I(mm(ax), mm(ay)))
    seg.SetEnd(pcbnew.VECTOR2I(mm(bx), mm(by)))
    seg.SetLayer(pcbnew.Edge_Cuts)
    seg.SetWidth(mm(0.15))
    board.Add(seg)

pcbnew.SaveBoard(pcb_path, board)
print("PLACED", [fp.GetReference() for fp in fps])

dsn = f"{proj}/build/{target}.dsn"
try:
    ok = pcbnew.ExportSpecctraDSN(board, dsn)
except TypeError:
    ok = pcbnew.ExportSpecctraDSN(dsn)
print("DSN", ok, dsn)
