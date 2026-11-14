# Flux Studio v1.2.0

Leg pins, EMX metrics on chosen pins, and an optional 3D preview.

- **Leg Pin manager.** The **Leg Pins** button turns the Layout page into a pin editor for the terminal leads: add several named pins per leg, drag them along the leg, rename, delete, and restore the defaults per leg or for the whole layout. Pins are stored as a fraction of the leg length, so they follow lead-extension sweeps. Same-named pins form one EMX terminal; names that would join two windings, mix signal and TAP pins or reuse ground pins are refused.
- **EMX metrics on chosen pins.** Objectives and constraints can name their pins, e.g. `L[rf_in,rf_out]` or `k[a,b,c,d]`. The same metric on different pins is a different goal; tables, charts and optimization videos add one column per pin-assigned metric. Layouts with custom leg pins must assign pins to every metric.
- **Optional 3D preview.** With `FLUX_VL3D_ROOT` pointing at a [Layout 3D Viewer](https://github.com/chloeyntu-byte/layout-3d-viewer) checkout (its `native/embed.py` interface, see its `docs/EMBEDDING.md`) and PyOpenGL 3.1.7 / pyopengltk 0.0.4 available (optionally through `FLUX_VL3D_PYTHONPATH`), the preview offers 2D, 3D Studio and 3D Metal; in Leg Pins mode 2D and 3D sit side by side. Use the latest Layout 3D Viewer, which prepares the system GL libraries under conda Pythons. Without `FLUX_VL3D_ROOT` Flux stays 2D only and unchanged.
- The open-source edition still builds only the differential inductor. Other structures shown in the screenshots are separate structure packs, not included; ask through [GitHub Issues](https://github.com/chloeyntu-byte/flux-studio/issues) if you need one.

Validation: Flux suite 509 tests passed, SNPFit suite 164 passed (3 skipped), on Linux with Python 3.11 and a private X display. The new tests use the built-in differential inductor (TAP on/off, several turns), the example structure pack and a synthetic two-winding test model; the 3D tests use a stand-in engine. The real 3D preview was checked with Layout 3D Viewer on the example technology. Native tools remain mocked.

---

# Flux Studio v1.1.0

Structures become pluggable. The open-source edition keeps one built-in structure, the symmetric **differential inductor** with optional centre TAP, and loads every other structure as a **structure pack**.

- `structure_packs.py` discovers packs in `FLUX_PACKS` and `packs/`, runs each pack's `register(flux)`, reports failures and rolls back a failing pack's models. `flux_cli.py packs` lists what was loaded.
- Pack models use the same parameter panel, shared PGS/ground-ring parameters, geometry checks, native generation, EMX campaigns, optimization and SNPFit hand-off as the built-in inductor. Routing variants, EMX metrics, default metal positions and role labels come from the model definition instead of hard-coded model ids.
- The implicit coplanar, octagonal, coplanar and stacked builders are no longer in this repository. Design files and campaigns that name a model from a missing pack fail with an explicit "load the structure pack" message.
- Documentation: [docs/STRUCTURE_PACKS.md](docs/STRUCTURE_PACKS.md) (English and Chinese) and a runnable example pack in `examples/packs/`.
- Screenshots are unchanged; some show structures that are now supplied by packs.

Validation: Flux suite 455 tests passed (including 28 new structure-pack tests), SNPFit suite 164 passed (3 skipped), on Linux with Python 3.11 and a private X display. Native tools remain mocked.

---

# Flux Studio v1.0.0

Initial public source release with bundled, independently launchable SNPFit Studio.

The desktop uses the unmodified RF-FIELDWORK theme: shared product headers, yellow primary actions, neutral secondary actions, explicit disabled states, status chips, progress bars and dark charts. Magnitude heatmaps and surfaces use canonical viridis; source/current comparison curves have distinct emphasis. Layout JSON profiles support offline use; native writes retain the existing attached-technology authorization path.

All distributed technology stacks, winding references and Touchstone files are fictional or formula-generated. Rebuild them with `examples/generate_technology.py` and `examples/generate_synthetic.py`. The campaign replay has nine ideal-circuit candidates and disables native candidate actions. No audit material, private model, imported layout, measured/process SNP, log, backup or runtime history is distributed.

## Validation scope

- Flux suite: 443 tests passed on the release tree before final child-window presentation polish; affected UI/video tests were then rerun (51 passed).
- SNPFit suite: 164 tests passed, including the optional two-page cached-session restore test using a synthetic session.
- New smoke tests create both real application windows on a private X display. Layout, fitting, integration boundaries and native environment handling use examples or mocks; no licensed EDA tool was run.
- Both application source and tests compile. The documented geometry CLI and numerical fit/export command were exercised. The actual Flux shell launcher displayed the offline example; the standalone SNPFit bootstrap was tested with an explicit interpreter command name.
- Screenshots cover empty, example, running, result and 1280x800 views. The source of truth for image contents is the real application, not a mockup.
- Release scanning requires zero findings, with no allowlist.

Native OA, stream export, EMX, Spectre, license operation, DRC/LVS and another machine's runtime compatibility have not been revalidated for this public release. Use your own environment and process data. Numerical fitting of the low-frequency example does not establish a resolved SRF or complete broadband Studio qualification.

The public version starts at v1.0.0; retained API schemas continue to identify their established data contracts.
