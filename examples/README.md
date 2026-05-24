# Examples

## `ldo_3v3` — 3.3V LDO regulator

A minimal but complete reference design: 5V (nominal) → **AMS1117-3.3** (LCSC `C6186`,
SOT-223) → 3.3V, with input/output ceramic decoupling. It exercises the whole loop —
real IC part-pick, power nets, place, autoroute, DRC — on the smallest board that's
still more than passives.

### Run it (over MCP)

```
eda_create_project   name=ldo_3v3
eda_write_source     project=ldo_3v3   content=<contents of ldo_3v3/main.ato>
eda_build            project=ldo_3v3      # picks parts, runs atopile ERC
eda_layout           project=ldo_3v3      # place + board outline + DSN
eda_route            project=ldo_3v3      # freerouting + import routes
eda_check            project=ldo_3v3      # DRC
eda_render_pcb       project=ldo_3v3      # -> SVG (view it with the client)
eda_export_fab       project=ldo_3v3      # Gerbers + drill + pick-and-place
```

Or locally without the server: drop `main.ato` + `ato.yaml` in a dir, `ato build`,
then run `../../place_and_dsn.py` and the freerouting + `../../import_ses.py` steps
(see the top-level README).

### Expected result

`eda_build` picks three real LCSC parts:

| Ref | Footprint | Value | MPN | LCSC |
|-----|-----------|-------|-----|------|
| U1 | SOT-223-3 | AMS1117-3.3 | AMS1117-3.3 | C6186 |
| C1 | C0805 | 10µF 25V X5R | CL21A106KAYNNNE | C15850 |
| C2 | C0805 | 22µF 25V X5R | CL21A226MAQNNNE | C45783 |

After `eda_layout` + `eda_route`, DRC reports **0 unconnected items, 0 errors** (only
cosmetic silkscreen-clearance warnings on this dense little board); the board routes on
a single layer (~16 track segments, 0 vias).

### What it demonstrates (atopile 0.12.5)

- An IC pinned by **explicit `lcsc_id`** (the generic `LDO` isn't auto-pickable in 0.12.5),
  so no parametric fields are set on it.
- Decoupling wired with **plain `~` + `.p1`/`.p2`** (not the experimental `~>` bridge).

## `blinker_555` — 555 astable LED blinker

A classic 555 astable (~1.4 Hz): R1/R2 + C1 set the rate, OUT drives a red LED through a
current-limiting resistor. 8 components on a 2-layer board — exercises a multi-pin IC, an
LED, and denser routing (vias) than the LDO.

Unlike the LDO (which used faebryk's built-in `LDO` module), there's no faebryk 555, so the
**NE555 is a generated atomic part** — committed under [`parts/`](blinker_555/parts) so the
example builds standalone. To regenerate it yourself:

```
ato create part --search C7593 --accept-single -p .   # NE555DR (SOIC-8)
```

### Run it

Same loop as the LDO (`eda_create_project` → `eda_write_source` → `eda_build` → `eda_layout`
→ `eda_route` → `eda_check` → `eda_render_pcb` / `eda_export_fab`).

### Expected result

`eda_build` picks **7/7** generics (the NE555 is pre-resolved): three R0805, three C0805,
and a red 0805 LED (`C2286`). After place + route the board is **2-layer** (~75 track
segments, 2 vias), and `eda_check` returns **0 errors, 0 unconnected** (the silkscreen
warnings are cosmetic — designators crowd on the dense grid).

### What it adds over the LDO

- An IC wired **pin-by-pin** via a generated atomic part's named signals (`u1.VCC`, `u1.OUT`,
  `u1.THRES`, …) rather than a faebryk module's power interfaces.
- A node tied to two pins (`u1.THRES ~ u1.TRIG`) and a multi-net astable RC network.
