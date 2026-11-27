"""Resume ONLY fresh Desktop turns terminated with usage_limit_exceeded."""
from dataclasses import dataclass
from contextlib import closing
from datetime import datetime
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sqlite3
import threading
import time

import codex_core as core
import desktop_bridge

OPERATION_LOCK = threading.RLock()
LIMIT_CODE = 'usage_limit_exceeded'
RESUME_PROMPT = 'Meu último pedido foi interrompido pelo limite de sessão. Veja onde parou e continue.'
TERMINALS = {'task_complete', 'turn_aborted'}


def epoch(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def eligible_usage(data):
    """Unknown windows must not be mistaken for 100% available."""
    try:
        session, weekly = data['session'], data['weekly']
        return session is not None and weekly is not None and session[0] > 0 and weekly[0] > 0
    except (KeyError, TypeError, IndexError):
        return False


def choose_account(index, excluded, usage_fn):
    candidates = []
    for account in index['accounts']:
        if account['key'] in excluded:
            continue
        try:
            usage = usage_fn(index, account['key'])
        except Exception:
            continue
        if eligible_usage(usage):
            candidates.append((usage['session'][0], usage['weekly'][0], account['key'], account))
    return max(candidates, default=(None, None, None, None))[-1]


@dataclass(frozen=True)
class InterruptedTurn:
    thread: str
    turn: str
    timestamp: float
    account: str
    path: Path

    @property
    def key(self):
        return self.thread + ':' + self.turn


class SessionTail:
    def __init__(self, path, baseline=False):
        self.path = Path(path)
        with self.path.open('rb') as handle:
            first = json.loads(handle.readline())
        self.meta = first.get('payload', {}) if first.get('type') == 'session_meta' else {}
        self.offset = self.path.stat().st_size if baseline else 0
        self.pending = b''
        self.latest = None
        self.account = None

    @property
    def desktop(self):
        return self.meta.get('originator') == 'Codex Desktop' and not self.meta.get('parent_thread_id')

    def read(self, since, account):
        events = []
        if self.path.stat().st_size < self.offset:
            # Truncated/replaced session: baseline again; never replay old turns.
            self.offset = self.path.stat().st_size
            self.pending = b''
            return events
        with self.path.open('rb') as handle:
            handle.seek(self.offset)
            chunk = handle.read()
            self.offset = handle.tell()
        lines = (self.pending + chunk).split(b'\n')
        self.pending = lines.pop()
        for line in lines:
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if entry.get('type') != 'event_msg' or epoch(entry.get('timestamp')) < since:
                continue
            payload = entry.get('payload') or {}
            kind, turn = payload.get('type'), payload.get('turn_id')
            if kind == 'task_started':
                self.account = account
                self.latest = ('running', turn)
                events.append(('started', payload))
            elif kind in TERMINALS:
                self.latest = ('stopped', turn)
                error = payload.get('error') or {}
                if (kind == 'task_complete' and isinstance(error, dict)
                        and error.get('codex_error_info') == LIMIT_CODE and turn
                        and self.desktop and self.has_start(turn)):
                    events.append(('limit', InterruptedTurn(self.meta['id'], turn,
                                  epoch(entry['timestamp']), self.account or account, self.path)))
                elif kind == 'task_complete' and not error:
                    events.append(('completed', payload))
        return events

    def has_start(self, turn):
        # A turn may already have been running when the switcher was started.
        with self.path.open('rb') as handle:
            for line in handle:
                if b'"task_started"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                    payload = entry.get('payload') or {}
                    if entry.get('type') == 'event_msg' and payload.get('type') == 'task_started' and payload.get('turn_id') == turn:
                        return True
                except (ValueError, UnicodeError):
                    pass
        return False


def last_lifecycle(path):
    """Return most recent turn state; read-only, preserves partial writes."""
    latest = None
    with Path(path).open('rb') as handle:
        for line in handle:
            if b'"event_msg"' not in line or not any(word in line for word in (b'"task_started"', b'"task_complete"', b'"turn_aborted"')):
                continue
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            payload = entry.get('payload') or {}
            if entry.get('type') == 'event_msg' and payload.get('type') in TERMINALS | {'task_started'}:
                latest = ('running' if payload['type'] == 'task_started' else 'stopped', payload.get('turn_id'))
    return latest


class Monitor:
    log_name = 'codex_autoresume'
    provider = 'Codex'
    limit_code = LIMIT_CODE

    def __init__(self, home=None, store=None, notify=None, changed=None):
        self.home = Path(home or core.CODEX_HOME)
        self.store = Path(store or core.STORE)
        self.notify = notify or (lambda text: None)
        self.changed = changed or (lambda: None)
        self.stop_event = threading.Event()
        self.recovering = False
        self.since = time.time()
        self.tails = {}
        self.queue = {}
        self.state_path = self.store / 'autoresume-state.json'
        self.config_path = self.store / 'autoresume.json'
        state_error = False
        try:
            self.state = json.loads(self.state_path.read_text(encoding='utf-8')) if self.state_path.exists() else {'handled': {}, 'chains': {}}
            if not isinstance(self.state.get('handled'), dict) or not isinstance(self.state.get('chains'), dict):
                raise ValueError('Invalid recovery state')
        except (OSError, ValueError, AttributeError):
            self.state = {'handled': {}, 'chains': {}}
            state_error = True
        self.store.mkdir(parents=True, exist_ok=True)
        if state_error or not self.config_path.exists():
            atomic_json(self.config_path, {'enabled': not state_error})
        self.log = logging.getLogger(self.log_name + '.' + hashlib.sha256(str(self.store).encode()).hexdigest()[:12])
        self.log.setLevel(logging.INFO)
        if not self.log.handlers:
            handler = RotatingFileHandler(self.store / 'autoresume.log', maxBytes=1024*1024, backupCount=2, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
            self.log.addHandler(handler)
        if state_error:
            self.log.error('Invalid saved state; automatic recovery disabled. Re-enable in the tray menu after inspection.')
        # Previously interrupted recoveries are recorded, never automatically resent.
        self.scan(baseline=True)

    def enabled(self):
        try:
            return json.loads(self.config_path.read_text(encoding='utf-8')).get('enabled') is True
        except (OSError, ValueError):
            return False

    def set_enabled(self, value):
        atomic_json(self.config_path, {'enabled': bool(value)})

    def current_account(self):
        try:
            return core.describe(core._read(core.AUTH))['key']
        except (OSError, ValueError):
            return None

    def persist(self):
        # Retain recent event IDs; startup baselining also prevents replay.
        self.state['handled'] = dict(list(self.state['handled'].items())[-1000:])
        atomic_json(self.state_path, self.state)

    def mark(self, event, status, **details):
        self.state['handled'][event.key] = {'status': status, 'time': time.time(), **details}
        self.persist()
        self.log.info('%s thread=%s turn=%s %s', status, event.thread, event.turn, json.dumps(details))

    def scan(self, baseline=False):
        account = self.current_account()
        for path in (self.home / 'sessions').rglob('*.jsonl'):
            try:
                if path not in self.tails:
                    self.tails[path] = SessionTail(path, baseline=baseline)
                tail = self.tails[path]
                if not tail.desktop or baseline:
                    continue
                for kind, item in tail.read(self.since, account):
                    thread = tail.meta['id']
                    if kind == 'started' and (item.get('turn_attribution') or {}).get('turn_trigger') == 'composer':
                        self.state['chains'].pop(thread, None)
                    elif kind == 'completed':
                        self.state['chains'].pop(thread, None)
                    elif kind == 'limit' and item.key not in self.state['handled']:
                        if not self.enabled():
                            self.mark(item, 'ignored_disabled')
                        else:
                            self.queue[item.key] = item
            except (OSError, ValueError, KeyError):
                continue  # Newly created or locked files are retried next scan.

    def valid(self, event):
        if time.time() - event.timestamp > 180 or self.current_account() != event.account:
            return False
        if last_lifecycle(event.path) != ('stopped', event.turn):
            return False  # A manual continuation or cancellation supersedes this event.
        return self.desktop_thread(event.thread)

    def desktop_thread(self, thread):
        with closing(sqlite3.connect((self.home / 'state_5.sqlite').as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            row = db.execute('SELECT originator, archived FROM threads WHERE id=?', (thread,)).fetchone()
        return bool(row and row[0] == 'Codex Desktop' and not row[1])

    def read_status(self, bridge, event):
        data = bridge.call('read_thread', {'threadId': event.thread, 'hostId': 'local', 'includeTurns': False}, event.thread)
        return data

    def recover(self, event):
        if event.key in self.state['handled']:
            return
        if not self.enabled() or not self.valid(event):
            self.mark(event, 'superseded')
            return
        self.mark(event, 'checking')  # Persistent claim BEFORE any side effect.
        bridge = None
        try:
            bridge = desktop_bridge.connect(timeout=8)
            status = self.read_status(bridge, event)
            thread_data = status.get('thread', status)
            thread_status = thread_data.get('status')
            if isinstance(thread_status, dict):
                thread_status = thread_status.get('type')
            if thread_status not in ('idle', 'notLoaded', 'systemError'):
                self.mark(event, 'superseded_running')
                return
            # Switching closes Desktop. Do not interrupt a different live turn.
            overview = bridge.call('list_threads', {'limit': 50}, event.thread)
            for thread in overview.get('pinnedThreads', []) + overview.get('threads', []):
                if (thread.get('kind') == 'codex' and thread.get('hostId') in (None, 'local')
                        and thread.get('id') != event.thread and thread.get('status') == 'active'):
                    self.mark(event, 'another_task_running')
                    self.notify('Uma tarefa parou por limite, mas outra conversa ainda está executando. A conta não foi trocada.')
                    return
            bridge.close()
            bridge = None
            index = core.sync_current(core.load())
            tried = set(self.state['chains'].get(event.thread, [])) | {event.account}
            target = choose_account(index, tried, core.usage)
            if not target:
                self.mark(event, 'no_available_account')
                self.notify('Codex parou por limite. Nenhuma outra conta tem limite disponível; a tarefa continua parada.')
                return
            # User may have resumed while the usage endpoints were being consulted.
            if not self.enabled() or not self.valid(event):
                self.mark(event, 'superseded')
                return
            self.state['chains'][event.thread] = list(tried | {target['key']})
            self.mark(event, 'switching', target=target['key'])
            self.notify('Tarefa interrompida por limite. Trocando a conta do Codex e retomando a mesma conversa…')
            if not core.switch(index, target['key']) or self.current_account() != target['key']:
                raise RuntimeError('Account switch verification failed')
            self.changed()
            # Re-discover the pipe: its name changes whenever Desktop restarts.
            bridge = desktop_bridge.connect(timeout=60)
            if not self.enabled() or last_lifecycle(event.path) != ('stopped', event.turn):
                self.mark(event, 'superseded_after_switch')
                return
            if self.current_account() != target['key']:
                raise RuntimeError('Active account changed before continuation')
            self.mark(event, 'send_attempted', target=target['key'])
            # Exactly one attempt. A timeout may mean delivery succeeded; never resend.
            bridge.call('send_message_to_thread', {'threadId': event.thread, 'hostId': 'local', 'prompt': RESUME_PROMPT}, event.thread)
            self.mark(event, 'sent', target=target['key'])
            bridge.close()
            bridge = None
            deadline = time.monotonic() + 60
            while not self.stop_event.is_set() and time.monotonic() < deadline:
                lifecycle = last_lifecycle(event.path)
                if lifecycle and lifecycle[1] != event.turn:
                    self.mark(event, 'resumed', target=target['key'], resumed_turn=lifecycle[1])
                    self.notify('Codex retomou a conversa na conta ' + target['email'] + '.')
                    return
                self.stop_event.wait(2)
            self.mark(event, 'sent_unconfirmed', target=target['key'])
            self.notify('A conta foi trocada e a mensagem de retomada foi enviada, mas o início do turno ainda não foi confirmado.')
        except Exception as error:
            self.mark(event, 'failed', error=type(error).__name__)
            self.log.exception('Recovery failed')
            self.notify('Não foi possível concluir a retomada automática do Codex. Veja autoresume.log; a mensagem não será reenviada automaticamente.')
        finally:
            if bridge:
                bridge.close()

    def poll(self):
        self.scan()
        if not self.queue or not OPERATION_LOCK.acquire(blocking=False):
            return
        try:
            for key, event in list(self.queue.items()):
                if time.time() - event.timestamp < 4:
                    continue  # Allow Desktop to publish the final idle/error status.
                self.recovering = True
                self.recover(event)
                self.queue.pop(key, None)
        finally:
            self.recovering = False
            OPERATION_LOCK.release()

    def run(self):
        self.log.info('Monitor started; only new terminal %s events are eligible', self.limit_code)
        while not self.stop_event.wait(3):
            try:
                self.poll()
            except Exception:
                self.log.exception('Monitor poll failed')

    def start(self):
        threading.Thread(target=self.run, name=self.provider.lower() + '-limit-monitor', daemon=True).start()

    def stop(self):
        self.stop_event.set()
