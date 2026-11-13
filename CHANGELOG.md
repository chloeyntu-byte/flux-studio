# Changelog

## v1.1.0 — 2026-11-13

- Structure packs: structures other than the built-in differential inductor now load at start-up from `FLUX_PACKS` or `packs/` (`structure_packs.py`, `docs/STRUCTURE_PACKS.md`).
- The open-source edition keeps one built-in structure, the symmetric differential inductor with optional centre TAP. The implicit coplanar, octagonal, coplanar and stacked transformer/inductor builders were removed from this repository; they are distributed as structure packs.
- Model metadata now carries routing variants, EMX metrics, default metal positions and role labels, so the UI, backend and EMX source no longer special-case model ids.
- New `flux_cli.py packs` command and a runnable example pack (`examples/packs/example_tapped_inductor.py`).
- Default model is the differential inductor; `examples/layout_design.json` uses it.

## v1.0.0 — 2026-11-10

- Initial public source release of Flux Studio with bundled SNPFit Studio.
- Shared RF-FIELDWORK dark theme, yellow primary actions, status chips and progress feedback.
- Dark chart backgrounds and viridis magnitude heatmaps/surfaces.
- Offline user technology JSON and fictional M1..M9/AP example; native writes require an actual attached OA technology.
- Removed machine defaults and private technology, measurement, audit and runtime assets.
- Added reproducible ideal RLC/coupled-inductor SNP examples, bilingual instructions and GUI smoke checks.
- Kept geometry and fitting algorithms; adapted technology-dependent test fixtures to synthetic inputs and isolated a test plugin registration.
