import sys
import logging
import socket
import subprocess
import time
import threading
import base64
from pathlib import Path

from base import DAPAdapter

MAME_HOST = 'localhost'
MAME_DEFAULT_PORT = 2159
LOG_FILE = '/tmp/mame-dap.log'

# Z80 register order in MAME gdbstub g/G response.
# Each register is 16-bit little-endian → 4 hex chars.
_REG_NAMES = ['AF', 'BC', 'DE', 'HL', 'IX', 'IY', 'SP', 'PC', "AF'", "BC'", "DE'", "HL'"]


class MameAdapter(DAPAdapter):
    def __init__(self):
        super().__init__(LOG_FILE)
        self._sock = None
        self._process = None
        self._active_breakpoints = {}  # valid_line -> address

    # ── GDB RSP ───────────────────────────────────────────────────────────────

    def _gdb_checksum(self, data):
        return f'{sum(ord(c) for c in data) & 0xFF:02x}'

    def gdb_send(self, cmd):
        pkt = f'${cmd}#{self._gdb_checksum(cmd)}'.encode('ascii')
        logging.debug(f'GDB >>> {pkt}')
        self._sock.sendall(pkt)
        ack = self._sock.recv(1)
        logging.debug(f'GDB ACK: {ack}')

    def gdb_recv(self):
        while True:
            c = self._sock.recv(1)
            if c == b'$':
                break
        data = b''
        while True:
            c = self._sock.recv(1)
            if c == b'#':
                break
            data += c
        self._sock.recv(2)       # checksum (ignored)
        self._sock.sendall(b'+') # ACK
        result = data.decode('ascii')
        logging.debug(f'GDB <<< {result}')
        return result

    def gdb_cmd(self, cmd):
        self.gdb_send(cmd)
        return self.gdb_recv()

    # ── Registers ────────────────────────────────────────────────────────────

    def read_registers(self):
        hex_str = self.gdb_cmd('g')
        regs = {}
        for i, name in enumerate(_REG_NAMES):
            offset = i * 4
            if offset + 4 > len(hex_str):
                break
            le = hex_str[offset:offset + 4]
            regs[name] = (int(le[2:4], 16) << 8) | int(le[0:2], 16)
        return regs

    def _write_registers(self, regs):
        parts = []
        for name in _REG_NAMES:
            v = regs.get(name, 0)
            parts.append(f'{v & 0xFF:02x}{(v >> 8) & 0xFF:02x}')
        resp = self.gdb_cmd('G' + ''.join(parts))
        logging.debug(f'write_registers response: {resp}')

    def _set_pc(self, address):
        regs = self.read_registers()
        regs['PC'] = address
        self._write_registers(regs)

    # ── Memory helpers ────────────────────────────────────────────────────────

    def _read_memory_gdb(self, address, count):
        resp = self.gdb_cmd(f'm{address:x},{count:x}')
        if resp.startswith('E') or not resp:
            return b'\x00' * count
        return bytes(int(resp[i:i + 2], 16) for i in range(0, len(resp), 2))

    def _write_memory_gdb(self, address, data, chunk=256):
        for offset in range(0, len(data), chunk):
            blob = data[offset:offset + chunk]
            addr = address + offset
            resp = self.gdb_cmd(f'M{addr:x},{len(blob):x}:{blob.hex()}')
            if resp != 'OK':
                logging.warning(f'write_memory at {addr:#x}: {resp}')

    # ── Stop monitor ──────────────────────────────────────────────────────────

    def _monitor_stop(self, reason):
        resp = self.gdb_recv()
        logging.debug(f'MAME stopped ({reason}): {resp}')
        self.send({
            'type': 'event',
            'event': 'stopped',
            'body': {'reason': reason, 'threadId': 1, 'allThreadsStopped': True},
        })

    def start_monitor(self, reason='breakpoint'):
        t = threading.Thread(target=self._monitor_stop, args=(reason,), daemon=True)
        t.start()

    # ── DAP handlers ──────────────────────────────────────────────────────────

    def handle_launch(self, msg):
        args = msg.get('arguments', {})
        port = int(args.get('mamePort', MAME_DEFAULT_PORT))
        if 'program' in args:
            self.bin_file = Path(args['program'])
        if 'sldFile' in args:
            self.sld_file = Path(args['sldFile'])
        if 'loadAddress' in args:
            self._load_address = int(str(args['loadAddress']), 0)

        if 'mameArgs' in args:
            mame_bin = args.get('mamePath', 'mame')
            launch_cmd = [mame_bin] + args['mameArgs'] + [
                '-debugger', 'gdbstub',
                '-debug',
                '-debugger_port', str(port),
            ]
            logging.debug(f'Launching MAME: {launch_cmd}')
            self._process = subprocess.Popen(launch_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._wait_for_mame(port)

        self._sock = socket.create_connection((MAME_HOST, port))
        logging.debug('Connected to MAME gdbstub')
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'launch',
            'success': True,
        })

    def _wait_for_mame(self, port, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                s = socket.create_connection((MAME_HOST, port), timeout=0.5)
                s.close()
                return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.5)
        raise RuntimeError(f'MAME did not start within {timeout}s')

    def handle_set_breakpoints(self, msg):
        self._source_path = msg['arguments']['source']['path']

        if self.sld_file is None and self._source_path:
            source_path = Path(self._source_path)
            project_root = source_path.parent.parent
            self.sld_file = project_root / 'build' / f'{source_path.stem}.sld'

        if self.sld_file and self.sld_file.exists() and not self.sld_map:
            self.sld_map, self.address_to_line = self.parse_sld(self.sld_file)

        for addr in self._active_breakpoints.values():
            resp = self.gdb_cmd(f'z0,{addr:x},1')
            logging.debug(f'Removed BP at {addr:#x}: {resp}')
        self._active_breakpoints = {}

        breakpoints = []
        for bp in msg['arguments'].get('breakpoints', []):
            line = bp['line']
            valid_line = self.snap_to_valid_line(line)
            if valid_line is not None:
                address = self.sld_map[valid_line]
                resp = self.gdb_cmd(f'Z0,{address:x},1')
                if resp == 'OK':
                    self._active_breakpoints[valid_line] = address
                    breakpoints.append({'verified': True, 'line': valid_line})
                    logging.debug(f'BP line {line} -> {valid_line} -> {address:#x}')
                else:
                    breakpoints.append({'verified': False, 'line': line})
            else:
                breakpoints.append({'verified': False, 'line': line})

        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'setBreakpoints',
            'success': True,
            'body': {'breakpoints': breakpoints},
        })

    def handle_configuration_done(self, msg):
        if self.bin_file and self.bin_file.exists():
            logging.debug(f'Loading binary {self.bin_file} at {self._load_address:#x}')
            self._write_memory_gdb(self._load_address, self.bin_file.read_bytes())
            self._set_pc(self._load_address)

        if self.sld_file and self.sld_file.exists() and not self.sld_map:
            self.sld_map, self.address_to_line = self.parse_sld(self.sld_file)

        self.gdb_send('c')
        self.start_monitor('entry')
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
        data = self._read_memory_gdb(address, count)
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
        self.gdb_send('s')
        resp = self.gdb_recv()
        logging.debug(f'Step stop: {resp}')
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
        self.gdb_send('c')
        self.start_monitor('breakpoint')
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'continue',
            'success': True,
            'body': {'allThreadsContinued': True},
        })

    def handle_disconnect(self, msg):
        if self._sock:
            try:
                if self._process:
                    self.gdb_cmd('k')  # tell MAME to exit (can't reconnect anyway)
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
        if self._process:
            try:
                self._process.terminate()
            except Exception:
                pass
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'disconnect',
            'success': True,
        })
        sys.exit(0)


if __name__ == '__main__':
    MameAdapter().main()
