# Screenshots

Captured from real Tk windows on a private 1600x1000 X display, using the fictional example technology (`app.py --example`). The 1280 image shows a 1280x800 window. No real technology or EDA result is shown.

The open-source repository builds only the **differential inductor**. Images marked **structure pack** show structures that are separate structure packs; they are not included in this repository (ask through [GitHub Issues](https://github.com/chloeyntu-byte/flux-studio/issues) if you need one). 3D images use the optional [Layout 3D Viewer](https://github.com/chloeyntu-byte/layout-3d-viewer) embedding.

## Flux Studio

| Image | State |
| --- | --- |
| [flux-empty.png](flux-empty.png) | First launch, no technology loaded |
| [flux-example.png](flux-example.png) | Differential inductor with centre TAP, PGS and ground ring |
| [flux-three-turns.png](flux-three-turns.png) | The same inductor with three turns |
| [flux-1280.png](flux-1280.png) | Layout at the supported minimum size |
| [flux-shields.png](flux-shields.png) | PGS and ground ring on (left) and off (right) |
| [flux-param-morph.gif](flux-param-morph.gif) | Animated: inner diameter, turns and width change, geometry is regenerated |
| [flux-3d-studio.png](flux-3d-studio.png) | 3D Studio view of the differential inductor |
| [flux-3d-metal.png](flux-3d-metal.png) | 3D Metal view of the differential inductor |
| [flux-3d-rotate.gif](flux-3d-rotate.gif) | Animated: 3D view rotating |
| [flux-leg-pins.png](flux-leg-pins.png) | Leg Pin manager: several pins per leg, renamed (rf_in, rf_in_b, rf_out, vdd_tap) |
| [flux-pin-drag.gif](flux-pin-drag.gif) | Animated: a pin dragged along its leg |
| [flux-leg-scale.gif](flux-leg-scale.gif) | Animated: lead extension changes, pins keep their fraction of the leg |
| [flux-leg-pins-3d.png](flux-leg-pins-3d.png) | Leg Pins beside the 3D view, selected pin marked in 3D |
| [flux-emx-pins.png](flux-emx-pins.png) | EMX goals assigned to custom pins; one result column per pin metric |
| [flux-results.png](flux-results.png) | Qp viridis heatmap from nine ideal-circuit SNPs (synthetic replay) |
| [flux-structures.png](flux-structures.png) | Built-in differential inductor (top left) and five **structure pack** layouts: implicit coplanar 1:1 direct / crossed leads, octagonal inductor with TAP, coplanar transformer, stacked transformer |
| [flux-structures-3d.png](flux-structures-3d.png) | The same six layouts in 3D Studio; all but the top-left tile are **structure packs** |
| [flux-pack-stacked-3d.png](flux-pack-stacked-3d.png) | Stacked transformer in 3D Metal (**structure pack**, not included) |

## SNPFit Studio

| Image | State |
| --- | --- |
| [snpfit-empty.png](snpfit-empty.png) | Independent SNPFit, no input |
| [snpfit-example.png](snpfit-example.png) | Original synthetic RLC samples |
| [snpfit-running.png](snpfit-running.png) | Numerical fitting with progress |
| [snpfit-results.png](snpfit-results.png) | Original/current L/Q/R; unresolved SRF remains explicit |
| [snpfit-circuit.png](snpfit-circuit.png) | Equivalent circuit and parameter view |
| [snpfit-1280.png](snpfit-1280.png) | SNPFit at the supported minimum size |
