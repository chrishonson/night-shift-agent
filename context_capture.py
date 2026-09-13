"""Opt-in local S4 boundary evidence. No prompt text or lease capabilities."""
import hashlib
import json
import os
import re
import time
import uuid
from collections import Counter
from pathlib import Path


SECRET_PATTERNS = [
    re.compile(r'ghp_[A-Za-z0-9_]{16,}', re.IGNORECASE),
    re.compile(r'github_pat_[A-Za-z0-9_]{16,}', re.IGNORECASE),
    re.compile(r'gh[oousr]_[A-Za-z0-9_]{16,}', re.IGNORECASE),
    re.compile(r'Bearer\s+[A-Za-z0-9\-_\.]{16,}', re.IGNORECASE),
    re.compile(r'(?i)(api[_-]?key|token|secret|password|lease[_-]?capability|bot[_-]?token)\s*[:=]\s*["\']?([A-Za-z0-9_\-\.]{6,})["\']?'),
]

SENSITIVE_KEYS = {'lease', 'token', 'secret', 'credential', 'credentials', 'password', 'key', 'auth', 'gh_bot_token', 'pat', 'authorization'}


def redact_text(text, max_chars=65536):
    if not isinstance(text, str):
        return text
    result = text
    for pat in SECRET_PATTERNS:
        result = pat.sub('[REDACTED_SECRET]', result)
    if len(result) > max_chars:
        result = result[:max_chars] + f"\n\n[TRUNCATED: original length {len(text)} characters]"
    return result


def redact_data(obj, max_chars=65536):
    if isinstance(obj, str):
        return redact_text(obj, max_chars=max_chars)
    elif isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            if str(k).lower() in SENSITIVE_KEYS:
                cleaned[k] = "[REDACTED_SECRET]"
            else:
                cleaned[k] = redact_data(v, max_chars=max_chars)
        return cleaned
    elif isinstance(obj, (list, tuple)):
        return [redact_data(item, max_chars=max_chars) for item in obj]
    return obj


def redact_card(card):
    """Sanitize task card, never export lease capabilities or credentials."""
    if not isinstance(card, dict):
        return {}
    sanitized = {}
    allowed_keys = ('id', 'title', 'goal', 'kind', 'repo', 'gate_ids', 'acceptance_criteria', 'acceptance')
    for k in allowed_keys:
        if k in card:
            sanitized[k] = redact_data(card[k])
    return sanitized


def redact_messages(messages):
    if isinstance(messages, str):
        messages = [{'role': 'user', 'content': messages}]
    redacted = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get('role', 'unknown')
        content = m.get('content', '')
        redacted.append({
            'role': role if role in ('system', 'user', 'assistant', 'tool') else 'unknown',
            'content': redact_text(content),
            'characters': len(content),
            'source_id': digest([role, content])
        })
    return redacted


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
    Supports opt-in payload mode (S4_PAYLOAD_CAPTURE=1) alongside metadata-only mode.
    """
    def __init__(self, directory, card, max_bytes=1_048_576, max_events=4096,
                 mode=None, evidence_class=None, run_record_dir=None):
        self.trace_id = str(uuid.uuid4())
        if mode is not None:
            self.mode = mode
        elif os.getenv("S4_PAYLOAD_CAPTURE") in ("1", "true", "True"):
            self.mode = "payload"
        else:
            self.mode = "metadata"

        self.evidence_class = evidence_class or "scripted_provider_fixture"
        self.run_record_dir = Path(run_record_dir).resolve() if run_record_dir else None

        if self.run_record_dir:
            self.run_record_dir.mkdir(parents=True, exist_ok=True)
            self.path = self.run_record_dir / "events.jsonl"
            flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY
        else:
            self.path = Path(directory) / (self.trace_id + '.jsonl')
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY

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
        self.card = card
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, flags, 0o600)
            self.stream = os.fdopen(fd, 'wb', buffering=0)
            attempt_data = {
                'card_id_hash': digest(card.get('id')),
                'harness_source_hash': digest({name: Path(__file__).with_name(name).read_text()
                                               for name in ('context_capture.py', 'agent_night_shift.py')}),
                'task_revision': digest({k: card.get(k) for k in ('id', 'title', 'goal', 'kind', 'repo', 'gate_ids')}),
                'coverage': {
                    'boundary': 'worker_actual_loop' if self.mode == 'payload' else 'worker_before_provider_adapter',
                    'retrieval': 'unobserved',
                    'provider_internal_context': 'unknown',
                    'payloads': 'retained_sanitized' if self.mode == 'payload' else 'not_retained',
                    'authenticated_actor': 'unobserved'
                },
                'learning_eligible': False
            }
            if self.mode == 'payload':
                attempt_data['mode'] = 'payload'
                attempt_data['evidence_class'] = self.evidence_class
                attempt_data['real_model_evidence'] = {'status': 'unavailable', 'reason': 'no_real_model_run_for_card_50'}
                attempt_data['task'] = redact_card(card)
                attempt_data['quota_status'] = {'status': 'unavailable', 'reason': 'deferred_offline'}

            self.emit('attempt_opened', attempt_data)
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
        data = {
            'decision_id': decision_id,
            'provider_adapter': type(provider).__name__,
            'provider_configuration_hash': digest([provider.name, getattr(provider, 'model', None)]),
            'ordered_sources': message_refs(messages),
            'measurement': 'python_unicode_characters',
            'provider_delivery': 'unknown'
        }
        if self.mode == 'payload':
            data['provider_name'] = getattr(provider, 'name', 'unknown')
            data['provider_model'] = getattr(provider, 'model', 'unknown')
            data['messages'] = redact_messages(messages)
        self.emit('context_assembled', data)

    def transformed(self, before, after, budget):
        data = {
            'operation': 'drop_oldest_message_pairs',
            'before': message_refs(before),
            'after': message_refs(after),
            'budget_characters': budget,
            'measurement': 'python_unicode_characters'
        }
        if self.mode == 'payload':
            data['before_count'] = len(before)
            data['after_count'] = len(after)
            data['dropped_pairs'] = (len(before) - len(after)) // 2
        self.emit('context_transformed', data)

    def provider_finished(self, decision_id, status, response=None, error=None, duration_ms=None):
        data = {'decision_id': decision_id, 'status': status}
        if self.mode == 'payload':
            data['response_payload'] = redact_text(response) if response is not None else None
            data['error_text'] = redact_text(str(error)) if error is not None else None
            data['duration_ms'] = duration_ms
        self.emit('provider_call_finished', data)

    def tool_executed(self, tool, args, output, error=None, duration_ms=None):
        if self.mode == 'payload':
            self.emit('tool_call_finished', {
                'tool': tool,
                'args': redact_data(args),
                'output': redact_text(output) if output is not None else None,
                'error': redact_text(str(error)) if error is not None else None,
                'duration_ms': duration_ms,
            })

    def verification_finished(self, gate_results, all_passed):
        if self.mode == 'payload':
            self.emit('verification_finished', {
                'gates': redact_data(gate_results),
                'all_passed': all_passed,
            })

    def finish(self, outcome, release_status, patch=None, acceptance=None, no_commit_explanation=None):
        if self.closed:
            return
        data = {
            'worker_outcome': outcome,
            'release_status': release_status,
            'dropped_events': self.dropped,
            'capture_io_failed': self.failed,
            'learning_eligible': False
        }
        if self.mode == 'payload':
            data['final_patch'] = redact_text(patch) if patch is not None else None
            data['independent_acceptance'] = acceptance
            if no_commit_explanation is not None:
                data['no_commit_explanation'] = redact_text(no_commit_explanation)
        self.emit('trace_finalized', data, terminal=True)
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
