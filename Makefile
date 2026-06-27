.DEFAULT_GOAL := today

PYTHON ?= python3
APP_DIR := $(CURDIR)
TMP_DIR := $(APP_DIR)/tmp
LOG_DIR := $(TMP_DIR)/logs

TOKEN_DETAIL_FILE ?= $(shell mktemp /tmp/codex-token-usage.XXXXXX.txt 2>/dev/null || mktemp -t codex-token-usage)
DEBUG_ARGS ?= --debug-limit-history --days 7

.PHONY: today run status debug clean chmod test

today:
	@mkdir -p "$(LOG_DIR)"
	@DETAIL_FILE="$(TOKEN_DETAIL_FILE)"; \
	$(PYTHON) "$(APP_DIR)/scripts/codex_token_usage.py" --today --summary-only --detail-file "$$DETAIL_FILE"

run:
	@mkdir -p "$(LOG_DIR)"
	@$(PYTHON) "$(APP_DIR)/scripts/watch_usage_limit.py"

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
