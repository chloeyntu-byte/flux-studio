# Optional local fallback. Automatic discovery is the default.
# Copy this file to flux_config.sh only if discovery needs help, then uncomment
# the relevant lines and use existing destination paths. Do not edit system files.
# TFGEN_* variables already set in the launching environment take precedence.
# Relative path values in this file are relative to the project directory.
# Relative path values inherited from your terminal are relative to its launch
# directory. Bare Python/Virtuoso command names are looked up in PATH.
# No path is evaluated as code by normalization. Use $HOME for a home path.

# export TFGEN_PYTHON='./.venv/bin/python'  # Or an existing absolute interpreter.
# export TFGEN_VIRTUOSO='virtuoso'         # Or an existing executable path.
# export TFGEN_CADENCE_ENV='../cadence_environment.sh'
# export TFGEN_WORKSPACE='..'
# export TFGEN_CDS_LIB='../cds.lib'
# export TFGEN_DESIGN_ROOT='../designs'    # Optional additional restriction.
# export TFGEN_RUN_USER='desktop_user'  # Root launch fallback only.

# EMX is optional for layout generation. Existing native PATH/setup hints and a
# unique EMX installation adjacent to the detected Cadence installation are
# discovered automatically. Use only the needed overrides when ambiguous or
# installed elsewhere; no tool, PDK or global shell configuration is modified.
# export TFGEN_EMX='emx'  # Or the actual executable path.
# export TFGEN_EMX_CONFIG='../local_emx/emxconfig.il'
# export TFGEN_EMX_PROCESS='../local_emx/matching_process.proc'
# export TFGEN_EMX_LAYER_MAP='../local_emx/matching_layer_map'
# export TFGEN_STRMOUT='strmout'

# Automatic dependency discovery normally makes these fields unnecessary.
# Explicit confirmed custom-Python dependencies remain authoritative overrides.
# Keep Cadence libraries out of Tk.
# The probe and actual GUI use these same optional interpreter-specific values.
# Use ./launch.sh --diagnose for startup diagnostics.
# export TFGEN_PYTHON_LD_LIBRARY_PATH='../python/lib'
# export TFGEN_PYTHONHOME='../python'
# export TFGEN_PYTHONPATH='../compatible_python_modules'
# export TFGEN_TCL_LIBRARY='../matching_tcl_data'
# export TFGEN_TK_LIBRARY='../matching_tk_data'

# Optional SNPFit Studio location. By default Flux uses its bundled SNPFit launcher.
# export TFGEN_SNPFIT_ROOT="../snpfit_studio"
