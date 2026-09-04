import sys
import json
import logging
import re
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
        self._last_frame = None     # (source_path, line) last resolved from address_to_source
        self._module_source_cache = {}  # {obj_path: full_path or None}
        self._project_stem_index = None # lazily built {file_stem: full_path}, see _resolve_module_source
        self._load_address = 0
        self.bin_file = None
        self.sld_file = None
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

        SLD record format (8 pipe-delimited fields):
            <source file>|<src line>|<def file>|<def line>|<page>|<value>|<type>|<data>
        Only two types matter here: 'T' (instruction Trace -- one line of
        real emitted code, the only type carrying a breakpointable
        address) and 'L' (Label -- a code label or `equ`, see
        _parse_sld_label). 'D'/'F' are older, now-redundant variants of
        'L' the spec says to treat as 'L' as well, but sjasmplus already
        emits a same-address 'L' line for everything 'D'/'F' would cover,
        so there is nothing to gain from also parsing them. 'Z' (memory
        model) and 'K' (keyword comment) carry no address/line data.

        The filepath in field 0 is whatever path sjasmplus was invoked
        with -- often relative to the project root (e.g. "src/hello.asm"),
        not absolute, if the build ran with the project root as its cwd.
        Caching a relative path as-is here clobbers the correct absolute
        path handle_set_breakpoints already registered for the file whose
        breakpoint triggered this parse, and nvim then can't find the file
        at all when a stopped event names it (it resolves against nvim's
        own cwd, not the build's) -- the source window just goes blank.
        Resolve against the project root (the .sld file's own grandparent
        directory, e.g. build/hello.sld -> project root) whenever the
        recorded path isn't already absolute.

        Also sets self.functions and self.globals from the 'L' records --
        self.functions so stack frames get a real label name instead of
        falling back to "PC=0x...." the way SDCC's .cdb already does for
        C builds (see _labels_to_functions); self.globals so the DAP
        Globals scope has something to show (see _global_variables).
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
        """Extract one 'L' record into {'name','address','equ'}, appended
        to `labels`.

        Data field (parts[7]) format: module,mainLabel,localLabel[,+trait...]
        -- see the SLD spec's list of traits (+local, +equ, +macro, +used,
        ...). Local sub-labels (localLabel non-empty, e.g. a `.loop` label
        scoped under the preceding global label) are skipped entirely --
        not a real global symbol, just a detail inside one; _func_name_at
        should still report the enclosing global label while PC is inside
        it, and the Globals scope has no use for internal loop counters.
        '+equ' labels are kept (unlike in _labels_to_functions -- an EQU
        constant, e.g. a hardware register address, is exactly the kind of
        thing worth showing in a Globals scope) but tagged so callers can
        tell a constant's *value* apart from a label's *address*.
        Module-qualified names (module non-empty) are rendered "module.label"
        to match sjasmplus's own qualified-name convention.
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
        """Turn address-ordered (name, address) code labels into (name,
        start, end) ranges for _func_name_at: each label "owns" every
        address up to the next label's start.

        SLD has no explicit function-boundary concept (a label is just an
        address with a name) -- this is an approximation good enough for
        naming a stack frame, not a substitute for real function-range
        debug info like SDCC's .cdb G$/XG$ pairs provide. In particular a
        label marking a data table, not a routine, would still "own" a
        range here; harmless in practice since PC only ever lands on
        addresses that are actually executed.
        """
        ordered = sorted(set(labels), key=lambda item: item[1])
        functions = []
        for i, (name, start) in enumerate(ordered):
            end = ordered[i + 1][1] - 1 if i + 1 < len(ordered) else 0xFFFF
            functions.append((name, start, end))
        return functions

    # ── MAP parsing (SDCC) ────────────────────────────────────────────────────

    def parse_map(self, map_path):
        """Parse SDCC linker .map file for C-level line → address mapping.

        With --debug, SDCC embeds C$ records in the map file:
            00004000  C$main.c$3$0_0$79    main
        Format: C$<filename>$<lineno>$<level>_<block>$<col>

        Also extracts a load address from the s__CODE symbol:
            00004000  s__CODE

        This is only correct when _CODE is the lowest area in the linked
        image, which is not always true -- a project can add its own (ABS)
        area at a lower address (e.g. to .incbin a blob at a fixed spot).
        ASxxxx's map output reports such (ABS,CON) areas' addresses as 0 in
        every summary table, so there is no way to find their real address
        from the .map file at all. parse_ihx_load_address() reads the true
        minimum address straight from the .ihx and should be preferred
        whenever a .ihx is available; this s__CODE guess is the fallback.

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

    @staticmethod
    def parse_ihx_load_address(ihx_path):
        """Read the true lowest address in a linked Intel HEX (.ihx) file.

        This is what hex2bin itself uses to decide where byte 0 of the .bin
        goes, so it is authoritative -- unlike guessing from the .map file's
        s__CODE symbol, it is correct even when the lowest area in the image
        is an (ABS) area sdld doesn't report a real address for (see
        parse_map's docstring). Records are not guaranteed to appear in
        address order, so every type-00 (data) record is scanned; type-01
        (EOF) and any others are ignored. Assumes flat 16-bit addressing
        (true for a Z80 target, which never emits 02/04 extended-address
        records) -- an .ihx using those would need them folded in here.

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
    #
    # Modules assembled directly by sdasz80 without going through SDCC's C
    # front-end (e.g. a project's own .s files calling into a C-compiled
    # codebase) never get C$/A$ line records anywhere -- not in the .map,
    # not in the .cdb, not even in their own per-module .sym file. Every PC
    # inside them is an address_to_source miss. But if the project builds
    # with sdasz80's `-l` listing flag, each module's .lst file has an exact
    # relative-address -> source-line mapping (see parse_lst), which
    # combined with that module's linked base address (from the .map) gives
    # a fully precise addr -> (file, line) mapping -- no guessing needed.

    _LST_PREFIX_WIDTH = 40  # sdasz80 listing: fixed-width addr/bytes/line prefix, then source text

    def parse_map_module_info(self, map_path):
        """Parse a linker .map file's "Files Linked" table into each linked
        object's own base address, in link order.

        "Files Linked" has rows `<obj path>.rel  [ <module> ]`, in the exact
        order sdld placed each object's _CODE area -- the first one starts
        at s__CODE, and every next one starts right after the previous
        object's own _CODE area ends. Each object's _CODE size comes from
        its own per-object .sym file's Area Table (`_CODE size B8`).

        Deriving base addresses this way -- rather than from the minimum
        address the global symbol table's "module" column attributes to a
        module name -- matters because a module name isn't guaranteed
        unique: this project has both wide_drawSolidBox.s and
        wide_drawSpriteMasked.s declare `.module wide_sprites`, so the
        symbol table's module column can't tell those two objects apart,
        but their distinct positions in "Files Linked" still can.

        Returns [{'module': str, 'obj_path': str, 'base_addr': int}, ...]
        in link order.
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
        """Parse an sdasz80 listing file (built with -l) for the _CODE
        area's relative-address -> source-line mapping.

        Relative addressing restarts at 0 for every `.area` block in the
        listing, so only the _CODE block is tracked -- mixing in another
        area's addresses (_DATA, _INITIALIZER, ...) would silently produce
        bogus entries indistinguishable from real _CODE ones. This only
        matters for execution addresses anyway, and code doesn't run out of
        those other areas.

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
                    # A label declaration and the instruction right after it
                    # can share the same relative address (the label emits
                    # no bytes of its own) -- keep the later (executable)
                    # line for that address rather than the label line.
                    addr_to_line[addr] = int(line_field)
        except OSError:
            pass
        return addr_to_line

    def _resolve_module_source(self, obj_path, project_root):
        """Find the source file a linked object was assembled from.

        Keyed by obj_path (unique per "Files Linked" row), not by the
        `.module` name inside it -- see parse_map_module_info for why that
        name can't be trusted to identify one object. Tries the project's
        usual layout first: substitute the object tree's top-level
        directory (e.g. "obj") for "src" in obj_path, keeping the rest of
        the path, and probe common source extensions. Falls back to a
        one-time recursive filename search under project_root for a file
        whose stem matches the object's own filename stem, for projects
        that don't mirror obj/ under src/.

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
        """Augment address_to_source with exact lines from every linked
        object's .lst listing (see parse_lst), for objects the .map/.cdb
        gave no C$/A$ records for at all -- typically hand-written asm.

        Returns {addr: (basename, line)}; also populates _path_cache for
        every object's source file it manages to resolve.
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

    def _global_variables(self):
        """Build the DAP variables list for the Globals scope.

        Sourced from self.globals (SLD 'L' records, see parse_sld). An
        '+equ' entry shows the constant it was defined with -- reading
        memory there would be meaningless (or, for something like
        print_char/wait_char in the helloworld sample, would read ROM
        firmware bytes miles away from anything this project owns).
        Everything else is a real label: shown as its address plus,
        whenever the emulator can read memory (read_memory_bytes is a
        base-class stub returning None unless a subclass implements it),
        the current byte stored there -- useful for a data label, and at
        least shows the first opcode byte for a code label.
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
        if self.globals:
            scopes.append({'name': 'Globals', 'variablesReference': 3, 'expensive': False})
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
            self._last_frame = (source_path, line)
        elif self._last_frame is not None:
            # No debug info for this PC (e.g. a hand-written .s module
            # assembled without line records) -- keep showing the last
            # resolved location instead of snapping to whatever file
            # _source_path happens to hold (typically unrelated), which
            # otherwise makes the source window jump away on every step.
            source_path, line = self._last_frame
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
        elif ref == 3:
            variables = self._global_variables()
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

    def main(self):
        while True:
            try:
                msg = self.read_message()
                self.handle(msg)
            except Exception:
                logging.exception('Unhandled exception')
                raise
