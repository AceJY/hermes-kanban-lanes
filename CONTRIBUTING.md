# Contributing to Hermes Kanban Lanes

Thanks for helping improve Hermes Kanban Lanes. This project is a
configuration, diagnostics, and execution kernel for a Hermes Kanban lane; keep
changes focused on that scope.

## Development environment

- **Python 3.13 or newer.**
- Clone the repository and run commands from the checkout. The test
  configuration puts `src` on the import path, so plain `pytest` works without
  installing the package.
- The product has no runtime dependencies of its own, but it imports modules
  from a Hermes host at run time. For manual CLI use, build a wheel and install
  it into the same Python environment as the Hermes host; a standalone virtual
  environment without the host dependencies is not a supported installation
  form.

## Running tests

Run the unit tests first:

```sh
python -m pytest tests/unit -q
```

Run the full suite from a checkout:

```sh
python -m pytest tests -q
```

Run the offline wheel build and out-of-repository installation test:

```sh
python -m pytest tests/unit/test_package_install.py -q
```

Live service tests are opt-in, macOS-only, and can install and remove a
user-level service in an isolated HOME:

```sh
HKL_LIVE_SERVICE=1 python -m pytest tests/live/test_w6_service.py -q
```

Review what a live test will do before running it. Do not run live tests
against a service you depend on.

## Making changes

- Open an issue before larger behavioral changes so the scope can be agreed
  first.
- Create a branch from `main` and keep one logical change per commit.
- Preserve the machine-readable contract: JSON-only output, the documented
  exit codes, and fail-closed behavior. v0.1 has no human-readable mode.
- Update both `README.md` and `README.zh-CN.md` when user-facing behavior,
  configuration, or commands change.
- Add or update tests for behavior changes. Do not delete assertions to make a
  test pass.
- Do not modify unrelated files or reformat code outside the change.

## Commit messages

- Use an imperative subject line, for example `docs: document exit codes`.
- Keep the subject short; put the reason for the change and the tests you ran
  in the body.
- Do not commit generated artifacts, local environment files, or editor
  metadata.

## Never commit credentials

- Do not commit tokens, API keys, authentication files, private keys, session
  cookies, or provider credentials of any kind.
- Do not commit personal absolute paths or machine names. Use placeholder
  paths such as `/absolute/path/to/...` in examples and documentation.
- If a secret has already been committed, rotate it and treat it as exposed;
  removing it from the latest commit is not enough.
- Review the staged diff before every commit and before opening a pull request.

## Pull requests

- Describe the problem, the change, and any behavior or compatibility impact.
- List the commands you ran and their results.
- Confirm `python -m pytest tests -q` passes.
- Keep the diff free of unrelated changes. A maintainer may ask for a focused
  follow-up instead of mixing concerns.
