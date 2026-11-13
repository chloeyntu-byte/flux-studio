"""Conservative cleanup of a verified, newly generated campaign cell."""
import hashlib
import json
import os
import pwd
from pathlib import Path
import stat
import shutil
import uuid

import cadence


def _walk_cell(path, destination=None):
    """Read only regular files through directory-relative, no-follow descriptors."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(str(path), flags)
    files = {}
    def walk(fd, relative):
        names = sorted(os.listdir(fd))
        if relative == Path() and names != ['layout']:
            raise ValueError('Cell has additional views or no layout.')
        for name in names:
            if 'lock' in name.lower() or name.endswith('.cdslck'):
                raise ValueError('Cell has a lock-like object: {}'.format(name))
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            rel = relative / name
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, flags, dir_fd=fd)
                try:
                    if destination is not None:
                        (destination / rel).mkdir(mode=stat.S_IMODE(info.st_mode))
                    walk(child, rel)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    current = os.fstat(child)
                    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                        raise ValueError('Cell file changed while opening: {}'.format(rel))
                    h = hashlib.sha256()
                    with os.fdopen(child, 'rb', closefd=False) as source:
                        if destination is None:
                            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                                h.update(chunk)
                        else:
                            with (destination / rel).open('xb') as target:
                                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                                    h.update(chunk); target.write(chunk)
                            os.chmod(str(destination / rel), stat.S_IMODE(info.st_mode))
                    after = os.fstat(child)
                    if (current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise ValueError('Cell file changed while reading: {}'.format(rel))
                    files[str(rel)] = h.hexdigest()
                finally:
                    os.close(child)
            else:
                raise ValueError('Cell contains a link or unsupported filesystem object: {}'.format(rel))
    try:
        walk(root_fd, Path())
    finally:
        os.close(root_fd)
    if not files:
        raise ValueError('Cell has no saved layout files.')
    return files


def _snapshot(path):
    return _walk_cell(path)


def prepare(generated, source):
    """Freeze successful write receipt, native binding and current OA hashes."""
    if not generated.get('ok') or generated.get('view') != 'layout':
        raise ValueError('A successful saved-layout receipt is required.')
    receipt = Path(generated['run_dir']) / 'result.json'
    saved = json.loads(receipt.read_text(encoding='utf-8'))
    for key in ('library', 'cell', 'view', 'technology', 'geometry_hash'):
        if saved.get(key) != generated.get(key):
            raise ValueError('Generation receipt mismatch: {}'.format(key))
    identity = source['identity']
    tech = source['library']['_tech']
    if generated['library'] != identity['library'] or generated['technology'] != tech['technology']:
        raise ValueError('Generation and frozen library identity differ.')
    base = Path(tech['library_path'])
    if not base.is_absolute() or any(p.is_symlink() for p in (base,) + tuple(base.parents)):
        raise ValueError('Invalid library directory.')
    cell_path = base / generated['cell']
    return dict(library=generated['library'], cell=generated['cell'], technology=generated['technology'],
                technology_path=tech['technology_path'], library_path=str(base),
                workspace=source['library']['workspace'], cds_lib=tech.get('cds_lib'),
                geometry_hash=generated['geometry_hash'], receipt=str(receipt),
                files=_snapshot(cell_path))


def prepare_historical(record, source):
    """Bind a historical success receipt and frozen inventory to the live library context."""
    receipt = Path(record['receipt'])
    saved = json.loads(receipt.read_text(encoding='utf-8'))
    design = json.loads((receipt.parent / 'design.json').read_text(encoding='utf-8'))
    geometry = json.loads((receipt.parent / 'geometry.json').read_text(encoding='utf-8'))
    digest = hashlib.sha256(json.dumps(geometry['geometry'], sort_keys=True).encode()).hexdigest()
    for key in ('library', 'cell', 'technology'):
        if record.get(key) != saved.get(key):
            raise ValueError('Inventory/receipt mismatch: {}'.format(key))
    if (not saved.get('ok') or saved.get('view') != 'layout' or digest != saved.get('geometry_hash')
            or design.get('library') != saved['library'] or design.get('cell') != saved['cell']):
        raise ValueError('Historical write receipt and design/geometry are inconsistent.')
    tech = source['library']['_tech']
    if (record.get('library_path') != tech['library_path'] or
            record.get('technology_path') != tech['technology_path'] or
            record.get('cds_lib') != tech.get('cds_lib')):
        raise ValueError('Inventory identity differs from current frozen mapping and technology.')
    script = (receipt.parent / 'job.il').read_text(encoding='utf-8')
    required = ['ddSetForcedLib(' + cadence.skill(record['cds_lib']) + ')',
                'lib~>readPath==' + cadence.skill(record['library_path']),
                'tf~>path==' + cadence.skill(record['technology_path']),
                'tf~>libName==' + cadence.skill(record['technology'])]
    if not all(item in script for item in required):
        raise ValueError('Original native writer did not prove this library and technology binding.')
    handle = prepare(saved, source)
    if handle['files'] != record.get('current_files_sha256'):
        raise ValueError('Cell changed since inventory; re-inventory and review it.')
    return handle


def delete(handle, runtime):
    """Delete only a still-identical candidate through the native library API."""
    account = pwd.getpwnam(runtime['user'])
    if os.geteuid() not in (0, account.pw_uid):
        raise ValueError('Cleanup must run as the frozen native user or its launcher.')
    base = Path(handle['library_path'])
    if base.stat().st_uid != account.pw_uid:
        raise ValueError('Native cleanup user does not own the design library.')
    if any(p.is_symlink() for p in (base,) + tuple(base.parents)):
        raise ValueError('Library path contains a symbolic link.')
    path = base / handle['cell']
    if _snapshot(path) != handle['files']:
        raise ValueError('Cell changed after the cleanup handle was captured.')
    directory = cadence.job_dir('cell_cleanup', runtime=runtime)
    s = cadence.skill
    source = '''procedure(tfgenDelete() let((lib tf cell cv)
lib=ddGetObj(%s)
unless(lib && lib~>readPath==%s error("TFGEN cleanup library binding changed"))
tf=techGetTechFile(lib)
unless(tf && tf~>libName==%s && tf~>path==%s && lib~>name!=tf~>libName error("TFGEN cleanup technology binding changed"))
cell=ddGetObj(%s %s)
unless(cell error("TFGEN cleanup cell missing"))
unless(length(cell~>views)==1 && car(cell~>views)~>name=="layout" error("TFGEN cleanup extra views"))
cv=dbOpenCellViewByType(%s %s "layout" "" "r")
unless(cv && cv~>tfgenGeometryHash==%s error("TFGEN cleanup geometry identity changed"))
dbClose(cv)
unless(ddDeleteObj(cell) error("TFGEN cleanup native delete failed"))
when(ddGetObj(%s %s) error("TFGEN cleanup cell still exists"))
printf("TFGEN_SUCCESS\\n")
t))
if(errset(tfgenDelete() t) then exit(0) else exit(1))
''' % tuple(map(s, (handle['library'], handle['library_path'], handle['technology'], handle['technology_path'],
                   handle['library'], handle['cell'], handle['library'], handle['cell'], handle['geometry_hash'],
                   handle['library'], handle['cell'])))
    output = cadence.run_skill(source, handle['workspace'], directory, cds_lib=handle['cds_lib'], runtime=runtime)
    if 'TFGEN_SUCCESS' not in output or path.exists():
        raise RuntimeError('Native cleanup did not confirm deletion; see {}'.format(directory))
    return str(directory)


def archive(handle, archive_root):
    """Archive an exact frozen candidate, with verified hashes and restore manifest."""
    root = Path(archive_root)
    if not root.is_absolute() or any(p.is_symlink() for p in (root,) + tuple(root.parents)):
        raise ValueError('Archive path must be absolute and have no symbolic link components.')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = Path(handle['library_path']) / handle['cell']
    before = _snapshot(path)
    if before != handle['files']:
        raise ValueError('Cell changed before archive.')
    destination = root / (handle['cell'] + '-' + uuid.uuid4().hex)
    destination.mkdir(mode=0o700)
    try:
        (destination / 'cell').mkdir(mode=0o700)
        copied = _walk_cell(path, destination / 'cell')
        if copied != before or _snapshot(destination / 'cell') != before or _snapshot(path) != before:
            raise ValueError('Cell changed during archive or archive differs.')
        manifest = dict(schema='flux.cell.archive.v1', handle=handle, files=before,
                        restore='Restore only to the original absent cell path and verify every SHA256.')
        with (destination / 'manifest.json').open('x', encoding='utf-8') as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        return str(destination)
    except Exception:
        shutil.rmtree(str(destination))
        raise


def restore(archive_dir):
    """Restore a verified archive only into its absent original cell path."""
    archive_dir = Path(archive_dir)
    if any(p.is_symlink() for p in (archive_dir,) + tuple(archive_dir.parents)):
        raise ValueError('Archive path contains a symbolic link.')
    manifest = json.loads((archive_dir / 'manifest.json').read_text(encoding='utf-8'))
    handle = manifest['handle']
    if manifest.get('schema') != 'flux.cell.archive.v1' or manifest['files'] != handle['files']:
        raise ValueError('Invalid archive manifest.')
    if _snapshot(archive_dir / 'cell') != manifest['files']:
        raise ValueError('Archive hash mismatch.')
    base = Path(handle['library_path'])
    if any(p.is_symlink() for p in (base,) + tuple(base.parents)):
        raise ValueError('Library path contains a symbolic link.')
    target = base / handle['cell']
    if target.exists() or target.is_symlink():
        raise ValueError('Refusing to overwrite existing cell.')
    tmp = base / ('.flux-restore-' + uuid.uuid4().hex)
    try:
        tmp.mkdir(mode=0o700)
        copied = _walk_cell(archive_dir / 'cell', tmp)
        if copied != manifest['files'] or _snapshot(tmp) != manifest['files']:
            raise ValueError('Restored files do not match archive.')
        # Restoration is performed as the design owner; preserve directory ownership.
        owner = base.stat()
        if os.geteuid() == 0:
            for b, ds, fs in os.walk(str(tmp)):
                os.chown(b, owner.st_uid, owner.st_gid)
                for n in fs:
                    os.chown(os.path.join(b, n), owner.st_uid, owner.st_gid)
        if target.exists() or target.is_symlink():
            raise ValueError('Cell appeared during restore.')
        os.rename(str(tmp), str(target))
        return str(target)
    except Exception:
        if tmp.exists():
            shutil.rmtree(str(tmp))
        raise
