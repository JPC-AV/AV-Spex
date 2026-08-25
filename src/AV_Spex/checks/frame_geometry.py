"""
Active-area geometry helpers shared by frame analysis and its reporting.

Kept in their own module so the reporting side can use them without importing
the analysis module back (which would be a cycle).
"""

from AV_Spex.utils.log_setup import logger

def is_valid_active_area(active_area) -> bool:
    """True if `active_area` is a usable (x, y, w, h) crop rectangle.

    Truthiness is not validity: (0, 0, -1, -1) — what border detection produced
    when OpenCV reported -1 dimensions — passes `if active_area:` but formats to
    `crop=-1:-1:0:0`, which ffmpeg rejects with exit status 234.
    """
    if not active_area or len(active_area) != 4:
        return False
    x, y, w, h = active_area
    return w > 0 and h > 0 and x >= 0 and y >= 0

def sanitize_active_area(active_area, context: str = ""):
    """Return `active_area` if usable, else None (logging why, once per caller).

    Returning None rather than a bad tuple means every downstream
    `if active_area:` naturally degrades to whole-frame analysis instead of
    emitting a crop filter ffmpeg cannot parse.
    """
    if active_area is None or is_valid_active_area(active_area):
        return active_area
    where = f" in {context}" if context else ""
    logger.warning(
        f"  Ignoring unusable active area {tuple(active_area)}{where} — "
        f"border detection could not measure the frame; analyzing the full frame instead"
    )
    return None

def build_crop_filter(active_area, trailing_comma: bool = True) -> str:
    """Format `active_area` as an ffmpeg crop filter, or '' if it isn't usable."""
    if not is_valid_active_area(active_area):
        return ""
    x, y, w, h = active_area
    return f"crop={w}:{h}:{x}:{y}{',' if trailing_comma else ''}"
