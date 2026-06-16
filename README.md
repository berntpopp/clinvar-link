# clinvar-link

MCP server grounding variant-pathogenicity questions in NCBI ClinVar.

`clinvar-link` is a sibling of [`gnomad-link`](https://github.com/berntpopp/gnomad-link)
and [`hgnc-link`](https://github.com/berntpopp/hgnc-link), following the same
conventions: a unified server providing REST API and MCP interfaces backed by a
local SQLite index built from ClinVar bulk downloads.

> Status: early scaffold. Functionality is being built out across subsequent tasks.

## Development

```bash
uv sync --group dev   # install project + dev dependencies
make ci-local         # ruff check, ruff format --check, mypy, pytest with coverage
```

## License

MIT
