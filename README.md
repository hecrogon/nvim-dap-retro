# nvim-dap-retro

A [nvim-dap](https://github.com/mfussenegger/nvim-dap) plugin for debugging retro-computing targets. Provides a DAP adapter for Z80 via [ZEsarUX](https://github.com/chernandezba/zesarux), a live memory dump panel, and dapui integration.

## Requirements

- Neovim >= 0.9
- [nvim-dap](https://github.com/mfussenegger/nvim-dap)
- [nvim-dap-ui](https://github.com/rcarriga/nvim-dap-ui) (optional, for the UI panels)
- [nvim-nio](https://github.com/nvim-neotest/nvim-nio) (required by nvim-dap-ui)
- Python 3 (for the adapter scripts)
- [ZEsarUX](https://github.com/chernandezba/zesarux)
- [sjasmplus](https://github.com/z00m128/sjasmplus) — the only assembler currently supported, as the adapter relies on its [SLD debug symbol format](https://github.com/z00m128/sjasmplus/blob/master/documentation/SLD.md) for line↔address mapping. Support for other assemblers (pasmo, nasm, etc.) would require adding a parser for their symbol/map output.

## Installation

Using [lazy.nvim](https://github.com/folke/lazy.nvim):

```lua
{
  "hecrogon/nvim-dap-retro",
  dependencies = {
    "mfussenegger/nvim-dap",
    "rcarriga/nvim-dap-ui",
    "nvim-neotest/nvim-nio",
  },
  config = function()
    require("nvim-dap-retro").setup()
  end,
}
```

> **Note:** if you use nvim-dap-ui, let nvim-dap-retro call `dapui.setup()` — it owns the layout config. Remove any standalone `dapui.setup()` call from your dapui plugin config.

## Setup

```lua
require("nvim-dap-retro").setup({
  -- Override the file extension → adapter mapping
  ext_map = {
    z80 = "zesarux",
    s80 = "zesarux",
    asm = "zesarux",
  },
  -- Memory dump panel options
  memory = {
    load_address = 0x4000,  -- fallback if not set in launch.json
    count        = 256,     -- bytes to display
  },
})
```

## Starting ZEsarUX

The adapter can launch and close ZEsarUX automatically. Add a `zesaruxArgs` field to your `launch.json` with the extra flags for your target machine — the adapter prepends `--noconfigfile --enable-remoteprotocol` automatically:

```json
"zesaruxArgs": ["--machine", "cpc6128", "--snap-no-change-machine"]
```

ZEsarUX will be started when the debug session begins and terminated when it ends.

If you prefer to manage ZEsarUX yourself, omit `zesaruxArgs` and start it manually before debugging:

```sh
zesarux --noconfigfile --machine cpc6128 --snap-no-change-machine --enable-remoteprotocol
```

| Flag | Description |
|------|-------------|
| `--noconfigfile` | Skip user config, ensures a clean predictable state |
| `--machine cpc6128` | Target machine (change to match your target) |
| `--snap-no-change-machine` | Prevent snapshot loading from switching the machine type |
| `--disable-autoframeskip` | Render every video frame — required for screen updates to appear during ZRCP execution |
| `--enable-remoteprotocol` | Enable ZRCP on port 10000 (required) |

The adapter connects to `localhost:10000`.

## Project configuration

The plugin looks for debug configuration in `.debug/launch.json`. Example:

**.debug/launch.json**
```json
{
  "configurations": [
    {
      "type": "zesarux",
      "request": "launch",
      "name": "ZEsarUX Debug",
      "program": "${workspaceFolder}/build/main.bin",
      "sldFile": "${workspaceFolder}/build/main.sld",
      "loadAddress": "0x4000",
      "zesaruxArgs": ["--machine", "cpc6128", "--snap-no-change-machine"],
      "preLaunchTask": "build"
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `program` | Path to the compiled binary. Inferred from the source filename if omitted. |
| `sldFile` | Path to the SLD debug symbols file. Inferred from the source filename if omitted. |
| `loadAddress` | Address where the binary is loaded in Z80 memory (hex or decimal). Defaults to `0x4000`. Used by both the adapter and the memory dump panel. |
| `zesaruxArgs` | Extra flags passed to ZEsarUX. When present, the adapter launches ZEsarUX automatically. `--noconfigfile` and `--enable-remoteprotocol` are always prepended. |
| `zesaruxPath` | Path to the ZEsarUX binary. Defaults to `zesarux` (assumed to be in `$PATH`). |
| `zesaruxHost` | Host where ZEsarUX is running. Defaults to `localhost`. |
| `zesaruxPort` | ZRCP port. Defaults to `10000`. |
| `preLaunchTask` | Label of a task in `tasks.json` to run before launching. |

**.debug/tasks.json**
```json
{
  "tasks": [
    {
      "label": "build",
      "command": "sh",
      "args": ["-c", "mkdir -p build && sjasmplus --sld=build/main.sld --fullpath src/main.z80"]
    }
  ]
}
```

If no `.debug/launch.json` is found, the plugin falls back to any matching DAP configuration already registered for the adapter.

## Usage

Call `require("nvim-dap-retro").debug()` from a source file — the plugin selects the adapter based on the file extension and starts the debug session, running `preLaunchTask` first if configured.

### Keymaps

```lua
local map = vim.keymap.set

-- Start debug session (auto-detects adapter from file extension)
map("n", "<leader>dzr", function() require("nvim-dap-retro").debug() end, { desc = "DAP Retro Debug" })

-- Standard DAP controls
map("n", "<leader>db",  function() require("dap").toggle_breakpoint() end, { desc = "DAP Toggle Breakpoint" })
map("n", "<leader>dc",  function() require("dap").continue()          end, { desc = "DAP Continue" })
map("n", "<leader>dov", function() require("dap").step_over()         end, { desc = "DAP Step Over" })
map("n", "<leader>di",  function() require("dap").step_into()         end, { desc = "DAP Step Into" })
map("n", "<leader>dou", function() require("dap").step_out()          end, { desc = "DAP Step Out" })
```

## Memory dump panel

When nvim-dap-ui is present, a `memory_dump` element is registered and can be added to any layout:

```lua
-- In your dapui_layout config:
{
  elements = { "memory_dump" },
  size = 10,
  position = "bottom",
}
```

The panel displays a hex dump of `count` bytes starting at the load address, updated on every breakpoint stop. The address is read from `loadAddress` in the project's `launch.json` at runtime, falling back to the `load_address` value in `setup()` if not set. `count` is configured via `setup()` only.

## File extension mapping

| Extension | Adapter |
|-----------|---------|
| `.z80`    | zesarux |
| `.s80`    | zesarux |
| `.asm`    | zesarux |

Custom mappings can be added via `opts.ext_map` in `setup()`.

## Samples

The `samples/` directory contains ready-to-debug projects:

| Sample | Target | Description |
|--------|--------|-------------|
| `amstrad/helloworld` | Amstrad CPC 6128 | Prints `HELLO` and draws pixels |

Each sample includes the Z80 source and a `.debug/` configuration. Bring your own build system — the samples use `sjasmplus` but any assembler that produces a `.bin` + `.sld` pair will work.

<!-- ## MAME adapter (not yet implemented)

The MAME adapter speaks the GDB Remote Serial Protocol to MAME's built-in gdbstub. It supports any MAME system that exposes a Z80 CPU.

### Starting MAME

Add a `mameArgs` field to your `launch.json` with the system name and any extra flags — the adapter appends `-debugger gdbstub -debug -debugger_port PORT` automatically:

```json
"mameArgs": ["cpc6128", "-window"]
```

MAME will be started when the debug session begins and sent the `k` (kill) command on disconnect.

If you prefer to manage MAME yourself, omit `mameArgs` and start it manually:

```sh
mame cpc6128 -window -debugger gdbstub -debug -debugger_port 2159
```

### MAME launch.json example

```json
{
  "configurations": [
    {
      "type": "mame",
      "request": "launch",
      "name": "MAME Debug",
      "program": "${workspaceFolder}/build/main.bin",
      "sldFile": "${workspaceFolder}/build/main.sld",
      "loadAddress": "0x4000",
      "mameArgs": ["cpc6128", "-window"],
      "mamePort": 2159
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `program` | Binary to load into RAM at `loadAddress`. Optional — omit if MAME already has the program. |
| `sldFile` | SLD debug symbols file. Inferred from source filename if omitted. |
| `loadAddress` | Address where the binary is written (hex or decimal). Defaults to `0`. |
| `mameArgs` | System name + extra flags passed to MAME. When present, the adapter launches MAME automatically. `-debugger gdbstub -debug -debugger_port PORT` are always appended. |
| `mamePort` | GDB stub port. Defaults to `2159`. |
| `mamePath` | Path to the MAME binary. Defaults to `mame` (assumed to be in `$PATH`). |

> **Note:** MAME's gdbstub does not support reconnection (MAME bug [#9578](https://github.com/mamedev/mame/issues/9578)). The adapter sends `k` on disconnect to terminate MAME. Restart MAME between sessions.

### File extension mapping for MAME

MAME supports many platforms — no default extension mapping is provided. Add your own in `setup()`:

```lua
require("nvim-dap-retro").setup({
  ext_map = {
    z80 = "mame",
  },
})
```
-->

## Troubleshooting

The adapter logs all DAP messages and emulator traffic to `/tmp/zesarux-dap.log`. Tail it while debugging:

```sh
tail -f /tmp/zesarux-dap.log
```
