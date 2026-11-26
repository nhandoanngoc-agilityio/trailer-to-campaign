# T32 — `t2c --version` prints the package version

**Ticket:** T32 (#86), `t2c --version` prints the package version.

## Decision

`t2c` gets a top-level `--version` flag. It prints `trailer-to-campaign <version>` to stdout and exits 0. The version comes from the installed package metadata (`importlib.metadata`). If the package isn't installed, it prints `unknown`. The flag is handled while the arguments are parsed, so it runs before logging or settings load. It needs no API key, no films folder and no valid `FILM_BUDGET_USD` or `T2C_WATCHER`.

Files: `src/trailer_to_campaign/cli.py` (a `DISTRIBUTION` constant, a `package_version()` helper, the `--version` argument and a docstring line), `tests/test_cli.py` (five new tests), `README.md` (one line in the command block), `docs/tickets/T32.md` (the ticket record) and `docs/lessons.md` (one lesson row).

## Reasons

- `importlib.metadata` is in the standard library, so no dependency is added. Reading the version from metadata keeps `pyproject.toml` as the only place it is set.
- argparse's `version` action exits before the required-subcommand check and before `load_settings`. A user can therefore check the version even when the environment is wrong.
- Printing `unknown` when the metadata is missing means `build_parser()` can't fail, so no other command breaks.
- Only `--version` is added, with no `-V`, because that is the only spelling the ticket names. Stdout follows argparse on 3.11+ and the Unix convention.
- The tests compare the output with the metadata value, not a literal. One test patches the lookup to `9.9.9` to prove the version isn't hard-coded. Another passes bad settings to prove the flag exits before they are read.

## Tests

`uv run pytest`: 1304 passed, 391 failed, 178 errors, 2 skipped, 1 deselected (exit 1). The report says all 569 failures and errors match the baseline by name. They come from the `.claude/` directory missing in the test environment, in `test_hooks.py`, `test_preflight.py`, `test_claude_setup.py`, `test_film_files.py` and `test_review_checklist.py`. None are in `test_cli.py`. Five more tests pass than in the baseline (1304 against 1299), which matches the five new tests.

## Not done

- No short `-V` alias, and no `t2c <subcommand> --version`. `t2c run --version` is still a usage error.
- No `__version__` attribute in `__init__.py`. `pyproject.toml`, the version and the build backend are unchanged.
- No subprocess test of the installed command. `test_t2c_is_installed_as_a_command` already covers that wiring.
