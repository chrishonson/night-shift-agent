#!/usr/bin/env python3
"""S4 synthetic worker replay / review-candidate extraction (no model or network)."""
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
    args = parser.parse_args()
    # The legacy worker configures a stdout log handler at import time.
    # Keep the command's stdout machine-readable, including that first import.
    with redirect_stdout(sys.stderr):
        result = replay(args.output_dir) if args.command == 'replay' else summarize_trace(args.trace)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
