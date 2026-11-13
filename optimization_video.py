"""Export a saved Flux optimization as a video without opening a GUI."""

import argparse
from pathlib import Path
import signal
import sys
import threading

from emx_search import evaluate_response, pareto
from result_data import hydrate_row, load_campaign
from video_chart import render_results
from video_layout import index_legacy_receipts, load_geometry, render_candidate


DEFAULT_FPS = 8
END_SECONDS = 3
FRAME_SIZE = (1280, 720)
PORTABLE_VIDEO_NAME = 'optimization-history.png'
FORMATS = {'apng': '.png', 'mp4': '.mp4', 'webm': '.webm'}


def _ordered_rows(rows):
    ordered = []
    for position, row in enumerate(rows, 1):
        index = row.get('index', position)
        if type(index) is not int or index < 1:
            raise ValueError('Candidate indices must be positive integers.')
        ordered.append(dict(row, index=index))
    if len({row['index'] for row in ordered}) != len(ordered):
        raise ValueError('Duplicate candidate indices prevent reliable playback order.')
    return sorted(ordered, key=lambda row: row['index'])


def _chart_row(row, goals, constraints):
    verified = hydrate_row(row)
    metrics = verified.get('metrics', [])
    selected = [sample for sample in metrics if any(
        abs(sample['frequency_hz'] - goal['frequency_hz']) <= goal['frequency_hz'] * 1e-12
        for goal in goals)]
    record = dict(index=row['index'], id=row['id'], status=row['status'], generation=row.get('generation'),
                  metrics=selected, feasible=None, data_error=verified.get('data_error'))
    if row['status'] == 'completed':
        try:
            record.update(evaluate_response(metrics, goals, constraints))
        except (ValueError, TypeError, KeyError):
            pass  # The ending chart can still show any individual measured goals.
    return record


def export_optimization(rows, source, config, directory, fps=DEFAULT_FPS,
                        output=None, cancel_event=None, progress=None, output_format=None):
    """Render one frame per candidate and retain only the finished video."""
    if config.get('mode') != 'optimize' or not config.get('objectives'):
        raise ValueError('Video export requires optimization results with at least one objective.')
    if type(fps) is not int or not 1 <= fps <= 60:
        raise ValueError('Video FPS must be an integer from 1 to 60.')
    ordered = _ordered_rows(rows)
    if not ordered:
        raise ValueError('This optimization has no candidates to export.')
    destination = Path(output).expanduser() if output else None
    if output_format is None:
        output_format = next((name for name, suffix in FORMATS.items()
                              if destination is not None and destination.suffix.lower() == suffix),
                             'apng')
    extension = FORMATS.get(output_format)
    if not extension or (destination is not None and destination.suffix.lower() != extension):
        raise ValueError('Choose a matching output extension: .png (APNG), .mp4 or .webm.')
    filename = PORTABLE_VIDEO_NAME if output_format == 'apng' else 'optimization-history' + extension
    destination = (destination or (Path(directory) / filename)).absolute()

    def check_cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError('Video export cancelled.')

    check_cancelled()
    legacy_receipts = index_legacy_receipts(ordered, cancel_event=cancel_event)
    goals = config['objectives']
    constraints = config.get('constraints', [])
    chart_rows = []
    missing_layout = 0
    total = len(ordered)

    def frames():
        nonlocal missing_layout
        for position, row in enumerate(ordered, 1):
            check_cancelled()
            geometry, reason = load_geometry(row, source, cancel_event=cancel_event,
                                             legacy_receipts=legacy_receipts)
            if geometry is None:
                missing_layout += 1
            chart_rows.append(_chart_row(row, goals, constraints))
            image = render_candidate(row, geometry, reason, position, total, size=FRAME_SIZE)
            if progress:
                progress('Rendering candidate {} / {}'.format(position, total))
            yield image.tobytes()
        if missing_layout == total:
            raise ValueError('No candidate layout geometry is available. Use the original campaign with its geometry receipts.')
        check_cancelled()
        if progress:
            progress('Encoding optimization results…')
        frontier = {str(row['id']) for row in pareto(chart_rows)}
        yield render_results(chart_rows, config, size=FRAME_SIZE, pareto_ids=frontier).tobytes()

    if output_format == 'apng':
        from video_animation import encode_animation
        count = encode_animation(destination, FRAME_SIZE, fps, frames(), end_seconds=END_SECONDS,
                                 cancel_event=cancel_event)
        encoder_name = 'Flux APNG'
    else:
        from video_encoder import choose_encoder, encode_video
        encoder = choose_encoder(extension)

        def video_frames():
            for frame in frames():
                yield frame
            for _ in range(END_SECONDS * fps - 1):
                yield frame

        count = encode_video(encoder, destination, FRAME_SIZE, fps, video_frames(), cancel_event=cancel_event)
        encoder_name = encoder.name
    return dict(path=str(destination), encoder=encoder_name, fps=fps, frames=count,
                candidates=total, layouts=total - missing_layout, missing_layouts=missing_layout,
                end_seconds=END_SECONDS, duration_seconds=total/fps + END_SECONDS)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('campaign', type=Path, help='Folder containing campaign.json and index.json')
    parser.add_argument('--fps', type=int, default=DEFAULT_FPS, help='Frames per second (1–60, default: %(default)s)')
    parser.add_argument('-o', '--output', type=Path, help='Video destination (default: optimization-history in the campaign folder)')
    parser.add_argument('--format', choices=sorted(FORMATS), help='Default: built-in APNG (.png), or infer from --output')
    options = parser.parse_args(argv)
    cancel = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: cancel.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        snapshot = load_campaign(options.campaign)
        result = export_optimization(snapshot['rows'], snapshot['source'], snapshot['config'],
                                     options.campaign, options.fps, options.output, cancel,
                                     output_format=options.format)
    except InterruptedError:
        print('Video export cancelled.', file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        print('Video export failed. {}'.format(exc), file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(result['path'])
    print('{} layouts, {} status frames, {} fps, {} frames.'.format(
        result['layouts'], result['missing_layouts'], result['fps'], result['frames']), file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
