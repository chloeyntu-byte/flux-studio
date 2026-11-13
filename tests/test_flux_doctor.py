"""Exercise capability reporting without starting native tools or opening a GUI."""

import builtins
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import flux_doctor as doctor


def _entry(report, check):
    return next(item for item in report if item['check'] == check)


class DoctorTests(unittest.TestCase):
    def test_worker_accepts_existing_tk_facts_without_creating_an_interpreter(self):
        real_import = builtins.__import__

        def no_tk_import(name, *args, **kwargs):
            if name == 'tkinter':
                self.fail('GUI worker must reuse Tk facts captured on the main thread')
            return real_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=no_tk_import), \
                patch.object(doctor, '_native_checks'):
            report = doctor.check_environment(tk_patchlevel='8.6.16', tk_photo=True)
        self.assertEqual(_entry(report, 'Tk display')['status'], 'PASS')
        self.assertEqual(_entry(report, 'Built-in playback')['status'], 'PASS')
        self.assertEqual(_entry(report, 'Bundled APNG export')['status'], 'PASS')

    def test_missing_zlib_is_scoped_to_optional_media(self):
        real_import = builtins.__import__

        def no_zlib_import(name, *args, **kwargs):
            if name == 'zlib':
                raise ImportError('fixture: zlib module unavailable')
            return real_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=no_zlib_import), \
                patch.object(doctor, '_native_checks'):
            report = doctor.check_environment(tk_patchlevel='8.6.16', tk_photo=True)
        export = _entry(report, 'Bundled APNG export')
        self.assertEqual(export['status'], 'WARN')
        self.assertIn('zlib module unavailable', export['detail'])
        self.assertFalse(any(item['status'] == 'FAIL' for item in report))

    def test_missing_media_file_is_reported_without_a_core_failure(self):
        with patch.object(doctor, '_VIDEO_RESOURCES', ('missing_font_atlas.zlib',)), \
                patch.object(doctor, '_native_checks'):
            report = doctor.check_environment(tk_patchlevel='8.6.16', tk_photo=False)
        self.assertEqual(_entry(report, 'Project files')['status'], 'PASS')
        self.assertEqual(_entry(report, 'Built-in playback')['status'], 'WARN')
        export = _entry(report, 'Bundled APNG export')
        self.assertEqual(export['status'], 'WARN')
        self.assertIn('missing_font_atlas.zlib', export['detail'])

    def test_native_detection_respects_explicit_override_and_keeps_license_private(self):
        with tempfile.TemporaryDirectory(prefix='flux doctor fixture ') as folder:
            workspace = Path(folder)
            (workspace / 'cds.lib').write_text('')
            tools = workspace / 'installed tools'
            tools.mkdir()
            sentinel = workspace / 'native-tool-was-started'
            for name in ('virtuoso', 'emx', 'strmout'):
                tool = tools / name
                tool.write_text('#!/bin/sh\ntouch "{}"\n'.format(sentinel))
                tool.chmod(0o755)
            environment = dict(TFGEN_WORKSPACE=folder, PATH=str(tools),
                               TFGEN_VIRTUOSO=str(tools / 'virtuoso'),
                               TFGEN_EMX=str(tools / 'missing-emx'),
                               **{'LM_LICENSE_FILE': 'private-license-marker'})
            with patch.dict(os.environ, environment, clear=True):
                report = doctor.check_environment(tk_patchlevel='8.6.16', tk_photo=True)
            self.assertEqual(_entry(report, 'Workspace mapping')['status'], 'PASS')
            self.assertEqual(_entry(report, 'Virtuoso executable')['status'], 'PASS')
            self.assertEqual(_entry(report, 'EMX executable')['status'], 'WARN')
            self.assertIn('TFGEN_EMX', _entry(report, 'EMX executable')['detail'])
            self.assertEqual(_entry(report, 'GDS exporter')['status'], 'PASS')
            self.assertNotIn('private-license-marker', doctor.format_report(report))
            self.assertFalse(sentinel.exists())

    def test_cli_status_distinguishes_core_failure_from_optional_warning(self):
        checks = [dict(section='Video', status='WARN', check='Playback', detail='missing')]
        for status in ('FAIL', 'PASS'):
            checks.append(dict(section='Core', status=status, check='Python', detail='fixture'))
            output = io.StringIO()
            with patch.object(doctor, 'check_environment', return_value=checks) as check, \
                    contextlib.redirect_stdout(output):
                self.assertEqual(doctor.main(['--display', '--json']), int(status == 'FAIL'))
            check.assert_called_once_with(probe_display=True)
            self.assertEqual(json.loads(output.getvalue()), checks)
            checks.pop()


if __name__ == '__main__':
    unittest.main()
