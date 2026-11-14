# Changelog

## v1.2.0 — 2026-11-14

- Leg Pin manager (`leg_pins.py`, Layout page **Leg Pins** mode): several named pins per terminal leg, add / delete / rename / drag along the leg, per-leg and global restore, non-leg geometry dimmed. Positions are stored as a fraction of the leg length and follow lead-extension sweeps. Pin layouts are saved with design JSON, presets and EMX campaigns.
- EMX metrics on chosen pins: `L[p,n]`, `Q[p,n]`, `Lp/Ls/Qp/Qs[p,n]` and `k[p1,n1,p2,n2]` goals; pin selectors in the goal editor; one result/chart/video column per pin-assigned metric; custom leg pins require explicit pins (`emx_search`, `emx_adapter`, `emx_campaign`, `emx_page`, `result_data`, `emx_charts`, `optimization_video`, `video_chart`).
- Optional 3D preview through the Layout 3D Viewer embedding interface: `FLUX_VL3D_ROOT` / `FLUX_VL3D_PYTHONPATH`, 2D / 3D Studio / 3D Metal selector, Leg Pins beside 3D, pin selection synchronised both ways, fallback to 2D when OpenGL fails. Without `FLUX_VL3D_ROOT` nothing changes. The launcher passes both variables.
- New tests: leg pins, Leg Pin manager, pin-assigned EMX goals and the 3D preview (with a stand-in engine). Flux suite: 509 tests; SNPFit suite: 164 tests (3 skipped).
- New screenshots and animations; README sections for the structure packs, 3D preview, Leg Pins and pin metrics.

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
