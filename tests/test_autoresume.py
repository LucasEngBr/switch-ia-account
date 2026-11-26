from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import codex_autoresume as auto


def record(kind, **payload):
    return {'type': 'event_msg', 'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'payload': {'type': kind, **payload}}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home, self.store = self.base / 'home', self.base / 'store'
        self.path = self.home / 'sessions' / 'rollout-test.jsonl'
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'thread', 'originator': 'Codex Desktop'}}) + '\n')
        with closing(sqlite3.connect(self.home / 'state_5.sqlite')) as con:
            con.execute('CREATE TABLE threads(id TEXT, originator TEXT, archived INTEGER)')
            con.execute("INSERT INTO threads VALUES('thread', 'Codex Desktop', 0)")
            con.commit()
        self.current = 'old'
        self.auth = patch.object(auto.Monitor, 'current_account', side_effect=lambda: self.current)
        self.auth.start()
        self.monitor = auto.Monitor(self.home, self.store)
        self.monitor.since = time.time() - 1  # Fixtures use whole-second timestamps.
        self.monitor.notify = Mock()
        self.monitor.changed = Mock()

    def tearDown(self):
        self.auth.stop()
        for handler in list(self.monitor.log.handlers):
            handler.close()
            self.monitor.log.removeHandler(handler)
        self.temp.cleanup()

    def append(self, *entries):
        with self.path.open('a', encoding='utf-8') as out:
            for entry in entries:
                out.write(json.dumps(entry) + '\n')

    def limit(self, turn='turn'):
        self.append(record('task_started', turn_id=turn, turn_attribution={'turn_trigger': 'composer'}),
                    record('task_complete', turn_id=turn, error={'codex_error_info': auto.LIMIT_CODE}))
        self.monitor.scan()
        return self.monitor.queue['thread:' + turn]

    def services(self, send_error=None):
        bridge = Mock()
        def call(tool, args, thread):
            if tool == 'read_thread':
                return {'thread': {'status': {'type': 'idle'}}}
            if tool == 'list_threads':
                return {'threads': []}
            self.assertEqual(args['prompt'], 'Meu último pedido foi interrompido pelo limite de sessão. Veja onde parou e continue.')
            if send_error:
                raise send_error
            self.append(record('task_started', turn_id='continued', turn_attribution={'turn_trigger': 'app_tool_send_message'}))
            return {'threadId': 'thread'}
        bridge.call.side_effect = call
        index = {'active': 'old', 'accounts': [{'key': 'old'}, {'key': 'new', 'email': 'new@example.test'}]}
        def switch(index, key):
            self.current = key
            return True
        return bridge, index, switch

    def test_only_real_terminal_limit_is_queued(self):
        event = self.limit()
        self.assertTrue(self.monitor.valid(event))
        self.assertEqual(event.account, 'old')

    def test_zero_meter_warnings_user_text_and_completion_do_nothing(self):
        self.append(record('task_started', turn_id='turn'),
                    record('token_count', rate_limits={'used_percent': 100}),
                    record('warning', message='usage_limit_exceeded'),
                    {'type': 'response_item', 'payload': {'role': 'user', 'content': [{'text': 'usage_limit_exceeded'}]}},
                    record('task_complete', turn_id='turn'))
        with patch.object(auto.core, 'switch') as switch:
            self.monitor.poll()
        self.assertFalse(self.monitor.queue)
        switch.assert_not_called()

    def test_other_errors_and_cancel_do_nothing(self):
        for code in ('rate_limit', 'http_429', 'network_error', 'authentication_failed'):
            self.append(record('task_started', turn_id=code), record('task_complete', turn_id=code, error={'codex_error_info': code}))
        self.append(record('turn_aborted', turn_id='cancel', reason='interrupted'))
        self.monitor.scan()
        self.assertFalse(self.monitor.queue)

    def test_requires_prior_started_turn(self):
        self.append(record('task_complete', turn_id='unknown', error={'codex_error_info': auto.LIMIT_CODE}))
        self.monitor.scan()
        self.assertFalse(self.monitor.queue)

    def test_old_errors_are_not_replayed_at_startup(self):
        self.limit()
        monitor = auto.Monitor(self.home, self.store)
        monitor.scan()
        self.assertFalse(monitor.queue)

    def test_partial_record_waits_for_newline(self):
        self.append(record('task_started', turn_id='turn'))
        line = json.dumps(record('task_complete', turn_id='turn', error={'codex_error_info': auto.LIMIT_CODE}))
        with self.path.open('a') as out:
            out.write(line[:30])
        self.monitor.scan()
        self.assertFalse(self.monitor.queue)
        with self.path.open('a') as out:
            out.write(line[30:] + '\n')
        self.monitor.scan()
        self.assertEqual(len(self.monitor.queue), 1)

    def test_existing_running_turn_can_fail_after_startup(self):
        self.append(record('task_started', turn_id='turn'))
        self.monitor = auto.Monitor(self.home, self.store)
        self.monitor.since = time.time() - 1
        self.append(record('task_complete', turn_id='turn', error={'codex_error_info': auto.LIMIT_CODE}))
        self.monitor.scan()
        self.assertEqual(len(self.monitor.queue), 1)

    def test_manual_resume_or_account_change_invalidates_error(self):
        event = self.limit()
        self.current = 'manual'
        self.assertFalse(self.monitor.valid(event))
        self.current = 'old'
        self.append(record('task_started', turn_id='manual'))
        self.assertFalse(self.monitor.valid(event))

    def test_disabled_consumes_event_without_replay_on_enable(self):
        self.monitor.set_enabled(False)
        self.append(record('task_started', turn_id='turn'), record('task_complete', turn_id='turn', error={'codex_error_info': auto.LIMIT_CODE}))
        self.monitor.scan()
        self.monitor.set_enabled(True)
        self.monitor.scan()
        self.assertFalse(self.monitor.queue)
        self.assertEqual(self.monitor.state['handled']['thread:turn']['status'], 'ignored_disabled')

    def test_recovery_switches_and_sends_once(self):
        event = self.limit()
        bridge, index, switch = self.services()
        with patch.object(auto.desktop_bridge, 'connect', return_value=bridge), \
             patch.object(auto.core, 'load', return_value=index), \
             patch.object(auto.core, 'sync_current', side_effect=lambda idx: idx), \
             patch.object(auto.core, 'usage', return_value={'session': (90, 100), 'weekly': (30, 100)}), \
             patch.object(auto.core, 'switch', side_effect=switch) as swap:
            self.monitor.recover(event)
            self.monitor.recover(event)
        swap.assert_called_once()
        sends = [call for call in bridge.call.call_args_list if call.args[0] == 'send_message_to_thread']
        self.assertEqual(len(sends), 1)
        self.assertEqual(self.monitor.state['handled'][event.key]['status'], 'resumed')

    def test_no_candidate_never_switches_or_sends(self):
        event = self.limit()
        bridge, index, _ = self.services()
        with patch.object(auto.desktop_bridge, 'connect', return_value=bridge), \
             patch.object(auto.core, 'load', return_value=index), \
             patch.object(auto.core, 'sync_current', side_effect=lambda idx: idx), \
             patch.object(auto.core, 'usage', return_value={'session': (100, 10), 'weekly': (0, 10)}), \
             patch.object(auto.core, 'switch') as swap:
            self.monitor.recover(event)
        swap.assert_not_called()
        self.assertEqual(self.monitor.state['handled'][event.key]['status'], 'no_available_account')

    def test_ambiguous_send_timeout_is_never_retried(self):
        event = self.limit()
        bridge, index, switch = self.services(TimeoutError('delivery unknown'))
        with patch.object(auto.desktop_bridge, 'connect', return_value=bridge), \
             patch.object(auto.core, 'load', return_value=index), \
             patch.object(auto.core, 'sync_current', side_effect=lambda idx: idx), \
             patch.object(auto.core, 'usage', return_value={'session': (90, 100), 'weekly': (30, 100)}), \
             patch.object(auto.core, 'switch', side_effect=switch):
            self.monitor.recover(event)
            self.monitor.recover(event)
        self.assertEqual(len([c for c in bridge.call.call_args_list if c.args[0] == 'send_message_to_thread']), 1)
        reloaded = auto.Monitor(self.home, self.store)
        self.assertIn(event.key, reloaded.state['handled'])

    def test_session_from_cli_is_ignored(self):
        self.monitor.tails[self.path].meta['originator'] = 'codex_cli_rs'
        self.append(record('task_started', turn_id='turn'), record('task_complete', turn_id='turn', error={'codex_error_info': auto.LIMIT_CODE}))
        self.monitor.scan()
        self.assertFalse(self.monitor.queue)

    def test_stale_event_is_ignored(self):
        event = self.limit()
        event = auto.InterruptedTurn(event.thread, event.turn, time.time() - 181, event.account, event.path)
        self.assertFalse(self.monitor.valid(event))

    def test_another_running_thread_prevents_switch(self):
        event = self.limit()
        bridge = Mock()
        bridge.call.side_effect = [{'thread': {'status': {'type': 'idle'}}},
                                   {'threads': [{'id': 'other', 'kind': 'codex', 'hostId': 'local', 'status': 'active'}]}]
        with patch.object(auto.desktop_bridge, 'connect', return_value=bridge), patch.object(auto.core, 'switch') as swap:
            self.monitor.recover(event)
        swap.assert_not_called()
        self.assertEqual(self.monitor.state['handled'][event.key]['status'], 'another_task_running')

    def test_chain_does_not_revisit_account_that_already_failed(self):
        event = self.limit()
        self.monitor.state['chains']['thread'] = ['old', 'new']
        bridge, index, _ = self.services()
        with patch.object(auto.desktop_bridge, 'connect', return_value=bridge), \
             patch.object(auto.core, 'load', return_value=index), \
             patch.object(auto.core, 'sync_current', side_effect=lambda idx: idx), \
             patch.object(auto.core, 'usage') as usage, patch.object(auto.core, 'switch') as swap:
            self.monitor.recover(event)
        usage.assert_not_called()
        swap.assert_not_called()

    def test_active_or_unknown_desktop_status_prevents_switch(self):
        event = self.limit()
        bridge = Mock()
        bridge.call.return_value = {'thread': {'status': {'type': 'active'}}}
        with patch.object(auto.desktop_bridge, 'connect', return_value=bridge), patch.object(auto.core, 'switch') as swap:
            self.monitor.recover(event)
        swap.assert_not_called()

    def test_archived_thread_is_not_resumed(self):
        event = self.limit()
        with closing(sqlite3.connect(self.home / 'state_5.sqlite')) as db:
            db.execute('UPDATE threads SET archived=1')
            db.commit()
        self.assertFalse(self.monitor.valid(event))


class AccountChoiceTests(unittest.TestCase):
    def test_sort_excludes_unknown_exhausted_and_previously_tried(self):
        idx = {'accounts': [{'key': key} for key in ('active', 'spent', 'unknown', 'less', 'best', 'tie')]}
        data = {'spent': {'session': [100, 1], 'weekly': [0, 1]}, 'unknown': {'session': [100, 1], 'weekly': None},
                'less': {'session': [90, 1], 'weekly': [90, 1]}, 'best': {'session': [100, 1], 'weekly': [70, 1]},
                'tie': {'session': [100, 1], 'weekly': [30, 1]}}
        self.assertEqual(auto.choose_account(idx, {'active'}, lambda _, key: data[key])['key'], 'best')
        self.assertEqual(auto.choose_account(idx, {'active', 'best'}, lambda _, key: data[key])['key'], 'tie')


if __name__ == '__main__':
    unittest.main()
