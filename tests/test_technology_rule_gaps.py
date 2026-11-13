"""Truthful discovery with incomplete optional PDK rules (no native writes)."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import technology


def skill_value(value):
    if value is None:
        return 'nil'
    if value is True:
        return 't'
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return '(' + ' '.join(skill_value(v) for v in value) + ')'
    return str(value)


def row(kind, *values):
    return '\t'.join([kind] + [skill_value(value) for value in values])


def native_params():
    return ['CUT', .2, .2, 1, 1, [.2, .2], [.1, .1], [.1, .1], [0, 0], [0, 0], [0, 0]]


def base_rows(metals=('LOW', 'HIGH'), ranks=True, via=True):
    records = [row('LIB', 'Design', '/design', 'Tech', '/tech'), row('GRID', .005),
               row('LAYER', 'CUT', 9)]
    for index, name in enumerate(metals):
        records.append(row('LAYER', name, 100 - index))  # OA number is not stack order.
        records.append(row('FUNCTION', [name, 'metal'] + ([index * 2] if ranks else [])))
    if via and len(metals) > 1:
        for low, high in zip(metals, metals[1:]):
            records.append(row('VIA', 'V_' + low + '_' + high, low, high, native_params()))
    return records


class TechnologyRuleGapsTests(unittest.TestCase):
    def normalize(self, records):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'technology.tsv'
            path.write_text('\n'.join(records), encoding='utf-8')
            return technology._normalize(path, directory)

    def test_width_missing_does_not_remove_real_metals(self):
        result = self.normalize(base_rows())
        self.assertEqual([layer['name'] for layer in result['layers']], ['LOW', 'HIGH'])
        self.assertTrue(all(layer['min_width'] is None for layer in result['layers']))
        self.assertEqual(len(result['vias']), 1)
        warnings = technology.missing_rule_warnings(result, ['LOW'])
        self.assertEqual({warning.get('layer') for warning in warnings}, {'LOW'})
        self.assertEqual(warnings[0]['missing_rules'], ['min_width', 'min_spacing'])

    def test_spacing_missing_is_none_and_same_net_is_not_inter_net(self):
        records = base_rows() + [row('RULE', 'LOW', 'minWidth', .2),
                                 row('RULE', 'LOW', 'minSameNetSpacing', .5)]
        layer = self.normalize(records)['layers'][0]
        self.assertEqual(layer['min_same_net_spacing'], .5)
        self.assertIsNone(layer['min_spacing'])
        self.assertEqual(technology.spacing_for(layer, 10, 100), 0)

    def test_constraints_are_applied_after_identity_and_keep_known_limits(self):
        records = base_rows() + [row('GROUP', 'foundry'),
            row('CONSTRAINT', 'foundry', 'minWidth', ['LOW'], .4, 'id', None),
            row('CONSTRAINT', 'foundry', 'minSpacing', ['LOW'], .6, 'id', None),
            row('RULE', 'LOW', 'maxWidth', 20),
            row('CONSTRAINT', 'foundry', 'maxWidth', ['LOW'], 12, 'id', None)]
        layer = self.normalize(records)['layers'][0]
        self.assertEqual((layer['min_width'], layer['min_spacing'], layer['max_width']), (.4, .6, 12))

    def test_direct_rule_tables_are_retained_without_inventing_default(self):
        table = [[['width', 'length'], None], [[5, 100], 1.2]]
        result = self.normalize(base_rows() + [row('RULE', 'LOW', 'minSpacing', table)])
        layer = result['layers'][0]
        self.assertIsNone(layer['min_spacing'])
        self.assertEqual(technology.spacing_for(layer, 10, 101), 1.2)
        self.assertEqual(technology.spacing_for(layer, 1, 101), 0)
        self.assertTrue(any(w['code'] == 'missing_pdk_rule' and w.get('layer') == 'LOW'
                            for w in result['warnings']))

    def test_unbound_dfm_rules_are_not_silently_enforced_as_foundry(self):
        records = base_rows() + [row('CONSTRAINT', 'DFM_optional', 'minWidth', ['LOW'], 50, 'id', None)]
        result = self.normalize(records)
        self.assertIsNone(result['layers'][0]['min_width'])
        self.assertEqual(result['unbound_constraints'][0]['group'], 'DFM_optional')

    def test_one_or_two_verified_metals_can_load(self):
        for names in (('ONLY',), ('LOW', 'HIGH')):
            with self.subTest(metals=names):
                result = self.normalize(base_rows(names))
                self.assertEqual(len(result['layers']), len(names))
                self.assertTrue(result['order_verified'])
                self.assertIn(result['recommended']['body'], names)

    def test_complete_native_function_order_survives_missing_vias(self):
        result = self.normalize(base_rows(('LOW', 'MIDDLE', 'HIGH'), via=False))
        self.assertTrue(result['order_verified'])
        self.assertEqual(result['order_source'], 'native_layer_functions')
        self.assertEqual([layer['name'] for layer in result['layers']], ['LOW', 'MIDDLE', 'HIGH'])
        self.assertEqual(result['vias'], [])

    def test_incomplete_via_geometry_does_not_erase_endpoint_stack(self):
        for index, value in ((1, None), (5, None), (6, [None, .1]), (7, .2), (8, None)):
            records = base_rows(ranks=False, via=False)
            params = native_params()
            params[index] = value
            records.append(row('VIA', 'BAD', 'LOW', 'HIGH', params))
            with self.subTest(index=index):
                result = self.normalize(records)
                self.assertTrue(result['order_verified'])
                self.assertEqual(result['order_source'], 'native_via_endpoints')
                self.assertEqual(result['vias'], [])
                self.assertEqual(len(result['layers']), 2)
                self.assertTrue(any(w['code'] == 'unavailable_native_via' for w in result['warnings']))

    def test_missing_cut_identity_or_invalid_geometry_does_not_make_fake_via(self):
        for params in (None, [], ['MISSING'] + native_params()[1:], native_params()[:7]):
            result = self.normalize(base_rows(via=False) + [row('VIA', 'BAD', 'LOW', 'HIGH', params)])
            self.assertEqual(result['vias'], [])
            self.assertEqual(len(result['layers']), 2)

    def test_ambiguous_stack_is_display_rank_only(self):
        result = self.normalize(base_rows(('ALPHA', 'OMEGA'), ranks=False, via=False))
        self.assertFalse(result['order_verified'])
        self.assertEqual(result['order_source'], 'display_rank_only')
        self.assertTrue(all(layer['order_verified'] is False for layer in result['layers']))
        self.assertTrue(any(w['code'] == 'unverified_metal_order' for w in result['warnings']))

    def test_function_order_conflicting_with_native_via_does_not_override_graph(self):
        records = base_rows(ranks=False) + [row('FUNCTION', ['LOW', 'metal', 5]),
                                           row('FUNCTION', ['HIGH', 'metal', 3])]
        result = self.normalize(records)
        self.assertTrue(result['order_verified'])
        self.assertEqual(result['order_source'], 'native_via_endpoints')
        self.assertEqual([layer['name'] for layer in result['layers']], ['LOW', 'HIGH'])

    def test_names_and_oa_numbers_do_not_identify_metals(self):
        records = [row('LIB', 'Design', '/design', 'Tech', '/tech'), row('GRID', .005),
                   row('LAYER', 'M1', 1), row('LAYER', 'M2', 2), row('LAYER', 'CUT', 9),
                   row('RULE', 'M1', 'minWidth', .2), row('RULE', 'M2', 'minWidth', .2),
                   row('VIA', 'FAKE_LOOKING', 'M1', 'M2', native_params())]
        with self.assertRaisesRegex(ValueError, 'cannot be identified reliably'):
            self.normalize(records)

    def test_old_pdk_positive_sheet_resistance_and_native_endpoints_identify_metals(self):
        records = [r for r in base_rows(ranks=False) if not r.startswith('FUNCTION')]
        records += [row('RESISTANCE', 'LOW', .1), row('RESISTANCE', 'HIGH', .2)]
        result = self.normalize(records)
        self.assertEqual(len(result['layers']), 2)
        self.assertTrue(result['order_verified'])

    def test_manufacturing_grid_is_preserved_and_missing_grid_uses_real_dbu(self):
        records = [r for r in base_rows() if not r.startswith('GRID')]
        for grid in (None, -1, float('nan')):
            with self.subTest(grid=grid):
                result = self.normalize(records + [row('GRID', grid), row('DBU', 2000)])
                self.assertEqual(result['grid'], .0005)
                self.assertIsNone(result['manufacturing_grid'])
                self.assertEqual(result['grid_source'], 'database_units')
                self.assertTrue(any(w['code'] == 'missing_pdk_grid' for w in result['warnings']))
        result = self.normalize(records + [row('GRID', .00001), row('DBU', 2000)])
        self.assertEqual(result['grid'], .00001)
        self.assertEqual(result['grid_source'], 'manufacturing_grid')

    def test_no_grid_or_dbu_has_explicit_working_fallback(self):
        result = self.normalize([r for r in base_rows() if not r.startswith('GRID')])
        self.assertEqual(result['grid'], .005)
        self.assertEqual(result['grid_source'], 'working_fallback')
        self.assertTrue(any('not a verified manufacturing rule' in w['message'] for w in result['warnings']))
        for invalid in (None, 0, -1, float('inf'), float('nan'), 'invalid'):
            self.assertEqual(technology.drawing_grid({'grid': invalid}), .005)

    def test_warning_helper_normalizes_filters_and_deduplicates(self):
        tech = {'grid': .005, 'layers': [{'name': 'LOW', 'min_width': None, 'min_spacing': .2},
                                       {'name': 'HIGH', 'min_width': .2, 'min_spacing': None}],
                'warnings': ['Legacy advisory', 'Legacy advisory']}
        first = technology.missing_rule_warnings(tech, ['LOW'])
        tech['warnings'] = copy.deepcopy(first)
        second = technology.missing_rule_warnings(tech, ['LOW'])
        self.assertEqual(first, second)
        self.assertEqual(len(second), 2)
        self.assertTrue(all({'part', 'code', 'message', 'fields'} <= set(w) for w in second))

    def test_optional_nan_and_none_tables_do_not_crash_spacing(self):
        self.assertEqual(technology.spacing_for({'min_spacing': float('nan'), 'spacing_tables': None}, 10, 100), 0)
        layer = {'min_spacing': .25, 'spacing_tables': [None, {'axes': ['width'], 'default': None,
                  'entries': [None, {'threshold': [2], 'value': float('nan')}, {'threshold': [2], 'value': .8}]}]}
        self.assertEqual(technology.spacing_for(layer, 10, 100), .8)
        self.assertEqual(technology.spacing_for(layer, 1, 100), .25)
        warning = technology.missing_rule_warnings({'grid': .005, 'layers': [{'name': 'LOW', 'min_width': .2, 'min_spacing': float('nan')}]})
        self.assertEqual(warning[0]['missing_rules'], ['min_spacing'])

    def test_invalid_optional_rule_warns_but_absent_optional_limit_is_quiet(self):
        tech = {'grid': .005, 'layers': [{'name': 'LOW', 'min_width': .2, 'min_spacing': .2,
                                        'max_width': None, 'min_area': None}]}
        self.assertEqual(technology.missing_rule_warnings(tech), [])
        tech['layers'][0]['max_width'] = float('nan')
        warning = technology.missing_rule_warnings(tech)
        self.assertEqual(warning[0]['missing_rules'], ['max_width'])

    def test_native_params_and_array_constraints_remain_exact(self):
        params = native_params()
        params[8] = [.1, -.2]
        result = self.normalize(base_rows(via=False) + [row('VIA', 'REAL', 'LOW', 'HIGH', params),
            row('CONSTRAINT', 'foundry', 'viaSpacing', ['CUT'], [4, .4, .6], 'id', None)])
        via = result['vias'][0]
        self.assertEqual(via['cut_spacing'], [.2, .2])
        self.assertEqual(via['array_cut_spacing'], [.4, .6])
        self.assertEqual(via['lower_offset'], [.1, -.2])
        self.assertEqual(via['minimum_landing'], [.4, .4])


if __name__ == '__main__':
    unittest.main()
