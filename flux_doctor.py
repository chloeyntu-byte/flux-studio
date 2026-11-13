"""Check Flux Studio capabilities without starting Cadence or installing packages."""

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import sys


ROOT = Path(__file__).resolve().parent
_PPM = b'P6\n1 1\n255\n\0\0\0'
_CORE_MODULES = ('app', 'backend', 'cadence', 'cell_lifecycle', 'emx_adapter',
                 'emx_campaign', 'emx_charts', 'emx_page', 'emx_search', 'evidence',
                 'flux_results', 'geometry', 'passive_symmetric', 'result_data',
                 'runtime_config', 'structures', 'technology')
_VIDEO_MODULES = ('optimization_video', 'video_animation', 'video_chart',
                  'video_encoder', 'video_images', 'video_layout', 'video_player',
                  'video_raster')
_VIDEO_RESOURCES = ('video_font_atlas.json', 'video_font_LICENSE.txt')


def _tk_checks(add, probe_display, tk_patchlevel, tk_photo):
    if tk_patchlevel is None:
        try:
            import tkinter as tk
            from tkinter import ttk
            tcl = tk.Tcl()
            if not ttk.Frame:
                raise ImportError('tkinter.ttk.Frame is unavailable')
            add('Core', 'PASS', 'Tkinter/Tcl', 'Tcl {}'.format(tcl.call('info', 'patchlevel')))
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            add('Core', 'FAIL', 'Tkinter/Tcl',
                '{}: {}. Use an existing Python with matching Tcl/Tk support.'.format(
                    type(exc).__name__, exc))
            return
        except tk.TclError as exc:
            add('Core', 'FAIL', 'Tcl data',
                '{}. Use the launch.sh runtime discovery or a matching Tcl installation.'.format(exc))
            return

        if not probe_display:
            detail = ('Open the check in a desktop session with --display.'
                      if os.environ.get('DISPLAY') else
                      'DISPLAY is unset. Run launch.sh --diagnose from the desktop session.')
            add('Core', 'NOT TESTED', 'Tk display', detail)
        else:
            try:
                root = tk.Tk()
                try:
                    root.withdraw()
                    root.update_idletasks()
                    tk_patchlevel = str(root.tk.call('package', 'require', 'Tk'))
                    try:
                        image = tk.PhotoImage(master=root, data=_PPM, format='ppm')
                        tk_photo = (image.width(), image.height()) == (1, 1)
                    except tk.TclError:
                        tk_photo = False
                finally:
                    root.destroy()
            except tk.TclError as exc:
                add('Core', 'FAIL', 'Tk display',
                    '{}. Check the desktop session, DISPLAY, XAUTHORITY and Tk data.'.format(exc))
                return

    if tk_patchlevel is not None:
        add('Core', 'PASS', 'Tk display', 'Tk {}'.format(tk_patchlevel))
    if tk_photo is True:
        add('Video', 'PASS', 'Built-in playback', 'Tk decoded a PPM frame.')
    elif tk_photo is False:
        add('Video', 'WARN', 'Built-in playback', 'Tk could not decode a PPM frame. Check this Python/Tk installation.')
    else:
        add('Video', 'NOT TESTED', 'Built-in playback',
            'Requires Tk photo support. Check in a desktop session.')


def _file_check(add, check, path, unavailable):
    if path.is_file() and os.access(str(path), os.R_OK):
        add('Native', 'PASS', check, str(path))
    else:
        add('Native', 'WARN', check, unavailable)


def _native_checks(add):
    import runtime_config as runtime

    environment = os.environ
    requested = environment.get('TFGEN_WORKSPACE')
    launch_dir = environment.get('TFGEN_LAUNCH_CWD') or os.getcwd()
    workspace = (Path(requested).expanduser().resolve() if requested else
                 runtime._workspace(launch_dir) or runtime._workspace(ROOT.parent))
    explicit = environment.get('TFGEN_VIRTUOSO')
    source = None
    if not explicit:
        try:
            source = runtime.discover_source_process(workspace)
        except (OSError, ValueError) as exc:
            add('Native', 'WARN', 'Virtuoso source', str(exc))
    if workspace is None and source:
        workspace = Path(source['workspace'])
    mapping = environment.get('TFGEN_CDS_LIB')
    if workspace is None or not workspace.is_dir():
        add('Native', 'WARN', 'Workspace mapping',
            'Open Flux from a layout menu or select a workspace with a readable cds.lib.')
    else:
        _file_check(add, 'Workspace mapping', runtime.resolve_mapping(workspace, mapping),
                    'Mapping not readable. Select the workspace cds.lib or set TFGEN_CDS_LIB.')

    search_path = (source or {}).get('environment', {}).get('PATH', environment.get('PATH'))
    virtuoso = runtime._executable(explicit or (source or {}).get('executable') or 'virtuoso',
                                   search_path)
    if virtuoso:
        found_by = 'TFGEN_VIRTUOSO' if explicit else ('running editor' if source else 'PATH')
        add('Native', 'PASS', 'Virtuoso executable', '{} ({})'.format(virtuoso, found_by))
    else:
        add('Native', 'WARN', 'Virtuoso executable',
            ('TFGEN_VIRTUOSO does not resolve to an executable.' if explicit else
             'Executable not found. Launch from a layout menu or set TFGEN_VIRTUOSO.'))

    tool_environment = environment.get('TFGEN_CADENCE_ENV')
    if tool_environment:
        _file_check(add, 'Cadence environment script', Path(tool_environment).expanduser(),
                    'TFGEN_CADENCE_ENV does not name a readable file.')
    emx = environment.get('TFGEN_EMX')
    emx_dir = environment.get('EMX_path') or environment.get('EMX_PATH')
    emx_binary = (runtime._executable(emx, search_path) if emx else
                  (runtime._executable(str(Path(emx_dir).expanduser() / 'emx'), search_path)
                   if emx_dir else None) or runtime._executable('emx', search_path))
    if emx_binary:
        add('Native', 'PASS', 'EMX executable', emx_binary)
    else:
        add('Native', 'WARN', 'EMX executable',
            ('TFGEN_EMX does not resolve to an executable.' if emx else
             'Executable not found in current path hints. An applied PDK may supply its own EMX configuration.'))

    strmout = environment.get('TFGEN_STRMOUT')
    stream_binary = (runtime._executable(strmout, search_path) if strmout else
                     (runtime._executable(str(Path(virtuoso).parent / 'strmout'), search_path)
                      if virtuoso else None) or runtime._executable('strmout', search_path))
    if stream_binary:
        add('Native', 'PASS', 'GDS exporter', stream_binary)
    else:
        add('Native', 'WARN', 'GDS exporter',
            'strmout not found. Check the Cadence installation or TFGEN_STRMOUT.')
    for variable, check in (('TFGEN_EMX_CONFIG', 'Configured EMX setup'),
                            ('TFGEN_EMX_PROCESS', 'Configured EMX process'),
                            ('TFGEN_EMX_LAYER_MAP', 'Configured EMX layer map')):
        if environment.get(variable):
            _file_check(add, check, Path(environment[variable]).expanduser(),
                        '{} does not name a readable file.'.format(variable))
    add('Native', 'NOT TESTED', 'PDK, native startup and licenses',
        'Apply a layout library to detect its attached PDK and EMX process. No native tool was launched.')


def _media_checks(add):
    missing = [name + '.py' for name in _VIDEO_MODULES if not (ROOT / (name + '.py')).is_file()]
    missing.extend(name for name in _VIDEO_RESOURCES if not (ROOT / name).is_file())
    try:
        import zlib
        if zlib.decompress(zlib.compress(b'flux')) != b'flux':
            raise ValueError('compression round trip failed')
    except (ImportError, OSError, ValueError) as exc:
        missing.append('Python zlib ({})'.format(exc))
    if missing:
        add('Video', 'WARN', 'Bundled APNG export',
            'Unavailable: {}. Re-extract the complete release or select a Python with zlib.'.format(
                ', '.join(missing)))
    else:
        add('Video', 'PASS', 'Bundled APNG export',
            'Renderer, font and Python zlib available. Ready to use on demand.')

    ffmpeg_override = os.environ.get('TFGEN_FFMPEG')
    ffmpeg = (shutil.which(str(Path(ffmpeg_override).expanduser())) if ffmpeg_override else
              shutil.which('ffmpeg') or shutil.which(str(Path.home() / '.local/bin/ffmpeg')))
    gstreamer = shutil.which('gst-launch-1.0') and shutil.which('gst-inspect-1.0')
    if ffmpeg_override and not ffmpeg:
        add('Video', 'WARN', 'Optional external export',
            'TFGEN_FFMPEG does not resolve to an executable. Bundled APNG remains available if its checks pass.')
    elif ffmpeg or gstreamer:
        tools = ', '.join(name for name, found in (('FFmpeg', ffmpeg), ('GStreamer', gstreamer)) if found)
        add('Video', 'NOT TESTED', 'Optional external export',
            '{} found. Encoder support is checked when exporting MP4 or WebM.'.format(tools))
    else:
        add('Video', 'INFO', 'Optional external export',
            'FFmpeg/GStreamer absent. Bundled APNG needs neither tool.')


def check_environment(probe_display=False, tk_patchlevel=None, tk_photo=None):
    """Return structured capability checks. Pass GUI-thread Tk facts for a worker call."""
    checks = []

    def add(section, status, check, detail):
        checks.append(dict(section=section, status=status, check=check,
                           detail=' '.join(str(detail).splitlines())))

    on_linux = sys.platform.startswith('linux')
    try:
        libc = os.confstr('CS_GNU_LIBC_VERSION') or ''
    except (AttributeError, OSError, ValueError):
        libc = ''
    add('Core', 'PASS' if on_linux else 'FAIL', 'Platform',
        '{} {}{}{}'.format(platform.system(), platform.machine(),
                           ' / ' + libc if libc else '',
                           '' if on_linux else '. Flux targets Linux.'))
    supported = sys.version_info >= (3, 7)
    add('Core', 'PASS' if supported else 'FAIL', 'Python',
        '{} ({}){}'.format(platform.python_version(), sys.executable,
                           '' if supported else '. Flux requires Python 3.7 or later.'))
    missing = [name for name in _CORE_MODULES if not (ROOT / (name + '.py')).is_file()]
    if missing:
        add('Core', 'FAIL', 'Project files',
            'Re-extract the release. Missing: {}.'.format(', '.join(name + '.py' for name in missing)))
    else:
        add('Core', 'PASS', 'Project files', 'Core application modules found.')

    _tk_checks(add, probe_display, tk_patchlevel, tk_photo)
    if on_linux and 'runtime_config' not in missing:
        try:
            _native_checks(add)
        except (ImportError, OSError, RuntimeError, ValueError):
            add('Native', 'WARN', 'Discovery', 'Native configuration could not be inspected.')
    _media_checks(add)
    return checks


def format_report(report):
    return '\n'.join('{status} [{section}] {check}: {detail}'.format(**entry) for entry in report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--display', action='store_true', help='also create a withdrawn Tk window and decode a PPM frame')
    parser.add_argument('--json', action='store_true', help='print the structured check results')
    args = parser.parse_args(argv)
    report = check_environment(probe_display=args.display)
    output = json.dumps(report, indent=2) if args.json else format_report(report)
    encoding = getattr(sys.stdout, 'encoding', None)
    if encoding:
        output = output.encode(encoding, errors='backslashreplace').decode(encoding)
    print(output)
    return int(any(entry['status'] == 'FAIL' for entry in report))


if __name__ == '__main__':
    raise SystemExit(main())
