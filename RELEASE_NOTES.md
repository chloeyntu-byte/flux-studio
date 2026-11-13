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
