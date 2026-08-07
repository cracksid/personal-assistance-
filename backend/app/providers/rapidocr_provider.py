"""
Text extraction using RapidOCR.

RapidOCR runs the PaddleOCR models on onnxruntime -- already installed for
the memory index -- so it needs no extra runtime, no GPU, and no external
binary. That last point matters: the usual alternative, Tesseract, is a
separate Windows installer rather than a pip package.

This is the only module that knows RapidOCR exists.
"""

import io
import logging

import numpy as np
from PIL import Image

from app.providers.vision import OCRLine, OCRProvider, OCRResult, VisionProviderError

logger = logging.getLogger(__name__)


class RapidOCRProvider(OCRProvider):
    """Reads literal text out of images, locally."""

    def __init__(self) -> None:
        self._engine = None
        logger.info("RapidOCR ready")

    def _get_engine(self):
        """Loaded on first use -- construction reads model files from disk."""
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR

            logger.info("Loading RapidOCR models")
            self._engine = RapidOCR()
        return self._engine

    def read_text(self, image: bytes) -> OCRResult:
        if not image:
            raise VisionProviderError("No image was supplied.")

        engine = self._get_engine()

        try:
            # RapidOCR wants an array of pixels, not an encoded file, so the
            # PNG/JPEG has to be decoded first. RGB conversion matters: a
            # screenshot may arrive as RGBA, and the alpha channel would
            # confuse a model expecting three channels.
            picture = Image.open(io.BytesIO(image)).convert("RGB")
            found, _timings = engine(np.array(picture))

        except Exception as exc:
            raise VisionProviderError(f"OCR failed: {exc}") from exc

        if not found:
            # No text in the picture. A normal outcome, not an error.
            return OCRResult(text="", lines=[])

        lines: list[OCRLine] = []
        for _box, text, confidence in found:
            # RapidOCR returns confidence as a STRING, not a float. Passing
            # it straight into a float field would raise deep inside
            # Pydantic with a confusing message.
            lines.append(OCRLine(text=text, confidence=float(confidence)))

        return OCRResult(text="\n".join(line.text for line in lines), lines=lines)
