"""The registry is how /api/health and the upload path discover engines."""
from __future__ import annotations

import pytest

from backend.sources import factory
from backend.sources.generic_openai_adapter import GenericOpenAiAdapter
from backend.sources.tesseract_adapter import TesseractAdapter
from backend.sources.unlimited_ocr_adapter import UnlimitedOcrAdapter


def test_registry_contains_all_three():
    assert set(factory._REGISTRY) == {"unlimited", "tesseract", "generic_openai"}


def test_get_adapter_types():
    assert isinstance(factory.get_adapter("unlimited"), UnlimitedOcrAdapter)
    assert isinstance(factory.get_adapter("tesseract"), TesseractAdapter)
    assert isinstance(factory.get_adapter("generic_openai"), GenericOpenAiAdapter)


def test_name_is_normalized():
    assert factory.get_adapter(" Unlimited ").name == "unlimited"
    assert factory.get_adapter().name == "unlimited"  # default


def test_unknown_adapter_raises():
    with pytest.raises(ValueError, match="Unknown OCR adapter"):
        factory.get_adapter("nope")


def test_available_adapters_export():
    names = [a["name"] for a in factory.available_adapters()]
    assert names == ["unlimited", "tesseract", "generic_openai"]
