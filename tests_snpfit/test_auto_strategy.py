"""Real-network checks for automatic L priority and unchanged legacy paths."""
import copy
import json
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

from snpfit.circuit import component_counts, simulate
from snpfit.fitting import evaluate, evaluate_network, fit
from snpfit.lpriority import guard_score, l_score, polish, preference, select_strategy
from snpfit.models import get_model
from snpfit.network import Network


def pi_fixture():
    """Handwritten circuit, independent of the production topology builder."""
    return {'name': 'manual_pi', 'ports': ['P', 'N'], 'reference': 'REF',
            'metadata': {}, 'elements': [
                {'kind': 'res', 'name': 'rs', 'n1': 'P', 'n2': 'x', 'value': 1.7},
                {'kind': 'ind', 'name': 'ls', 'n1': 'x', 'n2': 'N', 'value': 2.4e-9},
                {'kind': 'cap', 'name': 'cw', 'n1': 'P', 'n2': 'N', 'value': 1e-13},
                {'kind': 'cap', 'name': 'cg1', 'n1': 'P', 'n2': 'REF', 'value': 1.4e-14},
                {'kind': 'cap', 'name': 'cg2', 'n1': 'N', 'n2': 'REF', 'value': 1.8e-14},
            ]}


def network(circuit):
    freq = np.geomspace(1e9, 8e9, 11)
    return Network(freq, simulate(circuit, freq, 50), 50)


def weak_transformer():
    return {'name': 'weak', 'ports': ['P', 'S'], 'reference': 'REF',
            'metadata': {}, 'elements': [
                {'kind': 'res', 'name': 'rp', 'n1': 'P', 'n2': 'a', 'value': 1.2},
                {'kind': 'ind', 'name': 'lp', 'n1': 'a', 'n2': 'REF', 'value': 2e-9},
                {'kind': 'res', 'name': 'rs', 'n1': 'S', 'n2': 'b', 'value': 1.8},
                {'kind': 'ind', 'name': 'ls', 'n1': 'b', 'n2': 'REF', 'value': 3e-9},
                {'kind': 'mind', 'name': 'k', 'ind1': 'lp', 'ind2': 'ls', 'value': .02},
            ]}


class AutomaticStrategy(unittest.TestCase):
    def test_strict_comparison_selects_strategy_without_user_flag(self):
        for limits, expected in [((5, 20), 'l_priority'), ((10, 20), 'l_priority'),
                                 ((5, 5), 'balanced'), ((10, 10), 'balanced'),
                                 ((20, 5), 'balanced')]:
            with self.subTest(limits=limits):
                self.assertEqual(select_strategy(*limits), expected)

    def test_real_asymmetric_fit_runs_refinement_and_replays_independently(self):
        source = network(pi_fixture())
        limits = {'l_pct': 5, 'q_pct': 20}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch('subprocess.Popen', side_effect=AssertionError('external fitter forbidden')):
            result = fit(source, 'inductor_dm_gnd', limits, directory, timeout_s=15)
        self.assertEqual(result.strategy, 'l_priority')
        receipts = [a['l_priority_refinement'] for a in result.attempts
                    if 'l_priority_refinement' in a and
                    a['l_priority_refinement'].get('algorithm')]
        self.assertTrue(receipts)
        self.assertTrue(any(r['iterations'] > 0 for r in receipts))
        self.assertTrue(all(r['added_elements'] == 0 and r['external_tools'] == [] for r in receipts))
        independent = evaluate(result.circuit, source, 'inductor_dm_gnd', limits)
        self.assertEqual(result.passed, independent['passed'])
        self.assertTrue(result.passed)
        self.assertLessEqual(guard_score(independent), 1)
        self.assertEqual(independent['limits']['network_pct'], 5)
        self.assertTrue(all(v <= 20 for v in component_counts(result.circuit).values()))

    def test_balanced_real_results_match_frozen_02_reference(self):
        # These numerical references were obtained from unmodified 0.2 using
        # the handwritten source above. Both cases need actual optimization.
        references = {
            (10, 10): [1.7294411047022171, 2.4038195114252803e-9,
                       9.961740159731026e-14, 1.3999435158891118e-14,
                       1.7999156973982635e-14],
            (20, 5): [1.7353305855611292, 2.485103004684013e-9,
                      9.969052247204675e-14, 1.4000000000001143e-14,
                      1.800000000000494e-14],
        }
        for (lp, qp), expected in references.items():
            with self.subTest(limits=(lp, qp)), tempfile.TemporaryDirectory() as directory:
                result = fit(network(pi_fixture()), 'inductor_dm_gnd',
                             {'l_pct': lp, 'q_pct': qp}, directory, timeout_s=15)
            self.assertTrue(result.passed)
            self.assertEqual(result.strategy, 'balanced')
            self.assertEqual(result.circuit['metadata']['candidate'], 'compact_pi')
            self.assertFalse(any('l_priority_refinement' in a for a in result.attempts))
            np.testing.assert_allclose([e['value'] for e in result.circuit['elements']],
                                       expected, rtol=1e-6, atol=0)

    def test_feasible_circuit_gets_better_l_without_extra_devices(self):
        truth = pi_fixture()
        source = network(truth)
        seed = copy.deepcopy(truth)
        seed['elements'][1]['value'] *= 1.01
        limits = {'l_pct': 5, 'q_pct': 20}
        original = evaluate(seed, source, 'inductor_dm_gnd', limits)
        self.assertTrue(original['passed'])
        result, metrics, receipt = polish(seed, source, get_model('inductor_dm_gnd'), limits, 2)
        actual = evaluate(result, source, 'inductor_dm_gnd', limits)
        self.assertTrue(metrics['passed'] and actual['passed'])
        self.assertLess(l_score(actual), l_score(original) * .9)
        self.assertEqual(component_counts(seed), component_counts(result))
        structure = lambda c: [{k: v for k, v in e.items() if k != 'value'} for e in c['elements']]
        self.assertEqual(structure(seed), structure(result))
        self.assertEqual(receipt['added_elements'], 0)

    def test_weak_transformer_phase_guard_beats_smaller_l_error(self):
        truth = weak_transformer()
        source = network(truth)
        limits = {'l_pct': 5, 'q_pct': 20}
        wrong = copy.deepcopy(truth)
        wrong['elements'][-1]['value'] *= -1
        feasible = copy.deepcopy(truth)
        for element in feasible['elements']:
            if element['kind'] == 'ind':
                element['value'] *= 1.01
        wrong_metrics = evaluate(wrong, source, 'transformer_se_gnd', limits)
        feasible_metrics = evaluate(feasible, source, 'transformer_se_gnd', limits)
        self.assertTrue(wrong_metrics['network']['pass'])
        self.assertFalse(wrong_metrics['passed'])
        self.assertTrue(feasible_metrics['passed'])
        self.assertLess(l_score(wrong_metrics), l_score(feasible_metrics))
        self.assertGreater(guard_score(wrong_metrics), 1)
        self.assertLess(preference(feasible_metrics), preference(wrong_metrics))

    def test_q_constraint_is_preserved_when_l_and_full_s_are_already_good(self):
        truth = weak_transformer()
        source = network(truth)
        wrong_loss = copy.deepcopy(truth)
        wrong_loss['elements'][0]['value'] *= 1.5
        metrics = evaluate(wrong_loss, source, 'transformer_se_gnd',
                           {'l_pct': 5, 'q_pct': 20})
        self.assertTrue(all(v['l']['pass'] for v in metrics['branches'].values()))
        self.assertTrue(metrics['network']['pass'])
        self.assertTrue(all(v['pass'] for v in metrics['mutual_impedance']['complex_relative_errors'].values()))
        self.assertFalse(metrics['branches']['primary']['q']['pass'])
        self.assertFalse(metrics['passed'])
        self.assertGreater(guard_score(metrics), 1)

    def test_full_s_common_mode_guard_cannot_be_hidden_by_exact_l(self):
        circuit = weak_transformer()
        circuit['ports'] = ['P1', 'P2', 'S1', 'S2']
        for element in circuit['elements']:
            if element['kind'] == 'mind':
                continue
            for node in ('n1', 'n2'):
                if element[node] == 'P': element[node] = 'P1'
                if element[node] == 'S': element[node] = 'S1'
            if element['kind'] == 'ind':
                element['n2'] = 'P2' if element['name'] == 'lp' else 'S2'
        source = network(circuit)
        changed_s = source.s.copy()
        changed_s[:, :2, :2] += .08
        candidate = Network(source.freq, changed_s, source.z0)
        metrics = evaluate_network(candidate, source, 'transformer_dm_gnd', {'l_pct': 5, 'q_pct': 20})
        self.assertTrue(all(v['l']['pass'] and v['q']['pass'] for v in metrics['branches'].values()))
        self.assertFalse(metrics['network']['pass'])
        self.assertFalse(metrics['passed'])
        self.assertGreater(guard_score(metrics), 1)

    def test_cancelled_or_expired_refinement_does_not_publish_success(self):
        truth = pi_fixture()
        source = network(truth)
        limits = {'l_pct': 5, 'q_pct': 20}
        cancellation = threading.Event()
        cancellation.set()
        _, metrics, receipt = polish(truth, source, get_model('inductor_dm_gnd'), limits,
                                     2, cancel=cancellation)
        self.assertEqual(receipt['state'], 'cancelled')
        self.assertEqual(receipt['iterations'], 0)
        self.assertFalse(metrics['passed'])
        seed = copy.deepcopy(truth)
        seed['elements'][1]['value'] *= 1.04
        start = time.monotonic()
        _, _, receipt = polish(seed, source, get_model('inductor_dm_gnd'), limits, 1e-6)
        self.assertEqual(receipt['state'], 'budget_exhausted')
        self.assertEqual(receipt['iterations'], 0)
        self.assertLess(time.monotonic() - start, .25)


if __name__ == '__main__':
    unittest.main()
