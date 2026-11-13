"""Example structure pack for Flux Studio.

It shows the pack contract without adding new geometry: it registers a
centre-tapped preset of the built-in differential inductor with two lead
variants.  Copy this file into ``packs/`` (or point ``FLUX_PACKS`` at it) and
restart Flux; "Example · Centre-tapped inductor" then appears in the model list.

A real pack builds its own winding with ``flux.geometry.GeometryBuilder`` and
returns ``flux.geometry.postprocess_geometry(...)``; see docs/STRUCTURE_PACKS.md.
"""
import copy

PACK_NAME = 'example-tapped-inductor'
PACK_VERSION = '1.0.0'
FLUX_PACK_API = 1

MODEL_ID = 'example_tapped_inductor'


def register(flux):
    base = next(item for item in flux.builtin_definitions if item['id'] == 'differential_inductor')
    definition = copy.deepcopy(base)
    definition.update(
        id=MODEL_ID,
        title='Example · Centre-tapped inductor',
        description='Structure-pack example: the built-in differential inductor with TAP on and three turns.',
        structures=[{'id': 'compact', 'title': 'Compact leads', 'description': 'Terminal gap as entered.'},
                    {'id': 'wide', 'title': 'Wide leads', 'description': 'Terminal gap at least four winding widths.'}],
        emx_metrics=['L', 'Q'],
        default_layers={'body': 2, 'crossing': 3, 'tap': 4},
        role_labels={'tap': 'Centre-tap metal'})
    definition['defaults'].update(center_tap=True, turns=3)
    for field in definition['parameter_schema']:
        if field['name'] in definition['defaults']:
            field['default'] = definition['defaults'][field['name']]

    def build(parameters, layers, technology, variant):
        screened = flux.screen_parameters(definition, parameters)
        if screened['errors']:
            return screened
        values = screened['parameters']
        if variant == 'wide':
            values['terminal_gap'] = max(values['terminal_gap'], 4 * values['width'])
        result = flux.build_builtin(values, layers, technology, 'differential_inductor')
        result['geometry'].setdefault('metrics', {}).update(model=MODEL_ID, variant=variant)
        return result

    flux.register_model(definition, build)
