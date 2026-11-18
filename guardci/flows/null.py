"""NullFlow: the do-nothing adapter used for skeleton projects/tests."""

from __future__ import annotations

from typing import ClassVar

from ..core.models import Tag
from .base import FlowAdapter, register


@register
class NullFlow(FlowAdapter):
    name = "null"
    capabilities: ClassVar[set] = set()

    def classify_delta(self, diff) -> set:
        # every non-protected change is unclassified
        return {Tag.UNKNOWN} if diff else set()
