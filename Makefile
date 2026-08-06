SRC := fancontrol/ tests/
CLEAN := .pytest_cache/ .ruff_cache/ __pycache__/ dist/
UNIT := fanctl

.PHONY: help
help:
	@echo "Usage: $(MAKE) <install | test | lint | format | service | logs | clean>"
	@echo ""
	@echo "  install      install fanctl onto PATH, editable, with uv tool"
	@echo "  sync         create .venv and install deps from uv.lock"
	@echo "  test         run the test suite"
	@echo "  lint         format, then check"
	@echo "  format       apply ruff formatting and fixes"
	@echo "  check        clean, lint and test"
	@echo ""
	@echo "  service      install the user unit and enable it"
	@echo "  start stop restart   control the running daemon"
	@echo "  logs         follow the daemon's journal"
	@echo "  status       device readings, then service state"
	@echo ""
	@echo "  clean        remove caches and build artifacts"

# Editable, so edits here take effect without reinstalling. The daemon runs
# from this same entry point, so it picks them up on its next restart.
.PHONY: install
install:
	uv tool install --editable . --force

.PHONY: sync
sync:
	uv sync

.PHONY: format
format:
	uv run --group dev ruff format $(SRC)
	uv run --group dev ruff check --fix $(SRC)

.PHONY: lint
lint: format
	uv run --group dev ruff check $(SRC)

.PHONY: test
test:
	uv run --group dev pytest -q

.PHONY: check
check: clean lint test

# --- the user service -------------------------------------------------
# Rootless throughout: udev grants uaccess on both hidraw nodes, so nothing
# here needs privilege and none of these targets should ever want sudo.

.PHONY: service
service: install
	install -Dm644 systemd/$(UNIT).service $(HOME)/.config/systemd/user/$(UNIT).service
	systemctl --user daemon-reload
	systemctl --user enable --now $(UNIT)
	@systemctl --user --no-pager status $(UNIT) | head -3

.PHONY: start
start:
	systemctl --user start $(UNIT)

# Stopping raises the fans to fallback_duty: once the daemon is gone nothing
# manages cooling, and the hub holds its last duty indefinitely.
.PHONY: stop
stop:
	systemctl --user stop $(UNIT)

.PHONY: restart
restart:
	systemctl --user restart $(UNIT)

.PHONY: logs
logs:
	journalctl --user -u $(UNIT) -f

.PHONY: status
status:
	@uv run fanctl status
	@echo
	@systemctl --user --no-pager status $(UNIT) | head -3

.PHONY: clean
clean:
	@rm -rf $(CLEAN)
	@find $(SRC) -type d -name __pycache__ -print0 | xargs -0 -n1 -P0 rm -rf
