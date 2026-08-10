# Testing

The canonical testing and validation guidance is in the
[developer guide](developer-guide.md#part-v---testing-and-validation).

For the common checks, run:

```console
tox -e unit
tox -e linters
tox -e molecule
tox -e oib-plan -- --help
tox -e oib-local -- --help
```

A real local lifecycle builds, publishes, validates, and cleans all images with
pinned source checkouts:

```console
tox -e oib-local -- ci
```

Use `prepare`, `run`, and `cleanup` separately for phase testing. The Molecule
suite includes the owned local-registry lifecycle without changing unrelated
container resources.

The compatibility environments `test` and `py3` run the same stdlib
`unittest` suite as `unit`:

```console
tox -e test
tox -e py3
```

Use the narrowest applicable check first. Tests must keep generated state below
`.tmp/` and must not modify maintained source files or unrelated container
resources.
