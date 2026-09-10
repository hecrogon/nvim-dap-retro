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
ZESARUX_RECV_TIMEOUT = 15.0

LOG_FILE = '/tmp/zesarux-dap.log'


class ZesaruxAdapter(DAPAdapter):
    def __init__(self):
        super().__init__(LOG_FILE)
        self._load_address = 0x4000
        self._load_address_explicit = False
        self._zesarux_host = ZESARUX_DEFAULT_HOST
        self._zesarux_port = ZESARUX_DEFAULT_PORT
        self._sock = None
        self._process = None
        self._setup_done = False
        self._machine_name = ''
        self._io_port_sections = {}  # {variablesReference: [child vars]}, see extra_scope_variables
        self._active_breakpoints = set()
        self._stop_on_exit = True
        self.map_file = None
        self.cdb_file = None

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

    @staticmethod
    def _find_free_port():
        """Ask the OS for a free local TCP port by binding to port 0, then
        release it immediately so ZEsarUX can bind it instead. There's a
        small window between closing this socket and ZEsarUX claiming the
        same port (TOCTOU), but for local single-user debugging sessions
        that race is negligible.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            return s.getsockname()[1]

    def _launch_zesarux(self, args):
        """Spawn a ZEsarUX process listening on self._zesarux_port.

        --remoteprotocol-port is appended *after* the user's own
        zesaruxArgs, so it wins if they happened to also set one there --
        keeping this adapter's own idea of the port authoritative, since
        it's what handle_launch is about to connect self._sock to.
        """
        zesarux_bin = args.get('zesaruxPath', 'zesarux')
        launch_cmd = [
            zesarux_bin, '--noconfigfile', '--enable-remoteprotocol',
            '--disable-debug-console-win', '--disable-all-first-aid', '--disable-restore-windows',
        ] + args['zesaruxArgs'] + ['--remoteprotocol-port', str(self._zesarux_port)]
        logging.debug(f'Launching ZEsarUX: {launch_cmd}')
        self._process = subprocess.Popen(launch_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait_for_zesarux()

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

    def zesarux_recv_until_prompt(self, timeout=ZESARUX_RECV_TIMEOUT):
        """Block until ZEsarUX sends a command prompt (command@ or command>).

        Bounded by `timeout` (seconds), except when a caller explicitly
        passes None -- used only by the continue/run monitor thread, where
        blocking as long as the debuggee runs before hitting a breakpoint
        (could legitimately be minutes) is the whole point. Every other
        caller wants a fast round-trip; if the connection is dead or
        ZEsarUX has stalled, hitting this bound raises instead of hanging
        the process forever with no feedback.
        """
        data = b''
        if timeout is not None:
            self._sock.settimeout(timeout)
        try:
            while b'command@' not in data and b'command>' not in data:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            raise RuntimeError(
                f'ZEsarUX did not respond within {timeout}s -- it may have '
                'crashed, been closed, or the connection stalled'
            ) from None
        finally:
            if timeout is not None:
                self._sock.settimeout(None)
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

    def write_register(self, name, value):
        """`set-register NAME=VALUEh` -- accepts PC/SP/IX/IY/AF/BC/DE/HL
        (+ their ' shadows), the 8-bit halves, and I/R/IFF1/IFF2. MEMPTR
        and MMU show up in the Registers scope (read_registers just
        regex-matches whatever get-registers reports) but aren't real
        settable registers -- ZEsarUX replies "Error changing register"
        for those, reported back here as a normal write failure.
        """
        logging.debug(f'ZRCP >>> set-register {name}={value:x}h')
        self._sock.sendall(f'set-register {name}={value:x}h\n'.encode('ascii'))
        response = self.zesarux_recv_until_prompt()
        return 'error' not in response.lower()

    def read_memory_bytes(self, address, count):
        self._sock.sendall(f'read-memory {address:x}h {count}\n'.encode('ascii'))
        parts = self.zesarux_recv_until_prompt().split()
        if not parts:
            return None
        try:
            hex_str = parts[0]
            return bytes(int(hex_str[i:i+2], 16) for i in range(0, len(hex_str), 2))
        except (ValueError, IndexError):
            return None

    def close_all_menus(self):
        """close-all-menus can take longer than zesarux_send's 0.1s budget
        to reply -- e.g. dismissing ZEsarUX's native Debug popup, which is
        slow the first time it's shown. Read reliably here so a late reply
        can't sit unread in the socket and get consumed by whatever ZRCP
        command runs next (a following read-memory or get-registers call).
        """
        self._sock.sendall(b'close-all-menus\n')
        self.zesarux_recv_until_prompt()

    def enter_cpu_step(self):
        """enter-cpu-step can take ZEsarUX up to ~3s internally (waiting
        out any menu still closing, then its own acknowledgement) -- far
        past zesarux_send's 0.1s budget, so use zesarux_recv_until_prompt
        instead.

        A stuck menu left over from a previous ZEsarUX process (e.g. one
        this adapter attached to instead of spawning fresh) is the usual
        cause of an "Error. Can not enter cpu step mode" reply; one extra
        close-all-menus + retry clears it. If it still won't take, raise
        rather than silently reporting launch success -- the debuggee
        would never actually run.
        """
        self._sock.sendall(b'enter-cpu-step\n')
        response = self.zesarux_recv_until_prompt()
        if 'error' in response.lower():
            logging.debug(f'enter-cpu-step failed ({response!r}) -- retrying after close-all-menus')
            self.close_all_menus()
            self._sock.sendall(b'enter-cpu-step\n')
            response = self.zesarux_recv_until_prompt()
        if 'error' in response.lower():
            raise RuntimeError(
                f'ZEsarUX would not enter cpu-step mode ({response!r}) -- it likely '
                'has a stuck menu/dialog open from a previous session. Close that '
                'ZEsarUX window (or kill the process) and relaunch the debug session'
            )

    # ── Stop monitor ──────────────────────────────────────────────────────────

    def _monitor_breakpoint(self, reason):
        response = self.zesarux_recv_until_prompt(timeout=None)
        logging.debug(f'ZEsarUX stopped ({reason}): {response}')
        if 'must first enter cpu-step mode' in response:
            logging.debug('run rejected -- ZEsarUX left cpu-step mode; reporting program exit')
            self.send({'type': 'event', 'event': 'exited', 'body': {'exitCode': 0}})
            self.send({'type': 'event', 'event': 'terminated'})
            return

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
        logging.debug(f'bin_file={self.bin_file} sld_file={self.sld_file} map_file={self.map_file} cdb_file={self.cdb_file} load_address=0x{self._load_address:04x}')

        if 'zesaruxArgs' in args:
            if 'zesaruxPort' in args:
                if self._is_running():
                    logging.debug('ZEsarUX already running, skipping launch')
                else:
                    self._launch_zesarux(args)
            else:
                self._zesarux_port = self._find_free_port()
                self._launch_zesarux(args)

        self._sock = socket.create_connection((self._zesarux_host, self._zesarux_port))
        logging.info(
            f'Connected to ZEsarUX ZRCP at {self._zesarux_host}:{self._zesarux_port} '
            f'-- telnet {self._zesarux_host} {self._zesarux_port} to poke it manually'
        )

        self.zesarux_recv()  # drain welcome banner
        self._machine_name = self.zesarux_send('get-current-machine').splitlines()[0].strip().lower()
        logging.debug(f'ZEsarUX machine: {self._machine_name!r}')

        self.close_all_menus()
        self.zesarux_send('hard-reset-cpu')
        self.enter_cpu_step()
        self.zesarux_send('set-debug-settings 0')
        self.zesarux_send('clear-membreakpoints')

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

    def _load_binary(self, source_path=None):
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
            # .ihx (if present) is authoritative -- it's what hex2bin itself
            # uses, so it's correct even when the lowest area in the image is
            # an (ABS) area sdld reports as address 0 everywhere in the .map.
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

        logging.debug(f'ZRCP >>> load-binary {resolved_bin} {self._load_address:x}h 0')
        self._sock.sendall(f'load-binary {resolved_bin} {self._load_address:x}h 0\n'.encode('ascii'))
        self.zesarux_recv_until_prompt()
        logging.debug('ZRCP >>> enable-breakpoints')
        self._sock.sendall(b'enable-breakpoints\n')
        self.zesarux_recv_until_prompt()
        self._setup_done = True

    def handle_set_breakpoints(self, msg):
        self._source_path = msg['arguments']['source']['path']
        self._register_source_path(self._source_path)
        self._load_binary(self._source_path)

        basename = Path(self._source_path).name
        new_indices = set()
        breakpoints = []
        for i, bp in enumerate(msg['arguments'].get('breakpoints', []), start=1):
            line = bp['line']
            valid_line = self.snap_to_valid_line(basename, line)
            if valid_line is not None:
                address = self.sld_map[(basename, valid_line)]
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
        self._load_binary(getattr(self, '_source_path', None))
        entry = next((start for name, start, end in self.functions if name == 'main'), self._load_address)
        self.zesarux_send(f'set-register PC={entry:x}h')
        # See _monitor_breakpoint: no close_all_menus() here -- it drops
        # cpu-step mode as a side effect and can permanently wedge ZEsarUX.
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'configurationDone',
            'success': True,
        })
        self.send({
            'type': 'event',
            'event': 'stopped',
            'body': {'reason': 'entry', 'threadId': 1, 'allThreadsStopped': True},
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

    def handle_write_memory(self, msg):
        args = msg['arguments']
        address = int(args['memoryReference'], 16)
        address += args.get('offset', 0)
        data = base64.b64decode(args['data'])
        hex_data = data.hex().upper()

        logging.debug(f'ZRCP >>> write-memory-raw {address:x}h {hex_data}')
        self._sock.sendall(f'write-memory-raw {address:x}h {hex_data}\n'.encode('ascii'))
        self.zesarux_recv_until_prompt()
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'writeMemory',
            'success': True,
            'body': {'bytesWritten': len(data)},
        })

    # ── Evaluate (debug console) ─────────────────────────────────────────────

    def handle_evaluate(self, msg):
        expr = msg['arguments'].get('expression', '').strip()
        low = expr.lower()

        if low in ('tstates reset', 'tstate reset', 'treset'):
            self.zesarux_send('reset-tstates-partial')
            result = 'T-state counter reset'
        elif low in ('tstates', 'tstate'):
            self._sock.sendall(b'get-tstates-partial\n')
            result = self._format_tstates(self.zesarux_recv_until_prompt())
        elif low.startswith('zrcp '):
            result = self._zrcp_passthrough(expr[len('zrcp '):].strip())
        else:
            result = (f'Unknown expression: {expr!r} -- try "tstates", '
                       f'"tstates reset", or "zrcp <command>"')

        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'evaluate',
            'success': True,
            'body': {'result': result, 'variablesReference': 0},
        })

    @staticmethod
    def _format_tstates(response):
        if 'OVERFLOW' in response.upper():
            return 'T-state counter overflowed -- reset it and measure a shorter region'
        match = re.search(r'(\d+)', response)
        if not match:
            return f'(unparseable get-tstates-partial response: {response!r})'
        t = int(match.group(1))
        return f'{t} T-states = {t / 4:.1f} us ({t / 4000:.4f} ms @ 4MHz)'

    def _zrcp_passthrough(self, raw_cmd):
        """Send an arbitrary ZRCP command and return its raw response.

        Bounded, unlike zesarux_recv_until_prompt: a command like `run` with
        no breakpoint set never produces a prompt on its own, and the normal
        unbounded wait would hang this handler -- and with it, every future
        DAP request, since read_message()/handle() are not re-entrant.
        """
        logging.debug(f'ZRCP >>> {raw_cmd} (evaluate passthrough)')
        self._sock.sendall(raw_cmd.encode('ascii') + b'\n')
        self._sock.settimeout(5.0)
        data = b''
        try:
            while b'command@' not in data and b'command>' not in data:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            data += b'\n(timed out after 5s waiting for a ZRCP prompt -- ' \
                    b'this command may need a breakpoint to return, e.g. `run`)'
        finally:
            self._sock.settimeout(None)
        response = data.decode('ascii', errors='replace').strip()
        logging.debug(f'ZRCP <<< {response}')
        return response

    # ── I/O ports scope ───────────────────────────────────────────────────────

    _IO_PORTS_SCOPE_REF = 4
    _IO_PORTS_SECTION_REF_BASE = 1000

    def extra_scopes(self):
        return [{'name': 'I/O Ports', 'variablesReference': self._IO_PORTS_SCOPE_REF, 'expensive': False}]

    def extra_scope_variables(self, ref):
        if ref == self._IO_PORTS_SCOPE_REF:
            self._sock.sendall(b'get-io-ports\n')
            top_level, sections = self._parse_io_ports(self.zesarux_recv_until_prompt())
            self._io_port_sections = {}
            variables = list(top_level)
            for i, (name, children) in enumerate(sections.items()):
                section_ref = self._IO_PORTS_SECTION_REF_BASE + i
                self._io_port_sections[section_ref] = children
                variables.append({
                    'name': name,
                    'value': f'{len(children)} entries',
                    'type': 'io-port-section',
                    'variablesReference': section_ref,
                })
            return variables
        return self._io_port_sections.get(ref, [])

    @staticmethod
    def _parse_io_ports(response):
        """Parse a get-io-ports response into (top_level, sections) for a
        grouped/expandable DAP variables tree, generically -- no section
        names hardcoded, just three line shapes recognized by structure:
          - "Some Section:"   (colon, nothing after it) -- a section header,
            e.g. "CRTC Registers:", "AY-3-8912 chip 0:". Opens a new group
            in `sections`; the caller turns it into a tree node pointing
            at its children.
          - "NN: NN"          (2 hex digits, colon, 2 hex digits) -- one
            entry of an indexed register table, e.g. CRTC/Gate Array's
            "00:  3F" rows. Named bare "RNN" since each table is its own
            group and every table restarts at 00 regardless.
          - "Some Key: value" (colon, something after it) -- a plain field,
            e.g. "ULA Data Bus value: FFH", "Motor: Off".
        Anything else (blank lines, decorative lines with no colon) is
        skipped.

        Returns (top_level_vars, {section_name: [child_vars]}), both in
        the order they first appeared.
        """
        top_level = []
        sections = {}
        section = None
        saw_register_in_section = False

        for raw in response.splitlines():
            line = raw.strip()
            if not line:
                continue

            reg_match = re.match(r'^([0-9A-Fa-f]{2}):\s+(\S+)$', line)
            if reg_match and re.fullmatch(r'[0-9A-Fa-f]{1,2}', reg_match.group(2)):
                reg_num, value = reg_match.groups()
                target = sections[section] if section else top_level
                target.append({
                    'name': f'R{reg_num}', 'value': f'0x{value}', 'type': 'register', 'variablesReference': 0,
                })
                saw_register_in_section = True
                continue

            if line.endswith(':'):
                section = line[:-1].strip()
                sections[section] = []
                saw_register_in_section = False
                continue

            if ':' in line:
                if saw_register_in_section:
                    section = None
                    saw_register_in_section = False
                key, _, value = line.partition(':')
                key, value = key.strip(), value.strip()
                target = sections[section] if section else top_level
                target.append({
                    'name': key, 'value': value, 'type': 'io-port', 'variablesReference': 0,
                })
        return top_level, sections

    def handle_step(self, msg):
        logging.debug('ZRCP >>> cpu-step')
        self._sock.sendall(b'cpu-step\n')
        self.zesarux_recv_until_prompt()
        # See _monitor_breakpoint -- no close_all_menus() here.
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

    def _terminate_spawned_process(self):
        """Kill the ZEsarUX process this adapter launched, if any. Shared by
        the normal handle_disconnect path and cleanup_on_crash (see base.py)
        -- a crash must not leave a spawned emulator process orphaned just
        because it skipped the normal disconnect sequence.
        """
        if self._process and self._stop_on_exit:
            logging.debug(f'Terminating spawned ZEsarUX process (pid {self._process.pid})')
            self._process.terminate()

    def cleanup_on_crash(self):
        self._terminate_spawned_process()

    def handle_disconnect(self, msg):
        if self._sock:
            for i in self._active_breakpoints:
                self.zesarux_send(f'disable-breakpoint {i}')
            self.zesarux_send('clear-membreakpoints')
            self.zesarux_send('disable-breakpoints')
            self.zesarux_send('exit-cpu-step')
            self._sock.close()
        self._terminate_spawned_process()
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'disconnect',
            'success': True,
        })
        sys.exit(0)


if __name__ == '__main__':
    ZesaruxAdapter().main()
