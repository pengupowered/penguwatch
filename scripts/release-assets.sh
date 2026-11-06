#!/usr/bin/env bash
# Assembles what a release attaches, from the wheel and sdist that CI built:
#   penguwatch-X.Y.Z-py3-none-any.whl, penguwatch-X.Y.Z.tar.gz   the package
#   penguwatch-X.Y.Z.lock      requirements.txt of that commit: the hash-locked dependencies the image installs
#   install.sh, install.ps1    the installers (Linux and macOS, Windows): they install exactly the files above
#   SHA256SUMS                 sha256 of every file above
# CI builds this once (job "release-assets"); the release job attaches the same files and attests them.
#
#   scripts/release-assets.sh <dist-dir> <out-dir>
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dist="${1:?usage: release-assets.sh <dist-dir> <out-dir>}"
out="${2:?usage: release-assets.sh <dist-dir> <out-dir>}"

shopt -s nullglob
wheels=("$dist"/penguwatch-*.whl)
[ "${#wheels[@]}" -eq 1 ] || { echo "release-assets: expected exactly one penguwatch-*.whl in $dist, found ${#wheels[@]}" >&2; exit 1; }
version="$(basename "${wheels[0]}" | sed -n 's/^penguwatch-\([0-9][^-]*\)-.*\.whl$/\1/p')"
[ -n "$version" ] || { echo "release-assets: cannot read the version from ${wheels[0]}" >&2; exit 1; }
[ -f "$dist/penguwatch-$version.tar.gz" ] || { echo "release-assets: $dist/penguwatch-$version.tar.gz is missing" >&2; exit 1; }

mkdir -p "$out"
rm -f "$out"/penguwatch-* "$out"/install.sh "$out"/install.ps1 "$out"/SHA256SUMS
cp "${wheels[0]}" "$dist/penguwatch-$version.tar.gz" "$out/"
cp "$ROOT/requirements.txt" "$out/penguwatch-$version.lock"
cp "$ROOT/scripts/install.sh" "$ROOT/scripts/install.ps1" "$out/"
chmod 755 "$out/install.sh"
(cd "$out" && sha256sum penguwatch-"$version"-*.whl penguwatch-"$version".tar.gz penguwatch-"$version".lock install.sh install.ps1 > SHA256SUMS)
echo "release-assets: $version"
cat "$out/SHA256SUMS"
