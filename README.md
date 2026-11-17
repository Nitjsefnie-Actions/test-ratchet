# test-ratchet

**Status: in development. No release exists yet, and nothing below is usable
until the first one ships.**

test-ratchet is planned as three composite GitHub Actions that run a
repository's tests in time-balanced shards with coverage, for Python, Node and
shell, and gate the coverage floor with a ratchet that only tightens.

## Planned sub-actions

| Sub-action | Planned role |
|---|---|
| `plan` | Split the repository's tests into shards balanced by recorded run time. |
| `run` | Run one shard's tests with coverage measured. |
| `report` | Combine the shards' coverage and gate it against a committed floor that only rises. |

Each sub-action's inputs, outputs and install block are documented here when
it lands.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
