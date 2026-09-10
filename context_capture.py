"""Opt-in local S4 boundary evidence. No prompt text or lease capabilities."""
import hashlib
import json
import os
import time
import uuid
from collections import Counter
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def message_refs(messages):
    if isinstance(messages, str):
        messages = [{'role': 'user', 'content': messages}]
    return [{'source_id': digest([m.get('role'), m.get('content', '')]),
             'role': m.get('role') if m.get('role') in ('system', 'user', 'assistant', 'tool') else 'unknown',
             'characters': len(m.get('content', ''))} for m in messages]


class ContextCapture:
    """Bounded local outbox; capture failure never changes worker execution.

    Hashes identify observed message values, not canonical memory documents.
    Finalization is local and does not assert the remote lease outcome.
    """
    def __init__(self, directory, card, max_bytes=1_048_576, max_events=4096):
        self.trace_id = str(uuid.uuid4())
        self.path = Path(directory) / (self.trace_id + '.jsonl')
        if max_bytes < 4096 or max_events < 2:
            raise ValueError("Capture limits must reserve a terminal record")
        self.max_bytes = max_bytes
        self.max_events = max_events
        self.sequence = 0
        self.bytes_written = 0
        self.dropped = 0
        self.failed = False
        self.closed = False
        self.stream = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            self.stream = os.fdopen(fd, 'wb', buffering=0)
            self.emit('attempt_opened', {'card_id_hash': digest(card.get('id')),
                      'harness_source_hash': digest({name: Path(__file__).with_name(name).read_text()
                                                     for name in ('context_capture.py', 'agent_night_shift.py')}),
                      'task_revision': digest({k: card.get(k) for k in ('id', 'title', 'goal', 'kind', 'repo', 'gate_ids')}),
                      'coverage': {'boundary': 'worker_before_provider_adapter',
                                   'retrieval': 'unobserved', 'provider_internal_context': 'unknown',
                                   'payloads': 'not_retained', 'authenticated_actor': 'unobserved'},
                      'learning_eligible': False})
        except (OSError, ValueError):
            self.failed = True

    def emit(self, kind, data, terminal=False):
        if self.closed or self.failed or self.stream is None:
            self.dropped += 1
            return
        event = {'schema_version': 1, 'trace_id': self.trace_id,
                 'sequence': self.sequence, 'observed_at_ns': time.time_ns(),
                 'kind': kind, 'data': data}
        encoded = (json.dumps(event, separators=(',', ':')) + '\n').encode()
        # Reserve room for a final record reporting dropped events.
        if not terminal and (self.sequence >= self.max_events - 1 or
                             self.bytes_written + len(encoded) > self.max_bytes - 2048):
            self.dropped += 1
            return
        try:
            if self.stream.write(encoded) != len(encoded):
                raise OSError("Incomplete capture write")
            self.bytes_written += len(encoded)
            self.sequence += 1
        except OSError:
            self.failed = True
            self.dropped += 1

    def assembled(self, messages, provider, decision_id):
        self.emit('context_assembled', {'decision_id': decision_id,
                  'provider_adapter': type(provider).__name__,
                  'provider_configuration_hash': digest([provider.name, getattr(provider, 'model', None)]),
                  'ordered_sources': message_refs(messages), 'measurement': 'python_unicode_characters',
                  'provider_delivery': 'unknown'})

    def transformed(self, before, after, budget):
        self.emit('context_transformed', {'operation': 'drop_oldest_message_pairs',
                  'before': message_refs(before), 'after': message_refs(after),
                  'budget_characters': budget, 'measurement': 'python_unicode_characters'})

    def provider_finished(self, decision_id, status):
        self.emit('provider_call_finished', {'decision_id': decision_id, 'status': status})

    def finish(self, outcome, release_status):
        if self.closed:
            return
        self.emit('trace_finalized', {'worker_outcome': outcome,
                  'release_status': release_status, 'dropped_events': self.dropped,
                  'capture_io_failed': self.failed, 'learning_eligible': False}, terminal=True)
        self.closed = True
        if self.stream:
            try:
                self.stream.flush()
                os.fsync(self.stream.fileno())
            except OSError:
                self.failed = True
            finally:
                self.stream.close()


def capture_call(capture, method, *args):
    """Best-effort instrumentation is deliberately outside provider failover."""
    if capture is None:
        return
    try:
        getattr(capture, method)(*args)
    except Exception:
        capture.failed = True


def summarize_trace(path):
    """Generate review candidates; missing evidence is never a quality pass."""
    events = [json.loads(line) for line in Path(path).read_text().splitlines()]
    if not events or any(e['schema_version'] != 1 or e['trace_id'] != events[0]['trace_id']
                         or e['sequence'] != i for i, e in enumerate(events)):
        raise ValueError('Invalid trace sequence or schema')
    if events[0]['kind'] != 'attempt_opened' or any(e['kind'] == 'trace_finalized' for e in events[:-1]):
        raise ValueError('Invalid attempt boundaries')
    terminal = events[-1]['data'] if events[-1]['kind'] == 'trace_finalized' else None
    candidates = []
    for event in events:
        if event['kind'] == 'context_transformed':
            data = event['data']
            omitted = Counter(s['source_id'] for s in data['before']) - Counter(s['source_id'] for s in data['after'])
            candidates.append({'sequence': event['sequence'], 'label': 'observed_message_omission',
                               'label_provenance': 'event_observation', 'review_status': 'needs_review',
                               'omitted_source_ids': list(omitted.elements()),
                               'task_relevance': 'unknown', 'learning_eligible': False})
    return {'schema_version': 1, 'trace_id': events[0]['trace_id'],
            'complete': bool(terminal and not terminal['dropped_events'] and not terminal['capture_io_failed']),
            'release_status': terminal['release_status'] if terminal else 'unknown',
            'provider_calls': sum(e['kind'] == 'context_assembled' for e in events),
            'candidates': candidates, 'agent_quality': 'not_evaluated', 'learning_eligible': False}
