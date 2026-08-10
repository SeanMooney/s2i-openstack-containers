# Agent Guidance

Read `developer-guide.md` for repository architecture, contributor workflows,
and validation guidance.

## Python imports

Follow the OpenStack Hacking import conventions:

- Do not use relative imports.
- Import modules rather than objects.
- Use `import package.module` or `from package import module` style.
- Keep imports grouped and alphabetized by full module path.

## Quality tooling

- Treat `pyproject.toml`, `.pre-commit-config.yaml`, and `tox.ini` as the
  portable tooling configuration.
- Use stdlib `unittest`, never pytest or Bash, for non-Ansible tests.
- Do not add new shell test harnesses.
- Run tests through tox and keep generated state below `.tmp/`.

## Container CI ownership

- Every mutating play below `playbooks/container-ci/{shared,zuul,local}/`
  targets `hosts: builder`; do not move builder work to `all`, `localhost`, or
  controller-side delegation.
- Zuul maps `builder` to its Nodepool host. OIB-local maps the `builder` group
  to exactly localhost with `ansible_connection: local`.
