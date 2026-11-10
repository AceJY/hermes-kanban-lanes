# Changelog

All notable changes to Hermes Kanban Lanes are documented here.

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
