import io
import json
import unittest
from unittest.mock import Mock

from artefact.qa_terminal import (
    QATerminalConfigError,
    TerminalSessionRunner,
    read_config,
    run_batch,
)


class QATerminalConfigTests(unittest.TestCase):
    def valid_env(self, root):
        return {
            'CODEX_DEMO_APPROVED': '1',
            'ARTEFACT_DATA_DIR': str(root / 'data'),
            'CODEX_DEMO_STATE_PATH': str(root / 'state.db'),
            'CODEX_DEMO_MAX_CALLS': '12',
            'CODEX_DEMO_MODEL': 'gpt-6-luna',
        }

    def test_requires_explicit_quota_approval(self):
        with self.assertRaises(QATerminalConfigError) as caught:
            read_config({})
        self.assertEqual(caught.exception.code, 'approval_required')

    def test_reads_explicit_model_call_cap_and_paths_without_api_key(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            config = read_config(self.valid_env(Path(directory)))
        self.assertEqual(config.model, 'gpt-6-luna')
        self.assertEqual(config.max_calls, 12)
        self.assertEqual(config.data_dir.name, 'data')

    def test_rejects_usage_database_under_source_data(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            root = Path(directory)
            env = self.valid_env(root)
            env['CODEX_DEMO_STATE_PATH'] = str(root / 'data' / 'state.db')
            with self.assertRaises(QATerminalConfigError) as caught:
                read_config(env)
        self.assertEqual(caught.exception.code, 'unsafe_state_path')


class QATerminalBatchTests(unittest.TestCase):
    def make_runner(self):
        agent = Mock()
        quota = Mock()
        quota.usage.side_effect = [0, 1, 1, 2, 2, 3]

        def answer(message, *, chat_id):
            agent.last_trace = {
                'status': 'success',
                'selected_tools': ['search_products'],
                'executed_tools': ['search_products'],
                'tool_details': [{'name': 'search_products'}],
                'error_code': None,
            }
            return f'reply for {chat_id}: {message}'

        agent.answer.side_effect = answer
        return agent, quota, TerminalSessionRunner(agent, quota, model='test-model')

    def test_batch_routes_turns_by_session_and_emits_tool_trace(self):
        agent, _quota, runner = self.make_runner()
        source = io.StringIO(
            '{"session_id":"qa-a","message":"first"}\n'
            '{"session_id":"qa-b","message":"separate"}\n'
            '{"session_id":"qa-a","message":"follow-up"}\n'
        )
        output = io.StringIO()
        errors = run_batch(source, output, runner)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(errors, 0)
        self.assertEqual([row['session_id'] for row in rows], ['qa-a', 'qa-b', 'qa-a'])
        self.assertEqual([call.kwargs['chat_id'] for call in agent.answer.call_args_list], ['qa-a', 'qa-b', 'qa-a'])
        self.assertEqual(rows[0]['tools_executed'], ['search_products'])
        self.assertEqual(rows[0]['tool_details'], [{'name': 'search_products'}])
        self.assertIn('latency_ms', rows[0])
        self.assertNotIn('message', rows[0])

    def test_invalid_json_is_explicit_and_does_not_stop_later_rows(self):
        _agent, _quota, runner = self.make_runner()
        source = io.StringIO('not-json\n{"session_id":"valid","message":"ok"}\n')
        output = io.StringIO()
        errors = run_batch(source, output, runner)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(errors, 1)
        self.assertEqual(rows[0]['error_code'], 'invalid_json')
        self.assertEqual(rows[1]['status'], 'success')

    def test_provider_error_is_not_reported_as_success(self):
        agent = Mock()
        agent.last_trace = {
            'status': 'error',
            'selected_tools': [],
            'executed_tools': [],
            'error_code': 'model_unavailable',
        }
        agent.answer.return_value = 'safe fallback'
        quota = Mock()
        quota.usage.side_effect = [0, 1]
        result = TerminalSessionRunner(agent, quota, model='gpt-6-luna').ask('session', 'question')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error_code'], 'model_unavailable')
        self.assertNotIn('response', result)


if __name__ == '__main__':
    unittest.main()
