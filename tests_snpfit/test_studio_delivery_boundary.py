"""Studio snapshots must never enter the weaker legacy native acceptance path."""
import unittest
from unittest import mock

from snpfit.delivery import deliver


class DeliveryBoundary(unittest.TestCase):
    def test_studio_snapshots_rejected_before_any_native_work(self):
        for identity in ({'engine': 'studio_joint_parameters_v1'},
                         {'stage': 'tune'}, {'metrics': {'lq': {}}}):
            snapshot = {'passed': True, 'circuit': {'elements': []}, **identity}
            with self.subTest(identity=identity), \
                    mock.patch('snpfit.native.build_native') as build, \
                    mock.patch('snpfit.native.replay_native') as replay:
                with self.assertRaisesRegex(ValueError, 'studio-action native'):
                    deliver(snapshot, '/unused', 'unused', 'unused')
                build.assert_not_called()
                replay.assert_not_called()

    def test_legacy_unaccepted_result_keeps_legacy_guard(self):
        with self.assertRaises(ValueError) as caught:
            deliver({'passed': False}, '/unused', 'unused', 'unused')
        self.assertNotIn('Studio snapshots', str(caught.exception))
