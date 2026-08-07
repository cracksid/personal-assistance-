"""
Screen capture.

Not a provider behind an ABC: there is exactly one way to photograph a
screen, and no plausible second implementation to swap in. An abstraction
with one implementation and no second candidate is cost without benefit.

mss rather than Pillow's ImageGrab or pyautogui: it is small, fast, and
handles multi-monitor setups by number rather than guesswork.
"""

import io
import logging

import mss
from PIL import Image

from app.config import settings
from app.providers.vision import VisionProviderError

logger = logging.getLogger(__name__)


def capture_screen(monitor: int = 1) -> bytes:
    """
    Photograph a monitor and return PNG bytes.

    Args:
        monitor: which display. mss numbers them from 1; index 0 is the
            union of every monitor, which is rarely what anyone wants.

    Raises:
        VisionProviderError: if capture fails or the monitor doesn't exist.
    """
    try:
        with mss.mss() as sct:
            # sct.monitors[0] is the combined virtual screen, so a request
            # for monitor N maps directly to index N.
            if monitor >= len(sct.monitors):
                available = len(sct.monitors) - 1
                raise VisionProviderError(
                    f"Monitor {monitor} does not exist ({available} attached)."
                )

            shot = sct.grab(sct.monitors[monitor])
            image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    except VisionProviderError:
        raise
    except Exception as exc:
        raise VisionProviderError(f"Screen capture failed: {exc}") from exc

    return encode_png(shrink_to_fit(image))


def shrink_to_fit(image: Image.Image) -> Image.Image:
    """
    Scale an image down so its longest side fits within the configured limit.

    Vision models bill by image size and get slower with it, and detail
    beyond roughly 1500px on the long edge buys nothing for reading a
    screen. Images already small enough are returned untouched -- scaling
    UP would invent detail and cost tokens for nothing.
    """
    limit = settings.vision_max_dimension
    longest = max(image.size)
    if longest <= limit:
        return image

    scale = limit / longest
    new_size = (round(image.width * scale), round(image.height * scale))
    logger.info("Scaling capture %s -> %s", image.size, new_size)

    # LANCZOS is the slowest but sharpest resampling filter. Worth it here:
    # blurry text is the one thing that would break both OCR and the model.
    return image.resize(new_size, Image.Resampling.LANCZOS)


def encode_png(image: Image.Image) -> bytes:
    """
    Encode to PNG in memory.

    PNG rather than JPEG because JPEG compression artefacts smear text,
    which is exactly what we are usually trying to read.
    """
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
