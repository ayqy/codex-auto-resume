.DEFAULT_GOAL := today

PYTHON ?= python3
APP_DIR := $(CURDIR)
TMP_DIR := $(APP_DIR)/tmp
LOG_DIR := $(TMP_DIR)/logs

TOKEN_DETAIL_FILE ?= $(shell mktemp /tmp/codex-token-usage.XXXXXX.txt 2>/dev/null || mktemp -t codex-token-usage)

.PHONY: today run once status clean test-sample chmod force

today:
	@mkdir -p "$(LOG_DIR)"
	@DETAIL_FILE="$(TOKEN_DETAIL_FILE)"; \
	$(PYTHON) "$(APP_DIR)/scripts/codex_token_usage.py" --today --summary-only --detail-file "$$DETAIL_FILE"

run:
	@mkdir -p "$(LOG_DIR)"
	@$(PYTHON) "$(APP_DIR)/scripts/watch_usage_limit.py"

once:
	@mkdir -p "$(LOG_DIR)"
	@$(PYTHON) "$(APP_DIR)/scripts/watch_usage_limit.py" --once

status:
	@mkdir -p "$(LOG_DIR)"
	@$(PYTHON) "$(APP_DIR)/scripts/watch_usage_limit.py" --status

test-sample:
	@mkdir -p "$(LOG_DIR)"
	@$(PYTHON) "$(APP_DIR)/scripts/watch_usage_limit.py" --test-sample

force:
	@mkdir -p "$(LOG_DIR)"
	@$(PYTHON) "$(APP_DIR)/scripts/watch_usage_limit.py" --force-latest

clean:
	@rm -rf "$(TMP_DIR)"
	@mkdir -p "$(TMP_DIR)/schedules" "$(TMP_DIR)/logs"

chmod:
	@chmod +x "$(APP_DIR)/scripts/"*.sh
