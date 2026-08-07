"""
Tests for the vision providers and the /vision endpoints.

No model is loaded and no screen is captured. Vision inference takes tens of
seconds and screen capture cannot be asserted on in CI, so the engines are
replaced with fakes. What is under test is our code -- request handling,
error translation, and the image maths -- not whether moondream can see.
"""

import io
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.api.deps import get_ocr, get_vision
from app.main import app
from app.providers import screen
from app.providers.vision import (
    OCRLine,
    OCRProvider,
    OCRResult,
    VisionProvider,
    VisionProviderError,
)


def make_png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, "PNG")
    return buffer.getvalue()


FAKE_PNG = make_png(40, 30)


class FakeVision(VisionProvider):
    def __init__(self, answer: str = "a cat on a keyboard") -> None:
        self.answer = answer
        self.received_image: bytes = b""
        self.received_prompt: str = ""

    async def describe(self, image: bytes, prompt: str) -> str:
        self.received_image = image
        self.received_prompt = prompt
        return self.answer


class FakeOCR(OCRProvider):
    def __init__(self, lines: list[OCRLine] | None = None) -> None:
        self.lines = lines if lines is not None else [OCRLine(text="hello", confidence=0.9)]

    def read_text(self, image: bytes) -> OCRResult:
        return OCRResult(text="\n".join(x.text for x in self.lines), lines=self.lines)


class BrokenVision(VisionProvider):
    async def describe(self, image: bytes, prompt: str) -> str:
        raise VisionProviderError("ANTHROPIC_API_KEY is not set.")


@pytest.fixture
def vision() -> FakeVision:
    return FakeVision()


@pytest.fixture
def client(vision: FakeVision) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_vision] = lambda: vision
    app.dependency_overrides[get_ocr] = lambda: FakeOCR()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# --- the abstractions ------------------------------------------------------


def test_incomplete_vision_providers_cannot_be_instantiated():
    class NoDescribe(VisionProvider):
        pass

    class NoReadText(OCRProvider):
        pass

    with pytest.raises(TypeError):
        NoDescribe()
    with pytest.raises(TypeError):
        NoReadText()


# --- image scaling ---------------------------------------------------------


def test_large_images_are_scaled_down_to_the_limit():
    """
    Vision models bill by image size, so an oversized capture is wasted
    money and latency for detail the model cannot use.
    """
    big = Image.new("RGB", (4000, 2000))

    result = screen.shrink_to_fit(big)

    assert max(result.size) == 1568
    # Aspect ratio preserved: a squashed screenshot is unreadable.
    assert result.width / result.height == pytest.approx(2.0, rel=0.01)


def test_small_images_are_left_alone():
    """
    Scaling UP would invent detail that is not there and cost tokens for
    inventing it.
    """
    small = Image.new("RGB", (800, 600))

    result = screen.shrink_to_fit(small)

    assert result.size == (800, 600)
    assert result is small  # returned untouched, not re-encoded


def test_encode_png_produces_a_real_png():
    data = screen.encode_png(Image.new("RGB", (10, 10)))

    assert data[:8] == b"\x89PNG\r\n\x1a\n"


# --- POST /vision/describe -------------------------------------------------


def test_describe_passes_image_and_prompt_through(client: TestClient, vision: FakeVision):
    response = client.post(
        "/vision/describe", content=FAKE_PNG, params={"prompt": "what is this?"}
    )

    assert response.status_code == 200
    assert response.json()["description"] == "a cat on a keyboard"
    assert vision.received_image == FAKE_PNG
    assert vision.received_prompt == "what is this?"


def test_describe_uses_a_default_prompt_when_none_given(
    client: TestClient, vision: FakeVision
):
    client.post("/vision/describe", content=FAKE_PNG)

    assert vision.received_prompt  # a sensible default, not an empty string


def test_describe_rejects_an_empty_body(client: TestClient):
    assert client.post("/vision/describe", content=b"").status_code == 400


def test_describe_rejects_oversized_images(client: TestClient):
    from app.api.routes.vision import MAX_IMAGE_BYTES

    response = client.post("/vision/describe", content=b"\x00" * (MAX_IMAGE_BYTES + 1))

    assert response.status_code == 413


def test_describe_translates_engine_failure_into_a_readable_error():
    app.dependency_overrides[get_vision] = lambda: BrokenVision()
    app.dependency_overrides[get_ocr] = lambda: FakeOCR()
    try:
        response = TestClient(app).post("/vision/describe", content=FAKE_PNG)

        assert response.status_code == 422
        assert "ANTHROPIC_API_KEY" in response.json()["error"]
    finally:
        app.dependency_overrides.clear()


# --- POST /vision/ocr ------------------------------------------------------


def test_ocr_returns_lines_with_confidence(client: TestClient):
    response = client.post("/vision/ocr", content=FAKE_PNG)

    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "hello"
    assert body["lines"][0]["confidence"] == 0.9


def test_an_image_with_no_text_is_success_not_failure(vision: FakeVision):
    """
    A picture of a cat contains no text. That is a normal answer, not an
    error the caller has to handle.
    """
    app.dependency_overrides[get_vision] = lambda: vision
    app.dependency_overrides[get_ocr] = lambda: FakeOCR(lines=[])
    try:
        response = TestClient(app).post("/vision/ocr", content=FAKE_PNG)

        assert response.status_code == 200
        assert response.json() == {"text": "", "lines": []}
    finally:
        app.dependency_overrides.clear()
