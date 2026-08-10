# S2I OpenStack Containers Developer Guide

This guide explains how this repository builds OpenStack service containers,
how source pins and generated dependency files fit together, and how
contributors can validate changes. It describes only workflows available in
this repository today.

Konflux is authoritative for hermetic production provenance and publication.
The shell and GitHub workflows described here are contributor and development
interfaces; they do not replace that production authority.

## How to read this guide

New contributors should read Parts I through III before changing a
Containerfile or dependency file. Parts IV through VI are task-oriented
references.

```text
Part I    Purpose, repository map, and terminology
Part II   Host preparation and quick starts
Part III  Sources, generated files, and build architecture
Part IV   Build and source-maintenance workflows
Part V    Testing and validation
Part VI   Maintenance and troubleshooting
```

Headings are intentionally not manually numbered below the part level. Use the
Markdown outline or search for a heading name when following a reference.

# Part I - Orientation

## Repository purpose

The repository produces UBI 10 based OpenStack service images from upstream Git
sources. It keeps service revisions, Python constraints, build requirements,
and RPM input lists in version control so a review can show the inputs expected
by a build.

The current image set is:

```text
base                       openstack-base
cyborg/cyborg              openstack-cyborg
cyborg/cyborg-agent        openstack-cyborg-agent
glance/glance-api          openstack-glance-api
watcher/watcher-base       openstack-watcher-base
```

`build.sh` is the build and source-maintenance interface. Buildah performs
image builds and pushes. Podman is useful for inspecting and running the
resulting images.

## Publication authority

Konflux owns hermetic production provenance and publication. Treat its recorded
inputs and outputs as authoritative for production images.

The GitHub and Zuul workflows have narrower development roles. The Zuul
content-provider job publishes selected speculative images only to its
ephemeral buildset registry; it is not a production publication lane.

The GitHub workflows have these roles:

- `build.yml` builds images for pull requests and manual runs.
- `test.yml` runs tests and verifies source regeneration.
- `linter.yml` runs repository checks.
- `update-sources.yml` proposes source and generated-file updates.
- `build-and-push.yml` builds on pushes to `main` and can be started manually.

The last workflow publishes development tags to Quay after pushes to `main`
and on approved manual runs. These convenient artifacts do not carry Konflux's
hermetic production provenance and do not replace its production authority.

## Repository map

```text
README.md                    short project entry point

developer-guide.md           contributor architecture and workflow reference
TESTING.md                   concise compatibility testing entry point
build.sh                     build, push, and source-maintenance implementation
tox.ini                      dependency-managed contributor commands
pyproject.toml               Python lint and format configuration
.pre-commit-config.yaml      repository-wide quality checks
.ansible-lint                Ansible lint policy
zuul.d/jobs.yaml             reusable test and content-provider jobs
zuul.d/projects.yaml         upstream project pipeline configuration
.github/workflows/           GitHub development automation
playbooks/container-ci/      shared builder and Zuul adapter playbooks
containers/base/             common UBI-based runtime image
containers/cyborg/           Cyborg source and image contexts
containers/watcher/          Watcher source and image context
tests/                       stdlib unittest suite
.tmp/                        ignored generated test and tool state
```

A service project directory contains shared source and dependency data. Each
image below it contains its Containerfile and image-specific dependency lists.

```text
containers/<project>/
  sources.txt
  requirements.lock.<stream>
  buildrequirements.lock.<stream>
  upper-constraints.txt.<stream>
  rpms.in.yaml
  src/
  <image>/
    Containerfile
    image.yaml
    bindeps.txt
    builddeps.txt
    pythondeps.txt
    pythonbuilddeps.txt
```

## Terminology

**Image target**
: A buildable directory known to `build.sh`, such as `base`,
  `cyborg/cyborg-agent`, or `watcher/watcher-base`.

**Project**
: A directory below `containers/` that can share sources and generated
  dependency data across one or more images.

**Stream**
: A named set of source revisions and generated dependency files. `master` is
  the maintained default stream.

**Source pin**
: The exact Git commit in a `sources.txt` record. The adjacent branch name
  records which branch source maintenance follows; builds use the commit.

**Build context**
: The project directory passed to Buildah. It contains project-level sources
  and the selected image subdirectory.

**Maintained input**
: A file edited by contributors, such as a Containerfile, `sources.txt`, or a
  dependency list.

**Generated file**
: A tracked file produced by `build.sh update-sources`, such as a lock file,
  constraints snapshot, RPM input file, or default-stream symlink.

**Content provider**
: A paused Zuul job that builds and publishes selected images to the buildset's
  ephemeral registry, then returns exact references to dependent jobs.

**Deployment key**
: An OpenStackVersion custom-container-image field listed in an image's local
  `image.yaml`. The provider expands that field to the image's exact successful
  buildset reference. Empty key lists are valid.

# Part II - Preparing and Building

## Host prerequisites

Building requires Linux, Git, Buildah, Podman, and enough storage for UBI base
layers and Python wheels. Source maintenance also needs the Python tools pinned
by tox.

Install the container tools supported by the host package manager with:

```console
./build.sh install-deps
```

The command uses `sudo` and supports `dnf`, `microdnf`, and `apt-get`. It does
not configure registry credentials or alter source pins.

Tox manages Python test and generation dependencies. Use the repository's tox
environments rather than installing those packages into the system Python.

## Quick start: inspect targets

```console
./build.sh list
```

This discovers targets from the `containers/` directory and prints their final
image names.

## Quick start: build everything

```console
STREAM=master tox -e build
```

The build uses maintained `master` pins. It builds the base before service
images and tags images under the default local naming scheme.

For a narrower build, call the shell interface directly or through `custom`:

```console
STREAM=master ./build.sh build watcher
STREAM=master tox -e custom -- build cyborg/cyborg-agent
```

A project target builds all images in that project. An image target builds only
that image and its required base.

For a complete job-like local lifecycle, use the installed OIB adapter:

```console
tox -e oib-local -- ci
```

OIB prepares pinned repositories and a local `builder` inventory, starts an
owned ephemeral registry, invokes the shared Ansible preparation and run
playbooks, and always cleans owned runtime resources. Konflux remains the
production publication authority; OIB-local produces developer validation
images and retained diagnostic manifests.

## Quick start: run checks

```console
tox -e unit
tox -e linters
```

The `test` and `py3` environments are aliases for the same unit suite:

```console
tox -e test
tox -e py3
```

# Part III - Sources and Build Architecture

## Source records

A non-comment `sources.txt` line has five fields:

```text
<stream> <name> <repository-url> <branch-to-follow> <pinned-commit>
```

For example:

```text
master watcher https://opendev.org/openstack/watcher.git master <sha>
```

The branch field is maintenance input. A normal build checks out the exact
commit. Updating branch tips is an explicit source-maintenance action.

Source records can appear at global, project, or image level. Project sources
are shared by every image in the project. Image sources add dependencies needed
by only one image. `upper-constraints` is special: its file is fetched from the
pinned OpenStack requirements revision instead of being treated as a service
checkout.

## Source checkout ownership

Before a build, `build.sh` places missing repositories below the applicable
`src/` directory. Checkouts created by the script are removed by its exit trap.
A checkout already present before the command is used as-is and is not removed.
This permits an intentional developer checkout to replace a maintained pin for
a local experiment.

That override is powerful and visible only in the local filesystem. Remove it
before claiming that a result came from maintained pins.

OIB-local models a Zuul job with two separate layers:

```text
.tmp/local/git-cache/<canonical-name>.git   persistent bare object cache
.tmp/local/workspace/src/<canonical-name>  disposable detached checkout
```

External projects and the exact `.zuul-jobs-ref` revision use locked,
origin-validated bare caches. A cached maintained commit requires no network
access. A miss fetches the exact object, with its declared ref as a fallback,
and verifies the requested commit before creating a normal local clone. Cleanup
preserves caches but removes disposable checkouts. Cache origins are never
silently rewritten, and source URLs containing credentials are rejected. When
multiple selected contexts declare different maintained commits for the same
canonical project, the first base-first context supplies the one prepared
checkout while every placement retains its own declared pin for diagnostics.

The current container repository uses its existing Git object store. Developer
mode overlays tracked files and non-ignored untracked files onto a detached HEAD
clone while excluding `.tmp`, `.tox`, `.venv`, bytecode, and other generated
state. `--strict-worktree` rejects any dirty state instead. The local source
manifest records the base commit, authority reason, dirty checksum, copied-file
checksums, exact external pins, and cache hit or fetch details.

A transitive source override can be placed at:

```text
containers/<project>/src/overrides/<package>/
```

Containerfiles build directories found there as source packages. No additional
`sources.txt` entry is required for this explicit developer override.

## Maintained and generated files

Edit these inputs directly:

- `Containerfile`;
- `sources.txt`;
- `bindeps.txt` and `builddeps.txt`;
- `pythondeps.txt` and `pythonbuilddeps.txt`;
- base scripts and repository-maintained service configuration.

Do not hand-edit these generated outputs:

- `upper-constraints.txt.<stream>`;
- `requirements.lock.<stream>`;
- `buildrequirements.lock.<stream>`;
- `rpms.in.yaml`;
- unsuffixed default-stream symlinks.

When a source record or dependency input changes, regenerate the related files
in the same change. Review both the maintained input and generated diff.

Tool caches, test state, and temporary checkouts belong below ignored `.tmp/`.
Tests must not leave generated state elsewhere in the repository.

## Base image

`containers/base/Containerfile` starts from UBI 10 minimal. It installs common
RPM and Python dependencies, Kolla helper scripts, service user/group support,
and the common entry point. Service images refer to its resulting tag through
the `BASE_IMAGE` build argument.

## Service images

Service Containerfiles use two stages.

The build stage:

1. copies project and image source directories;
2. installs build-only RPM and Python dependencies;
3. removes source-built package names from effective constraints;
4. builds service and override wheels;
5. builds remaining dependency wheels;
6. records source package versions and commits; and
7. generates service configuration where required.

The runtime stage:

1. starts from `openstack-base`;
2. creates the service user;
3. installs runtime RPM dependencies;
4. installs the built wheels under the filtered constraints;
5. installs configuration and Kolla integration; and
6. switches to the deployed service user.

`/source-built-packages.txt` in a service image records package, commit, and
version information collected during the build.

## Image names and tags

The default naming inputs are:

```text
REGISTRY      localhost
NAMESPACE     openstack
IMAGE_PREFIX  openstack
TAG           <stream>-latest
```

A target such as `watcher/watcher-base` therefore becomes
`localhost/openstack/openstack-watcher-base:master-latest` for the `master`
stream. `TAG` accepts a comma-separated list when multiple tags are needed.

# Part IV - Contributor Workflows

## Build commands

Build one image serially:

```console
STREAM=master ./build.sh build watcher/watcher-base
```

Build all service images with bounded parallelism:

```console
STREAM=master PARALLEL=4 ./build.sh build-parallel all
```

The parallel command builds the base first, prepares shared sources, then runs
service builds concurrently. Set `BUILD_LOGS_DIR` to retain per-image logs.

Useful overrides include:

```text
REGISTRY
NAMESPACE
TAG
IMAGE_PREFIX
BASE_IMAGE
CONSTRAINTS_FILE
BUILD_CONSTRAINTS_FILE
PARALLEL
BUILD_LOGS_DIR
PIP_NO_BINARY
```

Use `PIP_NO_BINARY=:all:` when specifically testing source distribution builds.
It increases build time and is not needed for ordinary validation.

## Push commands

Authenticate to the destination registry before pushing. Then run:

```console
STREAM=master REGISTRY=quay.io NAMESPACE=<namespace> \
  ./build.sh push all
```

The push operation verifies that every expected local tag exists before it
pushes any target. This command is an explicit contributor action. It does not
change Konflux's production authority.

## Local OIB lifecycle

The local adapter exposes explicit phases through one installed command:

```console
tox -e oib-local -- prepare
tox -e oib-local -- run
tox -e oib-local -- cleanup
tox -e oib-local -- ci
```

`prepare` fills `.tmp/local/workspace`, generates
`.tmp/local/inventory.yaml`, materializes the pinned `zuul-jobs` role checkout,
starts a named local registry, and validates its normalized connection. The
inventory defines a `builder` group containing only `localhost` with
`ansible_connection: local`; the same shared playbooks therefore mutate the
local host through the same `hosts: builder` contract used by Zuul.

`run` imports shared source planning/context assembly and shell-backed
publication in one Ansible invocation. The shared playbook passes
`S2I_CONTEXTS_ROOT` and `ERROR_ON_CLONE=1`, so prepared local builds cannot fall
back to source cloning. `build.sh` remains the build, push, and exact-reference
backend. The OIB adapter does not implement native image operations.

`cleanup` is idempotent and accepts partially prepared state. It removes exact
workflow image tags, the explicitly owned `s2i_ci_registry` container,
credentials, inventory, role checkout, and workspace while preserving
`.tmp/local/git-cache`, `.tmp/local/zuul-output`, and the atomic lifecycle state.
`ci` composes all phases under `try/finally`. Add `--keep` to `ci` only when the
workspace, registry, images, and credentials must remain for deliberate
inspection; run `cleanup` afterward.

The state transitions through `preparing`, `prepared`, `running`, `ran`, and
`cleaned`, with failure states retained for diagnosis. Configuration selected
by `prepare` is authoritative for later phases. Useful artifacts include:

```text
.tmp/local/state.json
.tmp/local/source-manifest.json
.tmp/local/zuul-output/logs/inventory.yaml
.tmp/local/zuul-output/logs/container-build/build-plan.json
.tmp/local/zuul-output/logs/container-build/source-placements.json
.tmp/local/zuul-output/logs/container-build/build-contexts.json
.tmp/local/zuul-output/logs/container-build/published-images.json
```

Inspect or deliberately maintain caches without running a build:

```console
tox -e oib-local -- cache inspect
tox -e oib-local -- cache refresh
tox -e oib-local -- cache prune
tox -e oib-local -- cache --project opendev.org/openstack/watcher clear
```

Normal preparation never advances a source pin. Refresh only fetches the
declared ref and still requires the maintained commit. Clear and prune validate
the recorded credential-free origin and serialize against preparation.

## Selective Zuul content provider

The `s2i-openstack-container-content-provider` job runs on the
CentOS Stream 10 nodeset host named `builder`. All host preparation, registry
validation, builds, publication, result generation, and cleanup target that
host explicitly. The Zuul executor controls Ansible but does not perform those
mutations.

The provider accepts an explicit image list whose source repositories are
available through the job's `required-projects`. The job definition is the
source of truth for those concrete inputs, and child jobs may supply a different
compatible set. `build.sh` automatically places `base` first for the resulting
comma-separated selection. Existing single-image, project, and `all` shell
targets keep their standalone behavior.

### Planning and speculative source staging

The installed `oib plan` command is a side-effect-free planning boundary. It
reads the explicit image list, `sources.txt`, mandatory `image.yaml` files,
optional inventory mappings, and `zuul.projects`. Its atomic JSON output records
ordered images, context scopes, deployment keys, source destinations, declared
refs and maintained pins, inventory commits, and the
`zuul-prepared-workspace-head` authority reason. It does not fetch Git objects,
copy repositories, assemble contexts, invoke Ansible or Buildah, publish
images, or perform cleanup.

Zuul prepares every repository declared by the job's `required-projects` in its
standard workspace. The shared run playbook resolves those checkouts on
`builder`, records their actual HEADs, and leaves the prepared
repositories unchanged. It copies maintained context skeletons and source
content into isolated `.tmp/build-contexts/<scope>` trees, records separate
source-placement and context-assembly manifests, and activates a complete
context tree atomically.

The provider invokes `build.sh` with `S2I_CONTEXTS_ROOT` pointing at those
contexts and `ERROR_ON_CLONE=1`. Consequently, missing speculative service
source or prepared context content fails instead of falling back to network
acquisition. The context keeps each committed filtered
`requirements.lock.<stream>` as the build constraint input. The prepared
`requirements` HEAD and its upper-constraints file are validated, staged, and
recorded, but dependency-aware filtering against that checkout belongs to the
separate transitive-constraint workflow. Direct contributor and GitHub shell
builds without `S2I_CONTEXTS_ROOT` retain maintained-pin cloning as an
independent compatibility path.

### Image deployment metadata

Every buildable Containerfile has a sibling `image.yaml` with this local
schema:

```yaml
openstack_version:
  custom_container_images: []
```

The list may be empty or contain multiple deployment keys. The consolidated
`watcher/watcher-base` image declares:

```yaml
openstack_version:
  custom_container_images:
    - watcherAPIImage
    - watcherApplierImage
    - watcherDecisionEngineImage
```

All three keys resolve to the same exact `openstack-watcher-base` reference.
The image contains the API, applier, and decision-engine entry points and the
union of their runtime dependencies. Watcher is intentionally not split into
process-specific images.

Both Cyborg images build and publish, but their tracked key lists are empty.
Their exact references therefore appear in provider diagnostics without adding
fields to the default deployment map. When selected, the Glance API image maps
to `glanceAPIImage`.

A child job may provide `s2i_ci_image_mappings` as a mapping from a selected
image target to a replacement list of keys. Replacement is per image rather
than additive. The provider records whether each effective list came from
tracked or inventory metadata and rejects malformed values, unknown or unbuilt
image targets, empty key strings, duplicate keys, and a key assigned to more
than one image.

### Registry and returned data

The provider starts or inherits a Zuul buildset registry, validates push and
pull with a dedicated UBI tag, builds and pushes the selected image set, and
pulls every exact result back. Credentials and certificate data remain in
Zuul secret data. Returned public diagnostics use the buildset registry's
reachable host or IP and port, never the builder-local registry alias.

`s2i_ci_content.images` contains every exact successful reference, including
base and both unmapped Cyborg images. The partial
`content_provider_os_custom_container_images` map contains only effective
keys joined to exact successful references. The legacy global OS registry URL
remains the neutral `null` sentinel, while its namespace/tag and gating-repo
fields remain empty or false because this selective provider does not publish a
complete OpenStack image namespace. `cifmw_build_images_output`
remains an empty mapping and is not repurposed for service images.

Intended references are written before build mutation. Post-run cleanup
removes only those exact Podman pullback and Buildah build tags, verifies exact
absence, and removes a buildset registry only when its ownership marker is
valid.
Per-image parallel logs and registry/result manifests are retained under
`zuul-output/logs/container-build/`.

The provider pauses while dependent jobs run. Private onboarding may attach a
trivial child that prints the returned registry paths and maps. That debug job
does not pull images, patch an OpenStackVersion resource, deploy OpenStack, or
invoke a downstream repository's playbooks. Downstream consumption is separate
work.

## Updating source pins and locks

Advance source records and regenerate dependent files with:

```console
STREAM=master tox -e update-sources
```

To restrict work to one or more projects or images:

```console
STREAM=master tox -e update-sources -- watcher
STREAM=master tox -e update-sources -- watcher cyborg/cyborg-agent
```

This resolves configured branch tips, updates exact source commits, refreshes
constraints, regenerates RPM inputs and both lock-file classes, and recreates
default-stream symlinks.

Before changing any tracked file, the updater freezes every selected declared
reference to one exact commit. It retains those fetched objects for the whole
run, so a moving branch cannot supply different content after preflight. The
atomic record is written to:

```text
.tmp/source-maintenance/frozen-source-refs.<stream>.tsv
```

A slash in a stream name is encoded as `%2F` in this filename. Unsafe stream
components such as `.` or `..` are rejected before filesystem mutation.

Each repository-ordered row records the source manifest, declared ref,
committed pin, frozen effective commit, and authority. `declared-ref` means an
advancement run resolved the maintained branch or tag. `committed-pin` means a
pinned run used the existing SHA without querying a branch head.
`pre-existing-checkout` means an intentional Git checkout below `src/` remained
untouched and supplied its recorded HEAD while the maintained pin stayed
unchanged. An unversioned source directory or any unresolvable ref fails the
complete preflight before tracked mutation.

To regenerate from the existing commits without advancing them:

```console
STREAM=master tox -e update-lockfiles
```

This is the reproducibility mode used by the unattached Zuul
`s2i-openstack-containers-update-sources` job. Zuul tox jobs use Python 3.12,
matching the container runtime default; other tooling remains version-neutral.
The job uses an isolated tox cache, preserves the frozen manifest as a log
artifact, and fails when regeneration leaves any tracked diff. Generated
lock headers and resolver annotations are omitted so supported tooling runtimes
do not create cosmetic drift. Service dependency inputs include the WebOb CGI
compatibility package explicitly so Python 3.12 and newer resolvers produce the
same package set without selecting a tooling interpreter. The scheduled/manual
GitHub updater deliberately
advances source pins; it is the freshness lane that proposes source movement.

Maintained source records and dependency inputs are reviewable inputs.
Stream-suffixed constraints snapshots, requirements locks, build-requirements
locks, RPM input files, and default-stream symlinks are generator-owned outputs.
Regenerate them together; do not hand-edit generated output to hide drift.

Always inspect `git diff -- containers/` and compare updated pins with the
frozen manifest. A clean command exit does not replace review of source changes
and generated dependency movement.

## Adding an image

1. Create `containers/<project>/<image>/Containerfile`.
2. Add its runtime and build dependency files.
3. Add project-level or image-level source records as appropriate.
4. Add an image `src/.gitkeep` only when an image-level source directory is
   needed.
5. Run source generation for the project.
6. Build the image target.
7. Run unit and linter validation.

Follow an existing service Containerfile with similar runtime behavior. Keep
build-only packages out of `bindeps.txt`, and keep runtime packages out of
`builddeps.txt` unless they are genuinely needed in both stages.

# Part V - Testing and Validation

## Validation strategy

Start with the narrowest check that proves a change, then broaden validation
before publishing it. Tests use stdlib `unittest`; shell test harnesses are not
part of the supported non-Ansible test model.

A practical progression is:

1. run the affected unit test module;
2. run `tox -e unit`;
3. run `tox -e linters`;
4. regenerate files when maintained inputs changed; and
5. build the affected image or project.

## Unit tests

Run the complete suite with:

```console
tox -e unit
```

For focused iteration, pass a unittest name pattern through the environment:

```console
tox -e unit -- -k test_name_pattern
```

Tests use temporary directories and local bare Git remotes so they do not
change a contributor's source checkouts or container storage.

Exercise the installed planning command with:

```console
tox -e oib-plan -- --help
```

The planner environment installs the project wheel. It exposes planning only;
source staging and context assembly remain Ansible responsibilities.

## Formatting and static analysis

Run:

```console
tox -e linters
```

Pre-commit checks whitespace, YAML, JSON, Python syntax, shell style,
Containerfiles, Ruff formatting and lint rules, OpenStack Hacking conventions,
and Ansible lint policy. Configuration is committed so local and automation
interfaces use the same rules.

## Automation coverage

GitHub runs build, unit, linter, and source-update workflows as described in
`Publication authority`. The repository also defines reusable unit, linter,
pinned source-reproducibility, Molecule, and selective content-provider jobs
in `zuul.d/jobs.yaml`. The upstream `github-check` pipeline in
`zuul.d/projects.yaml` runs all five jobs on the CentOS Stream 10 builder
nodeset. The tox jobs use that node's Python 3.12 runtime, and the provider uses
the upstream container project directly.

The provider's Molecule scenario exercises normalized registry validation,
tracked and inventory deployment metadata, exact-reference expansion, and
matching Podman/Buildah cleanup with self-contained fake container clients.

Neither development automation path changes Konflux's authority for hermetic
production provenance and publication.

## Generated-state checks

After tests or source generation, run:

```console
git status --short
git diff --check
```

Unexpected tracked changes indicate a test isolation bug or stale generated
files. Tool-created untracked state should remain below `.tmp/`.

# Part VI - Maintenance and Troubleshooting

## Change-to-validation map

| Change | Minimum focused validation |
| --- | --- |
| Python test/helper | affected unittest, `tox -e unit`, `tox -e linters` |
| Containerfile | `tox -e linters`, affected image build |
| Python dependency input | regenerate locks, unit, linter, affected build |
| RPM dependency input | regenerate `rpms.in.yaml`, linter, affected build |
| `sources.txt` | ordinary frozen refresh, inspect manifest/pins/locks, pinned clean refresh, affected build |
| Generator-owned constraints, lock, RPM, or symlink | pinned refresh, require no tracked diff |
| Source-maintenance job or runtime | focused updater tests, pinned clean refresh with the default Python, stale-output failure |
| base image or script | unit, linter, build all service images |
| OIB-local cache/workspace/lifecycle | unit, linter, Molecule, phased and full local lifecycle |
| GitHub workflow | YAML/linter checks and workflow review |

## Build uses an unexpected source revision

Check for a pre-existing checkout below the project or image `src/` directory.
Such a checkout intentionally overrides `sources.txt`. Compare its `HEAD` with
the maintained pin, then remove or relocate it when a pinned build is required.

## A generated file changes unexpectedly

Confirm `STREAM`, `TARGET`, `DEFAULT_STREAM`, and `SKIP_HASH_UPDATE`. Verify
that all source records contain the expected exact commits. Re-run generation
from a clean tree and compare the complete `containers/` diff.

Moving branch tips can legitimately change pins when `SKIP_HASH_UPDATE` is not
set. Compare the resulting pins with
the stream-safe `.tmp/source-maintenance/frozen-source-refs.<stream>.tsv`;
every updated pin must match its frozen `declared-ref` commit. Use pinned regeneration when testing
determinism against committed sources.

If preflight fails, inspect the named URL/ref and authority before retrying. A
missing manifest means resolution stopped before the complete input set was
frozen. A `pre-existing-checkout` row is an explicit local override: compare its
recorded HEAD, then remove the checkout for a committed-pin reproduction.

If pinned regeneration leaves a tracked diff, do not dismiss it as cache noise.
Confirm `STREAM`, `TARGET`, and the isolated tox cache; verify every manifest
row says `committed-pin` on a clean tree; then inspect the first changed
generator-owned file and regenerate the complete selected scope. The Zuul
post-run diff assertion intentionally treats this state as a stale-input
failure.

## A service package still comes from an index

Verify the service repository exists in the applicable `sources.txt`, that the
checkout appears under `src/`, and that its package metadata can be recognized
by the Containerfile. Inspect `/source-built-packages.txt` in a successful
image.

## A build cannot find constraints or locks

Check the stream-suffixed files in the project directory and the unsuffixed
symlinks used by Containerfiles. Regenerate the project for the selected stream
rather than manually repairing a generated symlink or lock file.

## A parallel build fails

Set `BUILD_LOGS_DIR` and inspect the retained per-image files:

```console
STREAM=master BUILD_LOGS_DIR=.tmp/build-logs \
  ./build.sh build-parallel all
```

Parallel service output is prefixed by image and streamed while each build
runs. The command preserves the failing exit status, stops outstanding builds,
and reports the log directory without replaying every successful log.

## A local OIB phase fails

Read `.tmp/local/state.json` first, then the source manifest, inventory, and
container-build artifacts in the order printed by the command. `failed` means
prepare or run stopped; `cleanup-failed` means exact image or registry cleanup
must be corrected and retried. `oib local cleanup` can recover partial state and
preserves caches and output for diagnosis.

An origin mismatch is intentional protection against reusing an unrelated bare
cache. Inspect the cache, clear only the named project when the recorded source
URL is genuinely obsolete, and prepare again. A missing pin on a cache hit
indicates corruption; do not silently replace that cache. If preparation works
offline, the source manifest should report `hit: true` for every external
project and the pinned role checkout.

`--keep` retains credentials and a running registry as well as images and the
workspace. Always run cleanup when inspection ends. OIB refuses to replace a
pre-existing container named `s2i_ci_registry`.

## Provider output or cleanup is incomplete

Read provider artifacts in this order:

```text
intended-images.json
registry-state.json
build.log and per-image logs
push.log
published-images.json
```

`intended-images.json` exists before builds start and is the cleanup authority
after partial failure. `published-images.json` contains the completed exact
references, effective mappings, and mapping-source diagnostics. Cleanup failure
is a job failure; compare the recorded references with both Podman and Buildah
image listings.

Parallel image lines are prefixed by target and emitted while builds run. The
same prefixed lines remain in per-image logs, without a second successful-log
replay at the end.

## Registry push fails

Confirm login state, registry certificate trust, the `REGISTRY` and `NAMESPACE`
values, and every expected local tag. The push command checks tags before
starting, but authentication and network errors can still interrupt
publication.

## Contributor checklist

Before proposing a change:

1. confirm maintained and generated files are separated correctly;
2. run the narrow unit test and the complete unit suite;
3. run linters;
4. regenerate dependencies when an input changed;
5. build the affected image set;
6. inspect `git status`, `git diff`, and `git diff --check`; and
7. describe development publication accurately and keep Konflux production
   authority explicit.
