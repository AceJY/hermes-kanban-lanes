# W1 examples

Generic, machine-independent examples of the two documents the W1 CLI accepts.
They use `/srv/hermes/...` placeholders on purpose; validation does **not**
require those paths to exist and never creates directories or databases.

```sh
hermes-kanban-lanes validate-config examples/config.json
hermes-kanban-lanes validate-card examples/implementation-card.json --config examples/config.json
```

Successful validation prints one JSON object to stdout:

```json
{"valid": true, "schema_version": 1, "kind": "config"}
```

Invalid input exits `2` and prints `{"valid": false, "error": "..."}` to stderr.

## What validation does and does not mean

- It checks structure, enums, JSON types, identity fields, and repository-relative
  path syntax.
- It is **not** an execution permission, a filesystem symlink check, a credential
  check, or a run/claim fence. A later phase must re-check real paths and board
  identity before doing any work.
- A card may only name an executor and repository alias that the local
  configuration already allows. Cards cannot declare a new trusted repository.
