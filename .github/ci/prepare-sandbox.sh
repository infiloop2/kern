#!/usr/bin/env bash
# Share dependency images through GHCR. Only trusted main workflows publish;
# PRs can pull an exact dependency hash or build their own image on a miss.
set -euo pipefail

if [[ "$#" -ne 2 || ( "$1" != pull && "$1" != publish ) ]]; then
  echo "Usage: $0 <pull|publish> <registry-image>" >&2
  exit 1
fi

mode="$1"
registry_image="$2"
dependency_hash="$(sha256sum .github/ci/sandbox.Dockerfile .github/ci/requirements.txt | sha256sum | cut -d ' ' -f 1)"
image_ref="${registry_image}:deps-${dependency_hash}"

if [[ "$mode" == publish ]]; then
  if docker manifest inspect "$image_ref" > /dev/null 2>&1; then
    echo "Dependency image already published: ${image_ref}"
    exit 0
  fi
elif docker pull "$image_ref"; then
  docker tag "$image_ref" kern-ci:sandbox
  echo "Using published dependency image: ${image_ref}"
  exit 0
else
  echo "::notice::Dependency image unavailable; building this checkout's exact dependencies locally."
fi

# The image contains only build inputs and dependencies, never the repository
# checkout or credentials. Keep this input list in sync with the hash above.
build_context="$(mktemp -d "${RUNNER_TEMP:-/tmp}/kern-ci-build.XXXXXX")"
trap 'rm -rf "$build_context"' EXIT
mkdir -p "$build_context/.github/ci"
cp .github/ci/sandbox.Dockerfile "$build_context/Dockerfile"
cp .github/ci/requirements.txt "$build_context/.github/ci/requirements.txt"
docker build --tag kern-ci:sandbox \
  --label "org.opencontainers.image.source=https://github.com/${GITHUB_REPOSITORY:-infiversehq/kern}" \
  "$build_context"

if [[ "$mode" == publish ]]; then
  docker tag kern-ci:sandbox "$image_ref"
  docker push "$image_ref"
fi
