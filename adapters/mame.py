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

_REG_NAMES = ['AF', 'BC', 'DE', 'HL', "AF'", "BC'", "DE'", "HL'", 'IX', 'IY', 'SP', 'PC']


class MameAdapter(DAPAdapter):
    def __init__(self):
        super().__init__(LOG_FILE)
        self._sock = None
        self._process = None
        self._active_breakpoints = {}  # (basename, valid_line) -> address
        self.map_file = None
        self.cdb_file = None
        self._load_address_explicit = False
        self._setup_done = False

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
            if not c:
                raise ConnectionError('MAME gdbstub closed the connection (recv returned EOF)')
            if c == b'$':
                break
        data = b''
        while True:
            c = self._sock.recv(1)
            if not c:
                raise ConnectionError('MAME gdbstub closed the connection (recv returned EOF)')
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

    def gdb_handshake(self):
        # MAME won't answer register packets until the client does the standard GDB
        # handshake first: qSupported, then fetching qXfer:features:read:target.xml.
        self.gdb_cmd('qSupported')
        offset = 0
        while True:
            resp = self.gdb_cmd(f'qXfer:features:read:target.xml:{offset:x},1000')
            if not resp or resp[0] not in ('l', 'm'):
                break
            offset += len(resp) - 1
            if resp[0] == 'l':
                break

    def gdb_wait_for_boot(self, seconds):
        # MAME halts the Z80 right at power-on reset (PC=0), before the boot ROM has set up
        # its firmware jumpblock in RAM. Let it boot for a bit, then interrupt with a raw
        # break (0x03) before loading the binary.
        self.gdb_send('c')
        time.sleep(seconds)
        self._sock.sendall(b'\x03')
        self.gdb_recv()

    # ── Registers ────────────────────────────────────────────────────────────

    def read_registers(self):
        regs = {}
        for i, name in enumerate(_REG_NAMES):
            le = self.gdb_cmd(f'p{i:x}')
            if len(le) < 4:
                continue
            regs[name] = (int(le[2:4], 16) << 8) | int(le[0:2], 16)
        return regs

    def write_register(self, name, value):
        # Only the 16-bit pairs in _REG_NAMES are writable this way (no 8-bit halves, no I/R).
        if name not in _REG_NAMES:
            return False
        i = _REG_NAMES.index(name)
        le = f'{value & 0xFF:02x}{(value >> 8) & 0xFF:02x}'
        resp = self.gdb_cmd(f'P{i:x}={le}')
        return resp == 'OK'

    def _set_pc(self, address):
        self.write_register('PC', address)

    # ── Memory helpers ────────────────────────────────────────────────────────

    def read_memory_bytes(self, address, count):
        """DAPAdapter hook used by the Locals/Globals scopes (base.py)."""
        return self._read_memory_gdb(address, count)

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
        if 'mapFile' in args:
            self.map_file = Path(args['mapFile'])
            candidate = self.map_file.with_suffix('.cdb')
            if candidate.exists():
                self.cdb_file = candidate
        if 'cdbFile' in args:
            self.cdb_file = Path(args['cdbFile'])
        if 'loadAddress' in args:
            self._load_address = int(str(args['loadAddress']), 0)
            self._load_address_explicit = True

        if 'mameArgs' in args:
            mame_bin = args.get('mamePath', 'mame')
            launch_cmd = [mame_bin] + args['mameArgs'] + [
                '-debugger', 'gdbstub',
                '-debug',
                '-debugger_port', str(port),
            ]
            logging.debug(f'Launching MAME: {launch_cmd}')
            self._process = subprocess.Popen(launch_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        boot_delay = float(args.get('bootDelay', 2.0))

        self._sock = self._connect_to_mame(port)
        logging.debug('Connected to MAME gdbstub')
        self.gdb_handshake()
        if boot_delay > 0:
            logging.debug(f'Letting the machine boot for {boot_delay}s before loading the binary')
            self.gdb_wait_for_boot(boot_delay)
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'launch',
            'success': True,
        })

    def _connect_to_mame(self, port, timeout=15):
        # Retries the actual connection directly, no throwaway probe first -- MAME's gdbstub
        # only ever accepts one client. Drops back to a blocking socket once connected, so a
        # slow-to-hit breakpoint won't time out _monitor_stop's gdb_recv() later.
        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            try:
                sock = socket.create_connection((MAME_HOST, port), timeout=0.5)
                sock.settimeout(None)
                return sock
            except (ConnectionRefusedError, OSError) as e:
                last_err = e
                time.sleep(0.5)
        raise RuntimeError(f'MAME did not start within {timeout}s') from last_err

    def _load_binary(self, source_path=None):
        # Same idea as zesarux.py's _load_binary, but loading here is a raw GDB memory write
        # -- no separate "enable breakpoints" step, since GDB breakpoints go live as soon as
        # they're set. Safe to call more than once (self._setup_done guards it); both
        # handle_set_breakpoints and handle_configuration_done call it, the latter just as
        # a safety net in case a DAP client skips setBreakpoints.
        if self._setup_done:
            return
        has_debug = self.sld_file is not None or self.map_file is not None
        if self.bin_file is None or not has_debug:
            if source_path is None:
                logging.error('No source path available to resolve bin/sld files')
                return
            sp = Path(source_path)
            project_root = sp.parent.parent
            name = sp.stem
            resolved_bin = self.bin_file or project_root / 'build' / f'{name}.bin'
            resolved_sld = self.sld_file or project_root / 'build' / f'{name}.sld'
            resolved_map = self.map_file
        else:
            resolved_bin = self.bin_file
            resolved_sld = self.sld_file
            resolved_map = self.map_file

        if resolved_map is not None:
            self.sld_map, self.address_to_source, map_load_address = self.parse_map(resolved_map)
        elif resolved_sld is not None:
            self.sld_map, self.address_to_source = self.parse_sld(resolved_sld)
            map_load_address = None
        else:
            map_load_address = None

        if not self._load_address_explicit:
            ihx_load_address = self.parse_ihx_load_address(resolved_bin.with_suffix('.ihx'))
            if ihx_load_address is not None:
                self._load_address = ihx_load_address
                logging.debug(f'Load address from .ihx: 0x{self._load_address:04x}')
            elif map_load_address is not None:
                self._load_address = map_load_address
                logging.debug(f'Load address from map file s__CODE: 0x{self._load_address:04x}')

        if self.cdb_file is not None:
            cdb_lines, cdb_addrs, self.functions, self.local_vars = self.parse_cdb(self.cdb_file)
            self.sld_map.update(cdb_lines)
            self.address_to_source.update(cdb_addrs)
            logging.debug(f'CDB: {len(self.functions)} functions, {sum(len(v) for v in self.local_vars.values())} locals')

        if resolved_map is not None:
            asm_lines = self.build_asm_line_map(resolved_map)
            added = 0
            for addr, entry in asm_lines.items():
                if addr not in self.address_to_source:
                    self.address_to_source[addr] = entry
                    added += 1
            logging.debug(f'LST: filled {added} addr->line gaps from module listings')

        logging.debug(f'Loading binary {resolved_bin} at 0x{self._load_address:04x}')
        self._write_memory_gdb(self._load_address, resolved_bin.read_bytes())
        self._setup_done = True

    def handle_set_breakpoints(self, msg):
        self._source_path = msg['arguments']['source']['path']
        self._register_source_path(self._source_path)
        self._load_binary(self._source_path)

        for addr in self._active_breakpoints.values():
            resp = self.gdb_cmd(f'z0,{addr:x},1')
            logging.debug(f'Removed BP at {addr:#x}: {resp}')
        self._active_breakpoints = {}

        basename = Path(self._source_path).name
        breakpoints = []
        for bp in msg['arguments'].get('breakpoints', []):
            line = bp['line']
            valid_line = self.snap_to_valid_line(basename, line)
            if valid_line is not None:
                address = self.sld_map[(basename, valid_line)]
                resp = self.gdb_cmd(f'Z0,{address:x},1')
                if resp == 'OK':
                    self._active_breakpoints[(basename, valid_line)] = address
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

    def _step_over_breakpoint_at(self, pc):
        # A plain `c` just hangs if PC is already sitting on a breakpoint. Only used from
        # handle_continue -- not on entry, where landing on a breakpoint (e.g. main()'s first
        # line) should show up as a real hit instead of getting silently skipped.
        if pc in self._active_breakpoints.values():
            logging.debug(f'PC {pc:#06x} is an active breakpoint -- single-stepping over it before continuing')
            self.gdb_cmd('s')

    def handle_configuration_done(self, msg):
        self._load_binary(getattr(self, '_source_path', None))
        # Jump straight to main() when CDB info identifies it, same as zesarux.py.
        entry = next((start for name, start, end in self.functions if name == 'main'), self._load_address)
        self._set_pc(entry)

        landed_on_breakpoint = entry in self._active_breakpoints.values()
        if not landed_on_breakpoint:
            self.gdb_send('c')
            self.start_monitor('entry')
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'configurationDone',
            'success': True,
        })
        if landed_on_breakpoint:
            # See _step_over_breakpoint_at -- already sitting exactly where the breakpoint fires.
            self.send({
                'type': 'event',
                'event': 'stopped',
                'body': {'reason': 'breakpoint', 'threadId': 1, 'allThreadsStopped': True},
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
        pc = self.read_registers().get('PC')
        if pc is not None:
            self._step_over_breakpoint_at(pc)
        self.gdb_send('c')
        self.start_monitor('breakpoint')
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'continue',
            'success': True,
            'body': {'allThreadsContinued': True},
        })

    def _terminate_spawned_process(self):
        if not self._process:
            return
        try:
            self._process.terminate()
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            logging.debug('MAME ignored SIGTERM (likely paused at a debugger break) -- sending SIGKILL')
            try:
                self._process.kill()
                self._process.wait(timeout=2)
            except Exception:
                pass
        except Exception:
            pass

    def cleanup_on_crash(self):
        self._terminate_spawned_process()

    def handle_disconnect(self, msg):
        if self._sock:
            try:
                if self._process:
                    self.gdb_cmd('k')  # tell MAME to exit
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
        self._terminate_spawned_process()
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'disconnect',
            'success': True,
        })
        sys.exit(0)


if __name__ == '__main__':
    MameAdapter().main()
