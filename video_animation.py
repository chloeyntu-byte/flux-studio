"""Write and play Flux's full-frame RGB APNG exports with the standard library."""

from fractions import Fraction
import os
from pathlib import Path
import stat
import struct
import tempfile
import zlib


_SIGNATURE = b'\x89PNG\r\n\x1a\n'
_CHUNK_HEADER = struct.Struct('>I4s')
_UINT = struct.Struct('>I')
_IMAGE_HEADER = struct.Struct('>IIBBBBB')
_ANIMATION_CONTROL = struct.Struct('>II')
_FRAME_CONTROL = struct.Struct('>IIIIIHHBB')
_MAX_PIXELS = 4096 * 2160
_MAX_FRAMES = 100000


def _check_size(width, height):
    if (type(width) is not int or type(height) is not int or
            not 1 <= width <= 8192 or not 1 <= height <= 8192 or width * height > _MAX_PIXELS):
        raise ValueError('Animation frame size is outside the supported bounds.')


def _chunk_bytes(kind, data):
    checksum = zlib.crc32(data, zlib.crc32(kind)) & 0xffffffff
    return _CHUNK_HEADER.pack(len(data), kind) + data + _UINT.pack(checksum)


def _frame_control(sequence, width, height, delay):
    # Every frame covers the full canvas and replaces it. No compositing needed.
    return _FRAME_CONTROL.pack(sequence, width, height, 0, 0, *delay, 0, 0)


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError('Video export cancelled.')


def encode_animation(destination, size, fps, frames, end_seconds=3, cancel_event=None):
    """Stream candidate RGB frames and one final chart to an atomic APNG file."""
    width, height = size
    _check_size(width, height)
    if type(fps) is not int or not 1 <= fps <= 120:
        raise ValueError('Animation frame rate must be an integer from 1 to 120.')
    if type(end_seconds) not in (int, float) or not 0 < end_seconds <= 65535:
        raise ValueError('Animation ending hold must be positive and at most 65535 seconds.')
    end_delay = Fraction(str(end_seconds)).limit_denominator(1000)
    if not 1 <= end_delay.numerator <= 65535:
        raise ValueError('Animation ending hold cannot be represented in APNG.')
    destination = Path(destination).expanduser().absolute()
    if destination.suffix.lower() not in ('.png', '.apng'):
        raise ValueError('The standalone animation needs a .png or .apng filename.')
    row_bytes = width * 3
    frame_bytes = row_bytes * height
    _check_cancel(cancel_event)
    with tempfile.NamedTemporaryFile(mode='w+b', prefix='.flux-video-', suffix='.png',
                                     dir=str(destination.parent), delete=False) as output:
        partial = Path(output.name)
        try:
            output.write(_SIGNATURE)
            output.write(_chunk_bytes(b'IHDR', _IMAGE_HEADER.pack(width, height, 8, 2, 0, 0, 0)))
            control_position = output.tell()
            output.write(_chunk_bytes(b'acTL', _ANIMATION_CONTROL.pack(1, 0)))
            count = sequence = 0
            for frame in frames:
                _check_cancel(cancel_event)
                if count == _MAX_FRAMES:
                    raise ValueError('Animation exceeds the supported frame count.')
                if not isinstance(frame, (bytes, bytearray)) or len(frame) != frame_bytes:
                    raise ValueError('Each animation frame must be packed RGB bytes matching its dimensions.')
                # PNG scanline filter 0. A single frame's data is held in memory.
                rows = b''.join(b'\0' + frame[start:start+row_bytes]
                                for start in range(0, frame_bytes, row_bytes))
                data = zlib.compress(rows, 6)
                last_frame_position = output.tell()
                last_frame_sequence = sequence
                output.write(_chunk_bytes(b'fcTL', _frame_control(sequence, width, height, (1, fps))))
                sequence += 1
                if count == 0:
                    output.write(_chunk_bytes(b'IDAT', data))
                else:
                    output.write(_chunk_bytes(b'fdAT', _UINT.pack(sequence) + data))
                    sequence += 1
                count += 1
            _check_cancel(cancel_event)
            if count < 2:
                raise ValueError('Animation requires at least one candidate and an ending chart.')
            output.write(_chunk_bytes(b'IEND', b''))
            # Patch fixed-size control chunks so the sequence stays streaming and
            # the ending chart is encoded only once, with its longer hold time.
            output.seek(control_position)
            output.write(_chunk_bytes(b'acTL', _ANIMATION_CONTROL.pack(count, 0)))
            output.seek(last_frame_position)
            output.write(_chunk_bytes(b'fcTL', _frame_control(last_frame_sequence, width, height,
                                                             (end_delay.numerator, end_delay.denominator))))
            output.flush()
            _check_cancel(cancel_event)
            os.replace(str(partial), str(destination))
            return count
        finally:
            try:
                partial.unlink()
            except FileNotFoundError:
                pass


def _read_chunk(stream, kind, limit):
    header = stream.read(_CHUNK_HEADER.size)
    if len(header) != _CHUNK_HEADER.size:
        raise ValueError('Truncated animation chunk header.')
    length, actual_kind = _CHUNK_HEADER.unpack(header)
    if actual_kind != kind or length > limit:
        raise ValueError('Unexpected or oversized animation chunk.')
    data = stream.read(length)
    checksum = stream.read(_UINT.size)
    if len(data) != length or len(checksum) != _UINT.size:
        raise ValueError('Truncated animation chunk.')
    expected_checksum = zlib.crc32(data, zlib.crc32(kind)) & 0xffffffff
    if _UINT.unpack(checksum)[0] != expected_checksum:
        raise ValueError('Animation chunk checksum mismatch.')
    return data


class Animation:
    """Index a full-frame Flux APNG without retaining its pixels in memory."""

    def __init__(self, path):
        descriptor = os.open(str(Path(path).expanduser()), os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
        self._stream = os.fdopen(descriptor, 'rb')
        try:
            if not stat.S_ISREG(os.fstat(self._stream.fileno()).st_mode):
                raise ValueError('Animation must be a regular file.')
            self._read_index()
        except BaseException:
            self.close()
            raise

    def _read_index(self):
        stream = self._stream
        if stream.read(len(_SIGNATURE)) != _SIGNATURE:
            raise ValueError('File is not a PNG animation.')
        self._header = _read_chunk(stream, b'IHDR', _IMAGE_HEADER.size)
        if len(self._header) != _IMAGE_HEADER.size:
            raise ValueError('Invalid animation image header.')
        width, height, *format_fields = _IMAGE_HEADER.unpack(self._header)
        _check_size(width, height)
        if format_fields != [8, 2, 0, 0, 0]:
            raise ValueError('Expected a Flux full-frame RGB animation.')
        self.size = width, height
        control = _read_chunk(stream, b'acTL', _ANIMATION_CONTROL.size)
        if len(control) != _ANIMATION_CONTROL.size:
            raise ValueError('Invalid animation control chunk.')
        count, _plays = _ANIMATION_CONTROL.unpack(control)
        if not 1 <= count <= _MAX_FRAMES:
            raise ValueError('Invalid animation frame count.')
        raw_size = (width * 3 + 1) * height
        self._limit = raw_size + raw_size // 100 + 1024
        self._offsets = []
        self.delays = []
        sequence = 0
        for index in range(count):
            control = _read_chunk(stream, b'fcTL', _FRAME_CONTROL.size)
            if len(control) != _FRAME_CONTROL.size:
                raise ValueError('Invalid animation frame control chunk.')
            ident, w, h, x, y, numerator, denominator, dispose, blend = _FRAME_CONTROL.unpack(control)
            if (ident != sequence or (w, h) != self.size or x or y or dispose or blend or not numerator):
                raise ValueError('Unsupported or invalid full-frame animation control.')
            self.delays.append(numerator / (denominator or 100))
            sequence += 1
            self._offsets.append(stream.tell())
            data = _read_chunk(stream, b'fdAT' if index else b'IDAT', self._limit)
            if index:
                if len(data) <= 4 or _UINT.unpack(data[:4])[0] != sequence:
                    raise ValueError('Invalid animation data sequence.')
                sequence += 1
            elif not data:
                raise ValueError('Empty animation frame.')
        _read_chunk(stream, b'IEND', 0)
        if stream.read(1):
            raise ValueError('Unexpected bytes after the end of the animation.')

    def _frame_data(self, index):
        if type(index) is not int or not 0 <= index < len(self._offsets):
            raise IndexError('Animation frame index is out of range.')
        self._stream.seek(self._offsets[index])
        data = _read_chunk(self._stream, b'fdAT' if index else b'IDAT', self._limit)
        if index:
            if len(data) <= 4 or _UINT.unpack(data[:4])[0] != index * 2:
                raise ValueError('Invalid animation data sequence.')
            data = data[4:]
        if not data:
            raise ValueError('Empty animation frame.')
        return data

    def png_frame(self, index):
        """Return one independently decodable PNG."""
        data = self._frame_data(index)
        return (_SIGNATURE + _chunk_bytes(b'IHDR', self._header) +
                _chunk_bytes(b'IDAT', data) + _chunk_bytes(b'IEND', b''))

    def ppm_frame(self, index):
        """Return a PPM frame for Tk builds without a PNG image reader."""
        width, height = self.size
        stride = width * 3 + 1
        expected_size = stride * height
        decoder = zlib.decompressobj()
        try:
            rows = decoder.decompress(self._frame_data(index), expected_size + 1)
        except zlib.error as exc:
            raise ValueError('Invalid animation frame compression.') from exc
        if len(rows) != expected_size or not decoder.eof or decoder.unused_data:
            raise ValueError('Invalid animation frame scanline size.')
        if any(rows[::stride]):
            raise ValueError('Unsupported animation scanline filter.')
        header = ('P6\n%d %d\n255\n' % self.size).encode('ascii')
        return header + b''.join(rows[start + 1:start + stride] for start in range(0, expected_size, stride))

    def close(self):
        self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
