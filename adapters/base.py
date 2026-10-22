import sys
import json
import logging
import re
import threading
from pathlib import Path


class DAPAdapter:
    # variablesReference values for the built-in scopes. Adapters add their own
    # above these via extra_scopes().
    SCOPE_REGISTERS = 1
    SCOPE_LOCALS    = 2
    SCOPE_GLOBALS   = 3

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
        self._last_frame = None     # (source_path, line) last resolved from address_to_source
        self._module_source_cache = {}  # {obj_path: full_path or None}
        self._project_stem_index = None # built on first use: {file_stem: full_path}
        self._load_address = 0
        self._load_address_explicit = False
        self._setup_done = False
        self.bin_file = None
        self.sld_file = None
        self.map_file = None
        self.cdb_file = None
        self.functions = []     # [(name, start_addr, end_addr)]
        self.local_vars = {}    # {funcname: [{name, storage, register|sp_offset}]}
        self.globals = []       # [{name, address, equ}] top-level SLD labels, see parse_sld
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
            'setVariable':       self.handle_set_variable,
            'readMemory':        self.handle_read_memory,
            'writeMemory':       self.handle_write_memory,
            'evaluate':          self.handle_evaluate,
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

    def respond(self, msg, body=None, success=True, message=None):
        """Reply to a DAP request. Mirrors back its seq and command so callers don't have to."""
        response = {
            'type': 'response',
            'request_seq': msg['seq'],
            'command': msg['command'],
            'success': success,
        }
        if body is not None:
            response['body'] = body
        if message is not None:
            response['message'] = message
        self.send(response)

    def send_stopped(self, reason):
        self.send({
            'type': 'event',
            'event': 'stopped',
            'body': {'reason': reason, 'threadId': 1, 'allThreadsStopped': True},
        })

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
        """Parse an sjasmplus SLD file (pipe-delimited: source|line|deffile|defline|page|value|type|data).

        Only 'T' (breakpointable instruction) and 'L' (label/equ, see _parse_sld_label) records
        matter. Field 0's path may be relative to the project root -- resolved against the .sld
        file's own grandparent directory when not absolute, so nvim can find the source file.
        Also populates self.functions and self.globals from the 'L' records.
        """
        line_to_addr = {}
        addr_to_source = {}
        labels = []
        project_root = Path(sld_path).resolve().parent.parent
        with open(sld_path) as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) < 7:
                    continue
                rtype = parts[6]
                if rtype == 'T':
                    try:
                        full_path = parts[0]
                        basename  = Path(full_path).name
                        line_num  = int(parts[1])
                        address   = int(parts[5])
                        key = (basename, line_num)
                        line_to_addr[key] = address
                        addr_to_source[address] = key
                        if full_path:
                            if not Path(full_path).is_absolute():
                                full_path = str(project_root / full_path)
                            self._path_cache[basename] = full_path
                    except ValueError:
                        pass
                elif rtype == 'L':
                    self._parse_sld_label(parts, labels)
        self.globals = labels
        self.functions = self._labels_to_functions(
            [(g['name'], g['address']) for g in labels if not g['equ']]
        )
        return line_to_addr, addr_to_source

    @staticmethod
    def _parse_sld_label(parts, labels):
        """Extract one 'L' record into {'name','address','equ'}, appended to `labels`.

        Data field (parts[7]) format: module,mainLabel,localLabel[,+trait...]. Local
        sub-labels (e.g. a `.loop` scoped under a global label) are skipped -- not real
        global symbols. Module-qualified names render as "module.label".
        """
        try:
            address = int(parts[5])
        except (ValueError, IndexError):
            return
        data = parts[7] if len(parts) > 7 else ''
        fields = data.split(',')
        if len(fields) < 3:
            return
        module, main_label, local_label = fields[0], fields[1], fields[2]
        traits = fields[3:]
        if local_label or not main_label:
            return
        name = f'{module}.{main_label}' if module else main_label
        labels.append({'name': name, 'address': address, 'equ': '+equ' in traits})

    @staticmethod
    def _labels_to_functions(labels):
        """Turn (name, address) code labels into (name, start, end) ranges for _func_name_at --
        each label "owns" every address up to the next label's start. SLD has no real
        function-boundary concept, so this is an approximation good enough for stack frame names.
        """
        ordered = sorted(set(labels), key=lambda item: item[1])
        functions = []
        for i, (name, start) in enumerate(ordered):
            end = ordered[i + 1][1] - 1 if i + 1 < len(ordered) else 0xFFFF
            functions.append((name, start, end))
        return functions

    # ── MAP parsing (SDCC) ────────────────────────────────────────────────────

    def parse_map(self, map_path):
        """Parse SDCC linker .map file for C-level line -> address mapping.

        With --debug, SDCC embeds C$<filename>$<lineno>$<level>_<block>$<col> records, and a
        load address guess from the s__CODE symbol (only correct when _CODE is the lowest
        area in the image -- parse_ihx_load_address() is authoritative when available).

        Returns (line_to_addr, addr_to_line, load_address); load_address is None if s__CODE
        is not found.
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

    @staticmethod
    def parse_ihx_load_address(ihx_path):
        """Read the true lowest address in a linked Intel HEX (.ihx) file -- authoritative
        even when parse_map's s__CODE guess is wrong. Scans every type-00 record since
        they aren't guaranteed to appear in address order.

        Returns None if the file doesn't exist or has no data records.
        """
        try:
            min_addr = None
            with open(ihx_path) as f:
                for raw in f:
                    line = raw.strip()
                    if not line.startswith(':') or len(line) < 11:
                        continue
                    try:
                        addr = int(line[3:7], 16)
                        rtype = int(line[7:9], 16)
                    except ValueError:
                        continue
                    if rtype == 0 and (min_addr is None or addr < min_addr):
                        min_addr = addr
            return min_addr
        except OSError:
            return None

    # ── LST parsing (hand-written asm modules with no C$/A$ debug records) ─────

    _LST_PREFIX_WIDTH = 40  # sdasz80 listing: fixed-width addr/bytes/line prefix, then source text

    def parse_map_module_info(self, map_path):
        """Work out each linked object's base address from the linker .map's "Files Linked"
        table: the first object starts at s__CODE, and each next one picks up right after the
        previous object's _CODE ends (size read from that object's own .sym file). We use link
        order rather than the global symbol table's "module" column because a module name can
        be reused across objects.

        Returns [{'module': str, 'obj_path': str, 'base_addr': int}, ...] in link order.
        """
        entries = []
        code_start = None
        with open(map_path) as f:
            for raw in f:
                parts = raw.split()
                if len(parts) == 4 and parts[0].endswith('.rel') and parts[1] == '[' and parts[3] == ']':
                    entries.append((parts[2], parts[0]))
                    continue
                if len(parts) >= 2:
                    try:
                        addr = int(parts[0], 16)
                    except ValueError:
                        continue
                    if parts[1] == 's__CODE' and code_start is None:
                        code_start = addr

        project_root = Path(map_path).resolve().parent.parent
        base = code_start if code_start is not None else 0
        result = []
        for module, obj_path in entries:
            result.append({'module': module, 'obj_path': obj_path, 'base_addr': base})
            base += self._object_code_size(project_root / Path(obj_path).with_suffix('.sym'))
        return result

    @staticmethod
    def _object_code_size(sym_path):
        """Read one object's own _CODE area size from its .sym Area Table."""
        try:
            text = Path(sym_path).read_text()
        except OSError:
            return 0
        match = re.search(r'_CODE\s+size\s+([0-9A-Fa-f]+)', text)
        return int(match.group(1), 16) if match else 0

    @classmethod
    def parse_lst(cls, lst_path):
        """Parse an sdasz80 listing file (built with -l) into a relative-address -> line-number
        map for the _CODE area. We only track _CODE because addressing restarts at 0 for every
        `.area` block, and the other areas (_DATA, etc.) never hold executable code anyway.

        Returns {relative_addr: line_num}, or {} if the file doesn't exist.
        """
        addr_to_line = {}
        in_code_area = True  # sdasz80 defaults to _CODE until told otherwise
        try:
            with open(lst_path) as f:
                for raw in f:
                    line = raw.rstrip('\n')
                    if len(line) < cls._LST_PREFIX_WIDTH:
                        continue
                    prefix = line[:cls._LST_PREFIX_WIDTH]
                    text_tokens = line[cls._LST_PREFIX_WIDTH:].split()
                    if text_tokens[:1] == ['.area']:
                        in_code_area = len(text_tokens) > 1 and text_tokens[1] == '_CODE'
                        continue
                    addr_field = prefix[6:12].strip()
                    line_field = prefix[34:40].strip()
                    if not (in_code_area and addr_field and line_field.isdigit()):
                        continue
                    try:
                        addr = int(addr_field, 16)
                    except ValueError:
                        continue
                    # A label and the instruction right after it can share an address
                    # (labels emit no bytes) -- keep the instruction's line, not the label's.
                    addr_to_line[addr] = int(line_field)
        except OSError:
            pass
        return addr_to_line

    def _resolve_module_source(self, obj_path, project_root):
        """Figure out which source file a linked object was assembled from. Keyed by obj_path,
        not the `.module` name (which can repeat). Tries mirroring the object tree under src/
        first, then falls back to a one-time recursive filename search under project_root.

        Returns a full path string, or None if nothing matches.
        """
        if obj_path in self._module_source_cache:
            return self._module_source_cache[obj_path]

        rel = Path(obj_path)
        result = None
        if rel.parts:
            mirrored = Path(project_root, 'src', *rel.parts[1:])
            for ext in ('.s', '.asm', '.c'):
                candidate = mirrored.with_suffix(ext)
                if candidate.exists():
                    result = str(candidate)
                    break

        if result is None:
            if self._project_stem_index is None:
                self._project_stem_index = {}
                for path in Path(project_root).rglob('*'):
                    if path.suffix in ('.s', '.asm', '.c') and path.stem not in self._project_stem_index:
                        self._project_stem_index[path.stem] = str(path)
            result = self._project_stem_index.get(rel.stem)

        self._module_source_cache[obj_path] = result
        return result

    def build_asm_line_map(self, map_path):
        """Augment address_to_source with lines from every linked object's .lst listing,
        for objects the .map/.cdb gave no C$/A$ records for (typically hand-written asm).

        Returns {addr: (basename, line)}; also populates _path_cache for resolved sources.
        """
        extra = {}
        project_root = Path(map_path).resolve().parent.parent
        for info in self.parse_map_module_info(map_path):
            lst_path = project_root / Path(info['obj_path']).with_suffix('.lst')
            rel_lines = self.parse_lst(lst_path)
            if not rel_lines:
                continue
            source_path = self._resolve_module_source(info['obj_path'], project_root)
            if source_path is None:
                continue
            basename = Path(source_path).name
            self._register_source_path(source_path)
            for rel_addr, line_num in rel_lines.items():
                extra[info['base_addr'] + rel_addr] = (basename, line_num)
        return extra

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

    # S:L<module>.<func>$<var>$<level>_<block>$<col>(<type>),<storage>,...
    #   S:Lmain.main$result$1_0$80({1}SC:U),R,0,0,[l]   -- held in register l
    #   S:Lmain.sumUpTo$n$1_0$82({1}SC:U),B,1,4         -- held at SP+4
    _CDB_LOCAL_RE = re.compile(r'^S:L[^.]*\.([^$]+)\$([^$]+)\$.*\)(.*)$')

    @classmethod
    def _cdb_parse_local(cls, raw, local_vars):
        match = cls._CDB_LOCAL_RE.match(raw)
        if not match:
            return
        funcname, varname, trailing = match.groups()
        fields = trailing.lstrip(',').split(',')

        var = {'name': varname, 'storage': fields[0]}
        try:
            if var['storage'] == 'R' and len(fields) >= 4:
                reg = fields[3].strip()
                if reg.startswith('[') and reg.endswith(']'):
                    var['register'] = reg[1:-1]
            elif var['storage'] == 'B' and len(fields) >= 3:
                var['sp_offset'] = int(fields[2])
        except ValueError:
            return
        local_vars.setdefault(funcname, []).append(var)

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

    # ── Session setup ────────────────────────────────────────────────────────

    def read_symbol_args(self, args):
        """Pick the binary and symbol-file paths out of a DAP launch request.

        A .cdb sitting next to the .map under the same name is picked up on its own,
        since that's how SDCC emits it.
        """
        if 'program' in args:
            self.bin_file = Path(args['program'])
        if 'sldFile' in args:
            self.sld_file = Path(args['sldFile'])
        if 'mapFile' in args:
            self.map_file = Path(args['mapFile'])
            sibling_cdb = self.map_file.with_suffix('.cdb')
            if sibling_cdb.exists():
                self.cdb_file = sibling_cdb
        if 'cdbFile' in args:
            self.cdb_file = Path(args['cdbFile'])
        if 'loadAddress' in args:
            self._load_address = int(str(args['loadAddress']), 0)
            self._load_address_explicit = True

    def load_debug_info(self, source_path=None):
        """Work out which binary and symbol files this session uses, and parse them.

        Fills in sld_map, address_to_source, functions, local_vars and _load_address.
        Returns the binary's path, or None if we couldn't work out what to load.
        Actually getting the binary into the machine is the adapter's job -- every
        emulator has its own way of doing it.
        """
        if self.bin_file is not None and (self.sld_file or self.map_file):
            binary, sld = self.bin_file, self.sld_file
        elif source_path is None:
            logging.error('No source path available to resolve bin/sld files')
            return None
        else:
            # Fall back to the build/<name>.{bin,sld} convention next to the source.
            source = Path(source_path)
            build = source.parent.parent / 'build'
            binary = self.bin_file or build / f'{source.stem}.bin'
            sld = self.sld_file or build / f'{source.stem}.sld'

        map_load_address = None
        if self.map_file is not None:
            self.sld_map, self.address_to_source, map_load_address = self.parse_map(self.map_file)
        elif sld is not None:
            self.sld_map, self.address_to_source = self.parse_sld(sld)

        if not self._load_address_explicit:
            # The .ihx wins when it's there -- it's what hex2bin itself goes by, so it
            # stays right even when the lowest area is one sdld reports as address 0.
            ihx_address = self.parse_ihx_load_address(binary.with_suffix('.ihx'))
            if ihx_address is not None:
                self._load_address = ihx_address
                logging.debug(f'Load address from .ihx: 0x{self._load_address:04x}')
            elif map_load_address is not None:
                self._load_address = map_load_address
                logging.debug(f'Load address from map file s__CODE: 0x{self._load_address:04x}')

        if self.cdb_file is not None:
            cdb_lines, cdb_addrs, self.functions, self.local_vars = self.parse_cdb(self.cdb_file)
            self.sld_map.update(cdb_lines)
            self.address_to_source.update(cdb_addrs)
            logging.debug(
                f'CDB: {len(self.functions)} functions, '
                f'{sum(len(v) for v in self.local_vars.values())} locals'
            )

        if self.map_file is not None:
            # Hand-written .s modules get no C$ records, so fill their gaps from .lst listings.
            gaps = {
                addr: entry
                for addr, entry in self.build_asm_line_map(self.map_file).items()
                if addr not in self.address_to_source
            }
            self.address_to_source.update(gaps)
            logging.debug(f'LST: filled {len(gaps)} addr->line gaps from module listings')

        return binary

    # ── Register helpers ─────────────────────────────────────────────────────

    def read_memory_bytes(self, address: int, count: int):
        """Read raw bytes from emulator memory. Subclasses override to support stack locals."""
        return None

    def extra_scopes(self):
        """Additional DAP scopes beyond Locals/Registers/Globals. Subclasses override to
        contribute emulator-specific ones (e.g. ZesaruxAdapter's I/O Ports)."""
        return []

    def extra_scope_variables(self, ref):
        """variablesReference values not handled above (1/2/3) land here. Subclasses
        override alongside extra_scopes() to supply their content."""
        return []

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

    def _global_variables(self):
        """Build the DAP variables list for the Globals scope, from self.globals (SLD 'L'
        records). An '+equ' entry shows its constant value; everything else is a real label,
        shown as its address plus the current byte at that address when readable.
        """
        variables = []
        for g in self.globals:
            if g['equ']:
                value = f"{g['address']:#06x}"
            else:
                data = self.read_memory_bytes(g['address'], 1)
                value = f"{g['address']:#06x}" + (f' = {data[0]:#04x}' if data else '')
            variables.append({
                'name': g['name'],
                'value': value,
                'type': 'equ' if g['equ'] else 'label',
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
        self.respond(msg, {
            'supportsConfigurationDoneRequest': True,
            'supportsReadMemoryRequest': True,
            'supportsWriteMemoryRequest': True,
            'supportsSetVariable': True,
        })
        self.send({'type': 'event', 'event': 'initialized'})

    def handle_threads(self, msg):
        self.respond(msg, {'threads': [{'id': 1, 'name': 'Z80'}]})

    def handle_scopes(self, msg):
        scopes = []
        if self.local_vars:
            scopes.append({'name': 'Locals', 'variablesReference': self.SCOPE_LOCALS, 'expensive': False})
        scopes.append({'name': 'Registers', 'variablesReference': self.SCOPE_REGISTERS, 'expensive': False})
        if self.globals:
            scopes.append({'name': 'Globals', 'variablesReference': self.SCOPE_GLOBALS, 'expensive': False})
        scopes.extend(self.extra_scopes())
        self.respond(msg, {'scopes': scopes})

    def handle_stack_trace(self, msg):
        regs = self.read_registers()
        pc   = regs.get('PC', self._load_address)
        src  = self.address_to_source.get(pc)
        if src:
            basename, line = src
            source_path = self._path_cache.get(basename, self._source_path)
            self._last_frame = (source_path, line)
        elif self._last_frame is not None:
            # No debug info for this PC
            source_path, line = self._last_frame
        else:
            line        = 1
            source_path = self._source_path
        self.respond(msg, {
            'stackFrames': [{
                'id': 1,
                'name': self._func_name_at(pc) or f'PC={pc:#06x}',
                'source': {'path': source_path},
                'line': line,
                'column': 1,
            }],
            'totalFrames': 1,
        })

    def handle_variables(self, msg):
        ref = msg['arguments'].get('variablesReference')
        regs = self.read_registers()
        if ref == self.SCOPE_REGISTERS:
            variables = [
                {'name': n, 'value': f'0x{v:04x}', 'type': 'register', 'variablesReference': 0}
                for n, v in regs.items()
            ]
        elif ref == self.SCOPE_LOCALS:
            pc = regs.get('PC', self._load_address)
            variables = self._locals_for_pc(pc, regs)
        elif ref == self.SCOPE_GLOBALS:
            variables = self._global_variables()
        else:
            variables = self.extra_scope_variables(ref)
        self.respond(msg, {'variables': variables})

    @staticmethod
    def _hex_candidate(segment):
        """Strip an optional leading '> ' prompt marker and '0x' prefix,
        then parse as hex. Returns None if the segment isn't valid hex."""
        s = segment.strip()
        if s.startswith('> '):
            s = s[2:].strip()
        if s[:2].lower() == '0x':
            s = s[2:]
        try:
            return int(s, 16) & 0xFFFF
        except ValueError:
            return None

    def _parse_register_edit(self, raw_value, name):
        """Parse a setVariable value for the Registers scope into an int, or None.

        dapui's inline "edit" prompt is prefilled with the register's current value, and on
        some setups the submitted text ends up as the old value and the new one stuck together
        with a "> " marker between them. We treat each line as a candidate value and throw out
        whichever one matches what the register already holds -- that's the stale prefill, not
        what the user typed. If it's still unclear, just go with the last candidate.
        """
        segments = [seg for seg in raw_value.splitlines() if seg.strip()] or [raw_value]
        candidates = [(seg, self._hex_candidate(seg)) for seg in segments]
        candidates = [(seg, v) for seg, v in candidates if v is not None]
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0][1]

        current = self.read_registers().get(name)
        if current is not None:
            filtered = [(seg, v) for seg, v in candidates if v != (current & 0xFFFF)]
            if filtered:
                candidates = filtered
        return candidates[-1][1]

    def handle_set_variable(self, msg):
        """Only the Registers scope is editable -- Locals/Globals/etc. are derived
        read-only views with no single emulator write that would make sense to target.
        """
        args      = msg['arguments']
        ref       = args.get('variablesReference')
        name      = args.get('name')
        raw_value = args.get('value', '').strip()

        if ref != self.SCOPE_REGISTERS:
            self.respond(msg, success=False, message='Only the Registers scope supports editing')
            return

        value = self._parse_register_edit(raw_value, name)
        if value is None:
            self.respond(msg, success=False, message=f'Invalid hex value: {raw_value!r}')
            return

        if self.write_register(name, value):
            self.respond(msg, {'value': f'0x{value:04x}', 'variablesReference': 0})
        else:
            self.respond(msg, success=False, message=f'Emulator rejected writing register {name!r}')

    # ── Abstract interface ────────────────────────────────────────────────────

    def read_registers(self) -> dict:
        raise NotImplementedError

    def write_register(self, name: str, value: int) -> bool:
        """Write a single register. Return True on success. Subclasses
        override; see zesarux.py (ZRCP `set-register`) and mame.py
        (GDB RSP `P<index>=<value>`)."""
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

    def handle_evaluate(self, msg):
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

    def cleanup_on_crash(self):
        """Best-effort cleanup when main()'s loop dies from an unhandled exception, since a
        crash skips past handle_disconnect. Subclasses override; no-op by default.
        """
        pass

    def main(self):
        while True:
            try:
                msg = self.read_message()
                self.handle(msg)
            except Exception:
                logging.exception('Unhandled exception')
                try:
                    self.cleanup_on_crash()
                except Exception:
                    logging.exception('cleanup_on_crash itself failed')
                raise
