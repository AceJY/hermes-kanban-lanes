# Hermes Kanban Lanes

**English** · [中文](README.zh-CN.md)


Hermes Kanban Lanes is a fail-closed command-line tool for operating a Hermes
Kanban lane. Its subcommands validate configuration and cards (`validate-config`,
`validate-card`), diagnose one lane (`doctor`), run a single tick or a polling
daemon (`tick`, `run`), and render or manage the user-level service unit
(`service`).

This is `0.1.0.dev0`. Only the installation forms and platform promises listed
below are claimed; treat everything else as unverified.

## What this is — and is not

- It is **not Hermes itself**. It runs on top of a Hermes host checkout and
  imports the host's Kanban database modules with the interpreter that runs the
  product.
- It does **not** take over, proxy, or manage the Hermes Gateway. It operates on
  a Kanban board inside the configured Hermes host.
- It is not a web service, a remote API, or a credential store. All subcommand
  output is JSON; `--version` and `--help` do not emit JSON. There is no
  human-readable mode in v0.1.
- It does not create boards, repositories, or host environments. It validates
  and operates on what you configure.

## Install

### Prerequisites

- **Python 3.13 or newer.**
- **Install into the same Python environment as the Hermes host.** The product
  imports host modules from `hermes_source` using the interpreter that runs it,
  so the host's own dependencies must be installed in that interpreter. A
  standalone Python virtual environment without the host dependencies is not a
  supported installation form, even though the wheel declares no Python
  dependencies of its own.
- A Hermes host checkout at the commit named by `hermes_commit`.
- A Kanban board SQLite database containing a `tasks` table.
- A trusted git repository for the lane to work in.
- The executor binaries named in `executors` (`claude` and/or `codex`) on
  `PATH`, or absolute paths to them.

Platform promises:

- **macOS launchd**: the user-level lifecycle (`install`, `status`, `restart`,
  `uninstall`) is the claimed service platform.
- **systemd**: unit files can be rendered, but systemd is **render-only**.
  Lifecycle commands fail closed with guidance and never invoke `systemctl`.
- **Windows**: not supported.

### Install the package

The project repository is <https://github.com/AceJY/hermes-kanban-lanes>.

From a wheel:

```sh
python -m pip install /path/to/hermes_kanban_lanes-0.1.0.dev0-py3-none-any.whl
```

From a source checkout:

```sh
python -m pip wheel . --no-build-isolation -w dist
python -m pip install dist/hermes_kanban_lanes-*.whl
```

Confirm the installed distribution:

```sh
hermes-kanban-lanes --version
```

The command prints the installed version (for example `0.1.0.dev0`) and exits
`0`.

## Quick start

1. Copy `examples/config.json` to a local file and replace every
   `/absolute/path/to/...` value with a real absolute local path. Set
   `hermes_commit` to the host commit:

   ```sh
   git -C /absolute/path/to/hermes-agent rev-parse HEAD
   ```

2. Validate the configuration document:

   ```sh
   hermes-kanban-lanes validate-config /absolute/path/to/config.json
   ```

   Success prints `{"valid": true, "schema_version": 1, "kind": "config"}` and
   exits `0`. Validation checks structure, types, and bounds only; it does not
   require the configured paths to exist.

3. Diagnose the configured lane. `doctor` is read-only: it does not claim a
   card, write the board or configuration, start the daemon or a service, or
   run a model. It does start one short-lived probe interpreter to validate the
   host import chain.

   ```sh
   hermes-kanban-lanes doctor --config /absolute/path/to/config.json --json
   ```

   A green report (`"ok": true`, exit `0`) means every configured path, board,
   host commit, and executor resolved. Fix each failing check before
   continuing.

4. Run exactly one tick:

   ```sh
   hermes-kanban-lanes tick --config /absolute/path/to/config.json --json
   ```

   Exit `0` means the tick report contained no errors; exit `3` means the tick
   reported errors or another process holds the daemon lock; exit `2` means the
   configuration or preflight failed before any claim.

5. Move to a resident service (macOS launchd). Render first, then install and
   check status:

   ```sh
   hermes-kanban-lanes service render --platform launchd \
     --config /absolute/path/to/config.json --out /absolute/path/to/rendered
   hermes-kanban-lanes service install --platform launchd \
     --config /absolute/path/to/config.json
   hermes-kanban-lanes service status --platform launchd \
     --config /absolute/path/to/config.json
   ```

   The daemon always writes JSON lines to `<work_root>/logs/daemon.log`. The
   configured `service.log_root` controls where a resident service writes its
   stdout and stderr files: `launchd.out.log` and `launchd.err.log` for
   launchd, or `daemon.out.log` and `daemon.err.log` for systemd rendering. On
   failure, read the daemon log first, then re-run `doctor`.

## Configuration

`config.json` is strict JSON. Unknown keys are rejected. All path values must
be absolute; a leading tilde and environment variables are not expanded.

| Key | Required | Meaning |
|---|---|---|
| `schema_version` | yes | Must be `1`. |
| `hermes_source` | yes | Absolute path to the Hermes host checkout. |
| `hermes_commit` | yes | Expected host `HEAD` (40 or 64 hex characters). |
| `boards` | yes | Object mapping a board alias to an absolute SQLite path. |
| `repositories` | yes | Object mapping a repository alias to `{"path": "<absolute>", "trusted": true}`. |
| `executors` | yes | Object with `claude` and/or `codex`, each `{"argv": ["<binary>", ...]}`. |
| `work_root` | yes | Absolute writable directory for locks, runs, cards, and logs. |
| `max_concurrent` | yes | Positive integer. |
| `review` | no | Reviewer name, rework limit, authorization directory, and merge policy. |
| `service` | no | User-level service settings, see below. |
| `logging` | no | Retention and rotation settings, see below. |

### `service`

| Key | Default | Accepted values |
|---|---|---|
| `platform` | `null` (service checks skipped) | `"launchd"`, `"systemd"`, or `null`. |
| `label` | `"com.hermes-kanban-lanes.daemon"` | Non-empty string. |
| `log_root` | `<work_root>/logs` | Absolute path, or `null`. |
| `restart` | `"on-failure"` | `"on-failure"`, `"always"`, `"never"`. |

### `logging`

| Key | Default | Range |
|---|---|---|
| `retention_days` | `14` | `>= 1` |
| `max_total_bytes` | `2147483648` | `>= 1048576` |
| `max_log_bytes` | `10485760` | `>= 65536` |
| `backups` | `3` | `>= 1` |
| `keep_failed_runs` | `10` | `>= 0` |

A complete document with every optional block:

```json
{
  "schema_version": 1,
  "hermes_source": "/absolute/path/to/hermes-agent",
  "hermes_commit": "0123456789abcdef0123456789abcdef01234567",
  "boards": {
    "engineering": "/absolute/path/to/kanban/engineering.db"
  },
  "repositories": {
    "primary": {
      "path": "/absolute/path/to/repository",
      "trusted": true
    }
  },
  "executors": {
    "claude": {"argv": ["claude"]},
    "codex": {"argv": ["codex"]}
  },
  "work_root": "/absolute/path/to/work",
  "max_concurrent": 2,
  "service": {
    "platform": "launchd",
    "label": "com.hermes-kanban-lanes.daemon",
    "log_root": "/absolute/path/to/work/logs",
    "restart": "on-failure"
  },
  "logging": {
    "retention_days": 14,
    "max_total_bytes": 2147483648,
    "max_log_bytes": 10485760,
    "backups": 3,
    "keep_failed_runs": 10
  }
}
```

All values above are placeholders. Replace them with real absolute paths in
your own file; `examples/config.json` intentionally uses machine-independent
placeholder paths.

## Service

`hermes-kanban-lanes service` renders and manages a **user-level** service
unit. It never writes to system-wide service directories.

| Action | Command | Behavior |
|---|---|---|
| Render | `service render --platform launchd\|systemd --config PATH --out DIR` | Writes one unit file into `DIR` and nothing else. |
| Install | `service install --platform … --config PATH [--dry-run]` | Installs and loads the user-level unit. |
| Status | `service status --platform … --config PATH [--dry-run]` | Read-only status of the user-level unit. |
| Restart | `service restart --platform … --config PATH` | Restarts the installed user-level unit. |
| Uninstall | `service uninstall --platform … --config PATH [--dry-run]` | Unloads the job and removes the unit file. |

- **macOS launchd** is the only claimed lifecycle platform. The unit is a
  user-level plist loaded through `launchctl` in the current user's GUI
  session, and the service runs under your user account.
- **systemd** units are rendered for inspection or for use with tooling you
  operate yourself. `install`, `status`, `restart`, and `uninstall` fail
  closed with `service-platform-render-only` and never execute `systemctl`.
- **Windows** is not supported; the lock primitive used by `tick` and `run` is
  unavailable there.
- The unit uses the launch command bound to the Hermes installation, resolved
  through the host's `--print-runtime-command`. If that resolution fails, the
  command fails closed instead of falling back to the current interpreter.
- `uninstall` removes only the unit file and unloads the job. It does not
  delete repositories, run evidence under `work_root`, or Kanban board data.

## Exit codes

Every subcommand emits machine-readable JSON; `--version` and `--help` do not.
Report-style results such as
`doctor` and `service status` stay on stdout even when `ok` is false;
alternative error reports go to stderr as one JSON object. No human-readable
mode is provided in v0.1.

Error JSON always carries `reason`, `impact`, and `next_steps` alongside
`error`:

```json
{
  "ok": false,
  "error": "preflight-failed",
  "reason": "host-dependency-missing:ruamel",
  "impact": "tick and run cannot import the Hermes host import chain; the daemon cannot start.",
  "next_steps": [
    "Install hermes-kanban-lanes into the same Python environment as the Hermes host.",
    "Install the missing host dependency, then verify: python -c \"import ruamel\"",
    "Re-run: hermes-kanban-lanes doctor --config <absolute config path> --json"
  ]
}
```

| Exit code | Meaning |
|---|---|
| `0` | Success, including a clean SIGTERM/SIGINT shutdown of `run` and a valid config or card. |
| `1` | Internal error, including a credential-leak assertion. |
| `2` | Invalid input, failed preflight, unsupported platform precondition, a failing doctor check, or a failing service command (including `service status` when its unit is not installed). |
| `3` | Daemon lock held, a tick report containing errors, a fatal tick, or a missing service precondition. |

## Security

- **Same-UID isolation is not claimed.** The daemon and its executors run with
  your user's permissions and can read anything your user can read. If you need
  isolation, run the lane under a dedicated operating-system user or an
  external sandbox.
- **The service unit does not write credentials.** It carries only the runtime
  `PATH` and, only when `CODEX_HOME` is present in the install-time environment
  and is an absolute path, that path. Provider secrets are not written into the
  unit.
- **Credential values are kept out of output.** Errors and reports may name
  credential sources but must not print secret values. Review logs and evidence
  before sharing them.
- **Fail-closed behavior is intentional.** Missing host dependencies,
  unresolvable launch commands, unsupported platforms, and failed checks stop
  the command rather than degrading silently.
- **Report vulnerabilities privately.** Use this repository's GitHub Security
  Advisories page — see [SECURITY.md](SECURITY.md). Do not open a public issue
  for a vulnerability, and do not include live credentials in a report.

## Troubleshooting

Every error JSON names a `reason`, its `impact`, and `next_steps`. The three
most common classes are:

**Invalid configuration.** `validate-config`, `doctor`, `tick`, `run`, and the
service commands all fail with `reason: "invalid-config"`. Impact: the command
did not start and the board was not written. Next step: fix the field named in
`error`, then re-run `validate-config` and `doctor`; the configuration tables
above list every accepted key, default, and range.

**Missing Hermes host dependency.** `doctor` reports
`host-dependency-missing:<module>` in its `host-hermes` or `service-runtime`
check, and `tick`/`run` return the same `reason` before taking the daemon
lock. Impact: the Hermes host import chain cannot load, so no task can be
claimed. Next step: install Hermes Kanban Lanes and the host's dependencies
into the same Python environment as the Hermes host, verify the module with
the interpreter that runs `hermes-kanban-lanes`, then re-run `doctor`.

**Service not installed, or platform not claimed.** `service status` and
`service uninstall` return `reason: "service-not-installed"` with the exact
`service install` command to run. For systemd, lifecycle commands return
`reason: "service-platform-render-only"` with `service render` guidance; no
`systemctl` command is executed. Next step: render the unit if that is all you
need, or use the claimed macOS launchd lifecycle.

## Development

Run the test suite from a checkout:

```sh
python -m pytest tests -q
```

Run only the offline package-install test:

```sh
python -m pytest tests/unit/test_package_install.py -q
```

Build and smoke-test a wheel:

```sh
python -m pip wheel . --no-build-isolation -w /tmp/hkl-dist
python -m pip install --no-index --no-deps /tmp/hkl-dist/hermes_kanban_lanes-*.whl
hermes-kanban-lanes --version
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and commit
rules.

## License

MIT. See [LICENSE](LICENSE).
