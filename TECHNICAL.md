# nvim-dap-retro — Technical Overview

This document describes how nvim-dap-retro is put together: the Lua/Python
split, the DAP adapter protocol translation, how source-level debug info is
recovered from each supported toolchain, and the session lifecycle for a
debug run. It assumes familiarity with [nvim-dap](https://github.com/mfussenegger/nvim-dap)
and the [Debug Adapter Protocol](https://microsoft.github.io/debug-adapter-protocol/)
(DAP) in general terms.

For user-facing setup and configuration, see [README.md](README.md). For
planned work, see [TODO.md](TODO.md).

## 1. What this is

nvim-dap-retro is a set of DAP adapters for debugging 8-bit assembly/C
targets under emulation, plus a couple of nvim-dap-ui panels (memory dump,
build log) useful for that workflow. nvim-dap speaks DAP to whatever the
`dap.adapters.<name>` config points at; this plugin's adapters are Python
scripts run as child processes that translate DAP into whatever
protocol the underlying emulator actually understands (ZRCP for ZEsarUX,
GDB Remote Serial Protocol for MAME).

The plugin does not implement a debugger itself — it maps DAP requests onto
commands an already-running (or auto-launched) emulator understands, and
maps the emulator's state back into DAP responses. Line-level source
debugging works by parsing whatever symbol/debug-info file the target
toolchain produced (`.sld`, `.map`, `.cdb`, `.lst`) into an
address ↔ (file, line) table, entirely independent of the emulator.

## 2. Repository layout

```
nvim-dap-retro/
├── adapters/                    # Python DAP adapters (one process per debug session)
│   ├── base.py                  #   DAPAdapter: transport, symbol parsing, common handlers
│   ├── zesarux.py               #   ZEsarUX (ZRCP) — Z80, fully implemented
│   ├── mame.py                  #   MAME (GDB RSP) — Z80, fully implemented
│   └── vice.py                  #   VICE (6502) — stub, not implemented
├── lua/nvim-dap-retro/
│   ├── init.lua                 # setup(), ext_map, dapui_layout, debug()
│   ├── memory.lua               # memory_dump dapui element
│   ├── build_log.lua            # build_log dapui element (preLaunchTask output)
│   └── adapters/
│       ├── zesarux.lua          # registers dap.adapters.zesarux + a default config
│       ├── mame.lua             # registers dap.adapters.mame
│       └── vice.lua             # registers dap.adapters.vice (no default config — see §4.2)
├── samples/                     # ready-to-debug example projects, one per toolchain
├── docker/cpctelera/            # Dockerized CPCTelera/SDCC toolchain for the C samples
├── utils/asm2cheader.py         # sprite/data → C header conversion helper for samples
├── README.md
└── TODO.md
```

## 3. High-level architecture

```
┌─────────┐   DAP (JSON over stdio)   ┌──────────────────┐   emulator-specific   ┌──────────┐
│ nvim-dap │◄─────────────────────────►│ adapters/*.py     │◄──────────────────────►│ emulator │
│ (Lua)    │                            │ (Python child     │  (ZRCP / GDB RSP over  │ (ZEsarUX,│
└─────────┘                            │  process)          │   a TCP socket)        │  MAME…)  │
                                        └──────────────────┘                        └──────────┘
```

- nvim-dap owns the DAP client side: it starts the adapter as declared in
  `dap.adapters.<type>` (`type = "executable"`, `command = "python3"`), and
  speaks standard DAP framing (`Content-Length` headers + JSON body) over
  the child's stdin/stdout.
- Each `adapters/*.py` is a small standalone DAP *server* (from nvim-dap's
  point of view) that happens to be implemented as a protocol translator:
  it holds a second connection to the actual emulator and turns DAP
  requests into that emulator's own remote-control protocol.
- All source/line/symbol knowledge lives in the Python adapter, parsed
  from whatever debug-info file the target's build produced — the
  emulator itself has no idea what a "line 42 of craft.c" is.

## 4. Lua layer

### 4.1 `init.lua` — setup and session dispatch

`M.setup(opts)` does four things:

1. Merges `opts.ext_map` into the default file-extension → adapter-name
   table (`M.ext_map`), e.g. `s = "zesarux"`, `a = "vice"`.
2. Calls each adapter's Lua `setup(dap)` (`adapters/zesarux.lua`,
   `adapters/vice.lua`, `adapters/mame.lua`), which registers
   `dap.adapters.<name>` (how to spawn the Python process) and, for some,
   a default `dap.configurations.<filetype>` entry.
3. Registers the `memory_dump` and `build_log` dapui elements (see 4.3/4.4)
   if `nvim-dap-ui` is installed, and hooks `dapui.open()` to fire on every
   `event_stopped`.
4. Calls `memory.setup(opts.memory)` and `build_log.setup()`.

`M.debug()` is the entry point bound to a keymap (conventionally
`<leader>dzr`). It does **not** use nvim-dap's normal filetype-keyed
`dap.configurations` lookup directly — retro asm/C extensions (`.z80`,
`.s80`, `.s`, `.c`, `.asm`...) don't map cleanly onto a single Neovim
filetype the way `dap.configurations.<filetype>` expects, so the plugin
does its own resolution instead:

1. Take the current buffer's extension (`%:e`), look it up in `M.ext_map`
   to get an adapter name (e.g. `s` → `zesarux`).
2. Look for `.debug/launch.json` (falling back to `.vscode/tasks.json` /
   `.vscode/launch.json` naming) in the cwd, and pick the first
   configuration whose `type` matches the adapter name.
3. If no `launch.json` is found, fall back to scanning nvim-dap's own
   `dap.configurations` table (across all filetypes) for one with a
   matching `type` — this is what lets `adapters/zesarux.lua`'s bundled
   default configuration (`dap.configurations.asm`) work with zero project
   config.
4. If the chosen configuration has a `preLaunchTask`, resolve that label
   in `tasks.json` and run it via `vim.fn.jobstart` (streaming output into
   the `build_log` panel), then call `dap.run(config)` on success. If it
   fails (non-zero exit), the session is not started.

### 4.2 Per-emulator adapter registration (`lua/nvim-dap-retro/adapters/*.lua`)

Each file is a thin `dap.adapters.<name> = { type = "executable", command
= "python3", args = { <path to adapters/*.py> } }` registration, plus
(for zesarux only) a default `dap.configurations` entry so a bare
`debug()` works without a `.debug/launch.json`. All three files locate
the plugin root dynamically (`debug.getinfo(1, "S").source`) so the
adapter script path works regardless of install location. `vice.lua`
deliberately registers no default `dap.configurations.asm` entry — VICE
is a 6502/Commodore adapter (still an unimplemented stub) and shouldn't
show up as a debug option for Z80/CPC `.asm` files via nvim-dap's
built-in config picker; per-project `.debug/launch.json` configs are
unaffected.

### 4.3 `memory.lua` — the memory dump panel

Registered as a custom dapui element (`memory_dump`) in `init.lua`.
Maintains one scratch buffer showing a hex+ASCII dump (16 bytes/row) of
`count` bytes (default 256) starting at an address — the config's
`loadAddress` if set, else the `load_address` passed to `setup()`.

- `refresh()` fires on every `event_stopped` (`dap.listeners.after.event_stopped`)
  and issues a DAP `readMemory` request; the response (base64) is decoded
  and rendered.
- `j`/`k`/`<C-f>`/`<C-b>` scroll the viewed window by 16 bytes / a full
  page; `e` prompts for a hex byte value under the cursor and issues a DAP
  `writeMemory` request, then refreshes.
- Byte-under-cursor math (`byte_addr_at_cursor`) is coupled to the exact
  `"%04X: %-47s  %s"` line format `hex_dump` produces — the hex column
  starts at character 6, each byte is 3 columns wide.

### 4.4 `build_log.lua` — preLaunchTask output panel

A second scratch buffer (`build_log` dapui element) that streams
`jobstart` stdout/stderr from the `preLaunchTask` build command. Handles
the fact that `jobstart` delivers output in arbitrary chunks with a
trailing `""` sentinel marking a chunk boundary — `M.append` merges a
pending partial line across calls so a line split mid-chunk doesn't render
as two lines, and auto-scrolls any window currently showing the buffer.

## 5. Python adapter layer

### 5.1 Transport (`base.py`)

`DAPAdapter` implements the DAP wire format directly (no external DAP
library):

- `read_message()` reads `Content-Length: N\r\n\r\n` + N bytes of JSON from
  stdin — standard DAP/LSP-style framing.
- `send(msg)` assigns the next `seq`, serializes to JSON, and writes the
  same framing to stdout, guarded by a lock (`_stdout_lock`) since
  responses can be sent from the monitor thread — see 6.4 — concurrently
  with the main loop).
- `main()` loops `read_message()` → `handle()` forever; any unhandled
  exception is logged and re-raised, killing the adapter (surfaced to
  nvim-dap as a dead subprocess rather than a silent hang).
- `handle(msg)` dispatches on `msg['command']` via the `HANDLERS` dict
  built in `__init__`.

### 5.2 Handler split: common vs. abstract

`base.py` implements the handlers that don't depend on which emulator is
attached — `initialize`, `threads`, `scopes`, `stackTrace`, `variables` —
by working entirely off `self.address_to_source`, `self.sld_map`,
`self.functions`, `self.local_vars`, and one abstract method,
`read_registers()`. Everything that actually has to talk to an emulator
(`handle_launch`, `handle_set_breakpoints`, `handle_configuration_done`,
`handle_read_memory`, `handle_write_memory`, `handle_evaluate`,
`handle_step`, `handle_continue`, `handle_disconnect`, plus
`read_registers`) is declared `raise NotImplementedError` in the base
class and implemented per-adapter. This is why `MameAdapter` and
`ZesaruxAdapter` share stack-trace/scopes/variables behavior byte-for-byte
despite talking completely different wire protocols to their emulators.

### 5.3 Symbol/debug-info parsing

This is the part of the codebase that knows about specific toolchains.
All of it produces the same two shapes regardless of source format:

- `line_to_addr` / `self.sld_map`: `{(basename, lineno): address}` — used
  to resolve a breakpoint's line to an address (`snap_to_valid_line` walks
  forward up to 20 lines looking for one with a known address, since a
  comment-only or blank line has no code and thus no address).
- `addr_to_line` / `self.address_to_source`: `{address: (basename,
  lineno)}` — used by `handle_stack_trace` to turn the emulator's current
  PC into a source location.

| Parser | Toolchain | Source file | Gives |
|---|---|---|---|
| `parse_sld` | sjasmplus | `.sld` (pipe-delimited) | line↔address for every source line |
| `parse_map` | SDCC (via ASxxxx/sdld) | `.map` | line↔address only for `C$...` records — i.e. only for C-compiled files, plus a load-address guess from `s__CODE` |
| `parse_ihx_load_address` | SDCC | `.ihx` | the *authoritative* load address (see 5.3.3) |
| `parse_cdb` | SDCC | `.cdb` | line↔address (redundant with `.map`'s `C$` records, but also function ranges and per-function local variable storage) |
| `parse_map_module_info` + `parse_lst` + `build_asm_line_map` | SDCC (hand-written `.s` linked alongside C) | `.map` + per-object `.lst` | line↔address for modules that never went through the C front end — see 5.3.4 |

#### 5.3.1 sjasmplus SLD (`parse_sld`)

Straight-line: every SLD record with type `T` (a code/data-emitting line)
gives `(filepath, line, ..., address, ...)`; `filepath` is exactly what
was passed to sjasmplus on the command line, which is **not guaranteed to
be absolute** — a build invoked from the project root often records
something like `src/hello.asm`. Caching that verbatim into `_path_cache`
used to silently override the correct absolute path
`handle_set_breakpoints` had already registered for the file whose
breakpoint triggered the parse; nvim would then try to open a path that
only resolves if its own cwd happened to match the build's cwd, and fail
silently — the source window would just show nothing. `parse_sld` now
resolves any non-absolute recorded path against the project root (the
`.sld` file's own grandparent directory, following the `build/<name>.sld`
convention used everywhere else in this codebase) before caching it.

#### 5.3.2 SDCC `.map` (`parse_map`)

With `--debug`, SDCC's linker (`sdld`/ASxxxx) embeds `C$<file>$<line>$<block>$<col>`
records in the `.map` file for every C-compiled line, plus (usually) an
`s__CODE` symbol marking where the `_CODE` area starts. `parse_map` only
extracts `C$` records — it has no line-level information for anything
assembled directly (hand-written `.s`), see 5.3.4.

#### 5.3.3 Load address resolution priority

There are three possible sources for "where in Z80 memory does byte 0 of
the binary go", tried in this order in `ZesaruxAdapter._load_binary`:

1. `loadAddress` in `launch.json`, if present — always wins, never
   second-guessed.
2. `.ihx` (`parse_ihx_load_address`) — the true minimum address across
   every Intel-HEX data record, which is exactly what `hex2bin` itself
   uses to place byte 0. Authoritative when present.
3. `s__CODE` from the `.map` file — a guess that only holds when `_CODE`
   is the lowest area in the linked image. It's wrong whenever a project
   links its own lower `(ABS)` area (e.g. a fixed-address `.incbin`), and
   ASxxxx reports such areas' addresses as `0` in every `.map` summary
   table, so there's no way to recover the true address from the `.map`
   alone in that case — hence preferring the `.ihx` when it exists.

#### 5.3.4 Hand-written `.s` modules with no C-level debug info (`build_asm_line_map`)

A project can mix SDCC-compiled `.c` files with hand-written `.s` files
assembled directly by `sdasz80` (e.g. performance-critical routines).
Those `.s` files never go through SDCC's C front end, so they get **no**
`C$` records in the `.map`, no `L:` line records in the `.cdb`, and not
even line records in their own per-object `.sym` file — every address
inside them is a miss in `address_to_source`. If the project builds with
`sdasz80 -l` (a listing file), the fix is to mine the `.lst` file
directly:

1. **`parse_map_module_info(map_path)`** walks the `.map` file's "Files
   Linked" table (`<obj>.rel  [ <module> ]`), in link order, and
   reconstructs each object's true linked base address by starting at
   `s__CODE` and accumulating each object's own `_CODE` area size (read
   from that object's `.sym` file's Area Table, e.g. `_CODE size B8`).
   This is deliberately **not** done by taking the minimum address the
   `.map`'s global symbol table's "module" column attributes to a module
   name, because a `.module` name is not guaranteed unique — one real
   project in `samples/` has both `wide_drawSolidBox.s` and
   `wide_drawSpriteMasked.s` declare `.module wide_sprites`, which makes
   that column unable to tell the two objects apart. Walking link order
   with per-object sizes sidesteps the ambiguity entirely, since it's
   keyed by object path, not by the (possibly shared) module name.
2. **`parse_lst(lst_path)`** parses the listing's fixed-column format
   (`<6-hex addr><bytes><[T-states]><line number><source text>`, with the
   line number occupying columns 34–40 of a 40-column prefix before the
   source text resumes) into `{relative_addr: line_num}`, restricted to
   the `_CODE` area block (relative addressing restarts at 0 for every
   `.area` directive in the listing, so mixing in `_DATA`/`_INITIALIZER`
   addresses would silently produce addresses indistinguishable from real
   `_CODE` ones — and code never executes out of those areas anyway).
   When a label and the instruction right after it share a relative
   address (labels emit no bytes of their own), the later (executable)
   line wins.
3. **`_resolve_module_source(obj_path, project_root)`** maps an object
   back to the `.s`/`.c` file it was assembled from: first by mirroring
   the object tree's structure under `src/` (swapping the object tree's
   top directory for `src`, keeping the rest of the path, probing `.s`/
   `.asm`/`.c`), then falling back to a one-time recursive filename search
   under the project root, keyed by object filename stem — not by
   `.module` name, for the same aliasing reason as step 1.
4. **`build_asm_line_map(map_path)`** combines the three into `{addr:
   (basename, line)}`, and is merged into `self.address_to_source` in
   `ZesaruxAdapter._load_binary` — but only to fill gaps
   (`if addr not in address_to_source`), since existing `C$`/`.cdb`
   entries are never wrong and should win on overlap.

#### 5.3.5 Stack trace fallback when no debug info exists at all

Even after all the above, some code legitimately has no source mapping —
library routines with no shipped source, or a project that doesn't build
`.lst` listings. `handle_stack_trace` (in `base.py`) tracks the last
successfully resolved `(source_path, line)` in `self._last_frame` and
reuses it whenever the current PC has no `address_to_source` entry,
instead of falling back to whatever file `_source_path` happens to hold
(the file the *last breakpoint* was set in — usually unrelated to the
code actually executing). Without this, single-stepping through
undebuggable code would make nvim's source window jump to an unrelated
file on every step; with it, it just holds position on the last known
location.

### 5.4 Register/locals helpers

- `_resolve_sdcc_reg(reg_name, regs)`: SDCC's `.cdb` names register-stored
  locals by single-byte (`a`,`b`,`c`,`d`,`e`,`h`,`l`) or pair
  (`bc`,`de`,`hl`,`ix`,`iy`) register names; the emulator only reports
  16-bit pairs, so single-byte names are extracted via the appropriate
  high/low byte of their pair.
- `_func_name_at(pc)` / `_locals_for_pc(pc, regs)`: linear scan of
  `self.functions` (populated from `.cdb`) to find the enclosing function,
  then render its locals — register-stored ones read straight from
  `regs`, stack-stored ones (`storage == 'B'`, an SP-relative offset) read
  one byte via `read_memory_bytes(sp + offset, 1)`, which subclasses
  implement.

## 6. `ZesaruxAdapter` (`adapters/zesarux.py`)

Talks to ZEsarUX over its ZRCP (ZEsarUX Remote Command Protocol) — a
plain-text, line-oriented TCP protocol on port 10000 by default. Every
command's response is terminated by a prompt, either `command>` (idle) or
`command@cpu-step>` (in single-step mode, which the adapter always puts
ZEsarUX into on launch via `enter-cpu-step`).

### 6.1 `zrcp()`: one locked send+receive round-trip, and why

ZRCP is a bare line protocol with no request/response correlation of its
own — whatever text arrives next on the socket is assumed to be the reply
to whatever was sent last, terminated by a `command>` or
`command@cpu-step>` prompt. That's only safe if commands are strictly
serialized one at a time. `handle_continue` deliberately breaks that
assumption on purpose (it sends `run` and returns without waiting — see
6.5), which means the main thread (handling `variables`/`readMemory` for
dapui panels) and the monitor thread (waiting for `run`'s eventual reply)
are two independent callers on the *same* socket. Without synchronization
this corrupts both sides: a reply meant for one thread gets consumed by
the other, or a `variables` request's `get-registers` lands while the
monitor is mid-wait and desyncs the whole exchange (this was an open,
unreproduced bug — see TODO.md — until caught and fixed via the ZEsarUX
session log for a real debugging session).

Every ZRCP command goes through **`zrcp(cmd, timeout=15.0)`**, which holds
`self._zrcp_lock` for the entire send *and* receive: no other thread's
command can be sent, and no other thread's reply can be read, until this
one is fully done. The one exception is `run` itself (see 6.5's
`_wait_for_stop`), which needs the lock held for as long as the debuggee
runs, not just for sending it.

`_read_response()` (the actual socket reader, always called under the
lock) keeps a persistent `self._recv_buffer` across calls rather than
discarding whatever's left after the first prompt it finds. This matters
because TCP has no message framing beyond that prompt string: if two full
replies land in the same `recv()` chunk — e.g. `run`'s long-deferred reply
immediately followed by a command that got queued behind it — a naive
"stop at the first prompt" reader would silently drop the second reply on
the floor. The leftover bytes are saved for the *next* call instead.

### 6.2 Launch (`handle_launch`)

Reads `program`/`sldFile`/`mapFile`/`cdbFile`/`loadAddress`/`zesaruxArgs`/
`zesaruxPath`/`zesaruxHost`/`zesaruxPort`/`stopOnExit` from the DAP launch
arguments. If `mapFile` is given and a same-named `.cdb` exists alongside
it, `cdbFile` is auto-detected. If `zesaruxArgs` is present and ZEsarUX
isn't already listening on the target port, it's launched
(`--noconfigfile --enable-remoteprotocol` are always prepended). Either
way, the adapter then connects, drains the welcome banner, and puts the
emulator into a known state: `hard-reset-cpu`, `enter-cpu-step`,
`set-debug-settings 0`, `clear-membreakpoints`, and disabling all 100
breakpoint slots in one bulk send (100 individual round-trips would be
needlessly slow).

### 6.3 Binary + symbol loading (`_load_binary`)

Runs once per session (guarded by `_setup_done`), triggered from the
first `setBreakpoints` request (so it has a concrete source path to
derive `build/<name>.bin` / `.sld` from, if `program`/`sldFile` weren't
given explicitly) and again, as a no-op past the guard, from
`configurationDone`. Order of operations: resolve paths → parse
`.map`/`.sld` → resolve load address (5.3.3) → parse `.cdb` if present and
merge into `sld_map`/`address_to_source` → fill remaining gaps from
`.lst` listings via `build_asm_line_map` (5.3.4) → `load-binary` over ZRCP
→ `enable-breakpoints`.

### 6.4 Breakpoints (`handle_set_breakpoints`)

Each requested line is snapped to the nearest valid line
(`snap_to_valid_line`) and resolved to an address via `sld_map`. ZEsarUX
breakpoint slots are 1-indexed and stateful across calls: the adapter
tracks `self._active_breakpoints` and, after setting the new set,
disables any slot that's no longer wanted (`new_indices` vs. the previous
set) rather than clearing and rebuilding everything — cheaper, and avoids
a window where no breakpoints are armed.

### 6.5 Stepping and continuing

`handle_step` sends `cpu-step` via `zrcp()` and waits synchronously — a
single step is fast and bounded, so there's no need for the async monitor
pattern. `handle_continue` is different: it starts a **daemon thread**
(`start_monitor` → `_monitor_breakpoint`) that calls **`_wait_for_stop()`**
— sends `run` and blocks for however long it takes to hit a breakpoint,
then sends the `event: stopped` DAP event. Unlike every other command,
`_wait_for_stop` holds `self._zrcp_lock` for the *entire* wait, not just
the send: ZRCP has no pipelining, so nothing else may be sent to ZEsarUX
while `run` is outstanding regardless of which thread wants to send it.

The practical effect: any other DAP request that needs ZRCP (`variables`,
`readMemory` for the memory panel, etc.) arriving while the debuggee is
free-running will simply block on the same lock until it stops — the main
DAP read loop doesn't get to service them concurrently, despite `run`
itself returning immediately from the socket's point of view. That's a
deliberate correctness trade-off (see 6.1): the earlier design let those
requests through concurrently, which is exactly what caused replies to be
misattributed between the monitor thread and the main thread. One
consequence worth knowing: `disconnect` arriving during a free run with no
breakpoint ever hitting can't currently preempt `_wait_for_stop` — see
TODO.md. Both the monitor thread and the main thread write to stdout,
hence the separate `_stdout_lock` in `base.py`.

Neither `_monitor_breakpoint` nor `handle_step` sends `close-all-menus`
after stopping — see the memory dump panel note below and the "wedges
ZEsarUX" warning in `handle_configuration_done`'s comment: doing so once
cpu-step mode is entered can drop out of it entirely, and ZEsarUX has no
native window to dismiss at those particular stop points anyway (only the
initial `enter-cpu-step` in `handle_launch` triggers it, so
`handle_launch`/`handle_configuration_done` are the only paths that call
it).

### 6.6 `evaluate` — T-state timing and a ZRCP escape hatch

The DAP debug console (`dap.repl`, or dapui's `repl` element) routes
through `handle_evaluate`, which recognizes three forms:

- `tstates reset` — `reset-tstates-partial`.
- `tstates` — `get-tstates-partial`, formatted as T-states and µs/ms at
  4MHz (or reports counter overflow).
- `zrcp <command>` — an escape hatch that sends `<command>` to ZRCP
  verbatim and returns the raw response, for anything not covered above
  (`get-registers`, `read-memory ...`, `cpu-step-over`, ...). This uses a
  **bounded** wait (5s timeout), unlike the normal
  `zesarux_recv_until_prompt` — a passthrough command like `run` with no
  breakpoint set would otherwise never produce a prompt and hang this
  handler (and, since `read_message()`/`handle()` are not re-entrant,
  every subsequent DAP request too).

This exists as a replacement for border-colour screenshot timing (setting
the border to a marker colour and measuring how many pixel rows it stayed
that colour) — the T-state counter gives an exact cycle count with no
display-geometry calibration, and works for code that never touches the
border (interrupt handlers, off-screen work).

### 6.7 Disconnect

Disables tracked breakpoints, clears membreakpoints, exits single-step
mode, closes the socket, and — if the adapter launched ZEsarUX itself and
`stopOnExit` wasn't set to `false` — terminates the emulator process.

## 7. `MameAdapter` (`adapters/mame.py`)

Structurally a mirror of `ZesaruxAdapter` but speaks the **GDB Remote
Serial Protocol** to MAME's built-in `gdbstub` (`-debugger gdbstub -debug
-debugger_port PORT`) instead of ZRCP:

- `gdb_send`/`gdb_recv`/`gdb_cmd` implement the `$<packet>#<checksum>`
  framing and single-byte `+` ACK exchange.
- Registers come back as one packed hex blob from the `g` command; a
  fixed `_REG_NAMES` order (`AF, BC, DE, HL, IX, IY, SP, PC, AF', BC', DE',
  HL'`) decodes it, each register little-endian across 4 hex chars.
  Writing registers (`_set_pc`) round-trips through `read_registers` to
  avoid clobbering registers the caller didn't intend to change, then
  sends a full `G<blob>`.
- No `load-binary` equivalent exists in the protocol — `handle_configuration_done`
  writes the binary directly into target memory via GDB `M` (write
  memory) packets, then sets `PC` to the load address.
- Breakpoints use GDB `Z0,<addr>,1` / `z0,<addr>,1` (insert/remove
  software breakpoint), tracked in `self._active_breakpoints` keyed by
  `(basename, line)`.
- Stepping (`s`) and continuing (`c`) both block on `gdb_recv()` for the
  stop reply — continuing does so from a monitor thread, same rationale
  as 6.5.
- `handle_disconnect` sends `k` (kill) — MAME's gdbstub does not support
  reconnecting to the same instance ([MAME #9578](https://github.com/mamedev/mame/issues/9578)),
  so the adapter always terminates MAME on disconnect rather than leaving
  it attachable.

Uses the same `parse_sld`/symbol infrastructure as ZEsarUX (`.map`/`.cdb`
support has not been wired into `MameAdapter` yet — see TODO).

## 8. `VICEAdapter` (`adapters/vice.py`)

Not implemented. The file is a 10-line asyncio stub; `.a`/`.65s` are
mapped to it in the default `ext_map` for when it lands, but nothing
launches today if you use them. VICE exposes a binary monitor protocol
over TCP that a real implementation would speak, analogous to ZRCP/GDB
RSP above.

## 9. Debug session lifecycle

```
Neovim (dap.run)                Adapter (Python child process)              Emulator
      │                                  │                                      │
      │──initialize───────────────────►│                                      │
      │◄─────────────────initialized───│                                      │
      │──launch(program,sldFile,...)──►│──(spawn if needed)──────────────────►│
      │                                  │──connect, reset, enter-cpu-step─────►│
      │◄──────────────────launch resp──│                                      │
      │──setBreakpoints────────────────►│──_load_binary (parse symbols,        │
      │                                  │   load-binary, enable-breakpoints)──►│
      │                                  │──set-breakpoint per line────────────►│
      │◄────────────────setBP resp─────│                                      │
      │──configurationDone─────────────►│──set PC (to entry/main)─────────────►│
      │◄───────event: stopped(entry)───│                                      │
      │──stackTrace/scopes/variables───►│──get-registers / read-memory────────►│
      │◄────────────────────responses──│◄─────────────────────────────────────│
      │──continue──────────────────────►│──run (async monitor thread)────────►│
      │                                  │            ⋮ (emulator runs freely) │
      │◄──────────event: stopped───────│◄──breakpoint hit────────────────────│
      │──(readMemory for memory panel)─►│──read-memory─────────────────────────►│
      │──disconnect────────────────────►│──disable BPs, exit-cpu-step─────────►│
      │                                  │  (terminate emulator if launched it) │
```

`handle_configuration_done` in `ZesaruxAdapter` synthesizes its own
`event: stopped` (reason `entry`) right after loading — there is no
"natural" DAP stop yet at that point, since the emulator hasn't run
anything; this is what makes the source window show the entry point
immediately after a launch, before you've pressed continue.

## 10. Extending the plugin

### 10.1 Adding a new toolchain's symbol format

Write a `parse_<format>(self, path)` method on `DAPAdapter` (or, if it
needs per-object files the way `.lst` does, a small family of methods —
see 5.3.4) returning `(line_to_addr, addr_to_line)` in the same shapes as
`parse_sld`/`parse_map`, and wire it into the relevant adapter's
`_load_binary`-equivalent alongside the existing format detection. Keep
in mind: paths recorded in third-party debug-info files are not
guaranteed absolute (5.3.1) — resolve against a known-good anchor (the
debug-info file's own location, or an already-registered `_source_path`)
rather than trusting them as-is.

### 10.2 Adding a new emulator adapter

1. `adapters/<name>.py`: subclass `DAPAdapter`, implement the abstract
   methods listed in 5.2, call `<Name>Adapter().main()` under
   `if __name__ == '__main__':`.
2. `lua/nvim-dap-retro/adapters/<name>.lua`: register
   `dap.adapters.<name> = { type = "executable", command = "python3",
   args = { <path to the .py> } }`, plus an optional default
   `dap.configurations` entry.
3. Call the new Lua module's `setup(dap)` from `init.lua`'s `M.setup`.
4. Add file extensions to the default `M.ext_map` in `init.lua` if
   appropriate.

## 11. Logs and troubleshooting

Each adapter logs every DAP message and every command sent to/received
from the emulator to a fixed path (`/tmp/zesarux-dap.log`,
`/tmp/mame-dap.log`), via `logging.basicConfig` set up in
`DAPAdapter.__init__`. `tail -f` it while debugging a session — this is
almost always the fastest way to see what actually happened (e.g. which
address a breakpoint was snapped to, what the emulator's raw
`get-registers` response was, or the exact request/response pair around a
symptom like a blank source window: check the `stackTrace` response's
`source.path` and cross-reference against `_path_cache`/`_source_path`
logic in 5.3).

## 12. Known gaps

See [TODO.md](TODO.md) for the full list. Notably: a manually-managed
ZEsarUX instance (no `zesaruxArgs`) is still one shared process by the
user's own choice, so a second concurrent `debug()` call against it can
corrupt the first session's state (`hard-reset-cpu` on launch resets the
machine out from under it) — self-launched instances are already isolated
per-session (see §Infrastructure in TODO.md); and there is no automated
test suite for the symbol parsers yet.
