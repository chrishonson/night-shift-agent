"""Bounded real-model decision probes; context comes from frozen synthetic fixtures."""
import hashlib
import inspect
import json
import os
import statistics
import time
import urllib.request
from pathlib import Path

from context_capture import ContextCapture, digest, message_refs, summarize_trace


def write_json(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def load_protocol(bundle_path, protocol_path):
    bundle = json.loads(Path(bundle_path).read_text())
    protocol = json.loads(Path(protocol_path).read_text())
    if (bundle.get('schema_version') != 'provider-eval-bundle.v1' or
            protocol.get('schema_version') != 'context-decision-probes.v1' or
            protocol.get('bundle_sha256') != digest(bundle) or
            protocol.get('context_provider') != 'frozen_synthetic_fixture'):
        raise ValueError('Protocol/bundle mismatch')
    source_ids = set()
    for source in bundle['sources']:
        if (source['id'] in source_ids or source['scope'] != bundle['scope'] or
                source['sha256'] != hashlib.sha256(source['content'].encode()).hexdigest() or
                source['metadata'].get('origin') != 'synthetic'):
            raise ValueError('Invalid synthetic source')
        source_ids.add(source['id'])
    case_ids = [case['case_id'] for case in bundle['cases']]
    probe_ids = [case['case_id'] for case in protocol['cases']]
    if len(set(probe_ids)) != len(probe_ids) or sorted(case_ids) != sorted(probe_ids):
        raise ValueError('Probe coverage must exactly match bundle')
    splits = {}
    for case in bundle['cases']:
        incident = case['incident_id']
        if incident in splits and splits[incident] != case['split']:
            raise ValueError('Incident crosses splits')
        splits[incident] = case['split']
    for probe in protocol['cases']:
        returned, supplied = probe['returned_source_ids'], probe['supplied_source_ids']
        if (len(set(returned)) != len(returned) or len(set(supplied)) != len(supplied) or
                not set(supplied) <= set(returned) <= source_ids or
                probe['label_provenance'] != 'synthetic_definition' or probe['review_status'] != 'needs_review'):
            raise ValueError('Invalid source mapping or label provenance')
    return bundle, protocol


def build_input(bundle, protocol, probe):
    case = next(case for case in bundle['cases'] if case['case_id'] == probe['case_id'])
    sources = {source['id']: source for source in bundle['sources']}
    messages = [{'role': 'system', 'content': protocol['system_prompt']},
                {'role': 'user', 'content': json.dumps({'task': case['scenario']['request'],
                                                       'telemetry': probe['telemetry']}, sort_keys=True)}]
    mapping = []
    for source_id in probe['supplied_source_ids']:
        source = sources[source_id]
        message = {'role': 'user', 'content': json.dumps({'untrusted_source': source}, sort_keys=True)}
        messages.append(message)
        mapping.append({'canonical_source_id': source_id, 'source_sha256': source['sha256'],
                        'message_source_id': message_refs([message])[0]['source_id']})
    return messages, mapping


class LocalOllamaDecisionProvider:
    """One local request per probe. No retry, model pull, tool execution or fallback."""
    def __init__(self, model, timeout=45):
        if not 1 <= timeout <= 60:
            raise ValueError('Timeout must be between 1 and 60 seconds')
        self.model = model
        self.timeout = timeout
        self.name = 'Ollama local decision probe'
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.config = {'temperature': 0.2, 'num_ctx': 4096, 'num_predict': 256,
                       'think': False, 'format': 'json', 'keep_alive': '5m', 'timeout_seconds': timeout}
        self.descriptor = {'mode': 'live_local_model', 'model': model, 'model_digest': None,
                           'ollama_version': None, 'configuration': self.config}
        self.last_status = None

    def request(self, path, data=None):
        request = urllib.request.Request('http://127.0.0.1:11434' + path,
                                         data=json.dumps(data).encode() if data is not None else None,
                                         headers={'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=self.timeout) as response:
            raw = response.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError('Response exceeded limit')
        return json.loads(raw)

    def preflight(self):
        self.descriptor['ollama_version'] = self.request('/api/version')['version']
        installed = self.request('/api/tags')['models']
        found = next((item for item in installed if item['name'] == self.model), None)
        if not found or not found.get('digest'):
            raise ValueError('Model is not installed; this runner never pulls models')
        self.descriptor['model_digest'] = found['digest']

    def ask(self, messages):
        self.last_status = 'error'
        try:
            raw = self.request('/api/chat', {'model': self.model, 'messages': messages,
                       'stream': False, 'think': False, 'format': 'json', 'keep_alive': '5m',
                       'options': {key: self.config[key] for key in ('temperature', 'num_ctx', 'num_predict')}})
            if raw.get('error') or raw.get('done') is not True:
                return None
            if raw.get('done_reason') == 'length':
                self.last_status = 'truncated'
                return None
            # Never retain provider thinking, logprobs, or token accounting.
            message = raw.get('message', {})
            content = message.get('content')
            if message.get('tool_calls') or not isinstance(content, str) or not content.strip():
                self.last_status = 'invalid_response'
                return None
            self.last_status = 'returned'
            return content
        except Exception:
            # Network/provider exception strings may contain untrusted payloads.
            return None


def judge(content, expected):
    try:
        actual = json.loads(content)
        if (not isinstance(actual, dict) or set(actual) != {'action', 'arguments'} or
                not isinstance(actual['action'], str) or not isinstance(actual['arguments'], dict)):
            return 'invalid_output'
    except (ValueError, TypeError):
        return 'invalid_output'
    # JSON serialization preserves distinctions such as false versus 0.
    return 'passed' if digest(actual) == digest(expected) else 'failed'


def run_probes(bundle, protocol, provider, directory, repetitions=3):
    import agent_night_shift as ns
    if not 1 <= repetitions <= 3:
        raise ValueError('Repetitions must be between 1 and 3 for this bounded pilot')
    root = Path(directory).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_json(root / 'bundle.json', bundle)
    write_json(root / 'protocol.json', protocol)
    started = time.time_ns()
    harness_hash = digest({name: Path(__file__).with_name(name).read_text()
                           for name in ('context_decision_eval.py', 'context_capture.py', 'agent_night_shift.py')})
    preflight_ok = True
    try:
        provider.preflight()
    except Exception:
        preflight_ok = False
    # The production manager captures the exact input/fallback boundary. Construct
    # its ordinary configuration, then explicitly replace the chain with one provider.
    manager = ns.ProviderManager()
    manager.providers = [provider]
    manager.current_index = 0
    rows = []
    for repetition in range(1, repetitions + 1):
        for probe in protocol['cases']:
            case = next(c for c in bundle['cases'] if c['case_id'] == probe['case_id'])
            capture = ContextCapture(root / 'traces', {'id': probe['case_id'], 'goal': case['scenario']['request']})
            messages, mapping = build_input(bundle, protocol, probe)
            manager.context_capture = capture
            begin = time.monotonic()
            content = manager.ask(messages) if preflight_ok else None
            latency = (time.monotonic() - begin) * 1000 if preflight_ok else None
            status = judge(content, probe['expected']) if content is not None else 'provider_error'
            capture.finish('decision_probe_only', 'not_attempted')
            manager.context_capture = None
            try:
                summary = summarize_trace(capture.path)
                trace_events = [json.loads(line) for line in capture.path.read_text().splitlines()]
            except (OSError, ValueError, KeyError, TypeError):
                summary, trace_events = {'complete': False}, []
            assembled = [event['data'] for event in trace_events if event['kind'] == 'context_assembled']
            observed = {ref['source_id'] for event in assembled for ref in event['ordered_sources']}
            # Join only against the captured boundary, not the intended prompt alone.
            delivered = [item['canonical_source_id'] for item in mapping if item['message_source_id'] in observed]
            row = {'case_id': case['case_id'], 'incident_id': case['incident_id'], 'split': case['split'],
                   'repetition': repetition, 'status': status, 'latency_ms': latency,
                   'provider_status': provider.last_status if preflight_ok else 'preflight_error',
                   'input_sha256': digest(messages), 'output': content,
                   'output_sha256': digest(content) if content is not None else None,
                   'label_provenance': probe['label_provenance'], 'review_status': 'needs_review',
                   'fixture_returned_ids': probe['returned_source_ids'],
                   'observed_supplied_ids': delivered if assembled else None,
                   'source_mapping': mapping, 'trace': str(capture.path.relative_to(root)),
                   'capture_complete': summary['complete'], 'learning_eligible': False}
            rows.append(row)
            write_json(root / f'{case["case_id"]}-{repetition}.json', row)
            print(f'{case["case_id"]} repetition {repetition}: {status}', file=__import__('sys').stderr, flush=True)
    summary = summarize_rows(rows, protocol, repetitions)
    report = {'schema_version': 'context-decision-run.v1', 'started_at_ns': started,
              'finished_at_ns': time.time_ns(), 'bundle_sha256': digest(bundle),
              'protocol_sha256': digest(protocol), 'provider': provider.descriptor,
              'harness_sha256': harness_hash, 'grader_sha256': digest(inspect.getsource(judge)),
              'case_ids': [case['case_id'] for case in protocol['cases']],
              'repetitions': repetitions, 'context_provider': 'frozen_synthetic_fixture',
              'scope': 'structured_decision_probe_no_tools_executed', 'summary': summary, 'rows': rows,
              'end_to_end_agent_quality': 'not_evaluated', 'adoption_eligible': False, 'learning_eligible': False}
    write_json(root / 'report.json', report)
    return report


def summarize_rows(rows, protocol, repetitions):
    expected = {(case['case_id'], rep) for case in protocol['cases'] for rep in range(1, repetitions + 1)}
    actual = [(row['case_id'], row['repetition']) for row in rows]
    if len(set(actual)) != len(actual) or set(actual) != expected:
        raise ValueError('Missing, unexpected or duplicate probe observations')
    counts = {status: sum(row['status'] == status for row in rows)
              for status in ('passed', 'failed', 'invalid_output', 'provider_error')}
    if sum(counts.values()) != len(rows):
        raise ValueError('Unknown observation status')
    return {'observations': len(rows), **counts, 'passed_fraction_of_all_attempts': counts['passed'] / len(rows),
            'capture_gaps': sum(not row['capture_complete'] for row in rows),
            'latency_median_ms': statistics.median([row['latency_ms'] for row in rows if row['latency_ms'] is not None])
                                 if any(row['latency_ms'] is not None for row in rows) else None,
            'review_pending': len(protocol['cases'])}


def compare_probe_reports(baseline, candidate):
    """Paired, frozen-input comparisons. Errors remain incomparable, never passes."""
    keys = ('schema_version', 'bundle_sha256', 'protocol_sha256', 'grader_sha256', 'repetitions', 'context_provider', 'scope', 'case_ids')
    if baseline.get('schema_version') != 'context-decision-run.v1' or any(baseline.get(k) != candidate.get(k) for k in keys):
        raise ValueError('Incompatible decision protocols')
    if baseline['provider']['mode'] != candidate['provider']['mode']:
        raise ValueError('Cannot compare scripted and live observations')
    indexed = []
    for report in (baseline, candidate):
        rows = report['rows']
        mapping = {(r['case_id'], r['repetition']): r for r in rows}
        if len(mapping) != len(rows) or not rows:
            raise ValueError('Duplicate or empty observations')
        case_ids = set(report['case_ids'])
        if len(case_ids) != len(report['case_ids']):
            raise ValueError('Duplicate declared cases')
        if set(mapping) != {(case_id, rep) for case_id in case_ids for rep in range(1, report['repetitions'] + 1)}:
            raise ValueError('Incomplete repetition matrix')
        indexed.append(mapping)
    left, right = indexed
    if left.keys() != right.keys():
        raise ValueError('Different case matrices')
    comparisons = []
    for key, before in left.items():
        after = right[key]
        if any(before[field] != after[field] for field in ('input_sha256', 'incident_id', 'split')):
            raise ValueError('Changed case input or grouping')
        known = {'passed', 'failed', 'invalid_output'}
        if before['status'] not in known or after['status'] not in known or not before['capture_complete'] or not after['capture_complete']:
            status = 'incomparable'
        elif before['status'] == 'passed' and after['status'] != 'passed':
            status = 'regression'
        elif before['status'] != 'passed' and after['status'] == 'passed':
            status = 'improvement'
        else:
            status = 'unchanged'
        comparisons.append({'case_id': key[0], 'repetition': key[1], 'status': status,
                            'baseline': before['status'], 'candidate': after['status']})
    counts = {status: sum(row['status'] == status for row in comparisons)
              for status in ('regression', 'improvement', 'incomparable', 'unchanged')}
    return {'schema_version': 'context-decision-comparison.v1', **counts, 'rows': comparisons,
            'status': 'regression' if counts['regression'] else 'incomplete' if counts['incomparable'] else 'no_observed_regression',
            'adoption_eligible': False, 'end_to_end_agent_quality': 'not_evaluated'}
