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
