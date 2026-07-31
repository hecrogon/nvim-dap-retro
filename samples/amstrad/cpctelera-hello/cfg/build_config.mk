####
## SECTION 1: Project configuration
####
PROJNAME   := hello
Z80CODELOC := 0x4000

# Folders
SRCDIR  := src
OBJDIR  := obj

# File extensions
C_EXT   := c
ASM_EXT := s
OBJ_EXT := rel

# Binary output files
BINFILE := $(OBJDIR)/$(PROJNAME).bin
IHXFILE := $(OBJDIR)/$(PROJNAME).ihx
CDT     := $(PROJNAME).cdt
DSK     := $(PROJNAME).dsk

# Build target (DSK only; add $(CDT) for cassette image too)
TARGET  := $(DSK)

####
## SECTION 2: Tool path configuration (defines CPCT_SRC, CPCT_LIB, tool binaries)
####
include $(CPCT_PATH)cfg/global_paths.mk

####
## SECTION 3: Compilation flags
## --debug enables .cdb and .map output for debugger integration
####
# --sdcccall=0: use stack-based calling convention (SDCC 3.x compatible)
# CPCTelera's assembly library expects arguments on the stack, not in registers.
# SDCC 4.x defaults to --sdcccall=1 (register-based), which breaks all library calls.
Z80CCFLAGS    := --sdcccall 0 --debug
Z80ASMFLAGS   := -l -o -s
Z80CCINCLUDE  := -I$(CPCT_SRC) -I$(SRCDIR)
Z80CCLINKARGS := -mz80 --no-std-crt0 -Wl-u \
                 --code-loc $(Z80CODELOC) \
                 --data-loc 0 -l$(CPCT_LIB) \
                 --debug

####
## SECTION 4: Source file discovery and object file path computation
####
include $(CPCT_PATH)cfg/global_functions.mk

SUBDIRS    := $(filter-out ., $(shell find $(SRCDIR) -type d -print))
OBJSUBDIRS := $(foreach DIR, $(SUBDIRS), $(patsubst $(SRCDIR)%, $(OBJDIR)%, $(DIR)))

CFILES     := $(foreach DIR, $(SUBDIRS), $(wildcard $(DIR)/*.$(C_EXT)))
ASMFILES   := $(foreach DIR, $(SUBDIRS), $(wildcard $(DIR)/*.$(ASM_EXT)))

C_OBJFILES   := $(patsubst $(SRCDIR)%, $(OBJDIR)%, $(patsubst %.$(C_EXT),   %.$(OBJ_EXT), $(CFILES)))
ASM_OBJFILES := $(patsubst $(SRCDIR)%, $(OBJDIR)%, $(patsubst %.$(ASM_EXT), %.$(OBJ_EXT), $(ASMFILES)))
OBJFILES     := $(C_OBJFILES) $(ASM_OBJFILES)
