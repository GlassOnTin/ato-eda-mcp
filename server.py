"""Haven AI-EDA MCP server — atopile + kicad-cli + pcbnew + freerouting.

Exposes the design loop as MCP tools. Returns artifact PATHS (not pixels); Haven's
own `view_file` renders them. Run with the circuit-synth venv python (has fastmcp).
Placement/routing shell out to SYSTEM python3 (has pcbnew) and the freerouting jar.
"""
import glob
import json
import os
import subprocess
from pathlib import Path

from fastmcp import FastMCP

ATO = os.path.expanduser("~/.local/bin/ato")
KCLI = "kicad-cli"
SYS_PY = "python3"  # system python — has pcbnew (the csynth venv does NOT)
JAVA = next(iter(sorted(glob.glob("/usr/lib/jvm/java-2*-openjdk*/bin/java"))), "java")
FREEROUTING = "/root/freerouting.jar"
PLACE_SCRIPT = "/root/ato-mcp/place_and_dsn.py"
IMPORT_SES_SCRIPT = "/root/ato-mcp/import_ses.py"
ROOT = Path(os.path.expanduser("~/eda-projects"))
ROOT.mkdir(parents=True, exist_ok=True)

RENDER_LAYERS = "F.Cu,B.Cu,F.SilkS,F.Fab,Edge.Cuts"

mcp = FastMCP("ato-eda")


def _run(cmd, cwd=None, timeout=900):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _proj(name: str) -> Path:
    d = (ROOT / name).resolve()
    if ROOT not in d.parents and d != ROOT:
        raise ValueError("project name escapes eda-projects root")
    return d


ATO_YAML = """requires-atopile: "0.12.5"

paths:
  src: ./
  layout: ./layouts

builds:
  default:
    entry: main.ato:App
"""

STARTER_ATO = '''"""New atopile design."""

module App:
    pass
'''


@mcp.tool
def eda_list_projects() -> str:
    """List atopile projects managed by this server."""
    return json.dumps([p.name for p in ROOT.iterdir() if (p / "ato.yaml").exists()])


@mcp.tool
def eda_create_project(name: str) -> str:
    """Scaffold a new atopile project (ato.yaml + main.ato) under eda-projects/."""
    d = _proj(name)
    if d.exists():
        return json.dumps({"ok": False, "error": "project already exists", "path": str(d)})
    (d / "layouts").mkdir(parents=True)
    (d / "ato.yaml").write_text(ATO_YAML)
    (d / "main.ato").write_text(STARTER_ATO)
    return json.dumps({"ok": True, "path": str(d), "entry": "main.ato:App"})


@mcp.tool
def eda_read_source(project: str, file: str = "main.ato") -> str:
    """Read an .ato source file from a project."""
    f = _proj(project) / file
    if not f.exists():
        return json.dumps({"ok": False, "error": "not found", "path": str(f)})
    return json.dumps({"ok": True, "path": str(f), "content": f.read_text()})


@mcp.tool
def eda_write_source(project: str, content: str, file: str = "main.ato") -> str:
    """Write an .ato source file (the design-as-code source of truth)."""
    f = _proj(project) / file
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    return json.dumps({"ok": True, "path": str(f), "bytes": len(content)})


@mcp.tool
def eda_build(project: str, target: str = "default") -> str:
    """Compile the design: `ato build` -> KiCad netlist + PCB + BOM, picking real parts.
    atopile's electrical checks run here; a failing ERC fails the build. Footprints
    land at the origin — run eda_layout + eda_route to place and route."""
    d = _proj(project)
    rc, out, err = _run([ATO, "build"], cwd=str(d))
    log = (out + err)
    bdir = d / "build" / "builds" / target
    arts = {
        "pcb": str(next(iter(sorted(bdir.glob("*.kicad_pcb"))), "")),
        "netlist": str(next(iter(bdir.glob(f"{target}/*.net")), "")),
        "bom": str(bdir / f"{target}.bom.csv") if (bdir / f"{target}.bom.csv").exists() else "",
        "layout_pcb": str(d / "layouts" / target / f"{target}.kicad_pcb"),
    }
    tail = "\n".join(l for l in log.splitlines()
                     if l.strip() and "sanitize" not in l)[-2500:]
    return json.dumps({"ok": rc == 0, "exit": rc, "artifacts": arts, "log_tail": tail})


@mcp.tool
def eda_layout(project: str, target: str = "default") -> str:
    """Place footprints (off the origin), draw an Edge.Cuts board outline, and export a
    Specctra DSN — via pcbnew. Run after eda_build, before eda_route."""
    d = _proj(project)
    rc, out, err = _run([SYS_PY, PLACE_SCRIPT, str(d), target])
    return json.dumps({"ok": rc == 0, "exit": rc,
                       "dsn": str(d / "build" / f"{target}.dsn"),
                       "out": (out + err).strip().splitlines()[-6:]})


@mcp.tool
def eda_route(project: str, target: str = "default", passes: int = 20) -> str:
    """Autoroute the placed board with freerouting (DSN -> SES, headless) and import the
    routes back into the .kicad_pcb via pcbnew. Run after eda_layout, then eda_render_pcb
    / eda_check to inspect. Returns the imported track/via count."""
    d = _proj(project)
    dsn = d / "build" / f"{target}.dsn"
    ses = d / "build" / f"{target}.ses"
    if not dsn.exists():
        return json.dumps({"ok": False, "error": "no DSN — run eda_layout first"})
    rc1, o1, e1 = _run([JAVA, "-Djava.awt.headless=true", "-jar", FREEROUTING,
                        "-de", str(dsn), "-do", str(ses), "-mp", str(passes)],
                       cwd=str(d / "build"), timeout=600)
    if not ses.exists():
        return json.dumps({"ok": False, "error": "freerouting produced no SES",
                           "log": (o1 + e1)[-800:]})
    rc2, o2, e2 = _run([SYS_PY, IMPORT_SES_SCRIPT, str(d), target])
    return json.dumps({"ok": rc2 == 0, "ses": str(ses),
                       "import": (o2 + e2).strip().splitlines()[-4:]})


@mcp.tool
def eda_check(project: str, target: str = "default") -> str:
    """Run DRC on the PCB (kicad-cli pcb drc -> JSON). Returns rule-violation and
    unconnected-net counts (these gate `ok`) plus an informational schematic-parity
    count. atopile's ERC is covered by eda_build success."""
    d = _proj(project)
    pcb = d / "layouts" / target / f"{target}.kicad_pcb"
    if not pcb.exists():
        return json.dumps({"ok": False, "error": "no PCB — build first", "path": str(pcb)})
    rep = d / "build" / f"{target}.drc.json"
    rep.parent.mkdir(parents=True, exist_ok=True)
    _run([KCLI, "pcb", "drc", "--format", "json", "-o", str(rep), str(pcb)])
    data = json.loads(rep.read_text()) if rep.exists() else {}
    viol = data.get("violations", [])
    unconn = data.get("unconnected_items", [])
    parity = data.get("schematic_parity", [])

    def summ(items):
        return [f"{v.get('severity', '')}: {v.get('description', '')}" for v in items[:6]]

    clean = len(viol) == 0 and len(unconn) == 0
    return json.dumps({
        "ok": clean,
        "violations": len(viol),
        "unconnected": len(unconn),
        "schematic_parity_info": len(parity),
        "samples": {"violations": summ(viol), "unconnected": summ(unconn)},
    })


@mcp.tool
def eda_get_bom(project: str, target: str = "default") -> str:
    """Return the picked BOM (CSV text) with LCSC part numbers."""
    f = _proj(project) / "build" / "builds" / target / f"{target}.bom.csv"
    if not f.exists():
        return json.dumps({"ok": False, "error": "no BOM — build first", "path": str(f)})
    return json.dumps({"ok": True, "path": str(f), "csv": f.read_text()})


@mcp.tool
def eda_render_pcb(project: str, target: str = "default") -> str:
    """Render the PCB to an SVG (explicit layers — works around KiCad 10's --layers
    requirement). Returns the SVG path; call Haven `view_file` on it to see pixels."""
    d = _proj(project)
    pcb = d / "layouts" / target / f"{target}.kicad_pcb"
    if not pcb.exists():
        return json.dumps({"ok": False, "error": "no PCB — build first", "path": str(pcb)})
    svg = d / "build" / f"{target}.pcb.svg"
    svg.parent.mkdir(parents=True, exist_ok=True)
    rc, out, err = _run([KCLI, "pcb", "export", "svg", "--layers", RENDER_LAYERS,
                         "--page-size-mode", "2", "-o", str(svg), str(pcb)])
    return json.dumps({"ok": rc == 0 and svg.exists(), "svg": str(svg),
                       "hint": "view_file this path", "stderr": err[-400:]})


@mcp.tool
def eda_export_fab(project: str, target: str = "default") -> str:
    """Export manufacturing files (Gerbers + drill + pick-and-place) from the PCB."""
    d = _proj(project)
    pcb = d / "layouts" / target / f"{target}.kicad_pcb"
    if not pcb.exists():
        return json.dumps({"ok": False, "error": "no PCB — build first", "path": str(pcb)})
    out = d / "build" / "fab"
    out.mkdir(parents=True, exist_ok=True)
    rc1, _, e1 = _run([KCLI, "pcb", "export", "gerbers", "-o", f"{out}/", str(pcb)])
    rc2, _, e2 = _run([KCLI, "pcb", "export", "drill", "-o", f"{out}/", str(pcb)])
    rc3, _, e3 = _run([KCLI, "pcb", "export", "pos", "-o", f"{out}/pos.csv",
                       "--format", "csv", str(pcb)])
    files = sorted(p.name for p in out.iterdir())
    return json.dumps({"ok": all(r == 0 for r in (rc1, rc2, rc3)), "dir": str(out),
                       "files": files, "stderr": (e1 + e2 + e3)[-400:]})


if __name__ == "__main__":
    port = int(os.environ.get("EDA_MCP_PORT", "8770"))
    try:
        mcp.run(transport="http", host="127.0.0.1", port=port, path="/mcp")
    except TypeError:
        mcp.run(transport="streamable-http", host="127.0.0.1", port=port, path="/mcp")
