"""Independent local-impedance and graph-boundary checks for annotations."""
import copy
import json
import math
import unittest

import numpy as np

from snpfit.circuit import admittance
from snpfit.models import get_model
from snpfit.segment_q import describe_segments, evaluate_segments, rank_segments


def element(kind, name, a, b, value):
    return {'kind': kind, 'name': name, 'n1': a, 'n2': b, 'value': value}


def series_rl(resistance=3., inductance=2e-9):
    return {'name': 'local_test', 'ports': ['P', 'N'], 'reference': 'REF',
            'metadata': {'model_id': 'inductor_se_gnd'}, 'elements': [
                element('res', 'rw0_0', 'P', 'w0_0_r', resistance),
                element('ind', 'lw0_0', 'w0_0_r', 'N', inductance)]}


class SegmentQTests(unittest.TestCase):
    def test_series_rl_closed_form_frequency_and_reversed_resistor(self):
        circuit = series_rl()
        for f in (1e9, 3e9, 8e9):
            row = evaluate_segments(circuit, f)[0]
            self.assertAlmostEqual(row['q'], 2 * math.pi * f * 2e-9 / 3.)
            self.assertAlmostEqual(row['l_effective_h'], 2e-9)
            self.assertEqual(row['status'], 'finite')
        circuit['elements'][0]['n1'], circuit['elements'][0]['n2'] = 'w0_0_r', 'P'
        row = evaluate_segments(circuit, 1e9)[0]
        self.assertEqual((row['start_net'], row['end_net']), ('P', 'N'))
        self.assertAlmostEqual(row['q'], 2 * math.pi * 1e9 * 2e-9 / 3.)

    def test_skin_group_matches_independent_nodal_network(self):
        circuit = series_rl()
        circuit['elements'][1]['n2'] = 'skin'
        circuit['elements'] += [element('ind', 'lskinw0_0', 'skin', 'N', .4e-9),
                                element('res', 'rskinw0_0', 'N', 'skin', 7.)]
        for f in (1e8, 2e9, 2e10):
            row = evaluate_segments(circuit, f)[0]
            # A complete independent nodal solve of the actual local circuit.
            z = 1 / admittance(circuit, [f])[0, 0, 0]
            self.assertAlmostEqual(row['z_real'], z.real, places=10)
            self.assertAlmostEqual(row['z_imag'], z.imag, places=10)
            self.assertAlmostEqual(row['q'], z.imag / z.real, places=11)
            self.assertEqual(set(row['element_names']), {e['name'] for e in circuit['elements']})
            self.assertEqual(len(evaluate_segments(circuit, f)), 1)

    def test_lc_bypass_stays_in_parent_segment_and_preserves_negative_q(self):
        circuit = series_rl(resistance=1., inductance=1e-12)
        circuit['elements'][0]['n2'] = 'fit_loss_1_mid'
        f0, lind = 5e9, 1e-9
        cap = 1 / ((2 * math.pi * f0) ** 2 * lind)
        circuit['elements'] += [
            element('res', 'rfit_loss_1', 'fit_loss_1_mid', 'w0_0_r', 2.),
            element('ind', 'lfit_loss_1', 'fit_loss_1_mid', 'fit_loss_1_lc', lind),
            element('cap', 'cfit_loss_1', 'fit_loss_1_lc', 'w0_0_r', cap)]
        rows = evaluate_segments(circuit, f0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['loss_cells'][0]['id'], 1)
        self.assertAlmostEqual(rows[0]['z_real'], 1., places=12)
        self.assertAlmostEqual(rows[0]['q'], 2 * math.pi * f0 * 1e-12, places=12)
        below = evaluate_segments(circuit, f0 * .9)[0]
        self.assertLess(below['q'], 0)
        self.assertEqual(below['status'], 'non_inductive')
        self.assertEqual(set(rows[0]['element_names']), {e['name'] for e in circuit['elements']})

    def test_two_rewired_loss_cells_are_both_assigned(self):
        circuit = series_rl()
        circuit['elements'][0]['n2'] = 'mid2'
        for number, a, b in ((1, 'mid1', 'w0_0_r'), (2, 'mid2', 'mid1')):
            junction = 'junction%d' % number
            circuit['elements'] += [
                element('res', 'rfit_loss_%d' % number, a, b, 2.),
                element('ind', 'lfit_loss_%d' % number, a, junction, 1e-9),
                element('cap', 'cfit_loss_%d' % number, junction, b, 1e-12)]
        row = evaluate_segments(circuit, 1e9)[0]
        self.assertEqual(row['status'], 'finite')
        self.assertEqual([block['id'] for block in row['loss_cells']], [2, 1])
        self.assertEqual(len(row['element_names']), len(circuit['elements']))

    def test_dual_tap_transformer_model_role_order(self):
        model = get_model('transformer_dm_dual_tap_gnd')
        circuit = {'name': 'dual_tap', 'ports': list(model.port_roles), 'reference': 'REF',
                   'metadata': {'model_id': model.id}, 'elements': []}
        for index, (a, b) in enumerate((('P1', 'PTAP'), ('PTAP', 'P2'), ('S1', 'STAP'), ('STAP', 'S2'))):
            mid = 'r%d' % index
            circuit['elements'] += [element('res', 'rw%d_0' % index, a, mid, 2.),
                                    element('ind', 'lw%d_0' % index, mid, b, 1e-9)]
        rows = evaluate_segments(circuit, 1e9)
        self.assertEqual([row['winding'] for row in rows], ['primary', 'primary', 'secondary', 'secondary'])
        self.assertEqual([row['branch_index'] for row in rows], [0, 1, 2, 3])
        self.assertEqual([row['winding_ordinal'] for row in rows], [1, 2, 1, 2])
        self.assertTrue(all(row['status'] == 'finite' for row in rows))

    def test_mutual_and_shunt_loading_are_explicitly_excluded(self):
        circuit = {'name': 'mutual_test', 'ports': ['P', 'S'], 'reference': 'REF',
                   'metadata': {'model_id': 'transformer_se_gnd'}, 'elements': []}
        for index, role in enumerate(('P', 'S')):
            circuit['elements'] += [element('res', 'rw%d_0' % index, role, 'r%d' % index, 2.),
                                    element('ind', 'lw%d_0' % index, 'r%d' % index, 'REF', 1e-9)]
        expected = evaluate_segments(circuit, 2e9)
        circuit['elements'] += [{'kind': 'mind', 'name': 'km', 'ind1': 'lw0_0', 'ind2': 'lw1_0', 'value': -.8},
                                element('cap', 'cp', 'P', 'S', 1e-12),
                                element('res', 'rsub', 'P', 'REF', .001)]
        actual = evaluate_segments(circuit, 2e9)
        self.assertEqual([r['q'] for r in actual], [r['q'] for r in expected])
        self.assertTrue(all('mutual' in row['scope'] and 'shunt' in row['scope'] for row in actual))

    def test_lossless_and_unknown_graph_have_json_safe_distinct_status(self):
        circuit = {'ports': ['P', 'N'], 'reference': 'REF',
                   'metadata': {'model_id': 'inductor_dm'},
                   'elements': [element('ind', 'lw0_0', 'P', 'N', 2e-9)]}
        lossless = evaluate_segments(circuit, 1e9)[0]
        self.assertEqual(lossless['status'], 'lossless')
        self.assertIsNone(lossless['q'])
        circuit['elements'].append(element('ind', 'custom_aux', 'P', 'N', 1e-9))
        rows = evaluate_segments(circuit, 1e9)
        self.assertEqual(rows[-1]['status'], 'unassigned')
        self.assertIsNone(rows[-1]['q'])
        self.assertEqual({name for row in rows for name in row['element_names']}, {'lw0_0', 'custom_aux'})
        json.dumps(rows, allow_nan=False)
        unknown = evaluate_segments(circuit, 1e9, model_id='unknown_plugin')
        self.assertTrue(all(row['status'] == 'unassigned' for row in unknown))

    def test_broken_skin_never_receives_invented_q_or_disappears(self):
        circuit = series_rl()
        circuit['elements'][1]['n2'] = 'skin'
        circuit['elements'] += [element('ind', 'lskinw0_0', 'skin', 'N', 1e-9),
                                element('res', 'rskinw0_0', 'P', 'N', 2.)]
        rows = evaluate_segments(circuit, 1e9)
        self.assertTrue(all(row['q'] is None for row in rows))
        assigned = [name for row in rows for name in row['element_names']]
        self.assertEqual(assigned.count('lw0_0'), 1)
        self.assertEqual(assigned.count('lskinw0_0'), 1)

    def test_rank_ties_limit_and_nonfinite_status_do_not_change_input(self):
        rows = [{'id': 'seg%d' % i, 'q': float(i // 2), 'branch_index': i // 3,
                 'ordinal': i, 'status': 'finite'} for i in range(14)]
        rows += [{'id': 'ideal', 'q': None, 'status': 'lossless'},
                 {'id': 'unknown', 'q': None, 'status': 'undefined'}]
        rows.reverse(); before = copy.deepcopy(rows)
        ranked = rank_segments(rows)
        self.assertEqual([row['q'] for row in ranked], [0., 0., 1., 1., 2., 2., 3., 3., 4., 4.])
        self.assertEqual([row['id'] for row in ranked[:2]], ['seg0', 'seg1'])
        self.assertEqual(rows, before)
        ranked[0]['q'] = -999
        self.assertEqual(rows, before)
        self.assertEqual(rank_segments(rows, 0), [])
        for bad in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                rank_segments(rows, bad)

    def test_annotation_does_not_change_any_circuit_value_or_response(self):
        circuit = series_rl()
        original = json.dumps(circuit, sort_keys=True)
        before = admittance(circuit, [1e9, 5e9, 10e9])
        describe_segments(circuit)
        rank_segments(evaluate_segments(circuit, 7e9))
        self.assertEqual(json.dumps(circuit, sort_keys=True), original)
        np.testing.assert_array_equal(admittance(circuit, [1e9, 5e9, 10e9]), before)
        for bad in (0, -1, math.nan, math.inf):
            with self.assertRaises(ValueError):
                evaluate_segments(circuit, bad)


if __name__ == '__main__':
    unittest.main()
