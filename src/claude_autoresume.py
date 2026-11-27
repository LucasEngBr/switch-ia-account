"""Claude Desktop quota recovery with account-scoped CLI credentials.

Only Desktop errors, or a quota failure proven by our own CLI result, can trigger
a switch. Shared transcripts alone never identify which account ran an SDK CLI.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

import claude_core as core
from codex_autoresume import Monitor as BaseMonitor, InterruptedTurn, OPERATION_LOCK, RESUME_PROMPT, choose_account, epoch


def entries(path, offset=0):
    with Path(path).open('rb') as handle:
        handle.seek(offset)
        for line in handle:
            if not line.endswith(b'\n'):
                break
            try:
                yield json.loads(line)
            except (ValueError, UnicodeError):
                continue


def main_entry(entry, session):
    return (entry.get('sessionId') == session and not entry.get('isSidechain')
            and not entry.get('isMeta') and not entry.get('isCompactSummary'))


def user_text(entry):
    if entry.get('type') != 'user' or entry.get('isMeta'):
        return ''
    content = (entry.get('message') or {}).get('content')
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list) and not any(item.get('type') == 'tool_result' for item in content if isinstance(item, dict)):
        return '\n'.join(item.get('text', '') for item in content if isinstance(item, dict) and item.get('type') == 'text').strip()
    return ''


def quota_error(entry):
    return (entry.get('type') == 'assistant' and entry.get('isApiErrorMessage') is True
            and entry.get('apiError') == 'usage_limit_reached' and bool(entry.get('uuid')))


def lifecycle(entry, session):
    if not main_entry(entry, session):
        return None
    text = user_text(entry)
    if text:
        if text.startswith('[Request interrupted by user'):
            return 'stopped', entry.get('uuid')
        return 'running', entry.get('uuid')
    if entry.get('type') == 'assistant':
        if entry.get('isApiErrorMessage'):
            return ('limit' if quota_error(entry) else 'stopped'), entry.get('uuid')
        if (entry.get('message') or {}).get('stop_reason') in ('end_turn', 'stop_sequence'):
            return 'completed', entry.get('uuid')
    return None


def last_state(path, session):
    latest = None
    for entry in entries(path):
        state = lifecycle(entry, session)
        if state:
            latest = state
    return latest


@dataclass
class Session:
    desktop_id: str
    cli_id: str
    cwd: str
    path: Path
    settings: dict


def session_records(index, home):
    """Read real profile folders; keep the active profile's metadata authoritative."""
    transcripts = {path.stem: path for path in (home / 'projects').glob('*/*.jsonl')}
    records = {}
    accounts = sorted(index['accounts'], key=lambda account: account['key'] == index.get('active'))
    for account in accounts:
        if not account.get('account_uuid') or not account.get('org_uuid'):
            continue
        directory = Path(core._sessions_dir(account))
        for path in directory.glob('local_*.json'):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                cli_id = str(uuid.UUID(data['cliSessionId']))
                if data.get('isArchived') or (directory / ('deleted_' + path.stem[len('local_'):])).exists():
                    records.pop(cli_id, None)
                    continue
                if cli_id in transcripts and data.get('cwd'):
                    records[cli_id] = Session(data['sessionId'], cli_id, data['cwd'], transcripts[cli_id], data)
            except (OSError, KeyError, ValueError, TypeError):
                continue
    return records


class Tail:
    def __init__(self, path, baseline=False):
        self.path = Path(path)
        self.offset = self.path.stat().st_size if baseline else 0
        self.pending = b''

    def read(self):
        size = self.path.stat().st_size
        if size < self.offset:
            self.offset, self.pending = size, b''
            return []
        with self.path.open('rb') as handle:
            handle.seek(self.offset)
            chunk = handle.read()
            self.offset = handle.tell()
        lines = (self.pending + chunk).split(b'\n')
        self.pending = lines.pop()
        result = []
        for line in lines:
            try:
                result.append(json.loads(line))
            except (ValueError, UnicodeError):
                continue
        return result


def cli_environment(account, home):
    token, account_id, _ = core._best_token(account['dir'])
    if account_id != account['account_uuid'] or token.get('expiresAt', 0) <= time.time() * 1000:
        raise RuntimeError('Target Claude credential expired or mismatched')
    # Validate the actual credential, not only the index or a selected UI label.
    profile = core._api('/api/oauth/profile', token['token'])
    if profile.get('account', {}).get('email') != account['email']:
        raise RuntimeError('Target Claude credential belongs to another account')
    environment = dict(os.environ)
    for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_USE_BEDROCK',
                'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'):
        environment.pop(key, None)
    environment['CLAUDE_CODE_OAUTH_TOKEN'] = token['token']
    environment['ANTHROPIC_BASE_URL'] = 'https://api.anthropic.com'
    environment['CLAUDE_CONFIG_DIR'] = str(home)
    return environment


def cli_arguments(cli, session):
    args = [cli, '-p', RESUME_PROMPT, '--resume', session.cli_id, '--output-format', 'json']
    settings = session.settings
    if settings.get('model'):
        args += ['--model', settings['model']]
    if settings.get('effort') in ('low', 'medium', 'high', 'xhigh', 'max', 'ultracode'):
        args += ['--effort', settings['effort']]
    if settings.get('permissionMode') in ('default', 'acceptEdits', 'plan', 'auto', 'dontAsk', 'bypassPermissions'):
        args += ['--permission-mode', settings['permissionMode']]
    return args


def live_desktop_sessions():
    """Only CLI children of the current Desktop can be interrupted by its switch."""
    script = r'''
    $all = @(Get-CimInstance Win32_Process -Filter "Name='claude.exe'" -ErrorAction Stop)
    $roots = @($all | Where-Object { $_.ExecutablePath -match 'WindowsApps\\Claude_' -and $_.CommandLine -notmatch '--type=' } | ForEach-Object { $_.ProcessId })
    foreach ($candidate in $all) {
        if ($candidate.ExecutablePath -match 'WindowsApps\\Claude_') { continue }
        $parent = $candidate.ParentProcessId
        $belongs = $false
        for ($step=0; $step -lt 20; $step++) {
            if ($parent -in $roots) { $belongs = $true; break }
            $ancestor = $all | Where-Object { $_.ProcessId -eq $parent } | Select-Object -First 1
            if (-not $ancestor) { break }
            $parent = $ancestor.ParentProcessId
        }
        if ($belongs) {
            $match = [regex]::Match($candidate.CommandLine, '--(?:resume|session-id)(?:=|\s+)"?([a-f0-9-]{36})')
            if ($match.Success) { $match.Groups[1].Value } else { 'UNKNOWN' }
        }
    }
    '''
    result = subprocess.run(['powershell', '-NoProfile', '-Command', script], capture_output=True,
                            text=True, creationflags=core.NO_WINDOW, timeout=10, check=True)
    return set(result.stdout.splitlines())


@dataclass
class Job:
    process: object
    output: object
    event: InterruptedTurn
    session: Session
    account: str
    baseline: int
    accepted: bool = False
    intervened: bool = False
    running: bool = False
    offset: int = 0
    latest: object = None


def job_entries(job):
    """Incremental reads also tolerate a line that Desktop/CLI is still writing."""
    with job.session.path.open('rb') as handle:
        handle.seek(job.offset or job.baseline)
        for line in handle:
            if not line.endswith(b'\n'):
                break
            job.offset = handle.tell()
            try:
                yield json.loads(line)
            except (ValueError, UnicodeError):
                continue


class Monitor(BaseMonitor):
    provider = 'Claude'
    log_name = 'claude_autoresume'
    limit_code = 'usage_limit_reached'

    def __init__(self, home=None, store=None, **kwargs):
        self.records = {}
        self.jobs = {}
        self.owned_errors = {}
        super().__init__(home=home or Path.home() / '.claude', store=store or core.STORE, **kwargs)

    def index(self):
        return core.detect_active(core.load())

    def current_account(self):
        running = core.running_dir()
        if not running:
            return None
        return next((account['key'] for account in core.load()['accounts']
                     if core._norm(account['dir']) == core._norm(running)), None)

    def scan(self, baseline=False):
        index = self.index()
        account = self.current_account()
        self.records = session_records(index, self.home)
        for session in self.records.values():
            try:
                if session.path not in self.tails:
                    self.tails[session.path] = Tail(session.path, baseline=baseline)
                if baseline:
                    continue
                for entry in self.tails[session.path].read():
                    if epoch(entry.get('timestamp')) < self.since or not main_entry(entry, session.cli_id):
                        continue
                    state = lifecycle(entry, session.cli_id)
                    if state and state[0] == 'completed':
                        self.state['chains'].pop(session.cli_id, None)
                    if user_text(entry) and entry.get('entrypoint') == 'claude-desktop':
                        self.state['chains'].pop(session.cli_id, None)
                    if not quota_error(entry):
                        continue
                    source = entry.get('entrypoint')
                    owned = self.owned_errors.get(entry['uuid'])
                    # Never react to unrelated CLI calls with the default/stale account.
                    if source != 'claude-desktop' and not owned:
                        continue
                    event = InterruptedTurn(session.cli_id, entry['uuid'], epoch(entry['timestamp']),
                                            owned or account, session.path)
                    if event.key not in self.state['handled']:
                        if not self.enabled():
                            self.mark(event, 'ignored_disabled')
                        else:
                            self.queue[event.key] = event
            except (OSError, ValueError):
                continue

    def valid(self, event):
        if time.time() - event.timestamp > 180 or not event.account or self.current_account() != event.account:
            return False
        session = self.records.get(event.thread)
        if not session or last_state(event.path, event.thread) != ('limit', event.turn):
            return False
        pending = False
        for entry in entries(event.path):
            state = lifecycle(entry, event.thread)
            if entry.get('uuid') == event.turn:
                return bool(pending and quota_error(entry))
            if state:
                pending = state[0] == 'running'
        return False

    def other_task_running(self, event):
        if self.jobs:
            return True
        live = live_desktop_sessions()
        if 'UNKNOWN' in live or live - set(self.records) - {event.thread}:
            return True
        for session in self.records.values():
            if session.cli_id != event.thread and session.cli_id in live:
                state = last_state(session.path, session.cli_id)
                if state and state[0] == 'running':
                    return True
        return False

    def find_cli(self):
        cli = shutil.which('claude')
        if cli:
            return cli
        fallback = Path.home() / '.local/bin/claude.exe'
        return str(fallback) if fallback.is_file() else None

    def recover(self, event):
        if event.key in self.state['handled']:
            return
        if not self.enabled() or not self.valid(event):
            self.mark(event, 'superseded')
            return
        self.mark(event, 'checking')
        output = None
        try:
            if self.other_task_running(event):
                self.mark(event, 'another_task_running')
                self.notify('Claude parou por limite, mas outra tarefa ainda parece estar executando. A conta não foi trocada.')
                return
            cli = self.find_cli()
            session = self.records[event.thread]
            if not cli or not Path(session.cwd).is_dir():
                raise RuntimeError('Claude CLI or original workspace unavailable')
            index = self.index()
            tried = set(self.state['chains'].get(event.thread, [])) | {event.account}
            target = choose_account(index, tried, core.usage)
            if not target:
                self.mark(event, 'no_available_account')
                self.notify('Claude parou por limite. Nenhuma outra conta tem limite disponível; a tarefa continua parada.')
                return
            environment = cli_environment(target, self.home)
            if not self.enabled() or not self.valid(event):
                self.mark(event, 'superseded')
                return
            self.state['chains'][event.thread] = list(tried | {target['key']})
            self.mark(event, 'switching', target=target['key'])
            self.notify('Tarefa interrompida por limite. Trocando a conta do Claude e retomando a mesma conversa…')
            core.switch(index, target['key'])
            deadline = time.monotonic() + 35
            while self.current_account() != target['key']:
                if self.stop_event.wait(1) or time.monotonic() > deadline:
                    raise RuntimeError('Claude did not reopen in selected profile')
            self.changed()
            if not self.enabled() or last_state(event.path, event.thread) != ('limit', event.turn):
                self.mark(event, 'superseded_after_switch')
                return
            baseline = event.path.stat().st_size
            output = tempfile.TemporaryFile(mode='w+b')
            self.mark(event, 'send_attempted', target=target['key'])
            process = subprocess.Popen(cli_arguments(cli, session), cwd=session.cwd, env=environment,
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                                       creationflags=core.NO_WINDOW)
            self.jobs[event.thread] = Job(process, output, event, session, target['key'], baseline)
            output = None  # The job owns this handle until the CLI exits.
            self.mark(event, 'cli_started', target=target['key'])
            # Do not hold the global switch lock for the duration of a long task.
        except Exception as error:
            self.mark(event, 'failed', error=type(error).__name__)
            self.log.error('Claude recovery failed: %s', type(error).__name__)
            self.notify('Não foi possível concluir a retomada do Claude. Veja autoresume.log; não haverá reenvio automático.')
        finally:
            if output:
                output.close()

    def poll_jobs(self):
        for thread, job in list(self.jobs.items()):
            manual, accepted, running = False, False, False
            for entry in job_entries(job):
                if not main_entry(entry, thread):
                    continue
                if lifecycle(entry, thread):
                    job.latest = entry
                if user_text(entry):
                    manual |= entry.get('entrypoint') == 'claude-desktop'
                    accepted |= entry.get('entrypoint') == 'sdk-cli' and user_text(entry) == RESUME_PROMPT
                if ((accepted or job.accepted) and entry.get('entrypoint') == 'sdk-cli'
                        and entry.get('type') == 'assistant' and not entry.get('isApiErrorMessage')):
                    running = True
            if manual and not job.intervened:
                job.intervened = True
                if job.process.poll() is None:
                    job.process.terminate()
                self.mark(job.event, 'manual_intervention', target=job.account)
                self.notify('Você enviou uma mensagem no Desktop; a retomada pela CLI foi interrompida para não executar os dois ao mesmo tempo.')
            if not job.accepted and accepted and not job.intervened:
                job.accepted = True
                self.mark(job.event, 'sent', target=job.account)
            if not job.running and running and not job.intervened:
                job.running = True
                self.mark(job.event, 'resumed', target=job.account)
                self.notify('Claude retomou a mesma conversa usando a conta selecionada.')
            if job.process.poll() is None:
                continue
            try:
                if job.intervened:
                    continue
                job.output.seek(0)
                result = json.loads(job.output.read(16 * 1024 * 1024))
                if result.get('session_id') != thread:
                    raise ValueError('CLI returned a different session')
                if not result.get('is_error') and job.process.returncode == 0 and job.accepted:
                    self.state['chains'].pop(thread, None)
                    self.mark(job.event, 'completed', target=job.account)
                    self.notify('Claude continuou a conversa na conta escolhida e concluiu o turno.')
                elif result.get('is_error') and job.accepted:
                    latest = job.latest
                    if latest and quota_error(latest) and latest.get('entrypoint') == 'sdk-cli':
                        self.owned_errors[latest['uuid']] = job.account
                        event = InterruptedTurn(thread, latest['uuid'], epoch(latest['timestamp']), job.account, job.session.path)
                        self.queue[event.key] = event
                        self.mark(job.event, 'interrupted_again', target=job.account)
                    else:
                        self.mark(job.event, 'cli_error', target=job.account)
                        self.notify('A retomada do Claude encontrou outro erro; nenhuma nova troca foi iniciada.')
                else:
                    self.mark(job.event, 'unconfirmed', target=job.account)
            except (OSError, ValueError, TypeError):
                self.mark(job.event, 'unconfirmed', target=job.account)
                self.notify('O resultado da retomada do Claude não foi confirmado; a mensagem não será reenviada.')
            finally:
                job.output.close()
                self.jobs.pop(thread, None)

    def poll(self):
        self.poll_jobs()
        super().poll()
