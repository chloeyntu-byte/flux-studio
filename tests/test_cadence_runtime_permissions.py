import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cadence

@unittest.skipUnless(os.geteuid() == 0 and shutil.which('runuser'), 'requires root and runuser')
class NativeRuntimePermissions(unittest.TestCase):
    def setUp(self):
        try:
            self.user = pwd.getpwnam('nobody')
        except KeyError:
            self.skipTest('nobody unavailable')
        self.tmp = tempfile.TemporaryDirectory(prefix='cadence-permissions-', dir='/tmp')
        self.root = Path(self.tmp.name)
        self.root.chmod(0o755)
        self.old_umask = os.umask(0o077)
        self.patches = [mock.patch.object(cadence, 'ROOT', self.root),
                        mock.patch.object(cadence, 'OWNER', self.user.pw_name),
                        mock.patch.object(cadence, 'ensure_runtime', return_value={})]
        for patch in self.patches: patch.start()

    def tearDown(self):
        for patch in reversed(self.patches): patch.stop()
        os.umask(self.old_umask)
        self.tmp.cleanup()

    def test_fresh_runtime_is_private_to_native_user_and_source_readable(self):
        job = cadence.job_dir('technology')
        for path in (self.root / 'runtime', job):
            self.assertEqual(path.stat().st_uid, self.user.pw_uid)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        source = job / 'job.il'
        source.write_text('printf("TFGEN_PDK_SUCCESS\\n")\n')
        source.chmod(0o644)
        result = subprocess.run(['runuser','-u',self.user.pw_name,'--','/usr/bin/test','-r',str(source)])
        self.assertEqual(result.returncode, 0)

    def test_existing_root_private_runtime_unchanged_and_rejected(self):
        path = self.root / 'runtime'
        path.mkdir(mode=0o700)
        before = path.stat()
        with self.assertRaisesRegex(ValueError, 'cannot traverse existing directory.*runtime'):
            cadence.job_dir('technology')
        after = path.stat()
        self.assertEqual((before.st_uid,before.st_gid,stat.S_IMODE(before.st_mode)),
                         (after.st_uid,after.st_gid,stat.S_IMODE(after.st_mode)))
        self.assertEqual(list(path.iterdir()), [])

    def test_runtime_symlink_rejected_without_touching_target(self):
        target = self.root / 'target'
        target.mkdir(mode=0o755)
        before = target.stat()
        (self.root / 'runtime').symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'real directory, not a symlink'):
            cadence.job_dir('technology')
        after = target.stat()
        self.assertEqual((before.st_uid, before.st_mode), (after.st_uid, after.st_mode))
        self.assertEqual(list(target.iterdir()), [])

    def test_nonroot_native_user_creates_own_runtime(self):
        os.chown(self.root, self.user.pw_uid, self.user.pw_gid)
        code = ('from pathlib import Path;import cadence;'
                'cadence.ROOT=Path(%r);cadence.OWNER=%r;cadence.ensure_runtime=lambda: {};'
                'p=cadence.job_dir("native");print(p)' % (str(self.root),self.user.pw_name))
        environment = {'PYTHONPATH': str(Path(cadence.__file__).resolve().parent), 'PATH': os.defpath}
        result = subprocess.run([shutil.which('runuser'),'-u',self.user.pw_name,'--',sys.executable,'-c',code],
                                env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        path = Path(result.stdout.strip())
        self.assertEqual(path.stat().st_uid,self.user.pw_uid)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o700)

if __name__ == '__main__': unittest.main()
