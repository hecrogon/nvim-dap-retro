# TODO

Items marked ✅ are already implemented.

---

## Debugger Core

### Execution Control
- ✅ Step (next instruction)
- ✅ Continue (run until breakpoint)
- [ ] **Step out** — run until the current subroutine returns (DAP `stepOut`; detect `RET` or watch SP)
- [ ] **Pause** — break into a running program (DAP `pause`; ZRCP `cpu-step`, GDB RSP `\x03`)
- [ ] **Restart** — reload binary and reset CPU without tearing down the session (DAP `restart`)
- [ ] **Run to cursor** — set a temporary breakpoint at the current line and continue

### Breakpoints
- ✅ Line breakpoints (snapped to nearest valid address)
- [ ] **Conditional breakpoints** — break only when an expression is true (ZEsarUX supports `set-breakpoint N PC=ADDRh AND REG=VAL`; GDB RSP `Z0,addr,kind:cond=...`)
- [ ] **Watchpoints / memory breakpoints** — break on read or write to a memory address (ZRCP `set-membreakpoint`; GDB RSP `Z2`/`Z3`)
- [ ] **Logpoints** — print a message without stopping (emulate with a conditional BP that never triggers)
- [ ] **Hit-count breakpoints** — break only after N hits

### Registers
- ✅ Read registers (shown in Scopes → Registers)
- ✅ **Edit registers** — DAP `setVariable` on the Registers scope. ZEsarUX via `set-register NAME=VALh`; MAME via GDB RSP's per-index `P<n>=<val>`. Also works around an nvim-dap-ui quirk where its inline Scopes "edit" prompt can submit the prefilled old value and the freshly typed new one concatenated with a literal `"> "` marker, in no reliable order — `_parse_register_edit` cross-checks each candidate against the register's current value and discards whichever one matches (the stale prefill).

### Variables / Symbols
- ✅ **Locals scope** — `.cdb` parsed for register-stored and stack locals; function name shown in stack frame
- ✅ **Globals scope (`.sld`)** — sjasmplus `L` label records exposed as a Globals scope; `equ` constants show their defined value, other labels show address + current byte read from the emulator
  - [ ] **Globals scope (`.map`/`.cdb`)** — SDCC's global symbol table has 10k+ mostly-internal entries per real project (`C$`/`A$`/library symbols); needs filtering to be useful rather than noise
- ✅ **Generic, grouped "I/O Ports" scope (ZEsarUX, any machine)** — `get-io-ports` isn't CPC-specific; it returns whatever hardware sections are relevant to the current machine (ULA + PD765 on every Z80 target, plus CRTC/PPI/Gate Array/AY-3-8912 on CPC, an extra FE-port line on Spectrum, etc). `_parse_io_ports` parses the response generically by line shape — `"Section:"` headers, `"NN: NN"` register-table rows, plain `"key: value"` fields — with no per-machine special-casing. Sections render as their own expandable DAP tree nodes; loose top-level fields show directly, ungrouped.
- ✅ **Expression evaluation** — `handle_evaluate` (REPL): `tstates`/`tstates reset` for exact T-state timing, `zrcp <command>` as a raw ZRCP passthrough (ZEsarUX only)
- [ ] **Symbol browser** — list all known symbols and addresses from the loaded symbol file (could be a Telescope extension)

### Stack Trace
- ✅ Single frame showing PC → source line
- ✅ Frame name resolved to function name when `.cdb` (SDCC) or `.sld` (sjasmplus `L` labels, approximated as "owns every address until the next label") is available
- ✅ Falls back to the last successfully resolved source location (rather than an unrelated file) when the current PC has no debug info at all
- [ ] **Multi-frame call stack** — walk the Z80 hardware stack (SP pointer) and resolve return addresses to source lines for a realistic call stack

---

## Memory Panel

- ✅ Hex dump with ASCII sidebar
- ✅ Page and line navigation
- ✅ Edit single byte at cursor
- ✅ **Jump to address** — `a` prompts for a hex address and scrolls the dump there instantly
- [ ] **Multiple regions** — open a second memory panel pinned to a different address (e.g. video RAM at 0xC000 alongside code at 0x4000)
- ✅ **Memory search (loaded window only)** — `/` prompts for a hex byte sequence and jumps to the first match within the currently displayed bytes, wrapping like a normal Neovim search
  - [ ] **Full 64KB search** — extend past the loaded window by reading memory in chunks across the whole address space
  - [ ] **ASCII string search** — currently hex-bytes only
- [ ] **Highlight changed bytes** — diff memory state between steps and colour bytes that changed
- [ ] **Word (16-bit) display mode** — show values as 16-bit little-endian words in addition to bytes
- [ ] **Follow pointer** — place cursor on two bytes and jump to the address they form
- [ ] **Export region** — write a memory range to a local file for offline analysis

---

## Symbol File Support

- ✅ sjasmplus `.sld` (pipe-delimited)
- ✅ SDCC `.map` (`C$` records, `s__CODE` load address)
- ✅ **SDCC `.cdb`** — auto-detected alongside `.map`; provides function ranges, line mappings, and local variable storage (register or SP+offset)
  - [ ] **Type decoding** — currently all locals display as raw integers; decode `{1}SC:U` etc. to show `u8`, `i8`, `u16` in the type column
  - [ ] **Struct / array locals** — complex types need recursive type parsing
  - [ ] **Scope filtering** — only show variables in scope at the current block level (`.cdb` encodes `$level_block$` per symbol)
- ✅ **SDCC hand-written `.s` modules with no `C$`/`A$` records** — a project can link hand-assembled `.s` files (via `sdasz80`, not through SDCC's C front end) alongside C-compiled code; those get no line-level debug info in `.map`/`.cdb`/their own `.sym`. If built with `sdasz80 -l`, each module's `.lst` listing is parsed for relative-address→line info, combined with a link-order-derived base address (accumulated per-object `_CODE` size from the `.map`'s "Files Linked" table, which stays correct even when two source files share one `.module` name).
- [ ] **Pasmo `.dbg`** — Pasmo assembler symbol format
- [ ] **WLA-DX `.sym`** — WLA-DX assembler symbol format
- [ ] **Multi-file SLD** — stack trace and breakpoints across multiple source files (SLD records include filename; wire this up)

---

## Adapters

### ZEsarUX (ZRCP)
- ✅ Auto-launch and connect
- ✅ Binary load, breakpoints, step, continue, memory R/W, registers
- ✅ SLD and SDCC MAP symbol files
- [ ] **Conditional breakpoints** (see above)
- [ ] **Watchpoints** via `set-membreakpoint`
- [ ] **Pause** via entering cpu-step while running
- [ ] **Machine-specific launch args** documented per platform (CPC, Spectrum, Sam Coupé, ZX80/81)
- ✅ **Native "Debug" window left up after launch, requiring a manual dismiss (e.g. Esc) before the emulator was usable** — ZEsarUX pops its own native debug console on entering cpu-step mode regardless of `--disable-debug-console-win` (`continue` and `handle_step` already re-close it after every stop, per `_monitor_breakpoint`). The one stop path that didn't was the entry event in `handle_configuration_done`, right after `handle_launch` enters cpu-step mode — now sends `close-all-menus` there too, matching the other stop paths.

### MAME (GDB RSP)
- ✅ Auto-launch, GDB stub, binary load via memory write, breakpoints, step, continue, memory R/W
- [ ] **Watchpoints** via GDB `Z2`/`Z3`/`Z4`
- [ ] **Multi-CPU systems** — MAME can have more than one CPU; expose as additional threads
- [ ] **Architecture-agnostic registers** — currently hardcoded for Z80; detect target CPU from MAME and parse register layout accordingly
- [ ] **Symbol file support** — MAME has its own symbol/map format; wire it up

### VICE (MOS 6502)
- [ ] **Implement the adapter** — VICE exposes a binary monitor protocol on a TCP port; implement connect, reset, breakpoints, step, continue, register read/write, memory R/W
- [ ] **6502 register set** — A, X, Y, SP, PC, SR
- [ ] **C64/C128/VIC-20 machine selection**
- [ ] **Petcat / cc65 symbol file support** (`.lbl` / `.map`)

---

## Platform Samples

- ✅ Amstrad CPC — Z80 assembly (`amstrad/helloworld`)
- ✅ **ZX Spectrum** — Z80 assembly sample with `.sld` (`spectrum/helloworld`)
- [ ] **ZX Spectrum** — C sample
- [ ] **Amstrad CPC** — C via CPCTelera / SDCC sample (previously `cpctelera-hello`; moved out of the repo, add a trimmed-down version back under `nvim-dap-retro/samples`)

---

## Infrastructure

- ✅ **Configurable adapter paths** — every adapter's Lua config resolves the plugin root dynamically now instead of a hardcoded path
- ✅ **Multiple concurrent sessions (self-launched ZEsarUX)** — when `zesaruxArgs` is set and `zesaruxPort` isn't explicitly given, the adapter picks a free local port (bind to port 0, read it back, release it) and launches its own dedicated ZEsarUX process on it via `--remoteprotocol-port`, instead of every session sharing the port-10000 default. Two `debug()` calls now get two fully isolated emulator processes with nothing to collide over.
  - [ ] **Manually-managed ZEsarUX (no `zesaruxArgs`)** — still one shared process by the user's own choice; a second attach here can still silently corrupt the first session. Needs a lock (e.g. a PID file written on `handle_launch`, checked/cleaned on `handle_disconnect`) to refuse with a clear error instead
- ✅ **Crash orphaning a spawned emulator process** — an unhandled exception (a ZRCP response desync where ZEsarUX exited single-step mode outside the adapter's knowledge, `run` failed immediately, and a stale `get-registers` reply got consumed by the next command's `read-memory` instead) could skip straight past `handle_disconnect`, leaving the spawned ZEsarUX process running forever. Added `cleanup_on_crash()` (called from `base.py`'s `main()` on any unhandled exception, before re-raising) as a hook subclasses use to kill their spawned process regardless of why the crash happened, factored out of `handle_disconnect`'s existing cleanup into a shared `_terminate_spawned_process()` in both `zesarux.py` and `mame.py`.
  - [ ] **The underlying ZRCP desync itself** — still not root-caused beyond "ZEsarUX left single-step mode by some external means"; `zesarux_run()` doesn't check whether `run` actually succeeded before relying on the monitor thread to report a stop
- ✅ **Adapter health check (ZEsarUX)** — `nvim-dap` itself already notifies cleanly when the adapter *process exits* (non-zero exit code → a warning pointing at `:DapShowLog`), so the real gap was that `zesarux_recv_until_prompt` had no timeout and could hang this process forever if ZEsarUX died or the connection stalled mid-command. Now bounded to 15s for every caller except the continue/run monitor thread, which legitimately waits as long as the debuggee takes to hit a breakpoint; a timeout raises a clear `RuntimeError` that becomes a visible error instead of an indefinite hang.
  - [ ] **MAME** — same class of gap likely applies to `gdb_recv`'s blocking reads; not yet audited/fixed
- [ ] **DAP capabilities per adapter** — only advertise capabilities that the active adapter actually supports (e.g. don't offer `setDataBreakpoints` if the emulator has no watchpoint support)
- ✅ **dapui layout drift on terminal resize** — nvim-dap-ui never re-applies `dapui_layout`'s weighted sizes after a resize (e.g. a tmux pane zoom); Neovim's own `'equalalways'` pulls splits toward equal-ish instead. A `VimResized` autocmd now calls `dapui.open({ reset = true })` while a session is active to restore the configured proportions
- [ ] **Automated tests** — unit tests for `parse_sld`, `parse_map`, `parse_cdb`, `parse_lst`, `parse_map_module_info`, `snap_to_valid_line`; integration tests against a headless ZEsarUX instance. `parse_lst` in particular relies on sdasz80's listing being a fixed-column format (address at columns 6-12, line number at 34-40) with no test fixture pinning that assumption

---

## Nice to Have

- [ ] **Register history** — show a diff of which registers changed value since the last step
- [ ] **ZEsarUX screenshot** — capture the emulator screen into a Neovim float on demand (ZEsarUX has a screenshot command)
- [ ] **Reverse stepping** — ZEsarUX supports CPU history / time-travel; expose as `stepBack` if the DAP client supports it (DAP `stepBack` + `reverseContinue`)
- [ ] **Inline variable display** — show register or memory values as virtual text next to the source line (requires extmarks)
- [ ] **CPC DSK / CDT image builder** — trigger iDSK/2cdt from within Neovim after a build
