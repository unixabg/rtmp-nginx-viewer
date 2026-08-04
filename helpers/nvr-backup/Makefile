# Makefile for nvr-backup
#
# nvr-backup is a workstation-side tool, not a server component. It installs
# to the machine you run backups *from*, not to the NVR hosts.
#
#   make install          install to /usr/local/bin (needs sudo)
#   make install-user     install to ~/.local/bin (no sudo)
#   make uninstall        remove from /usr/local/bin
#   make uninstall-user   remove from ~/.local/bin
#   make check            syntax + shellcheck
#   make config           create ~/.config/nvr-backup with a sample hosts file
#   make tag              create an annotated git tag from VERSION
#   make version          print the version embedded in the script

SHELL     := /bin/bash
SCRIPT    := nvr-backup
VERSION   := $(shell sed -n 's/^VERSION="\(.*\)"/\1/p' $(SCRIPT))

PREFIX    ?= /usr/local
BINDIR    ?= $(PREFIX)/bin
DOCDIR    ?= $(PREFIX)/share/doc/$(SCRIPT)

USER_BINDIR ?= $(HOME)/.local/bin
CONFIG_DIR  ?= $(if $(XDG_CONFIG_HOME),$(XDG_CONFIG_HOME),$(HOME)/.config)/$(SCRIPT)

INSTALL   ?= install

.PHONY: all help install install-user uninstall uninstall-user \
        check config tag version clean

all: help

help:
	@sed -n '3,/^$$/s/^# \{0,1\}//p' Makefile

version:
	@echo "$(SCRIPT) $(VERSION)"

# --- Install ---------------------------------------------------------------

install: check
	$(INSTALL) -d $(DESTDIR)$(BINDIR)
	$(INSTALL) -m 755 $(SCRIPT) $(DESTDIR)$(BINDIR)/$(SCRIPT)
	@if [ -f README.md ]; then \
		$(INSTALL) -d $(DESTDIR)$(DOCDIR); \
		$(INSTALL) -m 644 README.md $(DESTDIR)$(DOCDIR)/README.md; \
	fi
	@echo
	@echo "Installed $(SCRIPT) $(VERSION) to $(DESTDIR)$(BINDIR)"
	@echo "Next: make config"

install-user: check
	$(INSTALL) -d $(USER_BINDIR)
	$(INSTALL) -m 755 $(SCRIPT) $(USER_BINDIR)/$(SCRIPT)
	@echo
	@echo "Installed $(SCRIPT) $(VERSION) to $(USER_BINDIR)"
	@case ":$$PATH:" in \
		*":$(USER_BINDIR):"*) ;; \
		*) echo "WARNING: $(USER_BINDIR) is not on your PATH" ;; \
	esac
	@echo "Next: make config"

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/$(SCRIPT)
	rm -rf $(DESTDIR)$(DOCDIR)
	@echo "Removed $(SCRIPT) from $(DESTDIR)$(BINDIR)"
	@echo "Note: $(CONFIG_DIR) and your backups were left in place."

uninstall-user:
	rm -f $(USER_BINDIR)/$(SCRIPT)
	@echo "Removed $(SCRIPT) from $(USER_BINDIR)"
	@echo "Note: $(CONFIG_DIR) and your backups were left in place."

# --- Config scaffold -------------------------------------------------------

config:
	@mkdir -p $(CONFIG_DIR)
	@if [ -e $(CONFIG_DIR)/hosts ]; then \
		echo "$(CONFIG_DIR)/hosts already exists, leaving it alone."; \
	else \
		printf '%s\n' \
			'# nvr-backup hosts — one per line.' \
			'# Blank lines and # comments are ignored.' \
			'# Anything ssh accepts works, including ~/.ssh/config aliases.' \
			'#' \
			'#your-server1' \
			'#your-server2' \
			'#user@nvr.example.com' \
			> $(CONFIG_DIR)/hosts; \
		chmod 600 $(CONFIG_DIR)/hosts; \
		echo "Created $(CONFIG_DIR)/hosts — edit it and uncomment your hosts."; \
	fi
	@echo
	@echo "Each NVR host also needs a passwordless sudo rule for tar:"
	@echo "  echo \"$$(id -un) ALL=(root) NOPASSWD: /usr/bin/tar\" | sudo tee /etc/sudoers.d/nvr-backup"
	@echo "  sudo chmod 0440 /etc/sudoers.d/nvr-backup"

# --- Checks ----------------------------------------------------------------

check:
	@bash -n $(SCRIPT) && echo "syntax OK"
	@if command -v shellcheck >/dev/null 2>&1; then \
		shellcheck -S warning $(SCRIPT) && echo "shellcheck OK"; \
	else \
		echo "shellcheck not installed, skipping"; \
	fi
	@test -n "$(VERSION)" || { echo "ERROR: no VERSION found in $(SCRIPT)"; exit 1; }

# --- Release ---------------------------------------------------------------

tag: check
	@test -z "$$(git status --porcelain)" || \
		{ echo "ERROR: working tree is dirty, commit before tagging."; exit 1; }
	@git rev-parse -q --verify "refs/tags/v$(VERSION)" >/dev/null && \
		{ echo "ERROR: tag v$(VERSION) already exists. Bump VERSION in $(SCRIPT)."; exit 1; } || true
	git tag -a "v$(VERSION)" -m "$(SCRIPT) $(VERSION)"
	@echo "Created tag v$(VERSION). Push it with: git push origin v$(VERSION)"

clean:
	@echo "Nothing to clean."
