# Testing

The canonical testing and validation guidance is in the
[developer guide](developer-guide.md#part-v---testing-and-validation).

For the common checks, run:

```console
tox -e unit
tox -e linters
```

The compatibility environments `test` and `py3` run the same stdlib
`unittest` suite as `unit`:

```console
tox -e test
tox -e py3
```

Use the narrowest applicable check first. Tests must keep generated state below
`.tmp/` and must not modify maintained source files or unrelated container
resources.

Dependency generation has a stricter runtime contract than ordinary tests. Use
Python 3.12 so environment markers resolve exactly as they do for the supported
container runtime:

```console
uvx --python 3.12 tox -e update-lockfiles
```

The environment rejects other Python minors because they can select a different
package set, such as the `legacy-cgi` compatibility package on Python 3.13.
