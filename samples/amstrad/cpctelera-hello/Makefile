ifdef CPCT_PATH
# Running inside Docker or with a local CPCTelera install
include cfg/build_config.mk
include $(CPCT_PATH)cfg/global_main_makefile.mk
else
# No local install — delegate to Docker
IMAGE   := cpctelera
RUN     := docker run --rm -v "$(CURDIR)":/project $(IMAGE)

build:
	$(RUN) make

clean:
	$(RUN) make clean
endif
