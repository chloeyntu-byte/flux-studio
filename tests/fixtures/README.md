# Synthetic technology fixtures

`generic_a` through `generic_d` are invented stacks using M1..M9/AP, arbitrary layer numbers and configurable via geometry. They differ in drawing grid. Nothing was extracted from an OA library, GDS or a technology kit.

Regenerate with `python3 -B examples/generate_technology.py`. That command also generates the procedural winding illustrations in the SNPFit package. Their hash fields identify synthetic fixture contents, not measured SNPs or imported layout exports. The profile key `pdk` is retained for the existing reference API and contains only generic identifiers.

The geometry regression cases retain connectivity, terminal-shortcut, grid and layer-isolation checks. Inputs that previously assumed particular private rules were replaced with synthetic dimensions that exercise the same physical failures. No production geometry algorithm was changed.

`ideal_coupled.s4p` is a copy of `examples/ideal_coupled.s4p` (ideal coupled inductors generated from formulas by `examples/generate_synthetic.py`); the pin-metric tests use it as a known Z network.
