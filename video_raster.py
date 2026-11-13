"""Small RGB rasterizer for Flux's offline optimization video renderer."""

import base64
from functools import lru_cache
import json
import math
from pathlib import Path
import zlib


def _rgb(color):
    if isinstance(color, str) and color.startswith('#'):
        return bytes.fromhex(color[1:])
    return bytes(color)


def _points(coordinates):
    if coordinates and isinstance(coordinates[0], (int, float)):
        return list(zip(coordinates[::2], coordinates[1::2]))
    return list(coordinates)


def _box(coordinates):
    (left, top), (right, bottom) = _points(coordinates)
    return round(left), round(top), round(right), round(bottom)


class _Image:
    mode = 'RGB'

    def __init__(self, size, color):
        self.size = self.width, self.height = size
        self.pixels = bytearray(_rgb(color) * (self.width * self.height))

    def tobytes(self):
        return bytes(self.pixels)

    def resize(self, size, resample=None):
        resized = _Image(size, (0, 0, 0))
        columns = [min(self.width - 1, int((x + 0.5) * self.width / resized.width)) * 3
                   for x in range(resized.width)]
        source_rows = {}
        for y in range(resized.height):
            source_y = min(self.height - 1, int((y + 0.5) * self.height / resized.height))
            if source_y not in source_rows:
                start = source_y * self.width * 3
                source = self.pixels[start:start + self.width * 3]
                source_rows[source_y] = b''.join(source[x:x + 3] for x in columns)
            start = y * resized.width * 3
            resized.pixels[start:start + resized.width * 3] = source_rows[source_y]
        return resized


class Image:
    LANCZOS = 1  # Compatibility token accepted by the offline nearest-neighbor resizer.

    @staticmethod
    def new(mode, size, color):
        if mode != 'RGB':
            raise ValueError('The offline video raster supports RGB only')
        return _Image(size, color)


@lru_cache(maxsize=1)
def _atlas():
    path = Path(__file__).with_name('video_font_atlas.json')
    return json.loads(path.read_text(encoding='utf-8'))


class _Font:
    def __init__(self, size, bold):
        atlas = _atlas()
        self.scale = size / atlas['size']
        self.data = atlas['bold' if bold else 'regular']
        self.glyphs = {}

    def glyph(self, char):
        if char not in self.data:
            char = '?'
        if char not in self.glyphs:
            width, height, left, top, advance, encoded = self.data[char]
            mask = base64.b64decode(encoded)
            scaled_width = max(1, round(width * self.scale)) if width else 0
            scaled_height = max(1, round(height * self.scale)) if height else 0
            alpha = bytearray()
            for y in range(scaled_height):
                source_y = max(0, min(height - 1, (y + 0.5) * height / scaled_height - 0.5))
                y0, y1 = int(source_y), min(int(source_y) + 1, height - 1)
                fraction_y = source_y - y0
                for x in range(scaled_width):
                    source_x = max(0, min(width - 1, (x + 0.5) * width / scaled_width - 0.5))
                    x0, x1 = int(source_x), min(int(source_x) + 1, width - 1)
                    fraction_x = source_x - x0
                    upper = mask[y0 * width + x0] * (1 - fraction_x) + mask[y0 * width + x1] * fraction_x
                    lower = mask[y1 * width + x0] * (1 - fraction_x) + mask[y1 * width + x1] * fraction_x
                    alpha.append(round(upper * (1 - fraction_y) + lower * fraction_y))
            self.glyphs[char] = (scaled_width, scaled_height, round(left * self.scale),
                                 round(top * self.scale), advance * self.scale, alpha)
        return self.glyphs[char]


@lru_cache(maxsize=32)
def _font(size, bold):
    return _Font(size, bold)


class ImageFont:
    @staticmethod
    def truetype(name, size):
        return _font(size, 'bold' in str(name).lower())

    @staticmethod
    def load_default():
        return _font(12, False)


class _Draw:
    def __init__(self, image):
        self.image = image
        self.width, self.height = image.size
        self.pixels = image.pixels

    def _span(self, y, left, right, rgb):
        left, right = max(0, left), min(self.width - 1, right)
        if left <= right and 0 <= y < self.height:
            start = (y * self.width + left) * 3
            count = right - left + 1
            self.pixels[start:start + count * 3] = rgb * count

    def rectangle(self, coordinates, fill=None, outline=None, width=1):
        left, top, right, bottom = _box(coordinates)
        rgb = _rgb(fill) if fill is not None else None
        border = _rgb(outline) if outline is not None else None
        for y in range(max(top, 0), min(bottom + 1, self.height)):
            if border is not None and (y < top + width or y > bottom - width):
                self._span(y, left, right, border)
                continue
            if rgb is not None:
                self._span(y, left, right, rgb)
            if border is not None:
                self._span(y, left, min(left + width - 1, right), border)
                self._span(y, max(right - width + 1, left), right, border)

    def _line(self, first, second, rgb, width):
        x0, y0 = map(round, first)
        x1, y1 = map(round, second)
        dx, dy = x1 - x0, y1 - y0
        before, after = (width - 1) // 2, width // 2
        if not dy:
            for y in range(max(0, y0 - before), min(self.height, y0 + after + 1)):
                self._span(y, min(x0, x1), max(x0, x1), rgb)
        elif not dx:
            for y in range(max(0, min(y0, y1)), min(self.height, max(y0, y1) + 1)):
                self._span(y, x0 - before, x0 + after, rgb)
        elif width > 1:
            scale = math.hypot(dx, dy)
            ox, oy = -dy / scale, dx / scale
            self._polygon([(x0 + ox * before, y0 + oy * before), (x1 + ox * before, y1 + oy * before),
                           (x1 - ox * after, y1 - oy * after), (x0 - ox * after, y0 - oy * after)], rgb)
        elif abs(dx) >= abs(dy):
            for x in range(max(0, min(x0, x1)), min(self.width, max(x0, x1) + 1)):
                self._span(round(y0 + (x - x0) * dy / dx), x, x, rgb)
        else:
            for y in range(max(0, min(y0, y1)), min(self.height, max(y0, y1) + 1)):
                x = round(x0 + (y - y0) * dx / dy)
                self._span(y, x, x, rgb)

    def line(self, coordinates, fill, width=1):
        points = _points(coordinates)
        rgb = _rgb(fill)
        for first, second in zip(points, points[1:]):
            self._line(first, second, rgb, width)

    def _polygon(self, points, rgb):
        if not points:
            return
        edges = []
        for first, second in zip(points, points[1:] + points[:1]):
            (x0, y0), (x1, y1) = sorted((first, second), key=lambda point: point[1])
            if y0 == y1:
                if y0 == round(y0):
                    self._span(round(y0), round(min(x0, x1)), round(max(x0, x1)), rgb)
            else:
                edges.append((x0, y0, y1, (x1 - x0) / (y1 - y0)))
        top = max(0, math.ceil(min(point[1] for point in points)))
        bottom = min(self.height - 1, math.floor(max(point[1] for point in points)))
        for y in range(top, bottom + 1):
            intersections = sorted(x + (y - start) * slope for x, start, end, slope in edges
                                   if start <= y < end)
            for left, right in zip(intersections[::2], intersections[1::2]):
                self._span(y, round(left), round(right), rgb)

    def polygon(self, coordinates, fill):
        self._polygon(_points(coordinates), _rgb(fill))

    def ellipse(self, coordinates, fill):
        left, top, right, bottom = _box(coordinates)
        rgb = _rgb(fill)
        center_x, center_y = (left + right) / 2, (top + bottom) / 2
        radius_x, radius_y = (right - left) / 2, (bottom - top) / 2
        for y in range(max(0, top), min(self.height, bottom + 1)):
            radius = radius_x * math.sqrt(max(0, 1 - ((y - center_y) / radius_y) ** 2)) if radius_y else radius_x
            self._span(y, math.ceil(center_x - radius), math.floor(center_x + radius), rgb)

    def _rounded_spans(self, box, radius, rgb):
        left, top, right, bottom = box
        radius = max(0, min(radius, (right - left) / 2, (bottom - top) / 2))
        for y in range(max(0, top), min(self.height, bottom + 1)):
            offset = max(0, radius - min(y - top, bottom - y))
            inset = round(radius - math.sqrt(max(0, radius * radius - offset * offset)))
            self._span(y, left + inset, right - inset, rgb)

    def rounded_rectangle(self, coordinates, radius=0, fill=None, outline=None):
        left, top, right, bottom = _box(coordinates)
        if outline is not None:
            self._rounded_spans((left, top, right, bottom), radius, _rgb(outline))
            left, top, right, bottom = left + 1, top + 1, right - 1, bottom - 1
            radius = max(0, radius - 1)
        if fill is not None:
            self._rounded_spans((left, top, right, bottom), radius, _rgb(fill))

    @staticmethod
    def textlength(text, font=None):
        font = font or ImageFont.load_default()
        return sum(font.glyph(char)[4] for char in text)

    @staticmethod
    def _text_layout(coordinates, text, font, anchor):
        glyphs = []
        x, y = coordinates
        cursor = 0
        left = right = 0
        top = bottom = None
        for char in text:
            width, height, offset_x, offset_y, advance, alpha = font.glyph(char)
            glyph_x = round(cursor) + offset_x
            if width and height:
                glyphs.append((glyph_x, offset_y, width, height, alpha))
                left = min(left, glyph_x)
                right = max(right, glyph_x + width)
                top = offset_y if top is None else min(top, offset_y)
                bottom = offset_y + height if bottom is None else max(bottom, offset_y + height)
            cursor += advance
        top, bottom = top or 0, bottom or 0
        if anchor:
            if anchor[0] == 'm':
                x -= cursor / 2
            elif anchor[0] == 'r':
                x -= cursor
            if anchor[1] == 't':
                y -= top
            elif anchor[1] == 'm':
                y -= (top + bottom) / 2
        x, y = round(x), round(y)
        return x, y, (x + left, y + top, x + max(right, round(cursor)), y + bottom), glyphs

    def textbbox(self, coordinates, text, font=None, anchor=None):
        return self._text_layout(coordinates, text, font or ImageFont.load_default(), anchor)[2]

    def text(self, coordinates, text, fill, font=None, anchor=None):
        x, y, _, glyphs = self._text_layout(coordinates, text, font or ImageFont.load_default(), anchor)
        rgb = _rgb(fill)
        for offset_x, offset_y, width, height, alpha in glyphs:
            left, top = x + offset_x, y + offset_y
            for row in range(max(0, -top), min(height, self.height - top)):
                start = ((top + row) * self.width + left) * 3
                for col in range(max(0, -left), min(width, self.width - left)):
                    opacity = alpha[row * width + col]
                    if not opacity:
                        continue
                    index = start + col * 3
                    if opacity == 255:
                        self.pixels[index:index + 3] = rgb
                    else:
                        for channel in range(3):
                            old = self.pixels[index + channel]
                            self.pixels[index + channel] = (old * (255 - opacity) + rgb[channel] * opacity + 127) // 255


class ImageDraw:
    Draw = _Draw
