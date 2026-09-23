.DEFAULT_GOAL := today

PYTHON ?= $(if $(wildcard $(HOME)/.venv/bin/python3),$(HOME)/.venv/bin/python3,python3)
APP_DIR := $(CURDIR)
TMP_DIR := $(APP_DIR)/tmp
LOG_DIR := $(TMP_DIR)/logs

DEBUG_ARGS ?= --debug-limit-history --days 7
D ?=
N ?= 30
F ?=
ID ?=
export ID F
USAGE_RANGE_ARG ?= -t

.PHONY: today yesterday usage recent session run check status debug clean chmod test config proxy workat resume reset

today: USAGE_RANGE_ARG := -t
yesterday: USAGE_RANGE_ARG := -y
today yesterday: usage

usage:
	@mkdir -p "$(LOG_DIR)"
	@set --; \
	if [ -n "$(D)" ]; then \
		set -- "$$@" -d "$(D)"; \
	else \
		set -- "$$@" "$(USAGE_RANGE_ARG)"; \
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

session:
	@if [ -z "$$ID" ]; then echo "usage: make session ID=<session-uuid>" >&2; exit 2; fi
	@mkdir -p "$(LOG_DIR)"
	@set -- --session-id "$$ID"; \
	if [ -n "$$F" ]; then \
		set -- "$$@" -f "$$F"; \
	fi; \
	$(PYTHON) "$(APP_DIR)/scripts/codex_token_usage.py" "$$@"

run:
	@mkdir -p "$(LOG_DIR)"
	@$(PYTHON) "$(APP_DIR)/scripts/watch_usage_limit.py"

check:
	@mkdir -p "$(LOG_DIR)"
	@"$(APP_DIR)/scripts/run_workat_prewarm.sh"

reset:
	@$(PYTHON) "$(APP_DIR)/scripts/reset_credit_workflow.py"

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
