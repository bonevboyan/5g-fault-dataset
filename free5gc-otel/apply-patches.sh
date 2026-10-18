#!/usr/bin/env bash
# Re-apply this project's source changes to fresh upstream checkouts:
#   free5gc-src/NFs/{amf,smf,udm,udr,pcf}  <- patches/applied/<nf>.diff   (internal/ only)
#   util-patched                           <- patches/applied/util.diff   (incl. go.mod replace)
# The per-NF diffs are cumulative snapshots of the working tree (generated with
# `git diff -- internal/`), so they apply in one step. The topic-split patches in patches/*.patch
# are the readable version of the same changes. go.mod/go.sum edits are done by build.sh.
# Run after: git submodule update --init --recursive
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../free5gc-src"
for nf in amf smf udm udr pcf; do
  d="$SRC/NFs/$nf"; p="$HERE/patches/applied/$nf.diff"
  if ! git -C "$d" diff --quiet -- internal/; then echo "skip $nf: internal/ already modified"; continue; fi
  git -C "$d" apply --check "$p" && git -C "$d" apply "$p" && echo "patched $nf"
done
d="$HERE/util-patched"; p="$HERE/patches/applied/util.diff"
if git -C "$d" diff --quiet; then
  git -C "$d" apply --check "$p" && git -C "$d" apply "$p" && echo "patched util"
else
  echo "skip util: already modified"
fi
