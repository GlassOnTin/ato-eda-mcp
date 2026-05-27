#!/usr/bin/env bash
# Provision the ato-eda-mcp toolchain. Tested on Arch Linux ARM (aarch64) in a
# Haven proot guest; adapt the package manager for other distros.
#
# In a proot, ALWAYS export UV_LINK_MODE=copy — uv's default hardlinking fails
# with "Operation not permitted" across the proot bind mounts.
set -euo pipefail
export UV_LINK_MODE=copy

# --- KiCad 10 (kicad-cli + pcbnew python) -----------------------------------
pacman -S --noconfirm --needed kicad kicad-library uv git

# --- Build toolchain (atopile builds from source on aarch64 — no wheels) -----
pacman -S --noconfirm --needed base-devel cmake ninja

# --- atopile -----------------------------------------------------------------
# faebryk's C++ core fails on modern GCC with "'uintptr_t' does not name a type"
# (newer libstdc++ dropped the transitive <cstdint> include). Force-include it.
CXXFLAGS="-include cstdint" CFLAGS="-include stdint.h" \
  uv tool install atopile          # resolves to 0.12.5 on aarch64
# `ato` lands in ~/.local/bin — ensure it's on PATH.

# --- circuit-synth venv (provides fastmcp; optional DigiKey/SnapEDA/PySpice) --
# Also gets schemdraw — the schematic geometry engine runs under THIS interpreter
# (the server shells out with sys.executable). cairosvg is optional: it rasterizes
# the schematic SVG to PNG; without it eda_render_schematic returns SVG only.
uv venv --python 3.13 "$HOME/csynth"
uv pip install --python "$HOME/csynth/bin/python" circuit-synth schemdraw cairosvg

# --- freerouting + a JRE >= 25 (2.2.4 needs class file 69 = Java 25) ----------
pacman -S --noconfirm --needed jre-openjdk-headless   # Arch ships Java 26
url=$(curl -s https://api.github.com/repos/freerouting/freerouting/releases/latest \
      | grep -oE '"browser_download_url": *"[^"]*\.jar"' \
      | grep -oE 'https://[^"]*' | head -1)
curl -sL "$url" -o "$HOME/freerouting.jar"

echo "Done. Run:  EDA_MCP_PORT=8770 $HOME/csynth/bin/python server.py"
