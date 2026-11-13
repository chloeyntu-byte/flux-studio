import hashlib
import tempfile
import unittest
from pathlib import Path

import cell_lifecycle


class CellSnapshotTests(unittest.TestCase):
    def test_tracks_content_and_rejects_extra_view(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'cell'
            (root / 'layout').mkdir(parents=True)
            (root / 'layout' / 'layout.oa').write_bytes(b'layout')
            before = cell_lifecycle._snapshot(root)
            self.assertEqual(before['layout/layout.oa'], hashlib.sha256(b'layout').hexdigest())
            (root / 'layout' / 'layout.oa').write_bytes(b'changed')
            self.assertNotEqual(before, cell_lifecycle._snapshot(root))
            (root / 'schematic').mkdir()
            with self.assertRaises(ValueError):
                cell_lifecycle._snapshot(root)

    def test_rejects_links_and_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'cell'
            (root / 'layout').mkdir(parents=True)
            (root / 'layout' / 'db.oa').write_bytes(b'x')
            (root / 'layout' / 'link').symlink_to('db.oa')
            with self.assertRaises(ValueError):
                cell_lifecycle._snapshot(root)
            (root / 'layout' / 'link').unlink()
            (root / 'layout' / 'edit.lock').write_text('')
            with self.assertRaises(ValueError):
                cell_lifecycle._snapshot(root)
