#!/usr/bin/env python3
"""S4 context replay, review candidates and opt-in local model decision probes."""
import argparse
from contextlib import redirect_stdout
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from context_capture import ContextCapture, digest, summarize_trace


def replay(directory):
    import agent_night_shift as ns
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    constraint = 'No credential rotation.'
    required_id = digest(['user', 'TOOL OUTPUT (read_file): ' + constraint])
    results = []
    original_cwd = Path.cwd()
    for label, budget in [('retained', 1_000_000), ('pruned', 9_000)]:
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            (root / 'constraint.txt').write_text(constraint)
            (root / 'distractor.txt').write_text('irrelevant fixture text ' * 1000)

            class Scripted(ns.LLMProvider):
                name = 'S4 scripted fixture (no model)'
                calls = 0

                def ask(self, messages):
                    self.calls += 1
                    if self.calls > 2:
                        return None
                    path = 'constraint.txt' if self.calls == 1 else 'distractor.txt'
                    return '<agent_action>' + json.dumps({'action': 'read_file', 'args': {'path': path}}) + '</agent_action>'

            capture = ContextCapture(directory / label, {'id': 'synthetic-pruning', 'goal': 'preserve a fetched constraint'})
            try:
                with patch.dict(os.environ, {'FORCE_PROVIDER': 'ollama'}), patch.object(ns, 'MAX_CONTEXT_CHARS', budget), patch.object(ns.time, 'sleep'):
                    agent = ns.NightShiftAgent(str(root), token='synthetic-unused-token')
                    agent.llm.providers = [Scripted()]
                    agent.llm.context_capture = capture
                    success = agent.process_task('Read constraint.txt, then distractor.txt. Keep the constraint available.', '', '')
                    capture.finish('succeeded' if success else 'failed', 'not_attempted')
                    for handler in agent._current_file_handlers:
                        ns.logger.removeHandler(handler)
                        ns.prompt_logger.removeHandler(handler)
                        handler.close()
            finally:
                os.chdir(original_cwd)
            events = [json.loads(line) for line in capture.path.read_text().splitlines()]
            calls = [e for e in events if e['kind'] == 'context_assembled']
            results.append({'variant': label, 'budget_characters': budget,
                            'required_source_at_final_boundary': required_id in {r['source_id'] for r in calls[-1]['data']['ordered_sources']},
                            'trace': str(capture.path), 'summary': summarize_trace(capture.path)})
    return {'schema_version': 1, 'scenario_id': 'worker_pruning_constraint',
            'incident_group': 'synthetic-worker-pruning-v1', 'split': 'development',
            'label_provenance': 'synthetic_definition', 'required_source_id': required_id,
            'replay_script_hash': digest(Path(__file__).read_text()),
            'real_model_run': False, 'agent_quality': 'not_evaluated',
            'results': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    generate = sub.add_parser('replay')
    generate.add_argument('--output-dir', type=Path, required=True)
    report = sub.add_parser('report')
    report.add_argument('trace', type=Path)
    decisions = sub.add_parser('decisions')
    decisions.add_argument('--bundle', type=Path, required=True)
    decisions.add_argument('--protocol', type=Path, default=Path(__file__).resolve().parents[1] / 'eval/s4/decision-probes.json')
    decisions.add_argument('--output-dir', type=Path, required=True)
    decisions.add_argument('--model', required=True)
    decisions.add_argument('--repetitions', type=int, default=3, choices=[1, 2, 3])
    decisions.add_argument('--timeout', type=int, default=45)
    compare = sub.add_parser('compare-decisions')
    compare.add_argument('--baseline', type=Path, required=True)
    compare.add_argument('--candidate', type=Path, required=True)
    compare.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    # The legacy worker configures a stdout log handler at import time.
    # Keep the command's stdout machine-readable, including that first import.
    with redirect_stdout(sys.stderr):
        if args.command == 'compare-decisions':
            from context_decision_eval import compare_probe_reports, write_json
            result = compare_probe_reports(json.loads(args.baseline.read_text()), json.loads(args.candidate.read_text()))
            write_json(args.out, result)
        elif args.command == 'decisions':
            from context_decision_eval import load_protocol, LocalOllamaDecisionProvider, run_probes
            bundle, protocol = load_protocol(args.bundle, args.protocol)
            provider = LocalOllamaDecisionProvider(args.model, args.timeout)
            result = run_probes(bundle, protocol, provider, args.output_dir, args.repetitions)
        else:
            result = replay(args.output_dir) if args.command == 'replay' else summarize_trace(args.trace)
    print(json.dumps(result, indent=2))
    if args.command == 'compare-decisions':
        raise SystemExit(2 if result['regression'] else 3 if result['incomparable'] else 0)
    if args.command == 'decisions':
        summary = result['summary']
        if summary['provider_error'] or summary['capture_gaps']:
            raise SystemExit(3)
        if summary['failed'] or summary['invalid_output']:
            raise SystemExit(2)


if __name__ == '__main__':
    main()
