#!/usr/bin/env bash
# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

# repair_checker.sh — restore the pinned canonical checker (PLAN W0 Task 2).
#
# usage: bash scripts/repair_checker.sh [project-root]
#
# Copies $VIBEWEAVER_HOME/scripts/assert_artifacts.py to
# <project>/tests/assert_artifacts.py and prints old/new digests. Never edits
# the canonical copy; never touches acceptance.md or verification_log.md.
set -euo pipefail

ROOT="${1:-.}"
if [[ ! -d "$ROOT" ]]; then
  echo "repair_checker: project root not found: $ROOT" >&2
  exit 2
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${VIBEWEAVER_HOME:-}" ]]; then
  HOME_DIR="$VIBEWEAVER_HOME"
else
  HOME_DIR=""
  for cand in "$SCRIPT_DIR/.." "$SCRIPT_DIR/../backend/skills/vibeweaver" "${HOME}/.config/opencode/skills/vibeweaver"; do
    if [[ -f "$cand/scripts/assert_artifacts.py" && -f "$cand/scripts/assert_artifacts.sha256" ]]; then
      HOME_DIR="$cand"
      break
    fi
  done
  HOME_DIR="${HOME_DIR:-${HOME}/.config/opencode/skills/vibeweaver}"
fi
SRC="$HOME_DIR/scripts/assert_artifacts.py"
PIN="$HOME_DIR/scripts/assert_artifacts.sha256"
DST="$ROOT/tests/assert_artifacts.py"

if [[ ! -f "$SRC" || ! -f "$PIN" ]]; then
  echo "repair_checker: canonical checker or pin missing under $HOME_DIR/scripts" >&2
  echo "  install a release bundle (vibeweaver/install.sh) first" >&2
  exit 1
fi

mkdir -p "$ROOT/tests"
if [[ -f "$DST" ]]; then
  OLD="$(shasum -a 256 "$DST" 2>/dev/null | awk '{print $1}')"
else
  OLD="(missing)"
fi
cp "$SRC" "$DST"
NEW="$(shasum -a 256 "$DST" | awk '{print $1}')"
echo "repair_checker: repaired $DST"
echo "  old sha256: $OLD"
echo "  new sha256: $NEW"
echo "  pinned:     $(awk '{print $1}' "$PIN")"
if [[ "$NEW" != "$(awk '{print $1}' "$PIN")" ]]; then
  echo "repair_checker: WARNING — canonical copy does not match its own pin; reinstall the release bundle" >&2
  exit 1
fi
