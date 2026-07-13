.DEFAULT_GOAL := today

PYTHON ?= python3
APP_DIR := $(CURDIR)
TMP_DIR := $(APP_DIR)/tmp
LOG_DIR := $(TMP_DIR)/logs

DEBUG_ARGS ?= --debug-limit-history --days 7
D ?=
N ?= 30
F ?=

.PHONY: today usage recent run check status debug clean chmod test config proxy workat resume

today: usage

usage:
	@mkdir -p "$(LOG_DIR)"
	@set --; \
	if [ -n "$(D)" ]; then \
		set -- "$$@" -d "$(D)"; \
	else \
		set -- "$$@" -t; \
	fi; \
	if [ -n "$(F)" ]; then \
		set -- "$$@" -f "$(F)"; \
	fi; \
	$(PYTHON) "$(APP_DIR)/scripts/codex_token_usage.py" "$$@"

recent:
	@mkdir -p "$(LOG_DIR)"
	@set -- -r -n "$(N)"; \
	if [ -n "$(F)" ]; then \
		set -- "$$@" -f "$(F)"; \
	fi; \
	$(PYTHON) "$(APP_DIR)/scripts/codex_token_usage.py" "$$@"

run:
	@mkdir -p "$(LOG_DIR)"
	@$(PYTHON) "$(APP_DIR)/scripts/watch_usage_limit.py"

check:
	@mkdir -p "$(LOG_DIR)"
	@"$(APP_DIR)/scripts/run_workat_prewarm.sh"

config:
	@$(PYTHON) "$(APP_DIR)/scripts/configure_config.py" $(filter-out $@,$(MAKECMDGOALS))

status:
	@mkdir -p "$(LOG_DIR)"
	@$(PYTHON) "$(APP_DIR)/scripts/watch_usage_limit.py" --status

debug:
	@mkdir -p "$(LOG_DIR)"
	@$(PYTHON) "$(APP_DIR)/scripts/watch_usage_limit.py" $(DEBUG_ARGS)

test:
	@$(PYTHON) -m pytest -q

clean:
	@rm -rf "$(TMP_DIR)"
	@mkdir -p "$(TMP_DIR)/schedules" "$(TMP_DIR)/logs"

chmod:
	@chmod +x "$(APP_DIR)/scripts/"*.sh

proxy:
	@if [ -n "$(filter config,$(MAKECMDGOALS))" ]; then \
		:; \
	else \
		echo "usage: make config $@"; \
		exit 1; \
	fi

workat:
	@if [ -n "$(filter config,$(MAKECMDGOALS))" ]; then \
		:; \
	else \
		echo "usage: make config $@"; \
		exit 1; \
	fi

resume:
	@if [ -n "$(filter config,$(MAKECMDGOALS))" ]; then \
		:; \
	else \
		echo "usage: make config $@"; \
		exit 1; \
	fi
