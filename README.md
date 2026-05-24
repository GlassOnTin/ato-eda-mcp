# ato-eda-mcp

An AI-native, **design-as-code** electronic-design MCP server: turn a text circuit
description into a routed, DRC-clean, fab-ready KiCad board — fully headless, driven
over [MCP](https://modelcontextprotocol.io). Runs anywhere a Python venv + KiCad fit:
a cloud container, a laptop, or an Android phone's proot guest via
[Haven](https://github.com/GlassHaven/Haven).

This is the **app-native engine** layer. Haven (or any MCP client) provides the
visual loop + presence + transport; this server owns the EDA structure. The two are
deliberately separate — Haven core does not contain EDA logic.

## The loop

```
author .ato ──build──▶ netlist + PCB + BOM (real LCSC parts)
                          │
                       layout ──▶ place + board outline + Specctra DSN   (pcbnew)
                          │
                        route ──▶ freerouting (headless) ──▶ import SES  (pcbnew)
                          │
                        check ──▶ DRC                                    (kicad-cli)
                          │
                       render / export ──▶ SVG · Gerbers · drill · pick-place
```

The agent edits the `.ato` source, builds, reads structured errors/violations, fixes,
and re-runs — a closed verify-fix loop. Layout is *derived*; the LLM owns the graph,
the tools own the drawing.

## MCP tools

| tool | does |
|------|------|
| `eda_create_project` / `eda_list_projects` | scaffold / list projects under `~/eda-projects` |
| `eda_read_source` / `eda_write_source` | the `.ato` design-as-code source of truth |
| `eda_build` | `ato build` → netlist + PCB + BOM, picks real LCSC parts, runs atopile ERC |
| `eda_layout` | place footprints + Edge.Cuts outline + export Specctra DSN (pcbnew) |
| `eda_route` | freerouting autoroute (DSN→SES, headless) + import routes (pcbnew) |
| `eda_check` | `kicad-cli pcb drc` → violation / unconnected counts |
| `eda_get_bom` | picked BOM CSV with LCSC part numbers |
| `eda_render_pcb` | PCB → SVG (explicit layers) for the client's image viewer |
| `eda_export_fab` | Gerbers + drill + pick-and-place |

Tools return artifact **paths**, not images — the MCP client renders them (e.g. Haven's
`view_file`).

## Setup

See `setup.sh`. Tested on **Arch Linux ARM** (aarch64) in a Haven proot guest, but the
same steps work on any modern Linux. Components:

- **atopile** (the `.ato` DSL + compiler). On aarch64 there are **no prebuilt wheels** —
  it builds from source and needs `base-devel cmake ninja` plus a workaround for modern
  GCC (`CXXFLAGS="-include cstdint"`, see `setup.sh`). Resolves to 0.12.5.
- **KiCad 10** (`kicad-cli` + the `pcbnew` Python module, used for DSN/SES + DRC + Gerbers).
- **circuit-synth** venv — only used here for its bundled `fastmcp` (and optional future
  DigiKey/SnapEDA sourcing + PySpice). The server runs under this venv's Python.
- **freerouting 2.2.4** (Java jar) + a **JRE ≥ 25** (freerouting 2.2.4 = class file 69).

Run the server:

```sh
EDA_MCP_PORT=8770 /path/to/csynth-venv/bin/python server.py
```

Under Haven, register it as a guest service (`isMcp=true`) so its tools are aggregated
into Haven's MCP surface and tunneled to a remote client.

## atopile 0.12.5 gotchas (learned the hard way)

- **No method calls in `.ato`** — `ldo.enable_output()` is a syntax error. (The faebryk
  `LDO` Python `usage_example` targets newer atopile; don't copy it verbatim.)
- **Pinned part vs parametric is mutually exclusive** — set `ldo.lcsc_id = "C6186"` *or*
  the parametric fields (`output_voltage`, `package`, …), never both. With an explicit
  LCSC part the properties come from the part.
- **`LDO`/`Regulator` aren't auto-pickable** in 0.12.5 (`is_pickable_by_type` is commented
  out). ICs need an explicit `lcsc_id`; passives (`Resistor`/`Capacitor`/`Diode`) pick
  fine from value + package.
- **`~>` bridge-connect is experimental** — use plain `~` with `.p1`/`.p2`, e.g.
  `ldo.power_in.hv ~ cap.p1; cap.p2 ~ ldo.power_in.lv`.

## Known limitations

- **Placement is a naive fixed grid** (`place_and_dsn.py` hardcodes U1/C1/C2). Fine for
  small reference designs; needs a real auto-placer for anything larger.
- Sourcing is atopile's LCSC pick only; circuit-synth's DigiKey/SnapEDA enrichment is
  installed but not yet wired in.
- atopile is schematic-less by design (the `.ato` code *is* the schematic) — there's no
  `.kicad_sch`, which is why schematic-first tools (e.g. circuit-synth's project
  conversion) can't ingest its output. The netlist + PCB are the interchange.

## Reference design

`examples/`-style: a 3.3 V LDO (AMS1117-3.3, LCSC C6186, SOT-223) with input/output
ceramics — built, placed, routed (0 unconnected, 0 DRC errors), Gerbers exported, all
headless. See the `.ato` snippet in this README's history / the Haven session notes.
