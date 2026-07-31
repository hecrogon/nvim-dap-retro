# CPCTelera build environment
#
# Generates image with the full CPCTelera env:
#   sdcc / sdasz80 / sdar  – Z80 compiler, assembler, archiver
#   hex2bin                – Intel HEX → binary
#   iDSK                   – DSK disk image creator
#   2cdt                   – CDT cassette image creator
#   cpctelera.lib          – CPCTelera Z80 library
#   tools/scripts/         – cpct_* helper scripts
#
# Usage (from your project root):
#   docker build -t cpctelera docker/cpctelera/
#   docker run --rm -v "$PWD":/project cpctelera make
#
# The CPCT_PATH env var is baked in; your project Makefile just needs:
#   include $(CPCT_PATH)cfg/global_main_makefile.mk

# ── Build stage ───────────────────────────────────────────────────────────────
FROM debian:bookworm-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    g++ \
    sdcc \
    git \
    ca-certificates \
    make \
    bash \
    perl \
    python3 \
 && rm -rf /var/lib/apt/lists/*

ARG CPCTELERA_REPO=https://github.com/lronaldo/cpctelera
ARG CPCTELERA_BRANCH=master

RUN git clone --depth=1 --branch "${CPCTELERA_BRANCH}" "${CPCTELERA_REPO}" /opt/cpctelera

# Symlink Debian's SDCC binaries to where CPCTelera's global_paths.mk expects them.
# Avoids building the bundled SDCC 3.6.8 from source.
RUN mkdir -p /opt/cpctelera/cpctelera/tools/sdcc-3.6.8-r9946/bin \
 && ln -s /usr/bin/sdcc    /opt/cpctelera/cpctelera/tools/sdcc-3.6.8-r9946/bin/sdcc \
 && ln -s /usr/bin/sdasz80 /opt/cpctelera/cpctelera/tools/sdcc-3.6.8-r9946/bin/sdasz80 \
 && ln -s /usr/bin/sdar    /opt/cpctelera/cpctelera/tools/sdcc-3.6.8-r9946/bin/sdar

# Build hex2bin  →  tools/hex2bin-2.0/bin/hex2bin
WORKDIR /opt/cpctelera/cpctelera/tools/hex2bin-2.0
RUN make

# Build iDSK  →  tools/iDSK-0.13/bin/iDSK
# Makefile bug: obj/ and bin/ directories are not auto-created
WORKDIR /opt/cpctelera/cpctelera/tools/iDSK-0.13
RUN mkdir -p obj bin && make

# Build 2cdt  →  tools/2cdt/bin/2cdt
WORKDIR /opt/cpctelera/cpctelera/tools/2cdt
RUN mkdir -p obj bin && make

# Build the CPCTelera Z80 library  →  cpctelera/cpctelera.lib
# global_paths.mk resolves CPCT_PATH from its own location, no env var needed here
WORKDIR /opt/cpctelera/cpctelera
RUN make

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    sdcc \
    make \
    bash \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/cpctelera /opt/cpctelera

ENV CPCT_PATH=/opt/cpctelera/cpctelera/
ENV PATH="${PATH}:/opt/cpctelera/cpctelera/tools/scripts"

WORKDIR /project
CMD ["make"]
