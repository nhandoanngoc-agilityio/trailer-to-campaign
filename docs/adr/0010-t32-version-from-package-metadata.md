# T32: `t2c --version` prints the package version

**Ticket:** T32 (issue #86), "`t2c --version` prints the package version".

## Decision

`t2c --version` prints `trailer-to-campaign <version>` to stdout and exits 0. The version comes from the installed package metadata (`importlib.metadata.version`), through a new `package_version()` helper in `src/trailer_to_campaign/cli.py`. If there is no metadata, the helper returns `unknown`. The flag is an argparse `version` action at the top level only, so it runs before the subcommand check and before settings load. It needs no API key and no valid environment. Six tests in `tests/test_cli.py` cover it. `README.md` and the `cli.py` docstring each gain one usage line. `docs/tickets/T32.md` and a `docs/lessons.md` row record the ticket.

## Reasons

- The ticket rules out a hard-coded version, so the metadata is the single source. The standard library reads it, so no dependency is added.
- argparse's `version` action exits while parsing. A broken environment can't stop `--version`, and the flag shows in `--help` with no extra work.
- The `unknown` fallback keeps every other command working when the code runs from source, and it never invents a number. Because the parser reads the metadata on every call, a lookup that raised would break every command.
- Long form only: `-V` would sit too close to `-v` (verbose), and the ticket does not ask for it.
- The tests compare against the metadata and against `pyproject.toml`, not a literal, so a version bump won't break them and stale metadata gets caught.

## Tests

`uv run pytest`: 1305 passed, 569 failed or errored, 2 skipped, 1 deselected, exit code 1. All 569 failures are known failures that were already there before this change, none are new, and the new `test_cli.py` tests pass. The failures are in `test_hooks.py`, `test_preflight.py`, `test_claude_setup.py`, `test_film_files.py` and `test_review_checklist.py`. They come from the run copy lacking the `.claude/` setup.

## Not done

- No `__version__` attribute, version bumping, or release or tag process.
- No short `-V` form, and no `--version` on subcommands.
- No change to `pyproject.toml`, `src/trailer_to_campaign/__init__.py`, the deploy files or CI.
- No glossary entry in `CONTEXT.md`, since the change needs no new domain term.
