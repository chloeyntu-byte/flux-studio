"""Missing optional PDK rules warn; actual topology and native vias remain real.

Pure geometry checks use synthetic generic fixtures. No OA write, GUI,
technology installation, or fabricated via definition is involved.
"""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import geometry
import structures
import backend
import cadence
import runtime_config
from pack_support import EXAMPLE_MODEL, install_example_pack


OPTIONAL_RULES = ('min_width', 'min_spacing', 'min_area', 'max_width',
                  'min_diagonal_width', 'min_diagonal_spacing', 'min_same_net_spacing')
# Six useful generation configurations of the built-in differential inductor:
# single / multi turn, odd / even turn counts, with and without the centre TAP.
CASES = (
    ('single', 'differential_inductor', {'turns': 1}),
    ('multi', 'differential_inductor', {'turns': 2}),
    ('single_tap', 'differential_inductor', {'turns': 1, 'center_tap': True}),
    ('multi_tap', 'differential_inductor', {'turns': 3, 'center_tap': True}),
    ('even_tap', 'differential_inductor', {'turns': 2, 'center_tap': True}),
    ('odd', 'differential_inductor', {'turns': 3}),
)


def profile():
    return json.loads((Path(__file__).resolve().parent / 'fixtures' / 'generic_d.json')
                      .read_text(encoding='utf-8'))


def sparse_profile(remove=False):
    tech = profile()
    tech['constraint_groups'] = []
    for layer in tech['layers']:
        layer['order_verified'] = True
        for key in OPTIONAL_RULES:
            if remove:
                layer.pop(key, None)
            else:
                layer[key] = None
        layer['spacing_tables'] = []
    return tech


def roles(technology, model):
    metals = sorted(technology['layers'], key=lambda layer: layer['order'])
    return dict(body=metals[-2]['name'], crossing=metals[-3]['name'], tap=metals[-4]['name'],
                pgs=metals[0]['name'], ring=metals[-1]['name'], ground_route=metals[1]['name'])


def generate(case, technology, parameters=None, layers=None):
    label, model, defaults = case
    values = dict(defaults)
    values.update(parameters or {})
    layers = layers or roles(technology, model)
    return structures.generate_model(values, layers, technology, model)


def unavailable_warnings(result):
    statuses = ('missing', 'unknown', 'unavailable', 'unverified', 'fallback',
                'not exposed', 'not published', 'not available', 'not verified')
    return [warning for warning in result['warnings']
            if any(status in (warning['code'] + ' ' + warning['message']).lower() for status in statuses)
            and any(subject in (warning['code'] + ' ' + warning['message']).lower()
                    for subject in ('rule', 'grid', 'order'))]


class RuleGapGenerationTests(unittest.TestCase):
    def assert_preview(self, result):
        self.assertTrue(result['ok'], result['errors'])
        self.assertTrue(result['geometry']['polygons'])
        self.assertTrue(result['geometry']['ports'])
        self.assertEqual(result['errors'], [])
        self.assertTrue(unavailable_warnings(result), result['warnings'])
        for warning in unavailable_warnings(result):
            self.assertTrue({'part', 'code', 'message', 'fields'} <= set(warning), warning)
            self.assertIsInstance(warning['fields'], list)
        self.assertEqual(result['geometry']['metrics']['winding_components'], {'L': 1})

    def test_six_configurations_preview_when_optional_rules_are_absent_or_none(self):
        for remove in (False, True):
            tech = sparse_profile(remove)
            native_names = {via['name'] for via in tech['vias']}
            for case in CASES:
                with self.subTest(configuration=case[0], removed_keys=remove):
                    result = generate(case, tech)
                    self.assert_preview(result)
                    for via in result['geometry']['vias']:
                        self.assertIn(via['via_def'], native_names)
                        self.assertGreater(via['rows'] * via['columns'], 0)

    def test_missing_manufacturing_grid_uses_one_reported_working_grid(self):
        for missing_value in (None, 'absent'):
            tech = sparse_profile()
            tech['manufacturing_grid'] = None
            if missing_value == 'absent':
                tech.pop('grid', None)
            else:
                tech['grid'] = missing_value
            for case in CASES:
                with self.subTest(configuration=case[0], grid=missing_value):
                    result = generate(case, tech)
                    self.assert_preview(result)
                    grid = result['geometry']['metrics']['grid']
                    self.assertGreater(grid, 0)
                    self.assertTrue(any('grid' in (warning['code'] + ' ' + warning['message']).lower()
                                        for warning in unavailable_warnings(result)))
                    # Revalidation must use the same fallback as construction.
                    errors, components = geometry.validate_geometry(result['geometry'], tech)
                    self.assertEqual(errors, [])
                    self.assertTrue(all(count == 1 for count in components.values()))

    def test_verified_fine_grid_is_preserved_in_construction_and_revalidation(self):
        tech = sparse_profile()
        tech['grid'] = tech['manufacturing_grid'] = .00005
        extent = 103.00005
        for case in CASES:
            with self.subTest(configuration=case[0]):
                result = generate(case, tech, {'ring_extent': extent})
                self.assert_preview(result)
                self.assertEqual(result['geometry']['metrics']['grid'], tech['grid'])
                # This inner ring edge distinguishes the real fine grid from
                # the former silent 0.0001-um drawing-grid clamp.
                ring_coordinates = [abs(coordinate) for poly in result['geometry']['polygons']
                                    if poly['part'] == 'ring' for point in poly['points'] for coordinate in point]
                self.assertIn(extent, ring_coordinates)
                errors, components = geometry.validate_geometry(result['geometry'], tech)
                self.assertEqual(errors, [])
                self.assertTrue(all(count == 1 for count in components.values()))

    def test_nonfinite_optional_values_and_none_spacing_tables_are_advisory(self):
        for unavailable in (float('nan'), float('inf')):
            tech = sparse_profile()
            for layer in tech['layers']:
                for key in OPTIONAL_RULES:
                    layer[key] = unavailable
                layer['spacing_tables'] = None
            for case in CASES:
                with self.subTest(configuration=case[0], unavailable=unavailable):
                    self.assert_preview(generate(case, tech))

    def test_known_width_spacing_and_diagonal_limits_still_reject_violations(self):
        checks = (('min_width', 20.0), ('max_width', 5.0),
                  ('min_diagonal_width', 20.0), ('min_spacing', 20.0),
                  ('min_diagonal_spacing', 20.0), ('min_same_net_spacing', 20.0))
        for rule_name, value in checks:
            for case in CASES:
                tech = sparse_profile()
                layer_roles = roles(tech, case[1])
                selected = next(layer for layer in tech['layers'] if layer['name'] == layer_roles['body'])
                selected[rule_name] = value
                with self.subTest(configuration=case[0], known_rule=rule_name):
                    result = generate(case, tech, {'rule_policy': 'strict'}, layers=layer_roles)
                    self.assertFalse(result['ok'])
                    self.assertTrue(result['errors'])
                    # A larger real minWidth also enlarges native landing pads,
                    # which may exhaust the crossover room first.
                    self.assertTrue(any('width' in error['code'] or 'spacing' in error['code']
                                        or error['code'] == 'crossover_room'
                                        for error in result['errors']), result['errors'])

    def test_numeric_rule_warns_but_real_short_remains_an_error(self):
        tech = sparse_profile()
        case = CASES[0]  # single-turn: no via fit needs to be relaxed
        layer_roles = roles(tech, case[1])
        selected = next(layer for layer in tech['layers'] if layer['name'] == layer_roles['body'])
        selected['min_width'] = 9.0
        result = generate(case, tech, {'rule_policy': 'warn'}, layers=layer_roles)
        self.assertTrue(result['ok'], result['errors'])
        self.assertTrue(any('width' in w['code'] for w in result['warnings']), result['warnings'])
        masks = copy.deepcopy(result['geometry'])
        wire = copy.deepcopy(next(p for p in masks['polygons'] if p['net'] == 'L' and p['part'] == 'body'))
        wire['net'] = 'S'
        masks['polygons'].append(wire)
        warnings = []
        errors, _ = geometry.validate_geometry(masks, tech, rule_policy='warn', warnings=warnings)
        self.assertIn('same_layer_short', {error['code'] for error in errors})

    def test_known_spacing_table_is_not_lost_when_base_rule_is_missing(self):
        tech = sparse_profile()
        layer_roles = roles(tech, 'differential_inductor')
        selected = next(layer for layer in tech['layers'] if layer['name'] == layer_roles['body'])
        selected['spacing_tables'] = [{'axes': ['width', 'length'], 'default': None,
                                       'entries': [{'threshold': [5.0, 100.0], 'value': 20.0}]}]
        result = generate(CASES[0], tech, {'rule_policy': 'strict'}, layers=layer_roles)
        self.assertFalse(result['ok'])
        self.assertTrue(any('spacing' in error['code'] for error in result['errors']), result['errors'])

    def test_known_area_rule_controls_native_landing_without_inventing_other_rules(self):
        tech = sparse_profile()
        for layer in tech['layers']:
            if layer['name'] in ('M9', 'AP'):
                layer['min_area'] = 180.0
        builder = geometry.GeometryBuilder({}, roles(tech, 'differential_inductor'), tech)
        landing = builder.add_via_stack([0, 0], 'M9', 'AP', 10.0, 'crossing', 'L')
        self.assertIsNotNone(landing)
        self.assertEqual(builder.errors, [])
        for polygon in builder.geometry['polygons']:
            box = geometry._bbox(polygon['points'])
            self.assertGreaterEqual((box[2] - box[0]) * (box[3] - box[1]), 180.0)

    def test_missing_optional_array_rule_uses_the_real_native_default_spacing(self):
        tech = sparse_profile()
        for via in tech['vias']:
            via['array_cut_spacing'] = None
        result = generate(CASES[1], tech)
        self.assert_preview(result)
        self.assertTrue(result['geometry']['vias'])
        definitions = {via['name']: via for via in tech['vias']}
        for via in result['geometry']['vias']:
            self.assertEqual(via['cut_spacing'], definitions[via['via_def']]['cut_spacing'])

    def test_missing_native_vias_block_only_configurations_that_need_them(self):
        tech = sparse_profile()
        tech['vias'] = []
        self.assert_preview(generate(CASES[0], tech))
        for case in CASES[1:]:
            with self.subTest(configuration=case[0]):
                result = generate(case, tech)
                self.assertFalse(result['ok'])
                self.assertTrue(any('via' in error['code'] for error in result['errors']), result['errors'])
                self.assertEqual(result['geometry']['vias'], [])

    def test_incomplete_native_geometry_is_rejected_without_type_errors_or_fake_arrays(self):
        invalid = (('cut_size', None), ('cut_size', [None, .1]),
                   ('cut_spacing', None), ('cut_spacing', [float('nan'), .1]),
                   ('lower_enclosure', None), ('upper_enclosure', .1))
        for key, value in invalid:
            tech = sparse_profile()
            for via in tech['vias']:
                via[key] = value
            with self.subTest(field=key, value=value):
                result = generate(CASES[1], tech)
                self.assertFalse(result['ok'])
                self.assertTrue(any('via' in error['code'] for error in result['errors']), result['errors'])
                self.assertEqual(result['geometry']['vias'], [])

    def test_unknown_rule_warnings_do_not_hide_true_short_disconnection_or_via_coverage(self):
        tech = sparse_profile()
        result = generate(CASES[1], tech)
        self.assert_preview(result)
        for fault in ('short', 'disconnection', 'via_coverage'):
            masks = copy.deepcopy(result['geometry'])
            if fault == 'short':
                wire = copy.deepcopy(next(poly for poly in masks['polygons'] if poly['net'] == 'L' and poly['part'] == 'body'))
                wire['net'] = 'S'
                masks['polygons'].append(wire)
            elif fault == 'disconnection':
                masks['vias'].pop(0)
            else:
                masks['vias'][0]['origin'] = [1000.0, 1000.0]
            with self.subTest(fault=fault):
                errors, components = geometry.validate_geometry(masks, tech)
                expected = {'short': 'same_layer_short', 'disconnection': 'disconnected_winding',
                            'via_coverage': 'via_coverage'}[fault]
                self.assertIn(expected, {error['code'] for error in errors}, errors)

    def test_unknown_spacing_does_not_call_positive_grid_clearance_a_physical_short(self):
        tech = sparse_profile()
        grid = tech['grid']
        masks = {'polygons': [
            {'points': geometry._rect([0, 0, 1, 1]), 'layer': 'M9', 'part': 'body', 'net': 'P'},
            {'points': geometry._rect([1 + grid, 0, 2 + grid, 1]), 'layer': 'M9', 'part': 'body', 'net': 'S'}],
            'vias': [], 'ports': [], 'metrics': {'expected_windings': ['P', 'S']}}
        errors, components = geometry.validate_geometry(masks, tech)
        self.assertEqual(errors, [])
        self.assertEqual(components, {'P': 1, 'S': 1})
        masks['polygons'][1]['points'] = geometry._rect([1, 0, 2, 1])
        errors, components = geometry.validate_geometry(masks, tech)
        self.assertIn('same_layer_short', {error['code'] for error in errors})

    def test_unknown_pgs_order_is_advisory_but_known_order_violation_is_not(self):
        for verified in (False, True):
            tech = sparse_profile()
            layer_roles = roles(tech, 'differential_inductor')
            pgs = next(layer for layer in tech['layers'] if layer['name'] == layer_roles['pgs'])
            pgs['order'] = 100
            pgs['order_verified'] = verified
            result = generate(CASES[0], tech, layers=layer_roles)
            if verified:
                self.assertFalse(result['ok'])
                self.assertIn('pgs_layer_order', {error['code'] for error in result['errors']})
            else:
                self.assert_preview(result)


class BackendRuleGapTests(unittest.TestCase):
    """Real backend and permission checks with no native reads or OA writes."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='flux-rule-gap-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "designer's workspace with spaces"
        self.workspace.mkdir()
        self.library = self.root / 'designs' / 'RuleGapDesign'
        self.library.mkdir(parents=True)
        self.technology_path = self.root / 'pdk' / 'RuleGapTechnology'
        self.technology_path.mkdir(parents=True)
        self.mapping = self.workspace / 'cds.lib'
        self.mapping.write_text('DEFINE RuleGapDesign ../designs/RuleGapDesign\n'
                                'DEFINE RuleGapTechnology ../pdk/RuleGapTechnology\n', encoding='utf-8')
        self.runtime = {'workspace': str(self.workspace), 'cds_lib': str(self.mapping),
                        'virtuoso': str(self.root / 'cadence' / 'tools' / 'bin' / 'virtuoso'),
                        'user': 'design_fixture', 'environment': {}, 'env_file': None,
                        'source': 'test fixture', 'helper_dirs': []}
        for module, name, value in (
                (backend, 'LIBRARIES', {}), (backend, 'LIBRARY_SCOPE', None),
                (backend, 'RUNTIME', None), (backend, 'LAUNCH_CONTEXT', None),
                (runtime_config, 'LAST_RUNTIME', None)):
            patcher = patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict(os.environ, {'TFGEN_DESIGN_ROOT': str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, replacement in (
                ('configure_context', lambda **kwargs: copy.deepcopy(self.runtime)),
                ('job_dir', lambda label: self.root / label)):
            patcher = patch.object(cadence, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(backend, 'load_technology')
        self.native_read = patcher.start()
        self.addCleanup(patcher.stop)

    def register(self, technology):
        technology = copy.deepcopy(technology)
        technology.update(library='RuleGapDesign', library_path=str(self.library),
                          technology='RuleGapTechnology', technology_path=str(self.technology_path),
                          cds_lib=str(self.mapping))
        self.native_read.return_value = technology
        return backend.register_library('RuleGapDesign', str(self.workspace), str(self.mapping))

    def design(self, case):
        label, model, parameters = case
        return {'library': 'RuleGapDesign', 'cell': 'RuleGap_' + label, 'model': model,
                'structure': 'layout', 'parameters': copy.deepcopy(parameters)}

    def test_one_or_two_identified_metals_register_every_model_layer_default(self):
        install_example_pack(self)
        model_ids = {'differential_inductor', EXAMPLE_MODEL}
        for count in (1, 2):
            tech = sparse_profile()
            tech['layers'] = tech['layers'][:count]
            tech['vias'] = []
            selected = {layer['name'] for layer in tech['layers']}
            with self.subTest(identified_metals=count):
                entry = self.register(tech)
                self.assertTrue(entry['available'])
                self.assertTrue(any(warning['code'] == 'metal_stack_small' for warning in entry['warnings']))
                for definition in backend.models():
                    if definition['id'] not in model_ids:
                        continue
                    defaults = backend.layer_defaults(tech, definition['id'])
                    self.assertEqual(defaults, entry['model_default_layers'][definition['id']])
                    self.assertTrue(set(definition['layer_roles']) <= set(defaults))
                    self.assertTrue(all(isinstance(name, str) and name and name in selected
                                        for name in defaults.values()))
                self.assertTrue(model_ids <= set(entry['model_default_layers']))

    def test_missing_optional_rules_reach_backend_preview_with_warnings(self):
        self.register(sparse_profile())
        for case in CASES:
            with self.subTest(configuration=case[0]):
                result = backend.preview(self.design(case))
                self.assertTrue(result['ok'], result['errors'])
                self.assertTrue(result['geometry']['polygons'])
                self.assertTrue(unavailable_warnings(result), result['warnings'])

    def test_warnings_allow_writer_and_preserve_exact_permission_binding(self):
        self.register(sparse_profile())
        with patch.object(backend, 'write_cell', side_effect=lambda *arguments: arguments[4]) as writer:
            for case in CASES:
                with self.subTest(configuration=case[0]):
                    design = self.design(case)
                    preview = backend.preview(design)
                    result = backend.generate(design)
                    self.assertTrue(result['ok'], result['errors'])
                    arguments = writer.call_args.args
                    self.assertIs(result, arguments[4])
                    self.assertEqual(result['warnings'], preview['warnings'])
                    self.assertTrue(unavailable_warnings(result))
                    self.assertEqual(arguments[:3], ('RuleGapDesign', design['cell'], str(self.workspace)))
                    self.assertEqual(arguments[3]['library_path'], str(self.library))
                    self.assertEqual(arguments[3]['technology_path'], str(self.technology_path))
                    self.assertEqual(arguments[3]['_permission_context'],
                                     {'library': 'RuleGapDesign', 'library_path': str(self.library),
                                      'workspace': str(self.workspace), 'cds_lib': str(self.mapping)})
                    self.assertEqual(arguments[5], backend.normalize_design(design))
                    self.assertFalse((self.library / design['cell']).exists())
            self.assertEqual(writer.call_count, len(CASES))

    def test_real_short_or_missing_vias_never_call_writer(self):
        with patch.object(backend, 'write_cell', side_effect=AssertionError('Physical errors must block native writes')) as writer:
            self.register(sparse_profile())
            design = self.design(CASES[0])
            # The two downward terminals physically cross a same-layer ring.
            design['layers'] = {'ring': 'M9'}
            result = backend.generate(design)
            self.assertFalse(result['ok'])
            self.assertIn('same_layer_short', {error['code'] for error in result['errors']})
            self.assertTrue(unavailable_warnings(result))
            tech = sparse_profile()
            tech['vias'] = []
            self.register(tech)
            result = backend.generate(self.design(CASES[1]))
            self.assertFalse(result['ok'])
            self.assertTrue(any('via' in error['code'] for error in result['errors']))
            self.assertTrue(unavailable_warnings(result))
            writer.assert_not_called()
        self.assertEqual(list(self.library.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
