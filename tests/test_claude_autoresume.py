from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import claude_autoresume as auto

SESSION = '11111111-1111-4111-8111-111111111111'


def entry(kind, identifier, source='claude-desktop', **extra):
    data = {'type': kind, 'uuid': identifier, 'entrypoint': source, 'sessionId': SESSION,
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'isSidechain': False}
    if kind == 'user':
        data['message'] = {'role': 'user', 'content': 'Do the task'}
    data.update(extra)
    return data


def limit(identifier='failure', source='claude-desktop', **extra):
    return entry('assistant', identifier, source, isApiErrorMessage=True, apiError='usage_limit_reached',
                 message={'role': 'assistant', 'content': [{'type': 'text', 'text': 'Usage limit reached'}]}, **extra)


class ClaudeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.home, self.store = self.base / 'home', self.base / 'store'
        self.cwd = self.base / 'workspace'
        self.cwd.mkdir()
        self.path = self.home / 'projects/test' / (SESSION + '.jsonl')
        self.path.parent.mkdir(parents=True)
        self.path.write_text('')
        self.index = {'active': 'old', 'accounts': [
            {'key': key, 'dir': str(self.base / key), 'email': key + '@example.test', 'account_uuid': key, 'org_uuid': 'org'}
            for key in ('old', 'new')]}
        for account in self.index['accounts']:
            directory = Path(auto.core._sessions_dir(account))
            directory.mkdir(parents=True)
            (directory / 'local_example.json').write_text(json.dumps({
                'sessionId': 'local_example', 'cliSessionId': SESSION, 'cwd': str(self.cwd),
                'isArchived': False, 'model': 'sonnet', 'effort': 'medium', 'permissionMode': 'default'}))
        self.current = 'old'
        self.enterContext(patch.object(auto.Monitor, 'current_account', side_effect=lambda: self.current))
        self.enterContext(patch.object(auto.core, 'load', side_effect=lambda: self.index))
        self.enterContext(patch.object(auto.core, 'detect_active', side_effect=lambda index: index))
        self.enterContext(patch.object(auto, 'live_desktop_sessions', return_value={SESSION}))
        self.monitor = auto.Monitor(self.home, self.store, notify=Mock(), changed=Mock())
        self.monitor.since = time.time() - 1
        self.addCleanup(self.close_logs)

    def close_logs(self):
        for handler in list(self.monitor.log.handlers):
            handler.close()
            self.monitor.log.removeHandler(handler)

    def append(self, *rows):
        with self.path.open('a', encoding='utf-8') as handle:
            for row in rows:
                handle.write(json.dumps(row) + '\n')

    def failed_event(self):
        self.append(entry('user', 'request'), limit())
        self.monitor.scan()
        return self.monitor.queue[SESSION + ':failure']

    def recovery_services(self, *, quota=False):
        self.enterContext(patch.object(self.monitor, 'find_cli', return_value='claude.exe'))
        self.enterContext(patch.object(auto, 'cli_environment', return_value={'CLAUDE_CODE_OAUTH_TOKEN': 'target-only'}))
        self.enterContext(patch.object(auto.core, 'usage', return_value={'session': (80, 100), 'weekly': (40, 100)}))
        def switch(index, key):
            self.current = key
            index['active'] = key
            return True
        swap = self.enterContext(patch.object(auto.core, 'switch', side_effect=switch))
        process = Mock(returncode=1 if quota else 0)
        process.poll.return_value = None
        def spawn(args, **kwargs):
            self.assertEqual(args[args.index('-p') + 1], auto.RESUME_PROMPT)
            self.assertEqual(kwargs['env']['CLAUDE_CODE_OAUTH_TOKEN'], 'target-only')
            result = {'session_id': SESSION, 'is_error': quota, 'result': 'limit' if quota else 'done'}
            kwargs['stdout'].write(json.dumps(result).encode())
            kwargs['stdout'].flush()
            self.append(entry('user', 'auto-request', 'sdk-cli', message={'role': 'user', 'content': auto.RESUME_PROMPT}))
            if quota:
                self.append(limit('next-failure', 'sdk-cli'))
            else:
                self.append(entry('assistant', 'auto-answer', 'sdk-cli', message={'stop_reason': 'end_turn', 'content': []}))
            return process
        spawn_mock = self.enterContext(patch.object(auto.subprocess, 'Popen', side_effect=spawn))
        return process, swap, spawn_mock

    def test_real_desktop_limit_matches_original_session(self):
        event = self.failed_event()
        self.assertTrue(self.monitor.valid(event))
        self.assertEqual(event.thread, SESSION)

    def test_unrelated_cli_quota_error_never_triggers_switch(self):
        self.append(entry('user', 'cli-request', 'sdk-cli'), limit(source='sdk-cli'))
        self.monitor.scan()
        self.assertFalse(self.monitor.queue)

    def test_zero_meter_warning_text_completion_and_cancel_do_nothing(self):
        self.append(entry('user', 'request'),
                    entry('system', 'warning', message='usage_limit_reached', utilization=100),
                    entry('assistant', 'normal', message={'stop_reason': 'end_turn', 'content': [{'text': 'usage_limit_reached'}]}),
                    entry('user', 'cancel', message={'content': '[Request interrupted by user]'}))
        self.monitor.scan()
        self.assertFalse(self.monitor.queue)

    def test_generic_rate_limit_or_network_errors_do_nothing(self):
        for name in ('rate_limit', 'overloaded', 'network_error'):
            error = limit(identifier=name)
            error['apiError'] = name
            self.append(entry('user', 'request'), error)
        self.monitor.scan()
        self.assertFalse(self.monitor.queue)

    def test_sidechain_and_other_session_are_ignored(self):
        self.append(entry('user', 'request'), limit(isSidechain=True), limit(identifier='other', sessionId='another-session'))
        self.monitor.scan()
        self.assertFalse(self.monitor.queue)

    def test_requires_interrupted_request(self):
        self.append(limit())
        self.monitor.scan()
        event = next(iter(self.monitor.queue.values()))
        self.assertFalse(self.monitor.valid(event))

    def test_success_before_error_does_not_count_as_interrupted_task(self):
        self.append(entry('user', 'request'), entry('assistant', 'answer', message={'stop_reason': 'end_turn'}), limit())
        self.monitor.scan()
        self.assertFalse(self.monitor.valid(next(iter(self.monitor.queue.values()))))

    def test_startup_does_not_replay_old_errors(self):
        self.failed_event()
        monitor = auto.Monitor(self.home, self.store)
        monitor.scan()
        self.assertFalse(monitor.queue)

    def test_running_task_from_before_startup_can_fail_afterwards(self):
        self.append(entry('user', 'request'))
        self.monitor = auto.Monitor(self.home, self.store)
        self.monitor.since = time.time() - 1
        self.append(limit())
        self.monitor.scan()
        self.assertTrue(self.monitor.valid(next(iter(self.monitor.queue.values()))))

    def test_disabled_event_is_not_replayed_when_reenabled(self):
        self.monitor.set_enabled(False)
        self.failed_event_without_queue()
        self.monitor.set_enabled(True)
        self.monitor.scan()
        self.assertFalse(self.monitor.queue)

    def failed_event_without_queue(self):
        self.append(entry('user', 'request'), limit())
        self.monitor.scan()

    def test_manual_continuation_cancellation_and_account_change_supersede(self):
        event = self.failed_event()
        self.current = 'new'
        self.assertFalse(self.monitor.valid(event))
        self.current = 'old'
        self.append(entry('user', 'manual'))
        self.assertFalse(self.monitor.valid(event))

    def test_partial_line_is_not_processed_until_complete(self):
        self.append(entry('user', 'request'))
        row = json.dumps(limit())
        with self.path.open('a') as handle:
            handle.write(row[:40])
        self.monitor.scan()
        self.assertFalse(self.monitor.queue)
        with self.path.open('a') as handle:
            handle.write(row[40:] + '\n')
        self.monitor.scan()
        self.assertEqual(len(self.monitor.queue), 1)

    def test_stale_event_does_not_switch(self):
        event = replace(self.failed_event(), timestamp=time.time()-181)
        self.assertFalse(self.monitor.valid(event))

    def test_archived_session_is_not_eligible(self):
        event = self.failed_event()
        self.monitor.records.clear()
        self.assertFalse(self.monitor.valid(event))

    def test_other_live_task_blocks_but_stale_history_does_not(self):
        event = self.failed_event()
        other = self.home / 'projects/test/other.jsonl'
        other.write_text(json.dumps(entry('user', 'other-request', sessionId='other'))+'\n')
        self.monitor.records['other'] = auto.Session('local_other', 'other', str(self.cwd), other, {})
        self.assertFalse(self.monitor.other_task_running(event))
        with patch.object(auto, 'live_desktop_sessions', return_value={SESSION, 'other'}):
            self.assertTrue(self.monitor.other_task_running(event))

    def test_full_recovery_sends_once_and_returns_without_waiting_for_task(self):
        event = self.failed_event()
        process, swap, spawn = self.recovery_services()
        self.monitor.recover(event)
        self.monitor.recover(event)
        swap.assert_called_once()
        spawn.assert_called_once()
        self.assertIn(SESSION, self.monitor.jobs)
        self.monitor.poll_jobs()
        self.assertEqual(self.monitor.state['handled'][event.key]['status'], 'resumed')
        process.poll.return_value = 0
        self.monitor.poll_jobs()
        self.assertFalse(self.monitor.jobs)
        self.assertEqual(self.monitor.state['handled'][event.key]['status'], 'completed')

    def test_poll_detects_new_error_switches_and_confirms_job(self):
        self.append(entry('user', 'request'), limit())
        process, swap, spawn = self.recovery_services()
        now = time.time()
        with patch.object(auto.time, 'time', return_value=now + 5):
            self.monitor.poll()
        swap.assert_called_once()
        spawn.assert_called_once()
        process.poll.return_value = 0
        self.monitor.poll()
        self.assertFalse(self.monitor.jobs)
        self.assertEqual(self.monitor.state['handled'][SESSION+':failure']['status'], 'completed')

    def test_no_quota_never_switches(self):
        event = self.failed_event()
        with patch.object(auto.core, 'usage', return_value={'session': (0, 10), 'weekly': (80, 10)}), \
             patch.object(self.monitor, 'find_cli', return_value='claude.exe'), patch.object(auto.core, 'switch') as swap:
            self.monitor.recover(event)
        swap.assert_not_called()
        self.assertEqual(self.monitor.state['handled'][event.key]['status'], 'no_available_account')

    def test_subprocess_start_failure_is_not_retried(self):
        event = self.failed_event()
        _, _, spawn = self.recovery_services()
        spawn.side_effect = OSError('could not launch')
        self.monitor.recover(event)
        self.monitor.recover(event)
        spawn.assert_called_once()
        self.assertEqual(self.monitor.state['handled'][event.key]['status'], 'failed')

    def test_only_own_cli_result_can_authorize_followup_limit(self):
        event = self.failed_event()
        process, _, _ = self.recovery_services(quota=True)
        self.monitor.recover(event)
        process.poll.return_value = 1
        self.monitor.poll_jobs()
        self.assertEqual(self.monitor.owned_errors['next-failure'], 'new')
        self.assertIn(SESSION+':next-failure', self.monitor.queue)

    def test_manual_message_stops_automatic_cli(self):
        event = self.failed_event()
        process, _, _ = self.recovery_services()
        self.monitor.recover(event)
        self.append(entry('user', 'manual-desktop-message'))
        self.monitor.poll_jobs()
        process.terminate.assert_called_once()
        process.poll.return_value = -1
        self.monitor.poll_jobs()
        self.assertFalse(self.monitor.jobs)


class CredentialTests(unittest.TestCase):
    def test_selected_token_is_used_without_changing_saved_or_parent_auth(self):
        account = {'dir': 'profile', 'account_uuid': 'new', 'email': 'new@example.test'}
        with patch.object(auto.core, '_best_token', return_value=({'token': 'chosen', 'expiresAt': (time.time()+300)*1000}, 'new', 'org')), \
             patch.object(auto.core, '_api', return_value={'account': {'email': account['email']}}), \
             patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'old-key', 'CLAUDE_CODE_OAUTH_TOKEN': 'old-token'}):
            environment = auto.cli_environment(account, Path('shared'))
            self.assertEqual(environment['CLAUDE_CODE_OAUTH_TOKEN'], 'chosen')
            self.assertNotIn('ANTHROPIC_API_KEY', environment)
            self.assertEqual(os.environ['CLAUDE_CODE_OAUTH_TOKEN'], 'old-token')

    def test_wrong_account_token_is_rejected(self):
        with patch.object(auto.core, '_best_token', return_value=({'token': 'wrong', 'expiresAt': (time.time()+300)*1000}, 'other', 'org')):
            with self.assertRaises(RuntimeError):
                auto.cli_environment({'dir': 'profile', 'account_uuid': 'expected'}, Path('shared'))

    def test_permission_mode_is_preserved_without_adding_bypass(self):
        session = auto.Session('local', SESSION, '.', Path('session'), {'permissionMode': 'default'})
        args = auto.cli_arguments('claude.exe', session)
        self.assertEqual(args[args.index('--permission-mode')+1], 'default')
        self.assertNotIn('--dangerously-skip-permissions', args)
        self.assertNotIn('--tools', args)


if __name__ == '__main__':
    unittest.main()
