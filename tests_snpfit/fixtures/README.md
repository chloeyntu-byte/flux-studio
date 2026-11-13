# Synthetic SNP fixture

`tap_synthetic_four_point.s3p` uses the explicit half-winding RL plus skin-branch admittance formula in `examples/generate_synthetic.py`, at 1, 5, 10 and 20 GHz. It is not an EM result. The regression checks failure of the series-RL seed and successful richer numerical fitting on exactly those four points; the winning seed depends on this synthetic circuit.
