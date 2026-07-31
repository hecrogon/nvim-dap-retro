#!/usr/bin/env python3
"""Convert a sjasmplus data-only .asm file to a C header + source pair.

Handles:
  name equ value      →  #define NAME value          (in .h)
  label:              →  extern const unsigned char label[];  (in .h)
      db 0x.., 0x..       const unsigned char label[] = { ... };  (in .c)

Usage:
  python3 asm2cheader.py input.asm [output_stem]
  (output_stem defaults to input stem; produces output_stem.h and output_stem.c)
"""

import sys
import re
from pathlib import Path


def convert(asm_path, out_stem=None):
    asm_path = Path(asm_path)
    if out_stem:
        h_path = Path(out_stem).with_suffix('.h')
        c_path = Path(out_stem).with_suffix('.c')
    else:
        h_path = asm_path.with_suffix('.h')
        c_path = asm_path.with_suffix('.c')

    defines = []          # [(name, value)]
    arrays  = []          # [(name, [value_strings])]
    current = None        # current array being accumulated

    for raw in asm_path.read_text().splitlines():
        line = raw.strip()

        # skip blank lines and comments
        if not line or line.startswith(';'):
            continue

        # strip inline comments
        line = line.split(';')[0].strip()
        if not line:
            continue

        # equ constant:  name equ value
        m = re.match(r'^(\w+)\s+equ\s+(\S+)$', line, re.IGNORECASE)
        if m:
            defines.append((m.group(1), m.group(2)))
            continue

        # label:
        m = re.match(r'^(\w+):$', line)
        if m:
            current = []
            arrays.append((m.group(1), current))
            continue

        # db  value, value, ...
        m = re.match(r'^db\s+(.+)$', line, re.IGNORECASE)
        if m and current is not None:
            values = [v.strip() for v in m.group(1).split(',') if v.strip()]
            current.extend(values)
            continue

    # ── emit .h ──────────────────────────────────────────────────────────────
    guard = h_path.stem.upper().replace('-', '_') + '_H'
    h_lines = [
        f'#ifndef {guard}',
        f'#define {guard}',
        '',
    ]

    for name, value in defines:
        h_lines.append(f'#define {name.upper()} {value}')
    if defines:
        h_lines.append('')

    for name, _ in arrays:
        h_lines.append(f'extern const unsigned char {name}[];')
    if arrays:
        h_lines.append('')

    h_lines.append(f'#endif /* {guard} */')
    h_lines.append('')

    h_path.write_text('\n'.join(h_lines))

    # ── emit .c ──────────────────────────────────────────────────────────────
    c_lines = [
        f'#include "{h_path.name}"',
        '',
    ]

    for name, values in arrays:
        c_lines.append(f'const unsigned char {name}[] = {{')
        for i in range(0, len(values), 16):
            chunk = values[i:i + 16]
            c_lines.append('    ' + ', '.join(chunk) + ',')
        c_lines.append('};')
        c_lines.append('')

    c_path.write_text('\n'.join(c_lines))

    print(f'{asm_path} -> {h_path}, {c_path}  ({len(arrays)} arrays, {len(defines)} defines)')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    convert(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
