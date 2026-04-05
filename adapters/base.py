import sys
import json
import logging
import threading
from pathlib import Path


class DAPAdapter:
    def __init__(self, log_file=None):
        if log_file:
            logging.basicConfig(
                filename=log_file,
                level=logging.DEBUG,
                format='%(asctime)s %(message)s',
            )
        self.seq = 0
        self.sld_map = {}
        self.address_to_line = {}
        self._source_path = None
        self._load_address = 0
        self.bin_file = None
        self.sld_file = None
        self._stdout_lock = threading.Lock()

        self.HANDLERS = {
            'initialize':        self.handle_initialize,
            'launch':            self.handle_launch,
            'setBreakpoints':    self.handle_set_breakpoints,
            'configurationDone': self.handle_configuration_done,
            'threads':           self.handle_threads,
            'stackTrace':        self.handle_stack_trace,
            'scopes':            self.handle_scopes,
            'variables':         self.handle_variables,
            'readMemory':        self.handle_read_memory,
            'next':              self.handle_step,
            'stepIn':            self.handle_step,
            'continue':          self.handle_continue,
            'disconnect':        self.handle_disconnect,
        }

    # ── DAP I/O ──────────────────────────────────────────────────────────────

    def send(self, msg):
        with self._stdout_lock:
            self.seq += 1
            msg['seq'] = self.seq
            body = json.dumps(msg)
            logging.debug(f'<<< {body}')
            sys.stdout.write(f'Content-Length: {len(body)}\r\n\r\n{body}')
            sys.stdout.flush()

    def read_message(self):
        headers = {}
        while True:
            line = sys.stdin.readline()
            if line in ('\r\n', '\n'):
                break
            name, _, value = line.strip().partition(': ')
            headers[name] = value
        length = int(headers.get('Content-Length', 0))
        body = sys.stdin.read(length)
        logging.debug(f'>>> {body}')
        return json.loads(body)

    # ── SLD parsing ───────────────────────────────────────────────────────────

    def parse_sld(self, sld_path):
        line_to_addr = {}
        addr_to_line = {}
        with open(sld_path) as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) >= 7 and parts[6] == 'T':
                    try:
                        line_num = int(parts[1])
                        address = int(parts[5])
                        line_to_addr[line_num] = address
                        addr_to_line[address] = line_num
                    except ValueError:
                        pass
        return line_to_addr, addr_to_line

    def snap_to_valid_line(self, line):
        for offset in range(0, 20):
            if line + offset in self.sld_map:
                return line + offset
        return None

    # ── Common DAP handlers ───────────────────────────────────────────────────

    def handle_initialize(self, msg):
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'initialize',
            'success': True,
            'body': {
                'supportsConfigurationDoneRequest': True,
                'supportsReadMemoryRequest': True,
            },
        })
        self.send({'type': 'event', 'event': 'initialized'})

    def handle_threads(self, msg):
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'threads',
            'success': True,
            'body': {'threads': [{'id': 1, 'name': 'Z80'}]},
        })

    def handle_scopes(self, msg):
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'scopes',
            'success': True,
            'body': {
                'scopes': [
                    {'name': 'Registers', 'variablesReference': 1, 'expensive': False},
                ],
            },
        })

    def handle_stack_trace(self, msg):
        regs = self.read_registers()
        pc = regs.get('PC', self._load_address)
        line = self.address_to_line.get(pc, 1)
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'stackTrace',
            'success': True,
            'body': {
                'stackFrames': [{
                    'id': 1,
                    'name': f'PC={pc:#06x}',
                    'source': {'path': self._source_path},
                    'line': line,
                    'column': 1,
                }],
                'totalFrames': 1,
            },
        })

    def handle_variables(self, msg):
        ref = msg['arguments'].get('variablesReference')
        variables = []
        if ref == 1:
            regs = self.read_registers()
            for name, value in regs.items():
                variables.append({
                    'name': name,
                    'value': f'0x{value:04x}',
                    'type': 'register',
                    'variablesReference': 0,
                })
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'variables',
            'success': True,
            'body': {'variables': variables},
        })

    # ── Abstract interface ────────────────────────────────────────────────────

    def read_registers(self) -> dict:
        raise NotImplementedError

    def handle_launch(self, msg):
        raise NotImplementedError

    def handle_set_breakpoints(self, msg):
        raise NotImplementedError

    def handle_configuration_done(self, msg):
        raise NotImplementedError

    def handle_read_memory(self, msg):
        raise NotImplementedError

    def handle_step(self, msg):
        raise NotImplementedError

    def handle_continue(self, msg):
        raise NotImplementedError

    def handle_disconnect(self, msg):
        raise NotImplementedError

    # ── Dispatch & main loop ─────────────────────────────────────────────────

    def handle(self, msg):
        cmd = msg.get('command')
        handler = self.HANDLERS.get(cmd)
        if handler:
            handler(msg)
        else:
            logging.warning(f'Unknown command: {cmd}')

    def main(self):
        while True:
            try:
                msg = self.read_message()
                self.handle(msg)
            except Exception:
                logging.exception('Unhandled exception')
                raise
