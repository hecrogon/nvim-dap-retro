import sys
import logging
import socket
import subprocess
import time
import threading
import re
import base64
from pathlib import Path

from base import DAPAdapter

ZESARUX_DEFAULT_HOST = 'localhost'
ZESARUX_DEFAULT_PORT = 10000
LOG_FILE = '/tmp/zesarux-dap.log'


class ZesaruxAdapter(DAPAdapter):
    def __init__(self):
        super().__init__(LOG_FILE)
        self._load_address = 0x4000
        self._zesarux_host = ZESARUX_DEFAULT_HOST
        self._zesarux_port = ZESARUX_DEFAULT_PORT
        self._sock = None
        self._process = None
        self._setup_done = False
        self._active_breakpoints = set()
        self._stop_on_exit = True

    # ── ZRCP communication ────────────────────────────────────────────────────

    def _is_running(self):
        try:
            s = socket.create_connection((self._zesarux_host, self._zesarux_port), timeout=0.5)
            s.close()
            return True
        except (ConnectionRefusedError, OSError):
            return False

    def _wait_for_zesarux(self, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                s = socket.create_connection((self._zesarux_host, self._zesarux_port), timeout=0.5)
                s.close()
                return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.5)
        raise RuntimeError(f'ZEsarUX did not start within {timeout}s')

    def zesarux_recv(self):
        """Timed recv for regular commands (ZEsarUX is stopped)."""
        self._sock.settimeout(0.1)
        data = b''
        try:
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        finally:
            self._sock.settimeout(None)
        response = data.decode('ascii').strip()
        logging.debug(f'ZRCP <<< {response}')
        return response

    def zesarux_recv_until_prompt(self):
        """Block until ZEsarUX sends the command@ prompt."""
        data = b''
        while b'command@' not in data:
            chunk = self._sock.recv(4096)
            if not chunk:
                break
            data += chunk
        response = data.decode('ascii').strip()
        logging.debug(f'ZRCP <<< {response}')
        return response

    def zesarux_send(self, cmd):
        logging.debug(f'ZRCP >>> {cmd}')
        self._sock.sendall(cmd.encode('ascii') + b'\n')
        return self.zesarux_recv()

    def zesarux_run(self):
        """Send run without reading response — monitor thread handles the stop."""
        logging.debug('ZRCP >>> run')
        self._sock.sendall(b'run\n')

    # ── Registers ────────────────────────────────────────────────────────────

    def read_registers(self):
        logging.debug('ZRCP >>> get-registers')
        self._sock.sendall(b'get-registers\n')
        response = self.zesarux_recv_until_prompt()
        regs = {}
        for match in re.finditer(r"([A-Z][A-Z0-9']*)\s*=\s*([0-9a-fA-F]+)", response):
            regs[match.group(1)] = int(match.group(2), 16)
        return regs

    # ── Stop monitor ──────────────────────────────────────────────────────────

    def _monitor_breakpoint(self, reason):
        response = self.zesarux_recv_until_prompt()
        logging.debug(f'ZEsarUX stopped ({reason}): {response}')
        self.send({
            'type': 'event',
            'event': 'stopped',
            'body': {'reason': reason, 'threadId': 1, 'allThreadsStopped': True},
        })

    def start_monitor(self, reason='breakpoint'):
        t = threading.Thread(target=self._monitor_breakpoint, args=(reason,), daemon=True)
        t.start()

    # ── DAP handlers ──────────────────────────────────────────────────────────

    def handle_launch(self, msg):
        args = msg.get('arguments', {})
        self._zesarux_host = args.get('zesaruxHost', ZESARUX_DEFAULT_HOST)
        self._zesarux_port = int(args.get('zesaruxPort', ZESARUX_DEFAULT_PORT))
        self._stop_on_exit = args.get('stopOnExit', True)
        if 'program' in args:
            self.bin_file = Path(args['program'])
        if 'sldFile' in args:
            self.sld_file = Path(args['sldFile'])
        if 'loadAddress' in args:
            self._load_address = int(str(args['loadAddress']), 0)
        logging.debug(f'bin_file={self.bin_file} sld_file={self.sld_file} load_address=0x{self._load_address:04x}')

        if 'zesaruxArgs' in args:
            if self._is_running():
                logging.debug('ZEsarUX already running, skipping launch')
            else:
                zesarux_bin = args.get('zesaruxPath', 'zesarux')
                launch_cmd = [zesarux_bin, '--noconfigfile', '--enable-remoteprotocol'] + args['zesaruxArgs']
                logging.debug(f'Launching ZEsarUX: {launch_cmd}')
                self._process = subprocess.Popen(launch_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._wait_for_zesarux()

        self._sock = socket.create_connection((self._zesarux_host, self._zesarux_port))
        logging.debug('Connected to ZEsarUX')
        self.zesarux_recv()  # drain welcome banner
        self.zesarux_send('close-all-menus')
        self.zesarux_send('hard-reset-cpu')
        self.zesarux_send('enter-cpu-step')
        self.zesarux_send('set-debug-settings 0')
        self.zesarux_send('clear-membreakpoints')
        # Pre-clear all 100 breakpoint slots — send in bulk then drain once
        bulk = ''.join(f'disable-breakpoint {i}\n' for i in range(1, 101))
        logging.debug('ZRCP >>> disable-breakpoint 1..100 (bulk)')
        self._sock.sendall(bulk.encode('ascii'))
        self.zesarux_recv_until_prompt()
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'launch',
            'success': True,
        })

    def handle_set_breakpoints(self, msg):
        self._source_path = msg['arguments']['source']['path']

        if not self._setup_done:
            if self.bin_file is None or self.sld_file is None:
                source_path = Path(self._source_path)
                project_root = source_path.parent.parent
                name = source_path.stem
                resolved_bin = self.bin_file or project_root / 'build' / f'{name}.bin'
                resolved_sld = self.sld_file or project_root / 'build' / f'{name}.sld'
            else:
                resolved_bin = self.bin_file
                resolved_sld = self.sld_file

            logging.debug(f'ZRCP >>> load-binary {resolved_bin} {self._load_address:x}h 0')
            self._sock.sendall(f'load-binary {resolved_bin} {self._load_address:x}h 0\n'.encode('ascii'))
            self.zesarux_recv_until_prompt()
            logging.debug('ZRCP >>> enable-breakpoints')
            self._sock.sendall(b'enable-breakpoints\n')
            self.zesarux_recv_until_prompt()
            self.sld_map, self.address_to_line = self.parse_sld(resolved_sld)
            self._setup_done = True

        new_indices = set()
        breakpoints = []
        for i, bp in enumerate(msg['arguments'].get('breakpoints', []), start=1):
            line = bp['line']
            valid_line = self.snap_to_valid_line(line)
            if valid_line is not None:
                address = self.sld_map[valid_line]
                self.zesarux_send(f'set-breakpointaction {i}')
                self.zesarux_send(f'set-breakpoint {i} PC={address:x}h')
                self.zesarux_send(f'enable-breakpoint {i}')
                new_indices.add(i)
                breakpoints.append({'verified': True, 'line': valid_line})
                logging.debug(f'Breakpoint line {line} -> snapped to {valid_line} -> {address:#06x}')
            else:
                breakpoints.append({'verified': False, 'line': line})

        for i in self._active_breakpoints - new_indices:
            self.zesarux_send(f'disable-breakpoint {i}')
        self._active_breakpoints = new_indices

        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'setBreakpoints',
            'success': True,
            'body': {'breakpoints': breakpoints},
        })

    def handle_configuration_done(self, msg):
        self.zesarux_send(f'set-register PC={self._load_address:x}h')
        self.start_monitor('breakpoint')
        self.zesarux_run()
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'configurationDone',
            'success': True,
        })

    def handle_read_memory(self, msg):
        args = msg['arguments']
        address = int(args['memoryReference'], 16)
        count = args.get('count', 64)
        address += args.get('offset', 0)

        logging.debug(f'ZRCP >>> read-memory {address:x}h {count}')
        self._sock.sendall(f'read-memory {address:x}h {count}\n'.encode('ascii'))
        parts = self.zesarux_recv_until_prompt().split()
        if not parts:
            self.send({'type': 'response', 'request_seq': msg['seq'], 'command': 'readMemory', 'success': False})
            return
        hex_string = parts[0]
        data = bytes(int(hex_string[i:i+2], 16) for i in range(0, len(hex_string), 2))
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'readMemory',
            'success': True,
            'body': {
                'address': f'0x{address:04x}',
                'data': base64.b64encode(data).decode('ascii'),
            },
        })

    def handle_step(self, msg):
        logging.debug('ZRCP >>> cpu-step')
        self._sock.sendall(b'cpu-step\n')
        self.zesarux_recv_until_prompt()
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': msg['command'],
            'success': True,
        })
        self.send({
            'type': 'event',
            'event': 'stopped',
            'body': {'reason': 'step', 'threadId': 1, 'allThreadsStopped': True},
        })

    def handle_continue(self, msg):
        self.start_monitor('breakpoint')
        self.zesarux_run()
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'continue',
            'success': True,
            'body': {'allThreadsContinued': True},
        })

    def handle_disconnect(self, msg):
        if self._sock:
            for i in self._active_breakpoints:
                self.zesarux_send(f'disable-breakpoint {i}')
            self.zesarux_send('clear-membreakpoints')
            self.zesarux_send('disable-breakpoints')
            self.zesarux_send('exit-cpu-step')
            self._sock.close()
        if self._process and self._stop_on_exit:
            self._process.terminate()
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'disconnect',
            'success': True,
        })
        sys.exit(0)


if __name__ == '__main__':
    ZesaruxAdapter().main()
