"""Select image drawing locally to the optional video feature."""

from functools import lru_cache


@lru_cache(maxsize=1)
def image_modules():
    try:
        from PIL import Image, ImageDraw, ImageFont
        if hasattr(ImageDraw.ImageDraw, 'rounded_rectangle'):
            return Image, ImageDraw, ImageFont
    except (ImportError, OSError):
        pass
    from video_raster import Image, ImageDraw, ImageFont
    return Image, ImageDraw, ImageFont
