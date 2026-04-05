# Hello World — Amstrad CPC 6128

A minimal Z80 assembly program for the Amstrad CPC 6128. Clears the screen, prints `HELLO`, draws a few pixels, then waits for a keypress.

## Requirements

- [sjasmplus](https://github.com/z00m128/sjasmplus) assembler
- [ZEsarUX](https://github.com/chernandezba/zesarux) emulator

## Structure

```
helloworld/
├── src/
│   └── hello.z80       Z80 assembly source
└── .debug/
    ├── launch.json     DAP debug configuration
    └── tasks.json      Build task (calls make build)
```

## Build

Assemble with sjasmplus:

```sh
mkdir build
sjasmplus --sld=build/hello.sld --fullpath src/hello.z80
```

The `.debug/tasks.json` already runs this command when you start a debug session.

## Debug

Open `src/hello.z80` in Neovim and run `<leader>dzr`. The adapter will launch ZEsarUX automatically, load the binary at `0x4000`, and stop at the first breakpoint.
