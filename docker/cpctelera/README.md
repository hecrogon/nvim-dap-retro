# CPCTelera Docker image

A self-contained build environment for [CPCTelera](https://github.com/lronaldo/cpctelera)
projects, replacing the original `setup.sh` with a reproducible Docker image.

Based on Debian bookworm-slim. Includes:

| Tool | Purpose |
|---|---|
| `sdcc` / `sdasz80` / `sdar` | Z80 compiler, assembler, archiver |
| `hex2bin` | Intel HEX → binary converter |
| `iDSK` | DSK disk image creator |
| `2cdt` | CDT cassette image creator |
| `cpctelera.lib` | CPCTelera Z80 library |
| `cpct_*` scripts | CPCTelera helper scripts |

> **Note:** `img2cpc`, `rgas`, and `arkosTracker` (asset conversion tools) are not
> included. They can be added later if needed.

## Build the image

```bash
make -C docker/cpctelera
```

The build clones CPCTelera from GitHub, compiles the bundled tools, and builds
the library. Docker layer caching means subsequent builds are fast.

To target a specific branch or fork, override the Makefile variables:

```bash
make -C docker/cpctelera REPO=https://github.com/yourfork/cpctelera BRANCH=main
```

To remove the image:

```bash
make -C docker/cpctelera clean
```

## Build a project

From your project root (where the `Makefile` lives):

```bash
docker run --rm -v "$PWD":/project cpctelera
```

The image default command is `make`. Pass targets explicitly if needed:

```bash
docker run --rm -v "$PWD":/project cpctelera make clean
docker run --rm -v "$PWD":/project cpctelera make $(TARGET)
```

## Project structure

A CPCTelera project must follow the standard layout:

```
myproject/
├── Makefile
├── cfg/
│   └── build_config.mk
└── src/
    └── main.c
```

`Makefile` should be the standard two-liner:

```makefile
include cfg/build_config.mk
include $(CPCT_PATH)cfg/global_main_makefile.mk
```

`CPCT_PATH` is baked into the image and points to the CPCTelera installation.
No environment setup is needed on the host.

## Design notes

### Why not run `setup.sh`?

The original `setup.sh`:
- Permanently modifies `~/.bashrc` with hardcoded absolute paths
- Compiles SDCC 3.6.8 from source (old, slow, and incompatible with musl libc)
- Requires system-level libraries (`mono`, `FreeImage`, `boost`) on the host
- Breaks if the CPCTelera directory is moved

The Dockerfile avoids all of this by building each tool explicitly in isolated
layers and baking the environment into the image.

### SDCC version

CPCTelera bundles SDCC 3.6.8-r9946 and expects it compiled at a specific path
within its own directory tree. Rather than building that old version from source,
the image uses Debian's maintained `sdcc` package and symlinks it to the path
CPCTelera expects:

```
tools/sdcc-3.6.8-r9946/bin/sdcc    → /usr/bin/sdcc
tools/sdcc-3.6.8-r9946/bin/sdasz80 → /usr/bin/sdasz80
tools/sdcc-3.6.8-r9946/bin/sdar    → /usr/bin/sdar
```

The CPCTelera library is rebuilt from source inside the image using this same
compiler, so everything is consistent.
