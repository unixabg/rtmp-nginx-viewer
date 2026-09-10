# Makefile for nvr-backup
#
# nvr-backup is a workstation-side tool, not a server component. It installs
# to the machine you run backups *from*, not to the NVR hosts.
#
#   make install          install to /usr/local/bin (needs sudo)
#   make install-user     install to ~/.local/bin (no sudo)
#   make upgrade          refresh every installed copy in place, keep config
#   make where            show where nvr-backup is installed and which one runs
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

.PHONY: all help install install-user upgrade where uninstall uninstall-user \
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

# --- Upgrade ---------------------------------------------------------------
#
# Refreshes whatever is already installed, wherever it is, without changing
# how it was installed. Your hosts file and backups are never touched.
#
# Two things this deliberately does more than a plain re-copy:
#
#   * If both a system and a user copy exist, it upgrades both. PATH order
#     decides which one actually runs, and a half-upgraded pair is a nasty
#     way to spend an afternoon.
#   * It diffs REMOTE_PATHS between old and new and reports what changed,
#     because a new path is silently ignored on any host whose
#     authorized_keys pins a forced tar command.

# Pull the REMOTE_PATHS array out of a copy of the script. Works on older
# versions too, which is the point -- it can't rely on 'nvr-backup -p'.
define extract_paths
awk '/^REMOTE_PATHS=\(/{f=1;next} f&&/^\)/{exit} f' "$(1)" \
  | sed 's/#.*//' | tr -d ' \t' | grep -v '^$$' | sort
endef

upgrade: check
	@found=""; \
	for d in "$(DESTDIR)$(BINDIR)" "$(USER_BINDIR)"; do \
		[ -f "$$d/$(SCRIPT)" ] && found="$$found $$d"; \
	done; \
	onpath=$$(command -v $(SCRIPT) 2>/dev/null || true); \
	if [ -n "$$onpath" ]; then \
		pd=$$(cd "$$(dirname "$$onpath")" && pwd); \
		case " $$found " in *" $$pd "*) ;; *) found="$$found $$pd" ;; esac; \
	fi; \
	if [ -z "$$found" ]; then \
		echo "ERROR: $(SCRIPT) is not installed in $(BINDIR) or $(USER_BINDIR),"; \
		echo "       and nothing named $(SCRIPT) is on your PATH."; \
		echo "       Run 'make install' or 'make install-user' first."; \
		exit 1; \
	fi; \
	primary="$$onpath"; \
	[ -n "$$primary" ] || primary="$$(set -- $$found; echo $$1)/$(SCRIPT)"; \
	oldpaths=$$(mktemp); newpaths=$$(mktemp); \
	trap 'rm -f "$$oldpaths" "$$newpaths"' EXIT; \
	$(call extract_paths,$$primary) > "$$oldpaths" 2>/dev/null || true; \
	$(call extract_paths,$(SCRIPT)) > "$$newpaths"; \
	n=0; for d in $$found; do n=$$((n+1)); done; \
	if [ "$$n" -gt 1 ]; then \
		echo "Found $$n installed copies -- upgrading all of them so PATH order"; \
		echo "stops mattering. '$$primary' is the one that runs today."; \
		echo; \
	fi; \
	for d in $$found; do \
		old=$$(sed -n 's/^VERSION="\(.*\)"/\1/p' "$$d/$(SCRIPT)" 2>/dev/null); \
		[ -n "$$old" ] || old="unknown"; \
		if [ -w "$$d" ]; then SUDO=""; else SUDO="sudo"; \
			echo "$$d is not writable by you -- using sudo"; fi; \
		$$SUDO $(INSTALL) -m 755 $(SCRIPT) "$$d/$(SCRIPT)"; \
		if [ "$$old" = "$(VERSION)" ]; then \
			echo "  $$d/$(SCRIPT): refreshed, still $(VERSION)"; \
		else \
			echo "  $$d/$(SCRIPT): $$old -> $(VERSION)"; \
		fi; \
		docdir="$$(dirname "$$d")/share/doc/$(SCRIPT)"; \
		if [ -d "$$docdir" ] && [ -f README.md ]; then \
			$$SUDO $(INSTALL) -m 644 README.md "$$docdir/README.md"; \
			echo "  $$docdir/README.md: refreshed"; \
		fi; \
	done; \
	echo; \
	if cmp -s "$$oldpaths" "$$newpaths"; then \
		echo "REMOTE_PATHS unchanged."; \
	else \
		echo "REMOTE_PATHS CHANGED:"; \
		added=$$(comm -13 "$$oldpaths" "$$newpaths"); \
		removed=$$(comm -23 "$$oldpaths" "$$newpaths"); \
		[ -n "$$added" ]   && echo "$$added"   | sed 's/^/  + /'; \
		[ -n "$$removed" ] && echo "$$removed" | sed 's/^/  - /'; \
		echo; \
		if [ -n "$$added" ]; then \
			echo "  If any NVR host pins a forced command in authorized_keys, that"; \
			echo "  command still carries the old path list and will silently skip"; \
			echo "  the new paths. Check with:"; \
			echo "    ssh <host> 'grep $(SCRIPT) ~/.ssh/authorized_keys'"; \
		fi; \
		echo "  Confirm what this copy now pulls with:  $(SCRIPT) -p"; \
	fi; \
	echo; \
	echo "$(CONFIG_DIR) was not touched."

where:
	@found=0; \
	for d in "$(DESTDIR)$(BINDIR)" "$(USER_BINDIR)"; do \
		if [ -f "$$d/$(SCRIPT)" ]; then \
			found=1; \
			v=$$(sed -n 's/^VERSION="\(.*\)"/\1/p' "$$d/$(SCRIPT)" 2>/dev/null); \
			[ -n "$$v" ] || v="unknown"; \
			printf '  %-40s %s\n' "$$d/$(SCRIPT)" "$$v"; \
		fi; \
	done; \
	[ "$$found" -eq 1 ] || echo "  (not installed in $(BINDIR) or $(USER_BINDIR))"; \
	echo; \
	onpath=$$(command -v $(SCRIPT) 2>/dev/null || true); \
	if [ -n "$$onpath" ]; then \
		echo "PATH resolves $(SCRIPT) to: $$onpath"; \
	else \
		echo "$(SCRIPT) is not on your PATH."; \
	fi; \
	echo "Repo copy is $(VERSION)."

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
