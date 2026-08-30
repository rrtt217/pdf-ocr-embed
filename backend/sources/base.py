"""OcrSource abstraction.

An adapter receives the raw output of an OCR engine (plus the original page
image dimensions in pixels) and produces a normalized `OcrPage` where every
bbox is expressed in original pixel space.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.models import OcrPage


@dataclass
class PageSpec:
    """One page's OCR input, as produced by the job's pre-render phase."""

    image_path: str
    width: int
    height: int
    page_index: int


class OcrSource(ABC):
    """Base class for all OCR engine adapters."""

    name: str = "base"

    # Engines whose output is independent per page default to one
    # recognize_pixels call per page.  Engines with native document-level
    # parsing (e.g. unlimited) set this to a positive page count so the job
    # layer can chunk pages and call recognize_pages() per chunk.
    max_batch_pages: int = 0

    @abstractmethod
    def recognize_pixels(
        self,
        image_path: str,
        width: int,
        height: int,
        page_index: int,
    ) -> OcrPage:
        """Run OCR on a page image and return a normalized OcrPage.

        Args:
            image_path: Path to the page image (PNG) on disk.
            width: Page image width in pixels.
            height: Page image height in pixels.
            page_index: Zero-based page number in the PDF.

        Returns:
            OcrPage with blocks in original pixel coordinates.
        """
        raise NotImplementedError

    def recognize_pages(self, specs: List[PageSpec]) -> List[OcrPage]:
        """Run OCR on several pages, returning one OcrPage per spec in order.

        The default implementation is the correct-but-slow fallback: one
        recognize_pixels call per page.  Engines that natively parse whole
        documents (unlimited's "Multi page parsing.") override this to send
        all specs in a single request and split the response per page.

        Args:
            specs: Page inputs; the caller chunks by ``max_batch_pages``.

        Returns:
            One OcrPage per spec, in the same order.

        Raises:
            RuntimeError: When any page in the group failed; the caller marks
                every page of the group failed and retries them later.
        """
        return [
            self.recognize_pixels(s.image_path, s.width, s.height, s.page_index)
            for s in specs
        ]

    def close(self) -> None:
        """Release any adapter-held resources."""

    def cache_fingerprint(self) -> Dict[str, Any]:
        """Settings that affect OCR output (used for the result-cache key).

        Returns a small JSON-safe dict.  The base implementation carries only
        the adapter class name; engines whose output depends on constructor
        settings must override this so that a changed setting misses the cache.
        Values are only ever *hashed* into a cache key (see
        ``backend.ocr_cache.build_key``) — never persisted or logged directly.
        """
        return {"engine": type(self).__name__}


def map_normalized_to_pixels(x: float, y: float, width: int, height: int) -> "tuple[float, float]":
    """Map a normalized 0..1000 canvas coordinate to original pixel space.

    Per the design doc, each dimension is independently normalized to 1000
    (non-uniform scale), so the inverse is a per-axis linear scale.
    """
    px = x * (width / 1000.0)
    py = y * (height / 1000.0)
    return px, py


def normalize_bbox(bbox, width: int, height: int) -> list:
    """Convert a normalized (0..1000) bbox into raw pixel coordinates."""
    x1, y1, x2, y2 = (float(v) for v in bbox)
    px1, py1 = map_normalized_to_pixels(x1, y1, width, height)
    px2, py2 = map_normalized_to_pixels(x2, y2, width, height)
    x1p, x2p = sorted((px1, px2))
    y1p, y2p = sorted((py1, py2))
    return [int(round(x1p)), int(round(y1p)), int(round(x2p)), int(round(y2p))]


class UnavailableError(RuntimeError):
    """Raised when an adapter cannot run (e.g. missing optional dependency)."""