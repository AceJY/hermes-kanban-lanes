# Changelog

All notable changes to Hermes Kanban Lanes are documented here.

## 0.2.7 - 2026-11-11

### Fixed
- A rework round that produced no new commit was refused as `evidence:sync-base-not-ancestor`, pointing at the wrong cause; it is now reported as `rework-no-changes` with the previous round's `changes_requested` summary attached, and the ancestry check is not applied to a no-op round.

## 0.2.6 - 2026-11-11

### Fixed
- The protected-root check ran *after* `git merge`: a card touching `tools/` was refused, yet its merge commit was already on `main`, with `release-report.json` still claiming `release-ready` and no post-merge regression or notification. Protected roots are now a pre-merge gate (a refusal leaves the working tree and `main` byte-identical), one release carries exactly one verdict, and a merge that did happen is reported as merged with its `merge_sha` so the recovery path never merges twice.

## 0.2.5 - 2026-11-11

### Fixed
- Review receipts drifted between `reviewed_head`, `reviewed_commit` and `frozen_commit`/`frozen_tree`, and `review_runs()` dropped every run that carried neither of the two names it knew: a card could be reviewed and approved and still never reach the release queue, with no entry anywhere explaining why. Known synonyms are now normalised (each one recorded audibly), a receipt whose head cannot be identified is reported instead of dropped, and an ambiguous head still fails closed.

## 0.2.4 - 2026-11-11

### Fixed
- `release_once()` still read the raw review receipt (`metadata["dev_run_id"]`), so a merge card could be created from a normalised binding and then refused with `review-dev-run-invalid` on execution. The merge path now uses the same normalisation as the queue, and an incomplete receipt blocks with its own named reason instead of a misleading one.
- The self-healing install only overwrote the package directory and never pruned stale `*.dist-info` directories, so the second upgrade onwards left several versions on disk and `importlib.metadata` resolved an older one: the probe reported `plugin-version-mismatch`, the daemon refused to start, and `doctor`'s cleanup hint also deleted the newest dist-info because it trusted the ambiguous version. Installs now prune foreign dist-infos, the probe fails closed on ambiguity instead of picking one, and `doctor` derives its cleanup command from the service state rather than the ambiguous value.

## 0.2.3 - 2026-11-11

### Fixed
- `gate:scope` judged a card against its original task base, so a baseline sync that pulled trunk changes (`CHANGELOG.md`, `pyproject.toml`, …) made a compliant card fail with `path outside allow` and block as `code_failure`. Scope now diffs from `git merge-base <sync_base> <run_commit>` to the run-authored commit: only what the card authored counts, across all its rounds.
- Release queueing silently skipped every reviewed card whose receipt carried no `reviewer` field: normalisation left `reviewer: null`, binding reported `review-reviewer-invalid`, and `queue_release_cards()` hit `continue` without creating the `hkl:merge` card. The reviewer identity is now taken from the review run's own profile; an unknown reviewer or an implementer-as-reviewer produces a named, visible reason instead of a silent skip.

## 0.2.2 - 2026-11-11

### Added
- Merge notification hook: `review.merge.notify_commands` (argv arrays, default `[]`) and `review.merge.notify_timeout_seconds` (default 60, positive) run once after a merge actually completes. Facts go to the hook as environment variables (`HKL_MERGE_SHA`, `HKL_MERGE_TREE`, `HKL_REVIEWED_HEAD`, `HKL_MAIN_BEFORE`, `HKL_AUTHORIZATION_SOURCE`, `HKL_REPO`, …), the child gets the daemon's own environment and `cwd` = the merged repository root.
- Idempotency: `notify/report.json` inside the run directory is keyed by `merge_sha`, so a resumed release never notifies twice.
- A notify failure is recorded (`record: release-notify`, reason list, report file) and never changes the merge outcome.

## 0.2.1 - 2026-11-11

### Added
- Policy merge authorization: ordinary cards are released by a synthesized, auditable authorization (`source: policy`, approver `policy:auto`) once the review binding is complete, while high-risk changes (`risk: high` or a path matching `require_authorization_paths`) still require an operator-written authorization file. New optional keys: `review.merge.authorization`, `review.merge.require_authorization_paths`, `review.merge.authorization_window_seconds`; the default stays `file`.

### Fixed
- Executor failure classification no longer reads the model's `result` summary on a successful run, so a finished run that merely mentions `F401`, `401`, `403` or a pass count is not blocked as an environment failure.
- If the executor itself reports an error (`is_error: true`), its own message is still classified: rate limit/timeout → transient, 401/403/not logged in → capability.
- HTTP status codes count only as structured `api_error_status` or as bounded tokens on a line carrying http/status/error context; `F401`, `?id=1401` and epoch digits no longer match.

## 0.2.0 - 2026-11-10

### Added
- Claude Code, Codex, and DeepSeek Harness (DSH) executors behind one Kanban lane.
- Fail-closed five-check gate, reproducible evidence manifest, review/rework routing, and optional merge loop.
- Generation-independent service launcher: resolves the current Hermes runtime, anchors `installs/*/environments/*/venv`, self-heals the plugin for the current generation, and fails closed with a marker.

### Fixed
- Runtime card snapshots are accepted by the merge executor when `cards/<id>.json` is absent.
- Released runs are no longer pruned before review/merge; terminal run `tmp/` is cleaned without deleting evidence.
- Review metadata accepts both `reviewed_head` and `reviewed_commit`; conflicting values fail closed.

## 0.1.0 - 2026-11-09

- Initial public release.
