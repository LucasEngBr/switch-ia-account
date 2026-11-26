"""Bounded requests to the running Codex Desktop's local app-tools pipe.

This is an internal Desktop interface, not a daemon or a second session writer.
Only the tools advertised by the connected Desktop are used.
"""
import ctypes
from ctypes import wintypes
import json
import os
import struct
import time
import uuid

PIPE_ROOT = '\\\\.\\pipe\\'
MAX_FRAME = 8 * 1024 * 1024


class DesktopBridge:
    def __init__(self, path, timeout=10):
        self.path, self.timeout = path, timeout
        self.handle = None
        self.next_id = 0
        self.tools = {}

    def __enter__(self):
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.api.CreateFileW.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.SetNamedPipeHandleState.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                                    ctypes.c_void_p, ctypes.c_void_p]
        for name in ('ReadFile', 'WriteFile'):
            function = getattr(self.api, name)
            function.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        handle = self.api.CreateFileW(self.path, 0xC0000000, 0, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle
        mode = wintypes.DWORD(1)  # PIPE_NOWAIT: every read/write is bounded below.
        if not self.api.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None):
            self.close()
            raise OSError('Cannot set a timeout-safe Desktop pipe mode')
        return self

    def close(self):
        if self.handle is not None:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def __exit__(self, *_):
        self.close()

    def _transfer(self, data, length, write, deadline):
        result = bytearray()
        offset = 0
        while offset < length:
            if time.monotonic() >= deadline:
                raise TimeoutError('Codex Desktop channel timed out')
            buffer = ctypes.create_string_buffer(data[offset:]) if write else ctypes.create_string_buffer(length - offset)
            count = wintypes.DWORD()
            ok = getattr(self.api, 'WriteFile' if write else 'ReadFile')(
                self.handle, buffer, length - offset, ctypes.byref(count), None)
            if not ok:
                error = ctypes.get_last_error()
                if error not in (232, 234):  # NO_DATA / MORE_DATA
                    raise ctypes.WinError(error)
            if count.value:
                if not write:
                    result.extend(buffer.raw[:count.value])
                offset += count.value
            else:
                time.sleep(0.02)
        return bytes(result)

    def request(self, method, params):
        self.next_id += 1
        body = json.dumps({'jsonrpc': '2.0', 'id': self.next_id, 'method': method, 'params': params}).encode()
        if len(body) > MAX_FRAME:
            raise ValueError('Desktop request too large')
        deadline = time.monotonic() + self.timeout
        frame = struct.pack('<I', len(body)) + body
        self._transfer(frame, len(frame), True, deadline)
        length = struct.unpack('<I', self._transfer(None, 4, False, deadline))[0]
        if length > MAX_FRAME:
            raise ValueError('Desktop response too large')
        response = json.loads(self._transfer(None, length, False, deadline))
        if response.get('id') != self.next_id or 'error' in response:
            raise RuntimeError('Desktop rejected request: ' + str(response.get('error', 'ID mismatch')))
        return response['result']

    def catalog(self):
        self.tools = {tool['name']: tool for tool in self.request('tools/list', {'threadStartKind': 'all'})['tools']}
        return self.tools

    def call(self, tool, arguments, thread_id):
        selected = self.tools[tool]
        result = self.request('tools/call', {
            'arguments': arguments, 'callerSource': 'codex', 'hostId': 'local',
            'callId': 'limit-recovery-' + str(uuid.uuid4()), 'namespace': selected['namespace'],
            'threadId': thread_id, 'tool': tool, 'turnId': 'limit-recovery-' + str(uuid.uuid4()),
        })
        if not result.get('success'):
            raise RuntimeError('Desktop tool failed: ' + str(result.get('contentItems'))[:500])
        texts = [item['text'] for item in result.get('contentItems', []) if item.get('type') == 'inputText']
        return json.loads('\n'.join(texts)) if texts else {}


def connect(timeout=30):
    """Return an entered connection. Call close() after use. No message is sent here."""
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        for name in os.listdir(PIPE_ROOT):
            if not name.startswith('codex-app-tools-'):
                continue
            bridge = DesktopBridge(PIPE_ROOT + name)
            try:
                bridge.__enter__()
                if {'send_message_to_thread', 'read_thread'} <= bridge.catalog().keys():
                    return bridge
            except (OSError, ValueError, RuntimeError, KeyError) as error:
                last_error = type(error).__name__
            bridge.close()
        time.sleep(1)
    raise TimeoutError('Codex Desktop unavailable: ' + str(last_error))
