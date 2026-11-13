"""Model-independent PDK discovery, parameter files and protected OA generation."""
import copy
import datetime
import json
import math
from pathlib import Path
import re
import threading

import geometry
from cadence import ROOT, NAME, write_cell
from structures import MODEL_DEFINITIONS, generate_model
from technology import load_technology, drawing_grid, missing_rule_warnings

DEFAULT_MODEL = 'differential_inductor'
# Design files written before model ids existed always describe this model,
# which is now supplied by a structure pack rather than built in.
LEGACY_MODEL = 'transformer_implicit'
EXTRA_MODELS = []
MODEL_BUILDERS = {}
LIBRARIES = {}
LIBRARY_SCOPE = None
RUNTIME = None
LAUNCH_CONTEXT = None
CONTEXT_LOCK = threading.Lock()
GENERATE_LOCK = threading.Lock()
PRESET_LOCK = threading.Lock()
PRESETS = ROOT / 'presets'
SHARED_ROLES = ['pgs', 'ring', 'ground_route']
SOURCE_PROVENANCE_FIELDS = ('schema', 'library', 'cell', 'view', 'workspace',
                            'cds_lib', 'library_path', 'view_path', 'technology')


def structure_pack_report():
    """Public summary of the structure packs found at start-up."""
    import structure_packs
    structure_packs.ensure_loaded()
    return structure_packs.report()


def public_source_context(source):
    """Export source identity only; keep runtime environment private."""
    if not isinstance(source, dict):
        return None
    return {key: copy.deepcopy(source[key]) for key in SOURCE_PROVENANCE_FIELDS if key in source}


def models():
    """Return body metadata plus the same independent shielding parameters.

    Built-in models come first, followed by models registered by structure
    packs (loaded once, on first use).
    """
    import structure_packs
    structure_packs.ensure_loaded()
    return [_public_definition(source) for source in MODEL_DEFINITIONS + EXTRA_MODELS]


def _public_definition(source):
    """One model's metadata with the shared shielding fields and roles added."""
    shared = [f for f in geometry.PARAMETER_SCHEMA if f['part'] not in ('body', 'legs')]
    definition = copy.deepcopy(source)
    own = {field['name'] for field in definition['parameter_schema']}
    definition['parameter_schema'] += [copy.deepcopy(f) for f in shared if f['name'] not in own]
    for field in shared:
        definition['defaults'].setdefault(field['name'], copy.deepcopy(
            geometry.DEFAULTS.get(field['name'], field.get('default'))))
    definition['layer_roles'] = list(dict.fromkeys(definition['layer_roles'] + SHARED_ROLES))
    return definition


def _validate_definition(definition):
    def strings(value):
        return isinstance(value, list) and all(isinstance(item, str) and item for item in value)
    if not all(isinstance(definition.get(key), str) for key in ('title', 'description')):
        raise ValueError('Model title and description must be strings.')
    if not isinstance(definition['defaults'], dict):
        raise ValueError('defaults must be an object.')
    if not strings(definition['layer_roles']) or not definition['layer_roles']:
        raise ValueError('layer_roles must be a non-empty list of role names.')
    names = [field['name'] for field in definition['parameter_schema']]
    if len(set(names)) != len(names):
        raise ValueError('parameter_schema has duplicate field names.')
    for field in definition['parameter_schema']:
        numeric = field.get('type') not in ('boolean', 'bool', 'enum') and not field.get('options')
        if field.get('options') is not None and not isinstance(field['options'], list):
            raise ValueError('Field %s options must be a list.' % field['name'])
        if numeric and not all(isinstance(field.get(key), (int, float)) and not isinstance(field.get(key), bool)
                               for key in ('min', 'max')):
            raise ValueError('Numeric field %s needs numeric min and max.' % field['name'])
        if field['name'] not in definition['defaults'] and 'default' not in field:
            raise ValueError('Field %s has no default.' % field['name'])
    if 'emx_metrics' in definition and not strings(definition['emx_metrics']):
        raise ValueError('emx_metrics must be a list of metric names.')
    for key in ('default_layers', 'role_labels'):
        if key in definition and not isinstance(definition[key], dict):
            raise ValueError('%s must be an object.' % key)


def _model_source(model):
    return next((item for item in MODEL_DEFINITIONS + EXTRA_MODELS if item['id'] == model), None)


def register_model(definition, builder):
    """Register body metadata and a common-contract geometry builder.

    ``builder(parameters, layers, technology, variant)`` returns the usual
    ``{ok, errors, warnings, parameters, geometry}`` result.  ``variant`` is
    the selected routing variant when the definition lists ``structures``,
    otherwise the model id.  Optional definition keys: ``structures``,
    ``emx_metrics``, ``default_layers`` (role -> position from the top metal,
    1 = top) and ``role_labels``.  See docs/STRUCTURE_PACKS.md.
    """
    if not isinstance(definition, dict) or not NAME.fullmatch(str(definition.get('id', ''))) or not callable(builder):
        raise ValueError('Invalid model definition or geometry builder.')
    if _model_source(definition['id']) is not None:
        raise ValueError('This model is already registered.')
    if not all(key in definition for key in ('title', 'description', 'defaults', 'parameter_schema', 'layer_roles')):
        raise ValueError('Model metadata is incomplete.')
    if not isinstance(definition['parameter_schema'], list) or not all(
            isinstance(field, dict) and isinstance(field.get('name'), str) for field in definition['parameter_schema']):
        raise ValueError('parameter_schema must be a list of named fields.')
    structures = definition.get('structures')
    if structures is not None and (not isinstance(structures, list) or not structures or not all(
            isinstance(item, dict) and NAME.fullmatch(str(item.get('id', ''))) for item in structures)):
        raise ValueError('structures must be a non-empty list of {id, title, description}.')
    _validate_definition(definition)
    _public_definition(definition)  # must not raise once listed
    EXTRA_MODELS.append(copy.deepcopy(definition))
    MODEL_BUILDERS[definition['id']] = builder


def unregister_model(model):
    """Remove a pack-registered model (built-in models cannot be removed)."""
    EXTRA_MODELS[:] = [item for item in EXTRA_MODELS if item['id'] != model]
    MODEL_BUILDERS.pop(model, None)


def layer_defaults(tech, model=DEFAULT_MODEL):
    stack = sorted(tech['layers'], key=lambda item: item['order'])
    if not stack:
        raise ValueError('No identified PDK metal layers are available.')
    count = len(stack)
    result = {'body': stack[-min(2, count)]['name'], 'crossing': stack[-min(3, count)]['name'],
              'secondary': stack[-min(2, count)]['name'],
              'pgs': stack[0]['name'], 'ring': stack[-1]['name'],
              'ground_route': stack[min(1, count - 1)]['name'], 'tap': stack[-min(4, count)]['name']}
    source = _model_source(model) or {}
    for role, position in (source.get('default_layers') or {}).items():
        if isinstance(position, int) and not isinstance(position, bool) and position >= 1:
            result[role] = stack[-min(position, count)]['name']
    return result


def normalize_design(design):
    """Pure one-model normalization, including one-time legacy ring migration."""
    if not isinstance(design, dict):
        raise ValueError('Design parameters must be a JSON object.')
    model = design.get('model', LEGACY_MODEL)
    definition = next((item for item in models() if item['id'] == model), None)
    if definition is None:
        raise ValueError('Unsupported body model %r. If it comes from a structure pack, load that pack first.' % (model,))
    supplied = design.get('parameters', {})
    layers = design.get('layers', {})
    if not isinstance(supplied, dict) or not isinstance(layers, dict):
        raise ValueError('Parameters and metal selections must be objects.')
    parameters = copy.deepcopy(definition['defaults'])
    names = {field['name'] for field in definition['parameter_schema']}
    parameters.update({name: copy.deepcopy(value) for name, value in supplied.items() if name in names})
    if 'ring_extent' not in supplied and 'ring_gap' in supplied and geometry.shielding_state(parameters)[1]:
        if not {'inner_width', 'gap', 'outer_width'} <= set(parameters):
            raise ValueError('Legacy ring-gap migration is only supported for two-winding coplanar models.')
        try:
            extent = (float(parameters['inner_diameter']) / 2 + float(parameters['inner_width']) +
                      float(parameters['gap']) + float(parameters['outer_width']) + float(supplied['ring_gap']))
            if not math.isfinite(extent):
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            raise ValueError('The legacy ring dimensions are invalid.')
        parameters['ring_extent'] = extent
    # Save one body only. Obsolete parameters from another model never survive.
    parameters.pop('ring_gap', None)
    variants = [item['id'] for item in definition.get('structures') or []]
    structure = design.get('structure', variants[0]) if variants else 'layout'
    if variants and structure not in variants:
        raise ValueError('Unsupported routing variant for this model.')
    return {'model': model, 'structure': structure, 'library': design.get('library', ''),
            'cell': design.get('cell', ''), 'parameters': parameters,
            'layers': {role: copy.deepcopy(layers[role]) for role in definition['layer_roles'] if role in layers}}


def register_library(name, workspace, cds_lib=None):
    from cadence import configure_context, job_dir
    from runtime_config import permitted_library, public_runtime
    global LIBRARY_SCOPE, RUNTIME
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError('Invalid library name.')
    workspace = Path(workspace).expanduser().resolve()
    mapping = Path(cds_lib).expanduser() if cds_lib else workspace / 'cds.lib'
    if not mapping.is_absolute():
        mapping = workspace / mapping
    mapping = mapping.resolve()
    if not workspace.is_dir() or not mapping.is_file():
        raise ValueError('Select an existing workspace and library mapping file.')
    with CONTEXT_LOCK:
        same_source = (LAUNCH_CONTEXT and LAUNCH_CONTEXT.get('library') == name and
                       Path(LAUNCH_CONTEXT.get('workspace', '')).expanduser().resolve() == workspace)
        snapshot = configure_context(context=LAUNCH_CONTEXT if same_source else None,
                                     workspace=str(workspace), cds_lib=str(mapping))
        scope = (str(workspace), str(mapping))
        if LIBRARY_SCOPE != scope:
            LIBRARIES.clear()
            LIBRARY_SCOPE = scope
        RUNTIME = snapshot
        tech = load_technology(name, str(workspace), job_dir('technology'), cds_lib=str(mapping))
        if same_source:
            if (LAUNCH_CONTEXT.get('library_path') and
                    Path(tech['library_path']).resolve() != Path(LAUNCH_CONTEXT['library_path']).resolve()):
                raise ValueError('The invoking editor and batch process resolved different library paths.')
            if LAUNCH_CONTEXT.get('technology') and LAUNCH_CONTEXT['technology'] != tech['technology']:
                raise ValueError('The invoking editor and batch process resolved different attached technologies.')
        permission = {'library': name, 'library_path': tech['library_path'],
                      'workspace': str(workspace), 'cds_lib': str(mapping)}
        permitted_library(tech['library_path'], str(workspace), tech,
                          context=LAUNCH_CONTEXT if same_source else permission)
        tech['_permission_context'] = permission
        definitions = models()
        entry = {'name': name, 'workspace': str(workspace), 'cds_lib': str(mapping),
                 'technology': tech['technology'], 'layers': tech['layers'],
                 'defaults': copy.deepcopy(geometry.DEFAULTS),
                 'default_layers': layer_defaults(tech),
                 'model_default_layers': {item['id']: layer_defaults(tech, item['id']) for item in definitions},
                 'available': True, 'grid': drawing_grid(tech), 'evidence': tech.get('evidence'),
                 'warnings': missing_rule_warnings(tech),
                 'runtime': public_runtime(snapshot), '_tech': tech}
        if len(tech['layers']) < 3:
            entry['warnings'].append({'part': 'technology', 'code': 'metal_stack_small',
                'message': 'Fewer than three metals were identified. All identified metals remain selectable; the chosen model and shielding must fit the actual layer stack.',
                'fields': ['library']})
        LIBRARIES[name] = entry
        public = public_library(entry)
        public['libraries'] = [public_library(item) for item in LIBRARIES.values()]
        return public


def public_library(entry):
    return {key: copy.deepcopy(value) for key, value in entry.items() if not key.startswith('_')}


def load_context(launch_context=None, workspace=None, cds_lib=None):
    from cadence import configure_context, list_libraries
    from runtime_config import permitted_library, public_runtime
    global LIBRARY_SCOPE, RUNTIME, LAUNCH_CONTEXT
    source = copy.deepcopy(launch_context)
    if source is not None:
        if not isinstance(source, dict) or source.get('schema') != 'tfgen.layout.context.v1':
            raise ValueError('Unsupported layout launch context.')
    snapshot = configure_context(context=source, workspace=workspace, cds_lib=cds_lib)
    actual_workspace, mapping = snapshot['workspace'], snapshot['cds_lib']
    with CONTEXT_LOCK:
        scope = (actual_workspace, mapping)
        if LIBRARY_SCOPE != scope:
            LIBRARIES.clear()
            LIBRARY_SCOPE = scope
        RUNTIME, LAUNCH_CONTEXT = snapshot, source
    if source:
        sources = [(source.get('library', ''), actual_workspace, mapping)]
    else:
        sources = []
        for item in list_libraries(actual_workspace, cds_lib=mapping):
            if item.get('is_technology') or not item.get('technology') or not NAME.fullmatch(item.get('name', '')):
                continue
            identity = dict(item, library=item['name'], cds_lib=mapping)
            try:
                permitted_library(item['library_path'], actual_workspace, identity)
            except ValueError:
                continue
            sources.append((item['name'], actual_workspace, mapping))
        if not sources:
            raise ValueError('No attached design libraries were found in the current mapping. Load another workspace or check TFGEN_WORKSPACE/TFGEN_CDS_LIB in flux_config.sh.')
    for name, workspace, mapping in sources:
        try:
            register_library(name, workspace, mapping)
        except Exception as exc:
            LIBRARIES[name] = {'name': name, 'workspace': workspace, 'cds_lib': mapping,
                               'available': False, 'layers': [], 'error': str(exc)}
    return {'version': '1.0.0', 'libraries': [public_library(entry) for entry in LIBRARIES.values()],
            'models': models(), 'structure_packs': structure_pack_report(),
            'defaults': copy.deepcopy(geometry.DEFAULTS), 'parameter_schema': copy.deepcopy(geometry.PARAMETER_SCHEMA),
            'presets': preset_list(), 'selected_library': source.get('library') if source else None,
            'source_context': public_source_context(source), 'runtime': public_runtime(snapshot)}


def load_file_context(filename):
    """Register an offline profile. Its geometry can be exported as JSON only."""
    from technology import load_profile
    tech = load_profile(filename)
    name = tech.get('library', 'Offline')
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError('Technology library must be a portable name')
    definitions = models()
    entry = dict(name=name, technology=tech.get('technology', name), available=True,
                 offline=True, layers=tech['layers'], defaults=copy.deepcopy(geometry.DEFAULTS),
                 default_layers=layer_defaults(tech),
                 model_default_layers={item['id']: layer_defaults(tech, item['id']) for item in definitions},
                 grid=drawing_grid(tech), warnings=missing_rule_warnings(tech), _tech=tech)
    with CONTEXT_LOCK:
        LIBRARIES[name] = entry
    return dict(version='1.0.0', libraries=[public_library(entry)], models=definitions,
                structure_packs=structure_pack_report(), defaults=copy.deepcopy(geometry.DEFAULTS),
                parameter_schema=copy.deepcopy(geometry.PARAMETER_SCHEMA), presets=[], selected_library=name)


def _preview_entry(normalized, entry):
    if not entry or not entry.get('available'):
        raise ValueError('Load an available target library first.')
    layers = dict(entry['model_default_layers'].get(normalized['model']) or layer_defaults(entry['_tech'], normalized['model']))
    layers.update(normalized['layers'])
    source = _model_source(normalized['model']) or {}
    builder = MODEL_BUILDERS.get(normalized['model'], generate_model)
    variant = normalized['structure'] if source.get('structures') else normalized['model']
    result = builder(copy.deepcopy(normalized['parameters']), layers, entry['_tech'], variant)
    if not isinstance(result, dict) or not isinstance(result.get('geometry'), dict):
        raise ValueError('The %s geometry builder returned an invalid result.' % normalized['model'])
    selected = {metal for role, metal in layers.items() if geometry.shielding_role_active(role, normalized['parameters'])}
    for warning in entry.get('warnings', missing_rule_warnings(entry['_tech'], selected)):
        if warning.get('layer') and warning['layer'] not in selected:
            continue
        if warning not in result.setdefault('warnings', []):
            result['warnings'].append(copy.deepcopy(warning))
    return result


def preview(design):
    normalized = normalize_design(design)
    return _preview_entry(normalized, LIBRARIES.get(normalized['library']))


def capture_generation_context(design, expected_library=None):
    """Capture a model and exact native source without blocking the Tk thread.

    The private snapshot may be held in memory by a campaign. Its native
    environment must never be persisted in public campaign settings or logs.
    """
    normalized = normalize_design(design)
    if not CONTEXT_LOCK.acquire(blocking=False):
        raise RuntimeError('Technology loading is in progress. Retry after it finishes.')
    try:
        entry = LIBRARIES.get(normalized['library'])
        if not entry or not entry.get('available') or entry.get('offline') or not RUNTIME:
            raise ValueError('Load an available target library before starting EMX.')
        identity = dict(library=normalized['library'], workspace=entry['workspace'],
                        cds_lib=entry['cds_lib'], technology=entry['technology'],
                        library_path=entry['_tech']['library_path'])
        for key in ('workspace', 'cds_lib'):
            if Path(RUNTIME[key]).resolve() != Path(identity[key]).resolve():
                raise ValueError('The selected library and native runtime no longer match. Reload the library.')
        if expected_library is not None:
            if expected_library.get('name', normalized['library']) != normalized['library']:
                raise ValueError('The layout source changed. Apply the current library design again.')
            for key in ('workspace', 'cds_lib', 'technology'):
                if expected_library.get(key) is not None and expected_library[key] != identity[key]:
                    raise ValueError('The layout source changed. Apply the current library design again.')
            expected_path = expected_library.get('library_path')
            if expected_path and Path(expected_path).resolve() != Path(identity['library_path']).resolve():
                raise ValueError('The layout library binding changed. Reload and apply it again.')
        definition = next(item for item in models() if item['id'] == normalized['model'])
        definition['emx_metrics'] = list(definition.get('emx_metrics') or ['L', 'Q'])
        layers = dict(entry['model_default_layers'].get(normalized['model']) or
                      layer_defaults(entry['_tech'], normalized['model']))
        layers.update(normalized['layers'])
        normalized['layers'] = {role: layers[role] for role in definition['layer_roles'] if role in layers}
        fields = []
        for field in definition['parameter_schema']:
            field = copy.deepcopy(field)
            name = field['name']
            kind = field.get('type') or ('int' if field.get('integer') else
                    'enum' if field.get('options') else
                    'bool' if isinstance(normalized['parameters'].get(name), bool) else 'float')
            field['type'] = {'number': 'float', 'integer': 'int', 'boolean': 'bool'}.get(kind, kind)
            if field.get('integer'):
                field['type'] = 'int'
            field['current'] = copy.deepcopy(normalized['parameters'].get(name))
            fields.append(field)
        if definition.get('structures'):
            fields.append(dict(name='structure', label='Lead routing', type='enum',
                               options=[item['id'] for item in definition['structures']], current=normalized['structure']))
        for role in definition['layer_roles']:
            fields.append(dict(name='layers.' + role, label=role.replace('_', ' ').title() + ' metal',
                               part='layers', type='enum', options=[item['name'] for item in entry['layers']],
                               current=normalized['layers'].get(role)))
        return copy.deepcopy(dict(schema='flux.emx.source.v1', design=normalized,
                                  definition=definition, fields=fields, library=entry,
                                  runtime=RUNTIME, identity=identity))
    finally:
        CONTEXT_LOCK.release()


def _frozen_entry(source_context, library):
    if not isinstance(source_context, dict) or source_context.get('schema') != 'flux.emx.source.v1':
        raise ValueError('A captured layout source is required for this campaign.')
    if source_context.get('identity', {}).get('library') != library:
        raise ValueError('The candidate library differs from the frozen campaign source.')
    entry = source_context.get('library')
    if not entry or not entry.get('available'):
        raise ValueError('The frozen design library is unavailable.')
    return entry


def preview_in_context(design, source_context):
    normalized = normalize_design(design)
    if normalized['model'] != source_context.get('design', {}).get('model'):
        raise ValueError('Candidates must keep the captured body model.')
    return _preview_entry(normalized, _frozen_entry(source_context, normalized['library']))


def generate_in_context(design, source_context, allow_existing_cell=False):
    """Write one new cell using only the campaign's captured tech and runtime."""
    from runtime_config import permitted_library
    normalized = normalize_design(design)
    entry = _frozen_entry(source_context, normalized['library'])
    with GENERATE_LOCK:
        result = preview_in_context(normalized, source_context)
        if not result['ok']:
            return result
        name = normalized['cell']
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError('Candidate cell names must be valid and at most 80 characters.')
        library_path = permitted_library(entry['_tech']['library_path'], entry['workspace'], entry['_tech'],
                                         context=entry['_tech'].get('_permission_context'))
        if (library_path / name).exists() and (not allow_existing_cell or (library_path / name / 'layout').exists()):
            raise ValueError('The candidate cell/layout already exists. Existing layout views are never overwritten.')
        options = dict(runtime=source_context['runtime'])
        if allow_existing_cell:
            options['allow_existing_cell'] = True
        return write_cell(normalized['library'], name, entry['workspace'], entry['_tech'], result,
                          normalized, **options)


def prepare_emx_job(generated, overrides=None, run_dir=None, source_context=None):
    """Future UI/optimizer entry: extract the saved generation, never a draft.

    The returned job can be passed to emx_adapter.run_job(job, RUNTIME, cancel).
    Process/map discovery remains an exact attached-technology association.
    """
    import hashlib
    import uuid
    import emx_adapter
    if not isinstance(generated, dict) or not generated.get('ok'):
        raise ValueError('Generate and verify a new layout before preparing EMX.')
    library, cell = generated.get('library'), generated.get('cell')
    entry = _frozen_entry(source_context, library) if source_context is not None else LIBRARIES.get(library)
    if not entry or not entry.get('available') or not NAME.fullmatch(cell or ''):
        raise ValueError('Load the exact generated layout library before preparing EMX.')
    saved = Path(generated.get('run_dir', '')) / 'geometry.json'
    if saved.is_symlink() or not saved.is_file():
        raise ValueError('The saved generation geometry contract is unavailable.')
    geometry = json.loads(saved.read_text(encoding='utf-8'))['geometry']
    digest = hashlib.sha256(json.dumps(geometry, sort_keys=True).encode()).hexdigest()
    if digest != generated.get('geometry_hash'):
        raise ValueError('The saved geometry differs from the verified generation.')
    tech = entry['_tech']
    source = dict(library=library, cell=cell, view='layout', technology=entry['technology'],
                  workspace=entry['workspace'], cds_lib=entry['cds_lib'], library_path=tech['library_path'])
    runtime = source_context['runtime'] if source_context is not None else RUNTIME
    config = emx_adapter.discover_config(entry['technology'], runtime, overrides)
    if run_dir is None:
        run_dir = Path(entry['workspace']) / (ROOT.name + '_runtime') / 'emx_runs' / uuid.uuid4().hex
    return emx_adapter.prepare_job(source, geometry, config, run_dir)


def preset_path(name):
    if not isinstance(name, str) or len(name) > 64 or not name or not re.fullmatch(r'[\w \-]+', name):
        raise ValueError('Preset names support letters, digits, spaces, underscores and hyphens, up to 64 characters.')
    PRESETS.mkdir(exist_ok=True)
    return PRESETS / (name + '.json')


def preset_list():
    return [{'name': file.stem, 'updated': datetime.datetime.fromtimestamp(file.stat().st_mtime).isoformat(timespec='seconds')}
            for file in sorted(PRESETS.glob('*.json'))]


def generate(design):
    from runtime_config import permitted_library
    if not GENERATE_LOCK.acquire(blocking=False):
        raise RuntimeError('Another cell is being generated. Wait for it to finish.')
    try:
        normalized = normalize_design(design)
        result = preview(normalized)
        if not result['ok']:
            return result
        entry = LIBRARIES[normalized['library']]
        if entry.get('offline'):
            raise ValueError('Offline technology supports JSON geometry export only. Connect a native workspace to write OA.')
        name = normalized['cell']
        if not isinstance(name, str) or not NAME.fullmatch(name):
            result['ok'] = False
            result['errors'].append({'part': 'cell', 'code': 'cell_name', 'message': 'Cell names must start with a letter and contain only letters, digits and underscores (up to 80 characters).', 'fields': ['cell']})
            return result
        try:
            library_path = permitted_library(entry['_tech']['library_path'], entry['workspace'],
                                             entry['_tech'], context=entry['_tech'].get('_permission_context'))
        except ValueError as exc:
            result['ok'] = False
            result['errors'].append({'part': 'library', 'code': 'protected_library', 'message': str(exc), 'fields': ['library']})
            return result
        if (library_path / name).exists():
            result['ok'] = False
            result['errors'].append({'part': 'cell', 'code': 'cell_exists', 'message': 'The target cell already exists. Choose a new cell name.', 'fields': ['cell']})
            return result
        return write_cell(normalized['library'], name, entry['workspace'], entry['_tech'], result, normalized)
    finally:
        GENERATE_LOCK.release()


def save_preset(name, design):
    normalized = normalize_design(design)
    file = preset_path(name)
    with PRESET_LOCK:
        if file.exists():
            backup = file.with_name(file.name + '.bak-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
            backup.write_bytes(file.read_bytes())
        temporary = file.with_suffix('.tmp')
        temporary.write_text(json.dumps({'schema': 'tfgen.design.v2', 'design': normalized}, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        temporary.replace(file)
    return {'ok': True, 'name': name, 'presets': preset_list()}


def load_preset(name):
    value = json.loads(preset_path(name).read_text(encoding='utf-8'))
    if not isinstance(value, dict) or value.get('schema') not in ('tfgen.design.v1', 'tfgen.design.v2'):
        raise ValueError('Unsupported parameter file version.')
    return {'schema': 'tfgen.design.v2', 'design': normalize_design(value.get('design'))}
