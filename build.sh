#!/usr/bin/env bash
# Build and push OpenStack service container images using buildah.
#
# Usage:
#   STREAM=master ./build.sh build all
#   STREAM=hibiscus ./build.sh build watcher
#   STREAM=master ./build.sh build cyborg/cyborg-agent
#   ./build.sh push all
#   ./build.sh list
#
# Streams:
#   A stream defines a set of source repos at specific commits. Streams are
#   defined in sources.txt files with the format:
#     <stream> <name> <repo-url> <branch-to-follow> <pinned-hash>
#
#   Examples:
#     master upper-constraints https://opendev.org/openstack/requirements.git master abc123def456
#     master watcher https://opendev.org/openstack/watcher.git master def789abc012
#     hibiscus upper-constraints https://opendev.org/openstack/requirements.git stable/2024.2 fed321cba654
#     hibiscus watcher https://opendev.org/openstack/watcher.git stable/2024.2 aaa111bbb222
#
#   The <branch-to-follow> field is informational — it records which branch
#   the pinned hash came from. The build always checks out <pinned-hash>.
#
#   sources.txt files can be at three levels:
#     containers/sources.txt                     — global (upper-constraints, shared libs)
#     containers/<project>/sources.txt           — common for all images in the project
#     containers/<project>/<image>/sources.txt   — image-specific extras
#
#   The special name "upper-constraints" is handled differently: instead of
#   cloning the full repo, build.sh fetches just upper-constraints.txt from
#   the repo at the pinned hash and places it in containers/base/.
#
#   The main service package must be listed in sources.txt. Its name is
#   derived from the repo URL (last path component minus .git).
#
# Image naming:
#   Image names are derived as ${IMAGE_PREFIX}-<directory-name>:
#     containers/base/            → openstack-base
#     containers/nova/nova-api/   → openstack-nova-api
#     containers/cyborg/cyborg/   → openstack-cyborg
#   IMAGE_PREFIX defaults to "openstack".
#
# Source management:
#   Sources are cloned into containers/<project>/src/<name>/ based on the
#   stream entries in sources.txt. If the directory already exists, it is
#   used as-is (sources.txt is ignored for that entry). Auto-cloned repos
#   are removed on exit.
#
#   Overrides: place patched dependencies in containers/<project>/src/overrides/<pkg>/
#   These are picked up automatically — no sources.txt entry needed.
#
#   Constraints file:
#     Defined via an "upper-constraints" entry in each project's sources.txt.
#     build.sh fetches the file from the repo at the pinned hash.
#     Each project can have a different constraints file (different streams
#     may track different releases).
#     Alternatively, place it manually at containers/<project>/upper-constraints.txt.
#
#   Lockfile:
#     When update-sources runs, it also generates a pip-compile lockfile at
#     containers/<project>/<CONSTRAINTS_FILE>.<stream> (e.g., requirements.lock.master).
#     build.sh prefers this lockfile over upper-constraints.txt when building.
#     It also generates a build-requirements lockfile at
#     containers/<project>/<BUILD_CONSTRAINTS_FILE>.<stream> (e.g., buildrequirements.lock.master)
#     using pybuild-deps compile against the requirements lockfile.
#
# Environment variables:
#   STREAM            Stream name (required for build)
#   REGISTRY          Container registry (default: localhost)
#   NAMESPACE         Registry namespace (default: openstack)
#   TAG               Image tag(s), comma-separated for multiple (default: latest)
#   IMAGE_PREFIX      Prefix for image names (default: openstack)
#   BASE_IMAGE        Base image for the base container (default: registry.access.redhat.com/ubi10/ubi:latest)
#   CONSTRAINTS_FILE  Constraints/lockfile base name (default: requirements.lock)
#   BUILD_CONSTRAINTS_FILE  Build-requirements lockfile base name (default: buildrequirements.lock)
#   DEFAULT_STREAM    Default stream (default: master). When update-sources runs
#                     for this stream, un-streamed symlinks are created (e.g.,
#                     requirements.lock -> requirements.lock.master)
#   SKIP_HASH_UPDATE  If set, update-sources skips updating pinned hashes in
#                     sources.txt and clones repos at existing pinned hashes
#                     instead. Lockfiles and rpms.in.yaml are still regenerated.
#   PIP_NO_BINARY     If set, passed as --build-arg to buildah so Containerfiles
#                     can set ENV PIP_NO_BINARY. Use ":all:" to force pip to
#                     build all packages from source instead of using wheels.
#   S2I_CONTEXTS_ROOT Root of complete prepared build contexts. When set,
#                     build.sh does not materialize sources under containers/.
#   ERROR_ON_CLONE    Fail rather than enter a Git clone/fetch fallback.
#   REGISTRY_AUTH_FILE Authentication file passed explicitly to buildah push.
#   REGISTRY_CERT_DIR  TLS certificate directory passed explicitly to buildah push.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
CONTAINERS_DIR="${REPO_ROOT}/containers"

# Configurable variables
STREAM="${STREAM:-master}"
REGISTRY="${REGISTRY:-localhost}"
NAMESPACE="${NAMESPACE:-openstack}"
TAG="${TAG:-${STREAM}-latest}"
IMAGE_PREFIX="${IMAGE_PREFIX:-openstack}"
BASE_IMAGE="${BASE_IMAGE:-${REGISTRY}/${NAMESPACE}/${IMAGE_PREFIX}-base:${TAG%%,*}}"
CONSTRAINTS_FILE="${CONSTRAINTS_FILE:-requirements.lock}"
BUILD_CONSTRAINTS_FILE="${BUILD_CONSTRAINTS_FILE:-buildrequirements.lock}"
UPSTREAM_CONSTRAINTS="upper-constraints.txt"
DEFAULT_STREAM="${DEFAULT_STREAM:-master}"
SKIP_HASH_UPDATE="${SKIP_HASH_UPDATE:-}"
PIP_NO_BINARY="${PIP_NO_BINARY:-}"
S2I_CONTEXTS_ROOT="${S2I_CONTEXTS_ROOT:-}"
ERROR_ON_CLONE="${ERROR_ON_CLONE:-}"
REGISTRY_AUTH_FILE="${REGISTRY_AUTH_FILE:-}"
REGISTRY_CERT_DIR="${REGISTRY_CERT_DIR:-}"
PARALLEL="${PARALLEL:-$(nproc)}"

# Discover all buildable images from the directory structure.
discover_images() {
  local images=()

  # base first (if it exists)
  if [[ -f "${CONTAINERS_DIR}/base/Containerfile" ]]; then
    images+=("base")
  fi

  # Then all project/image directories
  for project_dir in "${CONTAINERS_DIR}"/*/; do
    local project=$(basename "${project_dir}")
    [[ "${project}" == "base" ]] && continue

    for image_dir in "${project_dir}"/*/; do
      local image=$(basename "${image_dir}")
      [[ "${image}" == "common" || "${image}" == "src" ]] && continue
      if [[ -f "${image_dir}/Containerfile" ]]; then
        images+=("${project}/${image}")
      fi
    done
  done

  echo "${images[@]}"
}

# Derive the published image name from a directory path
image_name() {
  local dir_name="$1"
  local name
  if [[ "${dir_name}" == */* ]]; then
    name=$(basename "${dir_name}")
  else
    name="${dir_name}"
  fi
  if [[ -n "${IMAGE_PREFIX}" ]]; then
    echo "${IMAGE_PREFIX}-${name}"
  else
    echo "${name}"
  fi
}

# Derive the project name from a directory path
project_name() {
  local dir_name="$1"
  if [[ "${dir_name}" == */* ]]; then
    echo "${dir_name%%/*}"
  fi
}

# Compute the full image tag (first tag, used for display and base image ref)
image_tag() {
  local dir_name="$1"
  local first_tag="${TAG%%,*}"
  echo "${REGISTRY}/${NAMESPACE}/$(image_name "${dir_name}"):${first_tag}"
}

# Generate --tag arguments for all tags (TAG is comma-separated)
image_tag_args() {
  local dir_name="$1"
  local name
  name="$(image_name "${dir_name}")"
  local args=""
  IFS=',' read -ra tags <<< "${TAG}"
  for t in "${tags[@]}"; do
    args="${args} --tag ${REGISTRY}/${NAMESPACE}/${name}:${t}"
  done
  echo "${args}"
}

# Track which repos were auto-cloned so we can clean up
declare -A _AUTO_CLONED=()

# Remove auto-cloned sources on exit
cleanup_auto() {
  for src_dir in "${!_AUTO_CLONED[@]}"; do
    echo "--- Removing auto-cloned source: ${src_dir} ---"
    rm -rf "${src_dir}"
  done
}
trap cleanup_auto EXIT

require_clone_allowed() {
  local description="$1"
  if [[ -n "${ERROR_ON_CLONE}" ]]; then
    echo "ERROR: ${description} requires source acquisition while ERROR_ON_CLONE is set" >&2
    return 1
  fi
}

# Ensure constraints file exists for a project.
# Looks for an "upper-constraints" entry in the project's sources.txt
# for the current stream and fetches the file at the pinned hash.
ensure_project_constraints() {
  local project="$1"
  local stream="$2"
  local constraints_file="${CONTAINERS_DIR}/${project}/${UPSTREAM_CONSTRAINTS}.${stream}"

  if [[ -f "${constraints_file}" ]]; then
    return
  fi

  # Look for upper-constraints entry in project-level sources.txt
  local project_sources="${CONTAINERS_DIR}/${project}/sources.txt"
  if [[ -f "${project_sources}" ]]; then
    while IFS=' ' read -r entry_stream name url branch pinned_hash; do
      [[ -z "${entry_stream}" || "${entry_stream}" == \#* ]] && continue
      [[ "${entry_stream}" != "${stream}" ]] && continue
      if [[ "${name}" == "upper-constraints" ]]; then
        require_clone_allowed "constraints for ${project}" || return 1
        echo "--- Fetching ${UPSTREAM_CONSTRAINTS}.${stream} for ${project} from ${url} at ${pinned_hash} ---"
        local tmp_repo
        tmp_repo=$(mktemp -d)
        git clone --no-checkout "${url}" "${tmp_repo}" 2>/dev/null
        git -C "${tmp_repo}" checkout "${pinned_hash}" -- upper-constraints.txt
        cp "${tmp_repo}/upper-constraints.txt" "${constraints_file}"
        rm -rf "${tmp_repo}"
        return
      fi
    done < "${project_sources}"
  fi

  echo "ERROR: No constraints file at ${constraints_file}" >&2
  echo "       Add an 'upper-constraints' entry to containers/${project}/sources.txt for stream '${stream}'," >&2
  echo "       or place the file manually." >&2
  return 1
}

# Clone a repo at a specific commit hash if not already present
# Args: <dest_dir> <url> <pinned_hash>
clone_at_hash() {
  local dest="$1"
  local url="$2"
  local pinned_hash="$3"

  if [[ -d "${dest}" ]]; then
    return
  fi
  require_clone_allowed "source ${url} at ${pinned_hash}" || return 1

  mkdir -p "$(dirname "${dest}")"
  echo "--- Cloning ${url} at ${pinned_hash} into ${dest} ---"
  git clone "${url}" "${dest}"
  git -C "${dest}" checkout "${pinned_hash}"
  _AUTO_CLONED["${dest}"]=1
}

# Process sources.txt files for a stream.
# Project-level sources → containers/<project>/src/<name>/
# Image-level sources → containers/<project>/<image>/src/<name>/
# sources.txt format: <stream> <name> <repo-url> <branch-to-follow> <pinned-hash>
ensure_sources_for_stream() {
  local dir_name="$1"   # e.g., "watcher/watcher-api"
  local stream="$2"
  local project="${dir_name%%/*}"

  # Project-level sources.txt → containers/<project>/src/<name>/
  local project_sources="${CONTAINERS_DIR}/${project}/sources.txt"
  if [[ -f "${project_sources}" ]]; then
    local project_src_dir="${CONTAINERS_DIR}/${project}/src"
    while IFS=' ' read -r entry_stream name url branch pinned_hash; do
      [[ -z "${entry_stream}" || "${entry_stream}" == \#* ]] && continue
      [[ "${entry_stream}" != "${stream}" ]] && continue
      [[ "${name}" == "upper-constraints" ]] && continue
      clone_at_hash "${project_src_dir}/${name}" "${url}" "${pinned_hash}"
    done < "${project_sources}"
  fi

  # Image-level sources.txt → containers/<project>/<image>/src/<name>/
  local image_sources="${CONTAINERS_DIR}/${dir_name}/sources.txt"
  if [[ -f "${image_sources}" ]]; then
    local image_src_dir="${CONTAINERS_DIR}/${dir_name}/src"
    while IFS=' ' read -r entry_stream name url branch pinned_hash; do
      [[ -z "${entry_stream}" || "${entry_stream}" == \#* ]] && continue
      [[ "${entry_stream}" != "${stream}" ]] && continue
      [[ "${name}" == "upper-constraints" ]] && continue
      clone_at_hash "${image_src_dir}/${name}" "${url}" "${pinned_hash}"
    done < "${image_sources}"
  fi
}

# Resolve the complete context used for one image. Prepared contexts must stay
# beneath this repository's isolated .tmp tree.
build_context() {
  local dir_name="$1"
  local project
  project="$(project_name "${dir_name}")"
  if [[ -n "${S2I_CONTEXTS_ROOT}" ]]; then
    local prepared_root
    prepared_root=$(realpath -e "${S2I_CONTEXTS_ROOT}") || return 1
    case "${prepared_root}/" in
      "${REPO_ROOT}/.tmp/"*) ;;
      *)
        echo "ERROR: Prepared contexts must remain beneath ${REPO_ROOT}/.tmp" >&2
        return 1
        ;;
    esac
    if [[ -n "${project}" ]]; then
      echo "${prepared_root}/${project}"
    else
      echo "${prepared_root}/${dir_name}"
    fi
  elif [[ -n "${project}" ]]; then
    echo "${CONTAINERS_DIR}/${project}"
  else
    echo "${CONTAINERS_DIR}/${dir_name}"
  fi
}

# Build a single image
build_image() {
  local dir_name="$1"
  local full_tag
  full_tag="$(image_tag "${dir_name}")"
  local project
  project="$(project_name "${dir_name}")"
  local context_dir
  context_dir="$(build_context "${dir_name}")" || return 1
  local containerfile
  if [[ -n "${project}" ]]; then
    containerfile="${context_dir}/${dir_name#*/}/Containerfile"
  else
    containerfile="${context_dir}/Containerfile"
  fi

  echo "=== Building ${full_tag} ==="
  if [[ ! -d "${context_dir}" || ! -f "${containerfile}" ]]; then
    echo "ERROR: Prepared image context is incomplete: ${context_dir}" >&2
    return 1
  fi

  # openstack-base image: no service source, build context is its own directory
  if [[ -z "${project}" ]]; then
    local base_constraints="${CONSTRAINTS_FILE}.${STREAM}"
    local base_lock="${context_dir}/${base_constraints}"
    if [[ ! -f "${base_lock}" ]]; then
      if [[ -n "${S2I_CONTEXTS_ROOT}" ]]; then
        echo "ERROR: Prepared constraints not found at ${base_lock}" >&2
        return 1
      fi
      ensure_project_constraints "${dir_name}" "${STREAM}"
      base_constraints="${UPSTREAM_CONSTRAINTS}.${STREAM}"
    fi
    buildah bud \
      $(image_tag_args "${dir_name}") \
      --build-arg "CONSTRAINTS_FILE=${base_constraints}" \
      --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
      ${PIP_NO_BINARY:+--build-arg "PIP_NO_BINARY=${PIP_NO_BINARY}"} \
      -f "${containerfile}" \
      "${context_dir}/"
    return
  fi

  # Ensure stream is set for service images
  if [[ -z "${STREAM}" ]]; then
    echo "ERROR: STREAM is required for building service images." >&2
    echo "       Example: STREAM=master ./build.sh build ${dir_name}" >&2
    return 1
  fi

  if [[ -z "${S2I_CONTEXTS_ROOT}" ]]; then
    ensure_sources_for_stream "${dir_name}" "${STREAM}"
  fi

  # Verify main source exists
  local sources_dir="${context_dir}/src"
  local src="${sources_dir}/${project}"
  if [[ ! -d "${src}" ]]; then
    echo "ERROR: Main source not found at ${src}" >&2
    echo "       Ensure ${project} is listed in sources.txt for stream '${STREAM}'" >&2
    return 1
  fi

  # Prefer the configured stream constraints and forbid prepared fallbacks.
  local build_constraints="${CONSTRAINTS_FILE}.${STREAM}"
  local lock_file="${context_dir}/${build_constraints}"
  if [[ ! -f "${lock_file}" ]]; then
    if [[ -n "${S2I_CONTEXTS_ROOT}" ]]; then
      echo "ERROR: Prepared constraints not found at ${lock_file}" >&2
      return 1
    fi
    ensure_project_constraints "${project}" "${STREAM}"
    build_constraints="${UPSTREAM_CONSTRAINTS}.${STREAM}"
  fi

  buildah bud \
    $(image_tag_args "${dir_name}") \
    --build-arg "CONSTRAINTS_FILE=${build_constraints}" \
    --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
    ${PIP_NO_BINARY:+--build-arg "PIP_NO_BINARY=${PIP_NO_BINARY}"} \
    -f "${containerfile}" \
    "${context_dir}/"
}

# Check that all tags of an image exist locally
verify_image_exists() {
  local dir_name="$1"
  local name
  name="$(image_name "${dir_name}")"
  local missing=()

  IFS=',' read -ra tags <<< "${TAG}"
  for t in "${tags[@]}"; do
    local full_tag="${REGISTRY}/${NAMESPACE}/${name}:${t}"
    if ! buildah inspect "${full_tag}" &>/dev/null; then
      missing+=("${full_tag}")
    fi
  done

  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "ERROR: The following image tags do not exist locally:" >&2
    for m in "${missing[@]}"; do
      echo "  ${m}" >&2
    done
    return 1
  fi
}

# Push all tags of a single image
push_image() {
  local dir_name="$1"
  local name
  name="$(image_name "${dir_name}")"

  IFS=',' read -ra tags <<< "${TAG}"
  local registry_args=()
  [[ -n "${REGISTRY_AUTH_FILE}" ]] && \
    registry_args+=(--authfile "${REGISTRY_AUTH_FILE}")
  [[ -n "${REGISTRY_CERT_DIR}" ]] && \
    registry_args+=(--cert-dir "${REGISTRY_CERT_DIR}")

  for t in "${tags[@]}"; do
    local full_tag="${REGISTRY}/${NAMESPACE}/${name}:${t}"
    echo "=== Pushing ${full_tag} ==="
    buildah push "${registry_args[@]}" "${full_tag}"
  done
}

# Print the exact image references produced for a target.
target_refs() {
  local target="$1"
  local dir_name
  local name
  local tag
  local -a tags
  resolve_targets_array "${target}" || return 1
  for dir_name in "${_RESOLVED_TARGETS[@]}"; do
    name="$(image_name "${dir_name}")"
    IFS=',' read -ra tags <<< "${TAG}"
    for tag in "${tags[@]}"; do
      echo "${REGISTRY}/${NAMESPACE}/${name}:${tag}"
    done
  done
}

# List all images
list_images() {
  echo "Container images:"
  for dir_name in $(discover_images); do
    local full_tag
    full_tag="$(image_tag "${dir_name}")"
    local project
    project="$(project_name "${dir_name}")"
    if [[ -n "${project}" ]]; then
      echo "  ${dir_name} → ${full_tag}  (project: ${project})"
    else
      echo "  ${dir_name} → ${full_tag}"
    fi
  done
  if [[ -n "${STREAM}" ]]; then
    echo ""
    echo "Stream: ${STREAM}"
  fi
}

# Resolve which images to process. A comma-separated expression is an explicit
# ordered union and includes the base image when it selects a service image.
# Existing single-image, project, and all expressions retain their behavior.
resolve_targets() {
  local target="$1"
  local all_images
  all_images=($(discover_images))

  if [[ "${target}" == *,* ]]; then
    local -a requested
    local -a resolved=("base")
    local item
    local image
    local item_images
    local seen_base=0
    declare -A seen=()

    IFS=',' read -ra requested <<< "${target}"
    for item in "${requested[@]}"; do
      if [[ -z "${item}" || "${item}" != "${item//[[:space:]]/}" ]]; then
        echo "ERROR: Invalid empty or whitespace-containing target '${item}'" >&2
        return 1
      fi
      if ! item_images=$(resolve_targets "${item}"); then
        return 1
      fi
      for image in ${item_images}; do
        if [[ "${image}" == "base" ]]; then
          seen_base=1
          continue
        fi
        if [[ -z "${seen[${image}]:-}" ]]; then
          resolved+=("${image}")
          seen["${image}"]=1
        fi
      done
    done

    if [[ ${#resolved[@]} -eq 1 && ${seen_base} -eq 0 ]]; then
      echo "ERROR: Explicit target selection is empty" >&2
      return 1
    fi
    echo "${resolved[@]}"
    return
  fi

  if [[ "${target}" == "all" ]]; then
    echo "${all_images[@]}"
    return
  fi

  # Exact match
  for dir_name in "${all_images[@]}"; do
    if [[ "${dir_name}" == "${target}" ]]; then
      echo "${target}"
      return
    fi
  done

  # Project prefix match
  local matched=()
  for dir_name in "${all_images[@]}"; do
    if [[ "${dir_name}" == "${target}/"* ]]; then
      matched+=("${dir_name}")
    fi
  done

  if [[ ${#matched[@]} -gt 0 ]]; then
    echo "${matched[@]}"
    return
  fi

  echo "ERROR: Unknown image or project '${target}'" >&2
  echo "Available images:" >&2
  for dir_name in "${all_images[@]}"; do
    echo "  ${dir_name}" >&2
  done
  return 1
}

# Resolve an expression without losing failures in command substitutions.
declare -a _RESOLVED_TARGETS=()
resolve_targets_array() {
  local target="$1"
  local output
  if ! output=$(resolve_targets "${target}"); then
    return 1
  fi
  _RESOLVED_TARGETS=(${output})
}

# Clone a repo at a branch tip (or tag) and store the resolved commit hash
# in _CLONE_RESULT.  Must NOT be called via command substitution ($(...))
# because _AUTO_CLONED assignments would be lost in the subshell.
# If the destination already exists, use it as-is (same policy as clone_at_hash).
# Args: <dest_dir> <url> <branch>
clone_at_branch() {
  local dest="$1"
  local url="$2"
  local branch="$3"

  if [[ -d "${dest}" ]]; then
    echo "--- Using existing source: ${dest} ---"
  else
    mkdir -p "$(dirname "${dest}")"
    echo "--- Cloning ${url} (${branch}) into ${dest} ---"
    if ! git clone --branch "${branch}" "${url}" "${dest}" 2>/dev/null; then
      git clone "${url}" "${dest}" 2>/dev/null
      git -C "${dest}" checkout "${branch}"
    fi
    _AUTO_CLONED["${dest}"]=1
  fi

  _CLONE_RESULT=$(git -C "${dest}" rev-parse HEAD)
}

# Update pinned hashes in a single sources.txt file for the given stream.
# Clones source repos at the branch tip to resolve hashes, and extracts
# upper-constraints.txt when encountered.
# Args: <sources_file> <stream> <src_dir> <project_dir>
update_sources_file() {
  local sources_file="$1"
  local stream="$2"
  local src_dir="$3"
  local project_dir="$4"

  if [[ ! -f "${sources_file}" ]]; then
    return
  fi

  local tmp_file
  tmp_file=$(mktemp)
  local updated=0

  while IFS= read -r line; do
    # Preserve comments and blank lines
    if [[ -z "${line}" || "${line}" == \#* ]]; then
      echo "${line}" >> "${tmp_file}"
      continue
    fi

    read -r entry_stream name url branch pinned_hash <<< "${line}"

    # Only update entries for the requested stream
    if [[ "${entry_stream}" != "${stream}" ]]; then
      echo "${line}" >> "${tmp_file}"
      continue
    fi

    local new_hash
    if [[ "${name}" == "upper-constraints" ]]; then
      # Clone without checkout, resolve hash from branch, extract file
      local uc_tmp
      uc_tmp=$(mktemp -d)
      git clone --no-checkout "${url}" "${uc_tmp}" 2>/dev/null
      new_hash=$(git -C "${uc_tmp}" rev-parse --verify "origin/${branch}" 2>/dev/null \
        || git -C "${uc_tmp}" rev-parse --verify "${branch}" 2>/dev/null)
      git -C "${uc_tmp}" checkout "${new_hash}" -- upper-constraints.txt
      cp "${uc_tmp}/upper-constraints.txt" "${project_dir}/${UPSTREAM_CONSTRAINTS}.${stream}"
      rm -rf "${uc_tmp}"
    elif [[ -d "${src_dir}/${name}" ]]; then
      # Pre-existing checkout — use it for pip-compile but don't update the hash
      echo "  ${name}: skipped (pre-existing checkout at ${src_dir}/${name})"
      new_hash="${pinned_hash}"
    else
      clone_at_branch "${src_dir}/${name}" "${url}" "${branch}"
      new_hash="${_CLONE_RESULT}"
    fi

    if [[ -z "${new_hash}" ]]; then
      echo "ERROR: Could not resolve ref '${branch}' for ${url}" >&2
      rm "${tmp_file}"
      return 1
    fi

    if [[ "${new_hash}" != "${pinned_hash}" ]]; then
      echo "  ${name}: ${pinned_hash:-<empty>} → ${new_hash} (${branch})"
      updated=1
    fi
    echo "${entry_stream} ${name} ${url} ${branch} ${new_hash}" >> "${tmp_file}"
  done < "${sources_file}"

  if [[ ${updated} -eq 1 ]]; then
    mv "${tmp_file}" "${sources_file}"
  else
    rm "${tmp_file}"
    echo "  (no changes)"
  fi
}

# Collect Python package names provided via RPMs from bindeps.txt and
# builddeps.txt across a project and its images.  RPM packages for Python
# modules follow the convention python3-<module>.  Returns normalized pip
# package names (lowercase, dots/underscores replaced with hyphens) one per
# line.
collect_rpm_python_packages() {
  local project_dir="$1"
  local -A seen

  for depfile in "${project_dir}"/bindeps.txt "${project_dir}"/builddeps.txt; do
    [[ -f "${depfile}" ]] || continue
    while IFS= read -r line; do
      line="${line%%#*}"
      line="${line// /}"
      [[ -z "${line}" ]] && continue
      if [[ "${line}" == python3-* ]]; then
        local pkg="${line#python3-}"
        pkg=$(echo "${pkg}" | tr '[:upper:]' '[:lower:]' | sed 's/[._]/-/g')
        seen["${pkg}"]=1
      fi
    done < "${depfile}"
  done

  for image_dir in "${project_dir}"/*/; do
    local image
    image=$(basename "${image_dir}")
    [[ "${image}" == "common" || "${image}" == "src" ]] && continue
    [[ ! -f "${image_dir}/Containerfile" ]] && continue

    for depfile in "${image_dir}"/bindeps.txt "${image_dir}"/builddeps.txt; do
      [[ -f "${depfile}" ]] || continue
      while IFS= read -r line; do
        line="${line%%#*}"
        line="${line// /}"
        [[ -z "${line}" ]] && continue
        if [[ "${line}" == python3-* ]]; then
          local pkg="${line#python3-}"
          pkg=$(echo "${pkg}" | tr '[:upper:]' '[:lower:]' | sed 's/[._]/-/g')
          seen["${pkg}"]=1
        fi
      done < "${depfile}"
    done
  done

  printf '%s\n' "${!seen[@]}"
}

# Remove entries for RPM-provided packages from a pip-compile lockfile.
# Args: <lockfile> <space-separated normalized package names>
filter_lockfile_rpm_packages() {
  local lockfile="$1"
  local exclude_str="$2"

  [[ -z "${exclude_str}" ]] && return

  local tmp
  tmp=$(mktemp)
  awk -v excl="${exclude_str}" '
  BEGIN {
    n = split(excl, arr, " ")
    for (i = 1; i <= n; i++) exclude[arr[i]] = 1
  }
  /^[a-zA-Z]/ {
    pkg = $0
    sub(/[>=<\[;].*/, "", pkg)
    gsub(/^[ \t]+|[ \t]+$/, "", pkg)
    norm = tolower(pkg)
    gsub(/[._]/, "-", norm)
    if (norm in exclude) { skip = 1; next }
    skip = 0; print; next
  }
  /^[ \t]/ { if (!skip) print; next }
  { skip = 0; print }
  ' "${lockfile}" > "${tmp}"
  mv "${tmp}" "${lockfile}"
}

# Generate a single requirements.lock for a project by running pip-compile
# against requirements.txt from all source packages (project + all images)
# plus pythondeps.txt and pythonbuilddeps.txt from every image,
# constrained by upper-constraints.txt.
# The resulting lockfile pins every transitive dependency and replaces
# upper-constraints.txt as the constraints file used during container builds.
generate_requirements_lock() {
  local project="$1"   # e.g., "watcher"
  local stream="$2"    # e.g., "master"
  local project_dir="${CONTAINERS_DIR}/${project}"
  local constraints_file="${project_dir}/${UPSTREAM_CONSTRAINTS}.${stream}"

  if [[ ! -f "${constraints_file}" ]]; then
    echo "WARNING: No constraints file at ${constraints_file}, skipping lock for ${project}" >&2
    return
  fi

  # Collect input files using relative paths (from project_dir) so that
  # pip-compile doesn't embed full filesystem paths in the output.
  local input_files=()

  for req in "${project_dir}"/src/*/requirements.txt; do
    [[ -f "${req}" ]] && input_files+=("${req#"${project_dir}"/}")
  done

  # Collect dep files at the project level (used by base container)
  for depfile in pythondeps.txt pythonbuilddeps.txt; do
    if [[ -f "${project_dir}/${depfile}" ]]; then
      input_files+=("${depfile}")
    fi
  done

  # Collect requirements.txt and dep files from all images in the project
  for image_dir in "${project_dir}"/*/; do
    local image=$(basename "${image_dir}")
    [[ "${image}" == "common" || "${image}" == "src" ]] && continue
    [[ ! -f "${image_dir}/Containerfile" ]] && continue

    for req in "${image_dir}"/src/*/requirements.txt; do
      [[ -f "${req}" ]] && input_files+=("${req#"${project_dir}"/}")
    done

    for depfile in pythondeps.txt pythonbuilddeps.txt; do
      if [[ -f "${image_dir}/${depfile}" ]]; then
        input_files+=("${image}/${depfile}")
      fi
    done
  done

  if [[ ${#input_files[@]} -eq 0 ]]; then
    echo "WARNING: No requirements.txt found in source packages, skipping lock for ${project}" >&2
    return
  fi

  local lock_file="${CONSTRAINTS_FILE}.${stream}"

  echo "--- Generating ${project_dir}/${lock_file} ---"
  (cd "${project_dir}" && \
    pip-compile --allow-unsafe --strip-extras \
      -c "${UPSTREAM_CONSTRAINTS}.${stream}" \
      -o "${lock_file}" \
      "${input_files[@]}")

  local rpm_pkgs
  rpm_pkgs=$(collect_rpm_python_packages "${project_dir}")
  if [[ -n "${rpm_pkgs}" ]]; then
    local exclude_str
    exclude_str=$(echo "${rpm_pkgs}" | tr '\n' ' ')
    echo "--- Filtering RPM-provided packages from ${lock_file}: ${exclude_str}---"
    filter_lockfile_rpm_packages "${project_dir}/${lock_file}" "${exclude_str}"
  fi
}

# Generate requirements.lock for each project in the target scope.
# Expects sources to be already cloned and constraints fetched
# (done by update_sources). Cloned repos are cleaned up on exit.
generate_locks_for_targets() {
  local target="$1"
  local stream="$2"

  if ! command -v pip-compile &>/dev/null; then
    echo "ERROR: pip-compile not found. Install it with: pip install pip-tools" >&2
    return 1
  fi

  local targets
  resolve_targets_array "${target}" || return 1
  targets=("${_RESOLVED_TARGETS[@]}")

  declare -A _lock_projects_seen
  for img in "${targets[@]}"; do
    local project
    project="$(project_name "${img}")"
    if [[ -z "${project}" ]]; then
      [[ "${img}" != "base" ]] && continue
      project="base"
    fi
    [[ -n "${_lock_projects_seen[$project]:-}" ]] && continue
    _lock_projects_seen["${project}"]=1

    generate_requirements_lock "${project}" "${stream}"
  done
}

# Generate a buildrequirements.lock for a project by running pybuild-deps
# compile against the requirements.lock produced by generate_requirements_lock.
generate_buildrequirements_lock() {
  local project="$1"
  local stream="$2"
  local project_dir="${CONTAINERS_DIR}/${project}"
  local lock_file="${project_dir}/${CONSTRAINTS_FILE}.${stream}"
  local build_lock_file="${BUILD_CONSTRAINTS_FILE}.${stream}"

  if [[ ! -f "${lock_file}" ]]; then
    echo "WARNING: No ${lock_file} found, skipping build lock for ${project}" >&2
    return
  fi

  echo "--- Generating ${project_dir}/${build_lock_file} ---"
  (cd "${project_dir}" && \
    pybuild-deps compile \
      -o "${build_lock_file}" \
      "${CONSTRAINTS_FILE}.${stream}")
}

# Generate buildrequirements.lock for each project in the target scope.
generate_buildlocks_for_targets() {
  local target="$1"
  local stream="$2"

  if ! command -v pybuild-deps &>/dev/null; then
    echo "ERROR: pybuild-deps not found. Install it with: pip install pybuild-deps" >&2
    return 1
  fi

  local targets
  resolve_targets_array "${target}" || return 1
  targets=("${_RESOLVED_TARGETS[@]}")

  declare -A _buildlock_projects_seen
  for img in "${targets[@]}"; do
    local project
    project="$(project_name "${img}")"
    if [[ -z "${project}" ]]; then
      [[ "${img}" != "base" ]] && continue
      project="base"
    fi
    [[ -n "${_buildlock_projects_seen[$project]:-}" ]] && continue
    _buildlock_projects_seen["${project}"]=1

    generate_buildrequirements_lock "${project}" "${stream}"
  done
}

# Generate rpms.in.yaml for a project by collecting all packages from
# bindeps.txt and builddeps.txt across every image in the project.
generate_rpms_in_yaml() {
  local project="$1"
  local project_dir="${CONTAINERS_DIR}/${project}"
  local output="${project_dir}/rpms.in.yaml"

  local -A pkgs_seen

  # Collect dep files at the project level (used by base container)
  for depfile in bindeps.txt builddeps.txt; do
    [[ -f "${project_dir}/${depfile}" ]] || continue
    while IFS= read -r line; do
      line="${line%%#*}"
      line="${line// /}"
      [[ -z "${line}" ]] && continue
      pkgs_seen["${line}"]=1
    done < "${project_dir}/${depfile}"
  done

  # Collect dep files from all images in the project
  for image_dir in "${project_dir}"/*/; do
    local image=$(basename "${image_dir}")
    [[ "${image}" == "common" || "${image}" == "src" ]] && continue
    [[ ! -f "${image_dir}/Containerfile" ]] && continue

    for depfile in bindeps.txt builddeps.txt; do
      [[ -f "${image_dir}/${depfile}" ]] || continue
      while IFS= read -r line; do
        line="${line%%#*}"
        line="${line// /}"
        [[ -z "${line}" ]] && continue
        pkgs_seen["${line}"]=1
      done < "${image_dir}/${depfile}"
    done
  done

  if [[ ${#pkgs_seen[@]} -eq 0 ]]; then
    echo "WARNING: No packages found for ${project}, skipping rpms.in.yaml" >&2
    return
  fi

  local sorted_pkgs
  sorted_pkgs=$(printf '%s\n' "${!pkgs_seen[@]}" | sort)

  echo "--- Generating ${output} ---"
  {
    cat <<'HEADER'
contentOrigin:
  repofiles:
    - ./rpms.repo
context:
  bare: true

#
# To update rpms.lock.yaml:
#    rpm-lockfile-prototype rpms.in.yaml
#

arches:
  - x86_64
  - aarch64

packages:
HEADER
    while IFS= read -r pkg; do
      echo "  - ${pkg}"
    done <<< "${sorted_pkgs}"
  } > "${output}"
}

# Generate rpms.in.yaml for each project in the target scope.
generate_rpms_in_for_targets() {
  local target="$1"

  local targets
  resolve_targets_array "${target}" || return 1
  targets=("${_RESOLVED_TARGETS[@]}")

  declare -A _rpms_projects_seen
  for img in "${targets[@]}"; do
    local project
    project="$(project_name "${img}")"
    if [[ -z "${project}" ]]; then
      [[ "${img}" != "base" ]] && continue
      project="base"
    fi
    [[ -n "${_rpms_projects_seen[$project]:-}" ]] && continue
    _rpms_projects_seen["${project}"]=1

    generate_rpms_in_yaml "${project}"
  done
}

# Ensure sources and constraints exist for targets without updating hashes.
# Clones repos at the pinned hashes already recorded in sources.txt and
# fetches upper-constraints.txt at those same hashes.
ensure_sources_for_targets() {
  local target="$1"
  local stream="$2"

  if [[ -z "${stream}" ]]; then
    echo "ERROR: STREAM is required for update-sources." >&2
    return 1
  fi

  local targets
  resolve_targets_array "${target}" || return 1
  targets=("${_RESOLVED_TARGETS[@]}")

  declare -A _ensure_projects_seen

  for img in "${targets[@]}"; do
    local project
    project="$(project_name "${img}")"

    if [[ -z "${project}" ]]; then
      if [[ "${img}" == "base" ]] && [[ -z "${_ensure_projects_seen[base]:-}" ]]; then
        _ensure_projects_seen["base"]=1
        ensure_project_constraints "base" "${stream}"
      fi
      continue
    fi

    ensure_sources_for_stream "${img}" "${stream}"

    if [[ -z "${_ensure_projects_seen[$project]:-}" ]]; then
      _ensure_projects_seen["${project}"]=1
      ensure_project_constraints "${project}" "${stream}"
    fi
  done
}

# Update sources.txt files for targets in scope.
# Clones source repos at branch tips to resolve hashes, fetches
# upper-constraints.txt, and updates pinned hashes in sources.txt.
update_sources() {
  local target="$1"
  local stream="$2"

  if [[ -z "${stream}" ]]; then
    echo "ERROR: STREAM is required for update-sources." >&2
    echo "       Example: STREAM=master ./build.sh update-sources watcher" >&2
    return 1
  fi

  local targets
  resolve_targets_array "${target}" || return 1
  targets=("${_RESOLVED_TARGETS[@]}")

  declare -A projects_seen

  for img in "${targets[@]}"; do
    local project
    project="$(project_name "${img}")"

    # Base container: flat layout, sources.txt directly in containers/base/
    if [[ -z "${project}" ]]; then
      if [[ "${img}" == "base" ]] && [[ -z "${projects_seen[base]:-}" ]]; then
        projects_seen["base"]=1
        local base_sources="${CONTAINERS_DIR}/base/sources.txt"
        if [[ -f "${base_sources}" ]]; then
          echo "--- Updating ${base_sources} (stream: ${stream}) ---"
          if ! update_sources_file "${base_sources}" "${stream}" \
                "${CONTAINERS_DIR}/base/src" \
                "${CONTAINERS_DIR}/base"; then
            echo "ERROR: Failed to update ${base_sources}" >&2
            return 1
          fi
        fi
      fi
      continue
    fi

    # Project-level sources.txt (only process once per project)
    if [[ -z "${projects_seen[$project]:-}" ]]; then
      projects_seen["${project}"]=1
      local project_sources="${CONTAINERS_DIR}/${project}/sources.txt"
      if [[ -f "${project_sources}" ]]; then
        echo "--- Updating ${project_sources} (stream: ${stream}) ---"
        if ! update_sources_file "${project_sources}" "${stream}" \
              "${CONTAINERS_DIR}/${project}/src" \
              "${CONTAINERS_DIR}/${project}"; then
          echo "ERROR: Failed to update ${project_sources}" >&2
          return 1
        fi
      fi
    fi

    # Image-level sources.txt
    local image_sources="${CONTAINERS_DIR}/${img}/sources.txt"
    if [[ -f "${image_sources}" ]]; then
      echo "--- Updating ${image_sources} (stream: ${stream}) ---"
      if ! update_sources_file "${image_sources}" "${stream}" \
            "${CONTAINERS_DIR}/${img}/src" \
            "${CONTAINERS_DIR}/${project}"; then
        echo "ERROR: Failed to update ${image_sources}" >&2
        return 1
      fi
    fi
  done
}

# Main
ACTION="${1:-}"
TARGET="${2:-all}"

case "${ACTION}" in
  build)
    resolve_targets_array "${TARGET}" || exit 1
    for img in "${_RESOLVED_TARGETS[@]}"; do
      build_image "${img}"
    done
    ;;
  build-parallel)
    resolve_targets_array "${TARGET}" || exit 1
    _bp_targets=("${_RESOLVED_TARGETS[@]}")
    if [[ ! "${PARALLEL}" =~ ^[1-9][0-9]*$ ]]; then
      echo "ERROR: PARALLEL must be a positive integer" >&2
      exit 1
    fi
    if [[ -n "${BUILD_LOGS_DIR:-}" ]]; then
      _bp_logdir="${BUILD_LOGS_DIR}"
      mkdir -p "${_bp_logdir}"
    else
      _bp_logdir=$(mktemp -d)
    fi

    # Build base first (all service images depend on it)
    for _bp_img in "${_bp_targets[@]}"; do
      [[ -n "$(project_name "${_bp_img}")" ]] && continue
      set -o pipefail
      build_image "${_bp_img}" 2>&1 |
        sed -u "s|^|[${_bp_img}] |" |
        tee "${_bp_logdir}/${_bp_img//\//_}.log"
    done

    # Pre-clone only for the standalone path. Prepared contexts are complete.
    if [[ -z "${S2I_CONTEXTS_ROOT}" ]]; then
      for _bp_img in "${_bp_targets[@]}"; do
        [[ -z "$(project_name "${_bp_img}")" ]] && continue
        ensure_sources_for_stream "${_bp_img}" "${STREAM}"
      done
    fi

    # Build service images in parallel (max PARALLEL at a time)
    _bp_service_imgs=()
    for _bp_img in "${_bp_targets[@]}"; do
      [[ -z "$(project_name "${_bp_img}")" ]] && continue
      _bp_service_imgs+=("${_bp_img}")
    done

    if [[ ${#_bp_service_imgs[@]} -gt 0 ]]; then
      echo "--- Building ${#_bp_service_imgs[@]} images (max ${PARALLEL} parallel) ---"
      _bp_pids=()
      _bp_fail=0
      _bp_running=0

      for _bp_img in "${_bp_service_imgs[@]}"; do
        while [[ ${_bp_running} -ge ${PARALLEL} ]]; do
          _bp_pid="${_bp_pids[0]}"
          if wait "${_bp_pid}"; then
            _bp_pids=("${_bp_pids[@]:1}")
            ((_bp_running--)) || true
          else
            _bp_fail=1
            break 2
          fi
        done

        _bp_log="${_bp_logdir}/${_bp_img//\//_}.log"
        (
          set -o pipefail
          build_image "${_bp_img}" 2>&1 |
            sed -u "s|^|[${_bp_img}] |" |
            tee "${_bp_log}"
        ) &
        _bp_pids+=("$!")
        ((_bp_running++)) || true
      done

      if [[ ${_bp_fail} -eq 0 ]]; then
        for _bp_pid in "${_bp_pids[@]}"; do
          if wait "${_bp_pid}"; then
            ((_bp_running--)) || true
          else
            _bp_fail=1
            break
          fi
        done
      fi

      if [[ ${_bp_fail} -eq 1 ]]; then
        echo "ERROR: A build failed; stopping remaining builds" >&2
        for _bp_pid in "${_bp_pids[@]}"; do
          kill "${_bp_pid}" 2>/dev/null || true
        done
        wait 2>/dev/null || true
        echo "Build logs are available in ${_bp_logdir}" >&2
        exit 1
      fi

      [[ -z "${BUILD_LOGS_DIR:-}" ]] && rm -rf "${_bp_logdir}"
      echo "=== All builds completed successfully ==="
    fi
    ;;
  push)
    resolve_targets_array "${TARGET}" || exit 1
    _push_targets=("${_RESOLVED_TARGETS[@]}")

    # Verify all images and tags exist before pushing any
    echo "--- Verifying all images exist locally ---"
    for img in "${_push_targets[@]}"; do
      verify_image_exists "${img}"
    done

    # All verified — push
    for img in "${_push_targets[@]}"; do
      push_image "${img}"
    done
    ;;
  refs)
    target_refs "${TARGET}"
    ;;
  update-sources)
    if [[ -n "${SKIP_HASH_UPDATE}" ]]; then
      echo "=== Skipping hash update (SKIP_HASH_UPDATE is set) ==="
      ensure_sources_for_targets "${TARGET}" "${STREAM}"
    else
      update_sources "${TARGET}" "${STREAM}"
    fi

    # Generate lockfiles and metadata
    echo ""
    echo "=== Generating rpms.in.yaml files ==="
    generate_rpms_in_for_targets "${TARGET}"

    echo ""
    echo "=== Generating requirements.lock files ==="
    generate_locks_for_targets "${TARGET}" "${STREAM}"

    echo ""
    echo "=== Generating buildrequirements.lock files ==="
    generate_buildlocks_for_targets "${TARGET}" "${STREAM}"

    # Create un-streamed symlinks for the default stream
    if [[ "${STREAM}" == "${DEFAULT_STREAM}" ]]; then
      echo ""
      echo "=== Creating default stream symlinks (${DEFAULT_STREAM}) ==="
      resolve_targets_array "${TARGET}" || exit 1
      _symlink_targets=("${_RESOLVED_TARGETS[@]}")
      declare -A _symlink_seen
      for _s_img in "${_symlink_targets[@]}"; do
        _s_project="$(project_name "${_s_img}")"
        if [[ -z "${_s_project}" ]]; then
          [[ "${_s_img}" != "base" ]] && continue
          _s_project="base"
        fi
        [[ -n "${_symlink_seen[$_s_project]:-}" ]] && continue
        _symlink_seen["${_s_project}"]=1

        _s_pdir="${CONTAINERS_DIR}/${_s_project}"
        for _s_suffix in "${UPSTREAM_CONSTRAINTS}" "${CONSTRAINTS_FILE}" "${BUILD_CONSTRAINTS_FILE}"; do
          _s_streamed="${_s_pdir}/${_s_suffix}.${DEFAULT_STREAM}"
          if [[ -f "${_s_streamed}" ]]; then
            ln -sf "${_s_suffix}.${DEFAULT_STREAM}" "${_s_pdir}/${_s_suffix}"
            echo "  ${_s_pdir}/${_s_suffix} -> ${_s_suffix}.${DEFAULT_STREAM}"
          fi
        done
      done
    fi
    ;;
  install-deps)
    SYSTEM_DEPS=(git buildah podman)
    echo "=== Installing system dependencies ==="
    echo "Packages: ${SYSTEM_DEPS[*]}"
    if command -v dnf &>/dev/null; then
      sudo dnf install -y "${SYSTEM_DEPS[@]}"
    elif command -v microdnf &>/dev/null; then
      sudo microdnf install -y "${SYSTEM_DEPS[@]}"
    elif command -v apt-get &>/dev/null; then
      sudo apt-get install -y "${SYSTEM_DEPS[@]}"
    else
      echo "ERROR: No supported package manager found (dnf, microdnf, yum, apt-get)" >&2
      exit 1
    fi

    echo ""
    echo "=== Done ==="
    ;;
  list)
    list_images
    ;;
  *)
    echo "Usage: STREAM=<name> $0 {build|build-parallel|push|refs|update-sources|install-deps|list} [image-name|all]"
    echo ""
    echo "Images (discovered from containers/):"
    for dir_name in $(discover_images); do
      echo "  ${dir_name} → $(image_name "${dir_name}")"
    done
    echo ""
    echo "sources.txt format:"
    echo "  <stream> <name> <repo-url> <branch-to-follow> <pinned-hash>"
    echo ""
    echo "Environment variables:"
    echo "  STREAM            Stream name (required for build)"
    echo "  REGISTRY          Container registry (default: localhost)"
    echo "  NAMESPACE         Registry namespace (default: openstack)"
    echo "  TAG               Image tag(s), comma-separated (default: latest)"
    echo "  IMAGE_PREFIX      Prefix for image names (default: openstack)"
    echo "  BASE_IMAGE        Base image for the base container"
    echo "  CONSTRAINTS_FILE  Constraints/lockfile base name (default: requirements.lock)"
    echo "  BUILD_CONSTRAINTS_FILE  Build-requirements lockfile base name (default: buildrequirements.lock)"
    echo "  DEFAULT_STREAM    Default stream for un-streamed symlinks (default: master)"
    echo "  PARALLEL          Max concurrent builds for build-parallel (default: nproc)"
    echo "  BUILD_LOGS_DIR    Persist build-parallel logs to this directory"
    echo "  SKIP_HASH_UPDATE  Skip updating pinned hashes; regenerate locks only"
    echo "  PIP_NO_BINARY     Pass PIP_NO_BINARY to container build (e.g., ':all:')"
    echo "  REGISTRY_AUTH_FILE  Registry authentication file for pushes"
    echo "  REGISTRY_CERT_DIR   Registry TLS certificate directory for pushes"
    echo ""
    echo "Source directories: containers/<project>/src/<name>/"
    echo "Overrides:          containers/<project>/src/overrides/<pkg>/"
    exit 1
    ;;
esac
