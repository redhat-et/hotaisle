# AGENTS

- Python >=3.9, stdlib only at runtime. Do not add runtime dependencies to
  `hotaisle/`; optional extras go under `[project.optional-dependencies]`
  in `pyproject.toml`.
- Format with `black .` before committing (default 88-char line length;
  config in `pyproject.toml`). Run `black --check .` to verify.
- Tests use stdlib `unittest`, not pytest:

      python -m unittest discover -s tests

  pytest is not installed and must not be introduced.
- Tests run fully offline against fake HTTP servers in `tests/`; never
  make real network calls from tests.
- The live API bills real money. Never run `vm create|delete|action`,
  `bm create|delete|action`, or `hotaisle raw POST|PATCH|DELETE` against a
  live account unless the user explicitly asks. Read-only commands
  (`list`, `get`, `state`, `raw GET`, ...) are fine.
- Run the CLI from the source tree with `bin/hotaisle`.
