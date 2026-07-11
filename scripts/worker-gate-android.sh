#!/usr/bin/env bash
# Compile Android subprojects affected by the current git diff.
#
# Diff semantics intentionally mirror scripts/affected_tests.py:
#   worker-gate-android.sh [<ref>]
# With a ref, inspect everything differing from it. Without one, inspect the
# diff from HEAD's merge-base with main plus untracked files.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR/.." rev-parse --show-toplevel)"

changed_files() {
  local ref="${1:-}"
  if [[ -n "$ref" ]]; then
    git -C "$REPO_ROOT" diff --name-only "$ref"
    return
  fi

  local base
  base="$(git -C "$REPO_ROOT" merge-base HEAD main 2>/dev/null || true)"
  git -C "$REPO_ROOT" diff --name-only "${base:-HEAD}"
  git -C "$REPO_ROOT" ls-files --others --exclude-standard
}

mapfile -t files < <(changed_files "${1:-}" | sort -u)
projects=()

for file in "${files[@]}"; do
  case "$file" in
    android/hermes-voice/*)
      projects+=("android/hermes-voice")
      ;;
    android/hermes-dictate/*)
      projects+=("android/hermes-dictate")
      ;;
  esac
done

if [[ ${#projects[@]} -eq 0 ]]; then
  echo "worker-gate-android: no Android changes in hermes-voice or hermes-dictate — skipping Kotlin compile"
  exit 0
fi

# Worker shells are non-login: JAVA_HOME is usually unset there, but Gradle
# refuses to start without it. Best-effort probe of the known JDK locations;
# when none is found, gradlew itself fails with its own clear message.
if [[ -z "${JAVA_HOME:-}" ]]; then
  for candidate in "${HOME:-/nonexistent}/Android/jdk" /usr/lib/jvm/default-java /usr/lib/jvm/*; do
    if [[ -x "$candidate/bin/java" ]]; then
      export JAVA_HOME="$candidate"
      break
    fi
  done
fi

mapfile -t projects < <(printf '%s\n' "${projects[@]}" | sort -u)
for project in "${projects[@]}"; do
  project_dir="$REPO_ROOT/$project"
  gradlew="$project_dir/gradlew"
  if [[ ! -f "$gradlew" ]]; then
    echo "worker-gate-android: missing Gradle wrapper: $gradlew" >&2
    exit 1
  fi
  if [[ ! -x "$gradlew" ]]; then
    echo "worker-gate-android: Gradle wrapper is not executable: $gradlew" >&2
    exit 1
  fi

  echo "worker-gate-android: compiling $project"
  (cd "$project_dir" && ./gradlew :app:compileDebugKotlin)
done
