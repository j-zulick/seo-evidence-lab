"""Allowlisted AI telemetry. Never export query, document, output or exception text."""
import time


class AITelemetry:
    def __init__(self, sink, *, tracer=None, meter=None):
        self.sink, self.tracer = sink, tracer
        self.latency = None
        if meter:
            try:
                self.latency = meter.create_histogram('knowledge.ai.duration', unit='s')
            except Exception:
                pass

    def call(self, invoke, *, model, retrieval_id=None, usage=None):
        event = {'model': model, 'retrieval_id': retrieval_id, 'error': False}
        started = time.monotonic()
        context = span = None
        if self.tracer:
            try:
                context = self.tracer.start_as_current_span('knowledge.ai', record_exception=False,
                    set_status_on_exception=False)
                span = context.__enter__()
            except Exception:
                context = None
        try:
            result = invoke()
            if usage:
                try:
                    tokens = usage(result)
                    for key in ('input_tokens', 'output_tokens'):
                        value = tokens.get(key)
                        if type(value) is int and value >= 0:
                            event[key] = value
                except Exception:
                    pass
            return result
        except Exception:
            event['error'] = True
            raise
        finally:
            event['latency_seconds'] = time.monotonic() - started
            if span:
                try:
                    for key, value in event.items():
                        if value is not None:
                            span.set_attribute('knowledge.ai.' + key, value)
                except Exception:
                    pass
            if context:
                try:
                    # Never pass exception objects to instrumentation.
                    context.__exit__(None, None, None)
                except Exception:
                    pass
            try:
                self.sink(dict(event))
            except Exception:
                pass
            if self.latency:
                try:
                    self.latency.record(event['latency_seconds'], {'model': model, 'error': event['error']})
                except Exception:
                    pass


class InstrumentedSemanticAdapter:
    def __init__(self, adapter, telemetry, *, model, retrieval_id):
        self.adapter, self.telemetry = adapter, telemetry
        self.model, self.retrieval_id = model, retrieval_id

    def search(self, query, documents):
        return self.telemetry.call(lambda: self.adapter.search(query, documents),
            model=self.model, retrieval_id=self.retrieval_id)
