#!make
include scripts/env/default.env
ifneq (,$(wildcard ./scripts/env/override.env))
	include ./scripts/env/override.env
endif

# Image name; can be overridden via env.
IMAGE ?= $(TUONI_MCP_IMAGE)

# A standalone clone contains everything required for a frozen build.
BUILD_CONTEXT = .
DOCKERFILE = scripts/docker/Dockerfile

# Common Linux docker-run flags. Host networking lets a container reach the C2
# that is bound to localhost:8443 on the OpenCode host. --env-file pulls credentials.
RUN_FLAGS = --rm \
    --read-only --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:rw,nosuid,nodev,size=64m \
    --network host \
    --env-file scripts/env/default.env

ifneq (,$(wildcard ./scripts/env/override.env))
RUN_FLAGS += --env-file scripts/env/override.env
endif

export

.DEFAULT_GOAL := help

## help: list available targets
.PHONY: help
help:
	@echo "[*] tuoni-mcp development and container targets"
	@awk '/^## / { sub(/^## /, ""); split($$0, fields, ": "); printf "  %-14s %s\n", fields[1], fields[2] }' $(MAKEFILE_LIST)

## lock-check: verify pyproject.toml and uv.lock agree
.PHONY: lock-check
lock-check:
	uv lock --check

## sync: install the frozen development environment
.PHONY: sync
sync: lock-check
	uv sync --frozen --all-extras

## build: build the frozen production image (no development extras)
.PHONY: build
build:
	docker build --target production -f $(DOCKERFILE) -t $(IMAGE) $(BUILD_CONTEXT)

## build-test: build the frozen test image (includes development extras)
.PHONY: build-test
build-test:
	docker build --target test -f $(DOCKERFILE) -t $(IMAGE)-test $(BUILD_CONTEXT)

## run: launch the MCP server interactively over stdio (foreground)
.PHONY: run
run: build
	docker run -i $(RUN_FLAGS) $(IMAGE)

## test: run the unit test suite inside the frozen test image
.PHONY: test
test: build-test
	docker run $(RUN_FLAGS) $(IMAGE)-test -v tests

## shell: open a shell inside the production image (for debugging)
.PHONY: shell
shell: build
	docker run -it $(RUN_FLAGS) --entrypoint /bin/bash $(IMAGE)

## inspect: launch the MCP Inspector against a containerised tuoni-mcp
.PHONY: inspect
inspect: build
	npx @modelcontextprotocol/inspector \
		docker run -i $(RUN_FLAGS) $(IMAGE)

## env: copy scripts/env/override.env.example to override.env if it doesn't exist
.PHONY: env
env:
	@if [ ! -f scripts/env/override.env ]; then \
		cp scripts/env/override.env.example scripts/env/override.env; \
		echo "[*] created scripts/env/override.env - fill in TUONI_URL/USERNAME/PASSWORD"; \
	else \
		echo "[*] scripts/env/override.env already exists"; \
	fi

## clean: remove the docker image
.PHONY: clean
clean:
	-docker rmi $(IMAGE) $(IMAGE)-test

## clean-local: remove local environments, build outputs, and Python caches
.PHONY: clean-local
clean-local:
	rm -rf .venv build dist .pytest_cache .ruff_cache .mypy_cache
	find src tests evaluation scripts .agents -type d -name __pycache__ -prune -exec rm -rf {} +
