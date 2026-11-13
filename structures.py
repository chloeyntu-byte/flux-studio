"""Built-in winding model metadata and the parameter screen shared by packs.

The open-source edition ships one built-in structure: the symmetric
differential inductor (optional centre TAP).  Further structures are added at
run time as structure packs; see ``structure_packs.py`` and
``docs/STRUCTURE_PACKS.md``.
"""
import copy
import math


def _field(name, label, default, minimum, maximum, step=0.1, integer=False):
    field = {'name': name, 'label': label, 'part': 'body', 'default': default,
             'min': minimum, 'max': maximum, 'step': step, 'unit': 'um'}
    if integer:
        field.update({'integer': True, 'unit': 'turns'})
    return field


_INDUCTOR_SCHEMA = [
    _field('inner_diameter', 'Inner opening diameter', 100, 30, 1000),
    _field('width', 'Winding width', 8, 0.1, 100),
    _field('spacing', 'Turn spacing', 8, 0.1, 100),
    _field('turns', 'Turns', 2, 1, 8, 1, True),
    _field('leg_length', 'Lead extension', 45, 5, 500),
    _field('terminal_gap', 'Terminal edge gap', 12, 0.1, 200),
    {'name': 'center_tap', 'label': 'Add center TAP', 'part': 'body', 'type': 'boolean', 'default': False, 'import_default': True},
]
_DIFFERENTIAL_SCHEMA = _INDUCTOR_SCHEMA
_RULE_POLICY = {'name': 'rule_policy', 'label': 'PDK rule conflicts', 'part': 'body',
                'options': ['warn', 'strict'], 'default': 'warn', 'import_default': True}


def _definition(model_id, title, description, schema, roles):
    schema = copy.deepcopy(schema) + [copy.deepcopy(_RULE_POLICY)]
    return {'id': model_id, 'title': title, 'description': description,
            'defaults': {field['name']: field['default'] for field in schema},
            'parameter_schema': schema, 'layer_roles': roles}


MODEL_DEFINITIONS = [
    dict(_definition('differential_inductor', 'Differential inductor · Symmetric / optional TAP',
                     'Mirror-symmetric serial winding, alternating crossovers, optional true midpoint TAP.',
                     _DIFFERENTIAL_SCHEMA, ['body', 'crossing', 'tap']),
         emx_metrics=['L', 'Q'], default_layers={'body': 2, 'crossing': 3}),
]
_MODELS = {definition['id']: definition for definition in MODEL_DEFINITIONS}


def _error(part, code, message, fields=None):
    return {'part': part, 'code': code, 'message': message, 'fields': fields or []}


def screen_parameters(definition, parameters):
    """Merge defaults and range-check body fields; returns an empty result.

    Structure packs may call this before building their own geometry.  The
    returned ``result`` follows the common polygons/vias/ports contract.
    """
    p = copy.deepcopy(definition['defaults']) if definition else {}
    p.update(parameters or {})
    geometry = {'polygons': [], 'vias': [], 'ports': [], 'bounds': [[0, 0], [0, 0]], 'metrics': {}}
    result = {'ok': False, 'errors': [], 'warnings': [], 'geometry': geometry, 'parameters': p}
    if definition is None:
        result['errors'].append(_error('body', 'unsupported_model', 'Unknown winding model.', ['model']))
        return result
    for field in definition['parameter_schema']:
        name = field['name']
        if field.get('type') == 'boolean':
            if not isinstance(p[name], bool):
                result['errors'].append(_error('body', 'parameter_type', '%s must be true or false.' % name, [name]))
            continue
        if field.get('options'):
            if p[name] not in field['options']:
                result['errors'].append(_error('body', 'invalid_option', 'Unsupported %s option.' % name, [name]))
            continue
        try:
            if isinstance(p[name], bool):
                raise ValueError()
            value = float(p[name])
            if not math.isfinite(value) or not field['min'] <= value <= field['max']:
                raise ValueError()
            if field.get('integer') and (isinstance(p[name], bool) or value != int(value)):
                raise ValueError()
            p[name] = int(value) if field.get('integer') else value
        except (ValueError, TypeError, OverflowError):
            suffix = ' integer turns' if field.get('integer') else ' %s' % field.get('unit', 'um')
            result['errors'].append(_error('body', 'parameter_range',
                '%s must be between %s and %s%s.' % (field['label'], field['min'], field['max'], suffix), [name]))
    return result


def generate_model(parameters, layers, technology, model='differential_inductor'):
    """Screen parameters against the schema, then build a built-in model."""
    result = screen_parameters(_MODELS.get(model), parameters)
    if result['errors']:
        return result
    p = result['parameters']
    # The builder is loaded lazily so metadata can be consumed independently
    # of the winding-generation entry points.
    from passive_symmetric import generate_differential
    return generate_differential(p, layers, technology)


def generate_differential_inductor(parameters, layers, technology, structure='differential_inductor'):
    return generate_model(parameters, layers, technology, 'differential_inductor')
