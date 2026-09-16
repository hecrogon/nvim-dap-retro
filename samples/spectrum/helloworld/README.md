# Hello World — ZX Spectrum 48K

A minimal Z80 assembly program for the ZX Spectrum 48K. Prints `HELLO` via the ROM's `RST 0x10` print routine, then returns.

## Requirements

- [sjasmplus](https://github.com/z00m128/sjasmplus) assembler
- [ZEsarUX](https://github.com/chernandezba/zesarux) emulator

## Structure

```
helloworld/
├── src/
│   └── hello.asm       Z80 assembly source
└── .debug/
    ├── launch.json     DAP debug configuration
    └── tasks.json      Build task (calls sjasmplus)
```

## Build

Assemble with sjasmplus:

```sh
mkdir build
sjasmplus --sld=build/hello.sld --fullpath src/hello.asm
```

The `.debug/tasks.json` already runs this command when you start a debug session.

## Debug

Open `src/hello.asm` in Neovim and run `<leader>dzr`. The adapter will launch ZEsarUX with `--machine 48k` automatically, load the binary at `0x8000`, and stop at the first breakpoint.

## Notes

- The program is loaded and entered directly at `0x8000`, bypassing BASIC entirely — the same approach the Amstrad CPC samples use. `0x8000` is comfortably clear of the screen (`0x4000`–`0x57FF`), attributes (`0x5800`–`0x5AFF`), and system variables — a standard, safe load address for a small machine-code demo.
- **`main` starts with `LD IY,0x5C3A` and a `CHAN-OPEN` call (`LD A,2` / `CALL 0x1601`) before doing anything else — this is required, not decorative.** The adapter's launch sequence (`hard-reset-cpu` then immediately `enter-cpu-step`) freezes the CPU partway through the ROM's own cold-boot code, at a point that varies run to run, and *before* the ROM has set up `IY` to point at its system-variables table. Almost every ROM routine — `RST 0x10` included — addresses cursor position, current attributes, and channel/stream state via `(IY+offset)`, so calling one with `IY` still garbage reads and writes the wrong memory instead of printing anything visible. Re-doing this minimal bit of the ROM's own init (`IY` + open the screen channel) before using `RST 0x10` is the standard fix for bare-metal Spectrum code that skips BASIC, and is what makes this sample's output reliable regardless of exactly where the boot got interrupted.
