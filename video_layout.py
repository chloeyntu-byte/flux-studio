"""Read and render a campaign's layout previews without creating layout files."""
import colorsys
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat


ROOT = Path(__file__).resolve().parent
_GENERATION_DIR = re.compile(r'\d{8}-\d{6}_generate_[0-9a-f]+')
_SHA256 = re.compile(r'[0-9a-f]{64}')
_IDENTITY = ('library', 'cell', 'technology')
_BINDING = ('library', 'technology', 'workspace', 'cds_lib', 'library_path')
_READ_LIMIT = 32 * 1024 * 1024
_ROLE_COLORS = (
    ('body', '#79d1d5'), ('secondary', '#b6abd9'), ('crossing', '#dfb57a'),
    ('tap', '#e6a477'), ('pgs', '#a999ce'), ('ring', '#8ac7aa'),
    ('ground_route', '#849ee0'),
)
_MODEL_NAMES = {
    'transformer_implicit': 'Implicit coplanar transformer',
    'coplanar_transformer': 'Coplanar transformer',
    'stacked_transformer': 'Stacked transformer',
    'octagonal_inductor': 'Octagonal inductor',
    'differential_inductor': 'Differential inductor',
}
_STATUS = {
    'completed': ('Completed', '#8ac7aa'), 'failed': ('Failed', '#e0959a'),
    'rejected': ('Layout rejected', '#e0959a'), 'cancelled': ('Cancelled', '#dfb57a'),
}
_REASONS = {
    'Geometry receipt unavailable', 'Geometry receipt could not be verified',
    'Layout rejected during validation', 'Candidate stopped before layout generation',
    'Preview reconstructed from the frozen design',
}


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError('Video export cancelled.')


def _read_object(path):
    """Read a bounded regular JSON file, never a FIFO or a symbolic link."""
    if not path.is_absolute() or path.parent.is_symlink():
        raise ValueError('Invalid receipt path')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(str(path), flags), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _READ_LIMIT:
            raise ValueError('Invalid receipt file')
        data = stream.read(_READ_LIMIT + 1)
    if len(data) > _READ_LIMIT:
        raise ValueError('Receipt file is too large')
    value = json.loads(data.decode('utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Invalid receipt object')
    return value


def _identity(row, source):
    recorded = row.get('source') or {}
    design = row.get('design') or {}
    origin = source.get('identity') or {}
    return tuple(recorded.get(key) or design.get(key) or origin.get(key) for key in _IDENTITY)


def _receipt_key(receipt):
    identity = tuple(receipt.get(key) for key in _IDENTITY)
    digest = receipt.get('geometry_hash')
    if (receipt.get('ok') is not True or receipt.get('view') != 'layout'
            or not all(isinstance(value, str) and value for value in identity)
            or not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
        raise ValueError('Unverified layout receipt')
    return identity + (digest,)


def index_legacy_receipts(rows, cancel_event=None):
    """Resolve older candidates from their EMX job identity and geometry hash.

    The installed project's immediate generation receipts are scanned once per
    export. The match does not depend on which library is currently open.
    """
    wanted = {}
    for row in rows:
        _check_cancel(cancel_event)
        if (row.get('retention') or {}).get('handle') or not row.get('job_dir'):
            continue
        try:
            job = _read_object(Path(row['job_dir']) / 'job.json')
            job_source = job.get('source') or {}
            candidate_source = row.get('source') or {}
            identity = _identity(row, {})
            digest = job.get('geometry_sha256')
            if (not all(identity) or tuple(job_source.get(key) for key in _IDENTITY) != identity
                    or any(candidate_source.get(key) and candidate_source[key] != job_source.get(key)
                           for key in _BINDING)
                    or not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
                continue
            wanted.setdefault(identity + (digest,), []).append(str(row['id']))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    if not wanted:
        return {}
    indexed = {}
    runtime = ROOT / 'runtime'
    if runtime.is_symlink() or not runtime.is_dir():
        return indexed
    for directory in runtime.iterdir():
        _check_cancel(cancel_event)
        if not _GENERATION_DIR.fullmatch(directory.name) or directory.is_symlink():
            continue
        receipt = directory / 'result.json'
        try:
            key = _receipt_key(_read_object(receipt))
        except (OSError, ValueError, TypeError):
            continue
        if key in wanted and (directory / 'geometry.json').is_file():
            for ident in wanted.pop(key):
                indexed[ident] = (str(receipt), key[-1])
            if not wanted:
                break
    return indexed


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _box(bounds):
    if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
        if all(isinstance(point, (list, tuple)) and len(point) == 2 for point in bounds):
            bounds = tuple(bounds[0]) + tuple(bounds[1])
    if (isinstance(bounds, (list, tuple)) and len(bounds) == 4
            and all(_number(value) for value in bounds)
            and bounds[2] > bounds[0] and bounds[3] > bounds[1]):
        return tuple(bounds)
    return None


def geometry_bounds(geometry):
    """Return (xmin, ymin, xmax, ymax) for a drawable native preview."""
    if (isinstance(geometry, dict) and isinstance(geometry.get('polygons'), list)
            and geometry['polygons']):
        return _box(geometry.get('bounds'))
    return None


def _matching_context(row, source):
    if source.get('schema') != 'flux.emx.source.v1':
        return False
    entry = source.get('library') or {}
    if not isinstance(entry, dict) or not entry.get('available'):
        return False
    design = row.get('design') or {}
    identity = source.get('identity') or {}
    recorded = row.get('source') or {}
    return (bool(design.get('library')) and design.get('library') == identity.get('library')
            and bool(design.get('model')) and design.get('model') == (source.get('design') or {}).get('model')
            and all(identity.get(key) for key in _BINDING)
            and (not recorded or all(identity[key] == recorded.get(key) for key in _BINDING)))


def load_geometry(row, source, cancel_event=None, legacy_receipts=None):
    """Return (geometry, reason), using an original verified layout when possible."""
    _check_cancel(cancel_event)
    source = source or {}
    retention = row.get('retention') or {}
    handle = retention.get('handle') or {}
    paths = []
    expected_hash = handle.get('geometry_hash')
    if handle.get('receipt'):
        paths.append(handle['receipt'])
    elif legacy_receipts and str(row.get('id')) in legacy_receipts:
        path, expected_hash = legacy_receipts[str(row['id'])]
        paths.append(path)
    # On old or interrupted jobs log_path can still be a generation log.
    for name in (retention.get('receipt'), row.get('log_path')):
        if isinstance(name, str) and name:
            path = Path(name)
            directory = path if _GENERATION_DIR.fullmatch(path.name) else path.parent
            if _GENERATION_DIR.fullmatch(directory.name):
                paths.append(str(directory / 'result.json'))
    identity = _identity(row, source)
    geometry = None
    reason = 'Geometry receipt unavailable'
    for path in dict.fromkeys(paths):
        _check_cancel(cancel_event)
        try:
            path = Path(path)
            key = _receipt_key(_read_object(path))
            if key[:3] != identity or (expected_hash is not None and key[3] != expected_hash):
                raise ValueError('Layout identity does not match')
            original = _read_object(path.parent / 'geometry.json').get('geometry')
            digest = hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()
            if digest != key[3] or geometry_bounds(original) is None:
                raise ValueError('Layout geometry does not match')
        except OSError:
            continue
        except (ValueError, TypeError, OverflowError):
            reason = 'Geometry receipt could not be verified'
            continue
        geometry, reason = original, ''
        break
    frozen_match = _matching_context(row, source)
    if geometry is None:
        if row.get('status') == 'cancelled' and not row.get('design'):
            return None, 'Candidate stopped before layout generation'
        if frozen_match:
            _check_cancel(cancel_event)
            from backend import preview_in_context
            try:
                preview = preview_in_context(row['design'], source)
                if preview.get('ok') and geometry_bounds(preview.get('geometry')):
                    geometry, reason = preview['geometry'], 'Preview reconstructed from the frozen design'
            except (ValueError, TypeError, KeyError, RuntimeError):
                pass
        if geometry is None and row.get('status') == 'rejected':
            return None, 'Layout rejected during validation'
    if geometry is not None and frozen_match:
        geometry = dict(geometry, _video_layer_order={
            layer['name']: layer.get('order', 0) for layer in source['library'].get('layers', [])
        })
    _check_cancel(cancel_event)
    return geometry, reason


@lru_cache(maxsize=32)
def _font(size, bold=False):
    from video_images import image_modules
    _, _, ImageFont = image_modules()
    for family in ('DejaVuSans', 'LiberationSans'):
        name = family + ('-Bold.ttf' if bold else '.ttf')
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _nice_step(value):
    scale = 10 ** math.floor(math.log10(value))
    return max(tick * scale for tick in (.5, 1, 2, 5) if tick * scale <= value)


def _text_extent(draw, text, font):
    if hasattr(draw, 'textbbox'):
        _, _, right, bottom = draw.textbbox((0, 0), text, font=font)
        return right, bottom
    return draw.textsize(text, font=font)


def _layer_order(geometry):
    saved = geometry.get('_video_layer_order')
    if saved:
        return saved
    # Historical receipts include actual lower/upper via endpoints even if
    # the original PDK is unavailable. Preserve their relative paint order.
    polys = geometry.get('polygons', [])
    layers = list(dict.fromkeys(p['layer'] for p in polys if p.get('part') == 'pgs'))
    layers.extend(dict.fromkeys(p['layer'] for p in polys if p['layer'] not in layers))
    lower = {layer: set() for layer in layers}
    for via in geometry.get('vias', []):
        low, high = via.get('lower'), via.get('upper')
        if low in lower and high in lower and low != high:
            lower[high].add(low)
    rank = {}
    while layers:
        layer = next((name for name in layers if lower[name].issubset(rank)), layers[0])
        rank[layer] = len(rank)
        layers.remove(layer)
    return rank


def _port_colors(geometry):
    layers = sorted({item['layer'] for key in ('polygons', 'ports')
                     for item in geometry.get(key, []) if item.get('layer')})
    return {layer: tuple(round(v * 255) for v in colorsys.hsv_to_rgb(
        (.52 + index / len(layers)) % 1, .62, .98))
            for index, layer in enumerate(layers)}


def _draw_vias(draw, geometry, xy, scale):
    cut_budget = 1000
    for via in geometry.get('vias', []):
        box = _box(via.get('bbox'))
        if box is None:
            continue
        x0, y1 = xy(box[0], box[1])
        x1, y0 = xy(box[2], box[3])
        draw.rectangle((x0, y0, x1, y1), outline='#f1d5a4')
        rows, cols = via.get('rows', 0), via.get('columns', 0)
        if type(rows) is not int or type(cols) is not int or min(rows, cols) < 1 or rows * cols > cut_budget:
            continue
        size, gap, origin = via.get('cut_size'), via.get('cut_spacing'), via.get('origin')
        if not (size and gap and origin) or min(size) * scale < 1.5:
            continue
        cw, ch = size
        sx, sy = gap
        start_x = origin[0] - (cols * cw + (cols - 1) * sx) / 2
        start_y = origin[1] - (rows * ch + (rows - 1) * sy) / 2
        for row in range(rows):
            for col in range(cols):
                x = start_x + col * (cw + sx)
                y = start_y + row * (ch + sy)
                left, bottom = xy(x, y)
                right, top = xy(x + cw, y + ch)
                draw.rectangle((left, top, right, bottom), fill='#f4ddb3')
        cut_budget -= rows * cols


def _draw_ports(draw, geometry, xy, font, canvas_box):
    colors = _port_colors(geometry)
    labels = []
    for port in geometry.get('ports', []):
        box = _box(port.get('bbox'))
        if box is None:
            continue
        x0, y1 = xy(box[0], box[1])
        x1, y0 = xy(box[2], box[3])
        color = colors.get(port.get('layer'), '#95b3cb')
        draw.rectangle((x0, y0, x1, y1), fill=color)
        name = port.get('name')
        if isinstance(name, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_+.-]{0,19}', name):
            labels.append((name, x0, y0, x1, y1, color))
    placed = []
    left, top, right, bottom = canvas_box
    for name, x0, y0, x1, y1, color in labels:
        w, h = _text_extent(draw, name, font)
        w, h = w + 6, h + 4
        for x, y in ((x1 + 4, y0), (x0 - w - 4, y0), (x0, y1 + 4), (x0, y0 - h - 4)):
            if (left <= x and top <= y and x + w <= right and y + h <= bottom
                    and not any(x < a + w0 and x + w > a and y < b + h0 and y + h > b
                                for a, b, w0, h0 in placed)):
                placed.append((x, y, w, h))
                draw.rectangle((x, y, x + w, y + h), fill='#0b1b28', outline=color)
                draw.text((x + 3, y + 1), name, font=font, fill=color)
                break


def render_candidate(row, geometry, missing_reason, position, total, size=(1280, 720), bounds=None):
    """Rasterize one candidate frame, using an explicit status card if absent."""
    from video_images import image_modules
    Image, ImageDraw, _ = image_modules()
    width, height = size
    image = Image.new('RGB', size, '#0b131e')
    draw = ImageDraw.Draw(image)
    unit = min(width / 1280, height / 720)
    margin = max(8, round(36 * unit))
    header = round(90 * unit)
    footer = height - round(47 * unit)
    small = _font(max(8, round(16 * unit)))
    label = _font(max(8, round(12 * unit)))
    large = _font(max(10, round(29 * unit)), True)
    status, tone = _STATUS.get(row.get('status'), ('Candidate', '#89a0b3'))
    model = _MODEL_NAMES.get((row.get('design') or {}).get('model'), 'Passive device')
    index = row.get('index')
    generation = row.get('generation')
    count = 'Candidate {}'.format(index if type(index) is int and index > 0 else position)
    if type(generation) is int and generation >= 0:
        count += '  /  Generation {}'.format(generation + 1)
    draw.text((margin, round(14 * unit)), 'FLUX STUDIO  /  OPTIMIZATION', font=small, fill='#79d1d5')
    draw.text((margin, round(39 * unit)), model, font=large, fill='#e0ebf3')
    for text, y, color in ((count, 18, '#e0ebf3'), (status, 44, tone)):
        text_width, _ = _text_extent(draw, text, small)
        draw.text((width - margin - text_width, round(y * unit)), text, font=small, fill=color)
    draw.rectangle((margin, header, width - margin, footer), fill='#0f1c2a', outline='#2e4355')
    own_bounds = geometry_bounds(geometry)
    if own_bounds is None:
        reason = missing_reason if missing_reason in _REASONS else 'Geometry receipt unavailable'
        draw.text((2 * margin, header + round(48 * unit)), 'Layout preview unavailable',
                  fill='#e0ebf3', font=large)
        draw.text((2 * margin, header + round(94 * unit)), reason, fill='#89a0b3', font=small)
    else:
        common = _box(bounds) or own_bounds
        xmin, ymin = min(common[0], own_bounds[0]), min(common[1], own_bounds[1])
        xmax, ymax = max(common[2], own_bounds[2]), max(common[3], own_bounds[3])
        scale = min((width - 4 * margin) / (xmax - xmin),
                    (footer - header - 2 * margin) / (ymax - ymin))
        cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
        mx, my = width / 2, (header + footer) / 2

        def xy(x, y):
            return mx + (x - cx) * scale, my - (y - cy) * scale

        grid_step = _nice_step(max(xmax - xmin, ymax - ymin) / 12)
        for i in range(math.ceil((cx - (mx - margin) / scale) / grid_step),
                       math.floor((cx + (mx - margin) / scale) / grid_step) + 1):
            x, _ = xy(i * grid_step, 0)
            draw.line((x, header + 1, x, footer - 1), fill='#405666' if i == 0 else '#192c3b')
        for i in range(math.ceil((cy - (my - header) / scale) / grid_step),
                       math.floor((cy + (my - header) / scale) / grid_step) + 1):
            _, y = xy(0, i * grid_step)
            draw.line((margin + 1, y, width - margin - 1, y), fill='#405666' if i == 0 else '#192c3b')
        roles = (row.get('design') or {}).get('layers') or {}
        colors = {roles.get(role): color for role, color in reversed(_ROLE_COLORS) if roles.get(role)}
        order = _layer_order(geometry)
        for polygon in sorted(geometry['polygons'], key=lambda item: order.get(item['layer'], 100)):
            points = polygon.get('points') or []
            if len(points) >= 3:
                color = '#877aab' if polygon.get('part') == 'pgs' else colors.get(polygon['layer'], '#95b3cb')
                draw.polygon([xy(*point) for point in points], fill=color)
        _draw_vias(draw, geometry, xy, scale)
        _draw_ports(draw, geometry, xy, label, (margin, header, width - margin, footer))
        if missing_reason == 'Preview reconstructed from the frozen design':
            draw.text((margin + 6, header + 5), missing_reason, font=label, fill='#89a0b3')
        length = _nice_step((width - 2 * margin) / (8 * scale))
        start = width - margin - length * scale
        y = footer + round(18 * unit)
        draw.line((start, y, width - margin, y), fill='#79d1d5', width=max(1, round(2 * unit)))
        draw.text((start, y + 3), '{} um'.format('{:g}'.format(length)), font=label, fill='#89a0b3')
    draw.text((margin, footer + round(12 * unit)), 'Optimization order  /  {} of {}'.format(position, total),
              font=small, fill='#89a0b3')
    return image
