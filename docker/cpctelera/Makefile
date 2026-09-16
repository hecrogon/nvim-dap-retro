IMAGE  := cpctelera
REPO   := https://github.com/lronaldo/cpctelera
BRANCH := master

.PHONY: build clean

build:
	docker build \
		--build-arg CPCTELERA_REPO=$(REPO) \
		--build-arg CPCTELERA_BRANCH=$(BRANCH) \
		-t $(IMAGE) .

clean:
	docker rmi $(IMAGE)
