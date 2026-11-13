"""Stream Flux RGB frames to an installed local video encoder."""

import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import tempfile
import time
from typing import NamedTuple


class Encoder(NamedTuple):
    name: str
    extension: str
    executable: str


def _probe(argv):
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                encoding='utf-8', errors='replace', timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return result.stdout if result.returncode == 0 else ''


def choose_encoder(extension=None):
    """Find a usable FFmpeg MP4 or GStreamer WebM encoder for this user."""
    override = os.environ.get('TFGEN_FFMPEG') if extension != '.webm' else None
    choices = [str(Path(override).expanduser())] if override else [
        'ffmpeg', str(Path.home() / '.local' / 'bin' / 'ffmpeg')]
    for choice in choices if extension != '.webm' else ():
        executable = shutil.which(choice)
        if executable and re.search(r'^\s*V\S*\s+libx264\s',
                                    _probe([executable, '-hide_banner', '-encoders']), re.M):
            return Encoder('FFmpeg', '.mp4', str(Path(executable).absolute()))
    if override:
        raise RuntimeError('TFGEN_FFMPEG must identify a runnable FFmpeg with libx264 support.')

    if extension == '.mp4':
        raise RuntimeError('MP4 export needs FFmpeg with libx264. The built-in APNG export needs no encoder.')
    executable = shutil.which('gst-launch-1.0')
    inspector = shutil.which('gst-inspect-1.0')
    if executable and inspector:
        features = set(_probe([inspector, '--print-plugin-auto-install-info']).splitlines())
        required = {'element-' + name for name in
                    ('fdsrc', 'rawvideoparse', 'videoconvert', 'vp8enc', 'webmmux', 'filesink')}
        if required <= features:
            return Encoder('GStreamer', '.webm', str(Path(executable).absolute()))
    raise RuntimeError('Video export needs FFmpeg with libx264 or GStreamer with VP8/WebM support. '
                       'Set TFGEN_FFMPEG to an existing executable if it is outside PATH.')


def _command(encoder, width, height, fps):
    # Both tools open stdout as a seekable file supplied by the caller.
    if encoder.name == 'FFmpeg':
        return [encoder.executable, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                '-f', 'rawvideo', '-pixel_format', 'rgb24', '-video_size', '%dx%d' % (width, height),
                '-framerate', str(fps), '-i', 'pipe:0', '-an', '-c:v', 'libx264',
                '-threads', '2', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart', '-f', 'mp4', '/dev/stdout']
    if encoder.name == 'GStreamer':
        # Pass the file through stdout, keeping user paths out of pipeline syntax.
        return [encoder.executable, '-q', 'fdsrc', 'fd=0', '!', 'rawvideoparse', 'format=rgb',
                'width=%d' % width, 'height=%d' % height, 'framerate=%d/1' % fps, '!',
                'videoconvert', '!', 'vp8enc', 'deadline=1', 'cpu-used=4', 'threads=2',
                'target-bitrate=1800000', '!', 'webmmux', '!', 'filesink', 'location=/dev/stdout']
    raise ValueError('Unknown video encoder.')


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError('Video export cancelled.')


def _stream_frames(process, frames, frame_bytes, cancel_event):
    writer = process.stdin.fileno()
    reader = process.stderr.fileno()
    os.set_blocking(writer, False)
    os.set_blocking(reader, False)
    diagnostics = bytearray()

    def read_errors():
        try:
            diagnostics.extend(os.read(reader, 8192))
        except BlockingIOError:
            return
        del diagnostics[:-8192]

    def encoder_error():
        read_errors()
        return RuntimeError('Video encoder failed (exit %s). %s' % (
            process.poll(), diagnostics.decode('utf-8', errors='replace').strip()))

    count = 0
    for frame in frames:
        _check_cancel(cancel_event)
        if not isinstance(frame, (bytes, bytearray)) or len(frame) != frame_bytes:
            raise ValueError('Each video frame must be packed RGB bytes matching the output size.')
        remaining = memoryview(frame)
        deadline = time.monotonic() + 60
        while remaining:
            _check_cancel(cancel_event)
            if process.poll() is not None:
                raise encoder_error()
            if time.monotonic() >= deadline:
                raise RuntimeError('Video encoder stopped accepting frames.')
            _, writable, _ = select.select([], [writer], [], .1)
            read_errors()
            if writable:
                try:
                    written = os.write(writer, remaining[:65536])
                except BlockingIOError:
                    continue
                except BrokenPipeError:
                    raise encoder_error() from None
                remaining = remaining[written:]
                deadline = time.monotonic() + 60
        count += 1
    if not count:
        raise ValueError('There are no video frames to export.')

    process.stdin.close()
    deadline = time.monotonic() + 60
    while process.poll() is None:
        _check_cancel(cancel_event)
        if time.monotonic() >= deadline:
            raise RuntimeError('Video encoder did not finish the file.')
        read_errors()
        time.sleep(.05)
    if process.returncode != 0:
        raise encoder_error()
    return count


def encode_video(encoder, destination, size, fps, frames, cancel_event=None):
    """Encode a lazy iterable of packed RGB frames, then publish one video file."""
    width, height = size
    if (type(width) is not int or type(height) is not int or
            width <= 0 or height <= 0 or width % 2 or height % 2 or
            (encoder.name == 'GStreamer' and width % 4)):
        raise ValueError('Video dimensions must be positive and even, with WebM width a multiple of four.')
    if type(fps) is not int or not 1 <= fps <= 120:
        raise ValueError('Video frame rate must be a whole number between 1 and 120.')
    destination = Path(destination).expanduser().absolute()
    if destination.suffix.lower() != encoder.extension:
        raise ValueError('The selected encoder requires a %s output file.' % encoder.extension)
    _check_cancel(cancel_event)
    fd, temporary = tempfile.mkstemp(prefix='.flux-video-', suffix=encoder.extension,
                                     dir=str(destination.parent))
    partial = Path(temporary)
    process = None
    try:
        process = subprocess.Popen(_command(encoder, width, height, fps),
                                   stdin=subprocess.PIPE, stdout=fd,
                                   stderr=subprocess.PIPE, bufsize=0)
        count = _stream_frames(process, frames, width * height * 3, cancel_event)
        _check_cancel(cancel_event)
        encoded = os.fstat(fd)
        if not encoded.st_size:
            raise RuntimeError('The video encoder produced an empty output file.')
        expected = partial.lstat()
        if (encoded.st_dev, encoded.st_ino) != (expected.st_dev, expected.st_ino):
            raise RuntimeError('The temporary video file changed during export.')
        os.replace(str(partial), str(destination))
        return count
    finally:
        os.close(fd)
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdin.close()
            process.stderr.close()
        try:
            partial.unlink()
        except FileNotFoundError:
            pass
