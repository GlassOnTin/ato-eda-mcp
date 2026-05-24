"""Import a freerouting .ses back into the .kicad_pcb and save. Then report track count.

Usage: python3 import_ses.py <project_dir> [target]
Must run with the SYSTEM python that can `import pcbnew`.
"""
import sys
import pcbnew

proj = sys.argv[1]
target = sys.argv[2] if len(sys.argv) > 2 else "default"
pcb = f"{proj}/layouts/{target}/{target}.kicad_pcb"
ses = f"{proj}/build/{target}.ses"

board = pcbnew.LoadBoard(pcb)
try:
    ok = pcbnew.ImportSpecctraSES(board, ses)
except TypeError:
    ok = pcbnew.ImportSpecctraSES(ses)
pcbnew.SaveBoard(pcb, board)

tracks = list(board.GetTracks())
n_via = sum(1 for t in tracks if t.Type() == pcbnew.PCB_VIA_T)
print("SES_IMPORT", ok)
print("TRACKS", len(tracks), "VIAS", n_via)
