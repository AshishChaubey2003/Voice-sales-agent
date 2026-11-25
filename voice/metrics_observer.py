from collections import deque
from collections.abc import Callable

from loguru import logger
from pipecat.frames.frames import MetricsFrame
from pipecat.metrics import metrics as pipecat_metrics
from pipecat.metrics.metrics import ProcessingMetricsData, TTFBMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed

# Present in the Pipecat release this project pins (1.12.0). Looked up by name so an older
# release degrades to "not recorded" instead of failing to import.
TTFAMetricsData = getattr(pipecat_metrics, "TTFAMetricsData", None)
TTFATMetricsData = getattr(pipecat_metrics, "TTFATMetricsData", None)

# on_metric(kind, processor, seconds): kind is "processing", "ttfat" or "ttfa".
MetricCallback = Callable[[str, str, float], None]


class ServiceTTFBObserver(BaseObserver):
    """Forwards each service metric once, even though frames pass many hops.

    ``on_ttfb(processor, seconds)`` keeps its original meaning. ``on_metric`` is optional and also
    receives the Pipecat processing time (``processing``), the time to the first answer token
    (``ttfat``) and the time to first audible audio (``ttfa``) whenever a service reports them.
    A service that never reports one produces no callback: nothing is estimated.
    """

    def __init__(
        self,
        on_ttfb: Callable[[str, float], None],
        max_remembered: int = 1000,
        *,
        on_metric: MetricCallback | None = None,
    ) -> None:
        super().__init__()
        self._on_ttfb = on_ttfb
        self._on_metric = on_metric
        self._seen_ids: set[int] = set()
        self._seen_order: deque[int] = deque()
        self._max_remembered = max_remembered

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        if not isinstance(frame, MetricsFrame) or frame.id in self._seen_ids:
            return
        self._remember(frame.id)
        for item in frame.data:
            try:
                if isinstance(item, TTFBMetricsData):
                    self._on_ttfb(item.processor, item.value)
                elif self._on_metric is not None:
                    self._forward_extra(item)
            except Exception:
                # Telemetry must never break the call.
                logger.warning("A service metric could not be recorded")

    def _forward_extra(self, item) -> None:
        if isinstance(item, ProcessingMetricsData):
            self._on_metric("processing", item.processor, item.value)
        elif TTFATMetricsData is not None and isinstance(item, TTFATMetricsData):
            self._on_metric("ttfat", item.processor, item.ttfat)
        elif TTFAMetricsData is not None and isinstance(item, TTFAMetricsData):
            self._on_metric("ttfa", item.processor, item.ttfa)

    def _remember(self, frame_id: int) -> None:
        self._seen_ids.add(frame_id)
        self._seen_order.append(frame_id)
        if len(self._seen_order) > self._max_remembered:
            self._seen_ids.discard(self._seen_order.popleft())
