"""``null`` recorder: no checkpoints (source ``rebuild``, §12.2)."""

from .base import Recorder, register


@register
class NullRecorder(Recorder):
    type = "null"

    def capture(self, project, sha):
        return None
