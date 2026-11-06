"""Explicit local-only tracing; no global provider or remote exporter."""
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


class LocalTrace:
    def __init__(self):
        self.provider = TracerProvider()
        self.exporter = InMemorySpanExporter()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer('seo.fixture')

    def stage(self, name):
        if name not in {'import','diff','replay','comparison','retry'}:
            raise ValueError('Unknown fixture stage')
        return self.tracer.start_as_current_span(name, record_exception=False, set_status_on_exception=False)

    def finish(self):
        result=[{'stage':s.name,'duration_ms':(s.end_time-s.start_time)/1e6,'status':s.status.status_code.name}
                for s in self.exporter.get_finished_spans()]
        self.provider.shutdown()
        return result
