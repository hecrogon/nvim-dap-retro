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
        self.sld_map = {}           # {(basename, lineno): address}
        self.address_to_source = {} # {address: (basename, lineno)}
        self._path_cache = {}       # {basename: full_path}
        self._source_path = None
        self._load_address = 0
        self.bin_file = None
        self.sld_file = None
        self.functions = []     # [(name, start_addr, end_addr)]
        self.local_vars = {}    # {funcname: [{name, storage, register|sp_offset}]}
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
            'writeMemory':       self.handle_write_memory,
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

    # ── SLD parsing (sjasmplus) ───────────────────────────────────────────────

    def parse_sld(self, sld_path):
        """Parse sjasmplus SLD file.

        SLD record format: filepath|line|col|page|address|...|T
        The filepath in parts[0] is the full (absolute) path — we use its
        basename as the key so it matches the same scheme as .map/.cdb,
        and cache the full path for later source resolution.
        """
        line_to_addr = {}
        addr_to_source = {}
        with open(sld_path) as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) >= 7 and parts[6] == 'T':
                    try:
                        full_path = parts[0]
                        basename  = Path(full_path).name
                        line_num  = int(parts[1])
                        address   = int(parts[5])
                        key = (basename, line_num)
                        line_to_addr[key] = address
                        addr_to_source[address] = key
                        if full_path:
                            self._path_cache[basename] = full_path
                    except ValueError:
                        pass
        return line_to_addr, addr_to_source

    # ── MAP parsing (SDCC) ────────────────────────────────────────────────────

    def parse_map(self, map_path):
        """Parse SDCC linker .map file for C-level line → address mapping.

        With --debug, SDCC embeds C$ records in the map file:
            00004000  C$main.c$3$0_0$79    main
        Format: C$<filename>$<lineno>$<level>_<block>$<col>

        Also extracts the load address from the s__CODE symbol:
            00004000  s__CODE

        Returns (line_to_addr, addr_to_line, load_address).
        load_address is None if s__CODE is not found.
        """
        line_to_addr = {}
        addr_to_line = {}
        load_address = None
        with open(map_path) as f:
            for raw in f:
                parts = raw.split()
                if len(parts) < 2:
                    continue
                try:
                    addr = int(parts[0], 16)
                except ValueError:
                    continue
                if parts[1] == 's__CODE' and load_address is None:
                    load_address = addr
                elif parts[1].startswith('C$'):
                    try:
                        fields   = parts[1].split('$')
                        basename = fields[1]            # e.g. 'main.c'
                        line_num = int(fields[2])
                        key = (basename, line_num)
                        if key not in line_to_addr:
                            line_to_addr[key] = addr
                        if addr not in addr_to_line:
                            addr_to_line[addr] = key
                    except (ValueError, IndexError):
                        pass
        return line_to_addr, addr_to_line, load_address

    # ── CDB parsing (SDCC) ────────────────────────────────────────────────────

    @staticmethod
    def _cdb_parse_c_line(raw, line_to_addr, addr_to_line):
        """L:C$main.c$5$1_0$80:4000  →  ('main.c', 5) = 0x4000"""
        try:
            sym, addr_str = raw[4:].rsplit(':', 1)
            addr     = int(addr_str, 16)
            parts    = sym.split('$')
            basename = parts[0]   # e.g. 'main.c'
            lineno   = int(parts[1])
            key = (basename, lineno)
            if key not in line_to_addr:
                line_to_addr[key] = addr
            if addr not in addr_to_line:
                addr_to_line[addr] = key
        except (ValueError, IndexError):
            pass

    @staticmethod
    def _cdb_parse_func_start(raw, func_starts):
        """L:G$main$0$0:4000  →  main starts at 0x4000"""
        try:
            sym, addr_str = raw[4:].rsplit(':', 1)
            func_starts[sym.split('$')[0]] = int(addr_str, 16)
        except (ValueError, IndexError):
            pass

    @staticmethod
    def _cdb_parse_func_end(raw, func_ends):
        """L:XG$main$0$0:403C  →  main ends at 0x403C"""
        try:
            sym, addr_str = raw[5:].rsplit(':', 1)
            func_ends[sym.split('$')[0]] = int(addr_str, 16)
        except (ValueError, IndexError):
            pass

    @staticmethod
    def _cdb_parse_local(raw, local_vars):
        """S:Lmain.main$result$1_0$80({1}SC:U),R,0,0,[l]
           S:Lmain.sumUpTo$n$1_0$82({1}SC:U),B,1,4"""
        try:
            rest      = raw[3:]                                   # main.main$result...
            after_dot = rest[rest.index('.') + 1:]               # main$result...
            funcname  = after_dot[:after_dot.index('$')]          # main
            after_func = after_dot[after_dot.index('$') + 1:]    # result$1_0$80...
            varname   = after_func[:after_func.index('$')]        # result

            paren_end  = rest.rindex(')')
            fields     = rest[paren_end + 1:].lstrip(',').split(',')
            storage    = fields[0] if fields else ''

            var_info = {'name': varname, 'storage': storage}
            if storage == 'R' and len(fields) >= 4:
                reg = fields[3].strip()
                if reg.startswith('[') and reg.endswith(']'):
                    var_info['register'] = reg[1:-1]
            elif storage == 'B' and len(fields) >= 3:
                var_info['sp_offset'] = int(fields[2])

            local_vars.setdefault(funcname, []).append(var_info)
        except (ValueError, IndexError):
            pass

    def parse_cdb(self, cdb_path):
        """Parse SDCC .cdb file → (line_to_addr, addr_to_line, functions, local_vars).

        functions  — [(name, start_addr, end_addr)] sorted by start_addr
        local_vars — {funcname: [{name, storage, register|sp_offset}]}
        """
        line_to_addr = {}
        addr_to_line = {}
        func_starts  = {}
        func_ends    = {}
        local_vars   = {}

        with open(cdb_path) as f:
            for raw in f:
                line = raw.strip()
                if line.startswith('L:C$'):
                    self._cdb_parse_c_line(line, line_to_addr, addr_to_line)
                elif line.startswith('L:XG$'):
                    self._cdb_parse_func_end(line, func_ends)
                elif line.startswith('L:G$'):
                    self._cdb_parse_func_start(line, func_starts)
                elif line.startswith('S:L'):
                    self._cdb_parse_local(line, local_vars)

        functions = sorted(
            [(n, func_starts[n], func_ends[n]) for n in func_starts if n in func_ends],
            key=lambda x: x[1],
        )
        return line_to_addr, addr_to_line, functions, local_vars

    # ── Register helpers ─────────────────────────────────────────────────────

    def read_memory_bytes(self, address: int, count: int):
        """Read raw bytes from emulator memory. Subclasses override to support stack locals."""
        return None

    @staticmethod
    def _resolve_sdcc_reg(reg_name, regs):
        """Map an SDCC register name (e.g. 'l', 'bc') to its value from the regs dict.

        ZEsarUX returns 16-bit pairs (AF, BC, DE, HL, IX, IY).
        SDCC names single bytes as a/b/c/d/e/h/l and pairs as bc/de/hl/ix/iy.
        """
        r = reg_name.upper()
        low_byte  = {'C': 'BC', 'E': 'DE', 'L': 'HL'}
        high_byte = {'A': 'AF', 'B': 'BC', 'D': 'DE', 'H': 'HL'}
        if r in low_byte:
            return regs.get(low_byte[r], 0) & 0xFF
        if r in high_byte:
            return (regs.get(high_byte[r], 0) >> 8) & 0xFF
        return regs.get(r)

    def _func_name_at(self, pc):
        """Return the name of the function containing pc, or None."""
        for name, start, end in self.functions:
            if start <= pc <= end:
                return name
        return None

    def _locals_for_pc(self, pc, regs):
        """Build the DAP variables list for the Locals scope at the given pc."""
        funcname = self._func_name_at(pc)
        if not funcname:
            return []
        variables = []
        for var in self.local_vars.get(funcname, []):
            storage = var.get('storage')
            if storage == 'R':
                value = self._resolve_sdcc_reg(var.get('register', ''), regs)
                if value is not None:
                    variables.append({
                        'name': var['name'],
                        'value': str(value),
                        'type': 'uint8',
                        'variablesReference': 0,
                    })
            elif storage == 'B':
                sp_offset = var.get('sp_offset')
                if sp_offset is not None:
                    sp   = regs.get('SP', 0)
                    addr = (sp + sp_offset) & 0xFFFF
                    data = self.read_memory_bytes(addr, 1)
                    variables.append({
                        'name': var['name'],
                        'value': str(data[0]) if data else f'[SP+{sp_offset}]',
                        'type': 'uint8',
                        'variablesReference': 0,
                    })
        return variables

    # ─────────────────────────────────────────────────────────────────────────

    def _register_source_path(self, full_path):
        """Cache basename → full_path so handle_stack_trace can resolve filenames."""
        self._path_cache[Path(full_path).name] = full_path

    def snap_to_valid_line(self, filename, line):
        """Return the nearest line at or after `line` that has a known address, or None."""
        for offset in range(0, 20):
            if (filename, line + offset) in self.sld_map:
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
                'supportsWriteMemoryRequest': True,
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
        scopes = []
        if self.local_vars:
            scopes.append({'name': 'Locals', 'variablesReference': 2, 'expensive': False})
        scopes.append({'name': 'Registers', 'variablesReference': 1, 'expensive': False})
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'scopes',
            'success': True,
            'body': {'scopes': scopes},
        })

    def handle_stack_trace(self, msg):
        regs = self.read_registers()
        pc   = regs.get('PC', self._load_address)
        src  = self.address_to_source.get(pc)
        if src:
            basename, line = src
            source_path = self._path_cache.get(basename, self._source_path)
        else:
            line        = 1
            source_path = self._source_path
        self.send({
            'type': 'response',
            'request_seq': msg['seq'],
            'command': 'stackTrace',
            'success': True,
            'body': {
                'stackFrames': [{
                    'id': 1,
                    'name': self._func_name_at(pc) or f'PC={pc:#06x}',
                    'source': {'path': source_path},
                    'line': line,
                    'column': 1,
                }],
                'totalFrames': 1,
            },
        })

    def handle_variables(self, msg):
        ref = msg['arguments'].get('variablesReference')
        regs = self.read_registers()
        if ref == 1:
            variables = [
                {'name': n, 'value': f'0x{v:04x}', 'type': 'register', 'variablesReference': 0}
                for n, v in regs.items()
            ]
        elif ref == 2:
            pc = regs.get('PC', self._load_address)
            variables = self._locals_for_pc(pc, regs)
        else:
            variables = []
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

    def handle_write_memory(self, msg):
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
