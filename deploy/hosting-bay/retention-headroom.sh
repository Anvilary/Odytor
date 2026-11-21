#!/usr/bin/env bash
# Review and remove exact, operator-inventoried deployment artifacts.
# This intentionally does not inspect or prune container build caches.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: retention-headroom.sh --root DIR [--root DIR ...] --candidates FILE --keep FILE
       [--min-free-mib N] [--apply]

Candidate and keep files contain one absolute path per line. Blank lines and
lines beginning with # are ignored. Every candidate must be inside a supplied
root and is only removed with --apply. Protected paths are always skipped.
EOF
}

roots=()
candidates=
keep=
min_free_mib=2048
apply=0
while (($#)); do
  case "$1" in
    --root) (($# >= 2)) || { usage >&2; exit 2; }; roots+=("$2"); shift 2 ;;
    --candidates) (($# >= 2)) || { usage >&2; exit 2; }; candidates=$2; shift 2 ;;
    --keep) (($# >= 2)) || { usage >&2; exit 2; }; keep=$2; shift 2 ;;
    --min-free-mib) (($# >= 2)) || { usage >&2; exit 2; }; min_free_mib=$2; shift 2 ;;
    --apply) apply=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ ${#roots[@]} -gt 0 && -n $candidates && -n $keep ]] || { usage >&2; exit 2; }
[[ $min_free_mib =~ ^(0|[1-9][0-9]{0,8})$ ]] || { echo "min-free-mib must be a nonnegative integer" >&2; exit 2; }
[[ -r $candidates && -r $keep ]] || { echo "candidate and keep files must be readable" >&2; exit 2; }

for i in "${!roots[@]}"; do
  [[ -d ${roots[$i]} ]] || { echo "root is not a directory: ${roots[$i]}" >&2; exit 2; }
  roots[$i]=$(cd "${roots[$i]}" && pwd -P)
  [[ ${roots[$i]} != / ]] || { echo "the filesystem root is too broad" >&2; exit 2; }
done

declare -A protected=()
while IFS= read -r path || [[ -n $path ]]; do
  [[ -z $path || $path == \#* ]] && continue
  [[ $path = /* ]] || { echo "keep paths must be absolute: $path" >&2; exit 2; }
  protected["$(realpath -m -- "$path")"]=1
done < "$keep"

mount_table=$(findmnt --raw --noheadings --output TARGET)
declare -A selected=()
while IFS= read -r path || [[ -n $path ]]; do
  [[ -z $path || $path == \#* ]] && continue
  [[ $path = /* ]] || { echo "candidate paths must be absolute: $path" >&2; exit 2; }
  [[ -e $path || -L $path ]] || { echo "missing candidate: $path" >&2; continue; }
  [[ ! -L $path ]] || { echo "refusing symlink candidate: $path" >&2; continue; }
  real=$(realpath -m -- "$path")
  allowed=0
  for root in "${roots[@]}"; do
    [[ $real == "$root"/* ]] && allowed=1
  done
  [[ $allowed == 1 ]] || { echo "outside approved roots: $path" >&2; exit 2; }
  keep_path=0
  for protected_path in "${!protected[@]}"; do
    if [[ $real == "$protected_path" || $real == "$protected_path"/* || $protected_path == "$real"/* ]]; then
      keep_path=1
    fi
  done
  if [[ $keep_path == 1 ]]; then
    echo "KEEP $path"
    continue
  fi
  [[ -f $real || -d $real ]] || { echo "refusing a special file: $path" >&2; exit 2; }
  while IFS= read -r mount; do
    mount=$(printf '%b' "$mount")
    if [[ $mount == "$real" || $mount == "$real"/* ]]; then
      echo "refusing candidate containing a mount: $path" >&2
      exit 2
    fi
  done <<< "$mount_table"
  selected["$real"]=1
done < "$candidates"

for path in "${!selected[@]}"; do
  for parent in "${!selected[@]}"; do
    [[ $path != "$parent"/* ]] || { echo "nested candidates: $parent and $path" >&2; exit 2; }
  done
done

for path in "${!selected[@]}"; do
  bytes=$(du -s -B1 -- "$path" | cut -f1)
  if [[ $apply == 1 ]]; then
    printf 'REMOVE %s B %s\n' "$bytes" "$path"
    rm -rf --one-file-system -- "$path"
  else
    printf 'WOULD REMOVE %s B %s\n' "$bytes" "$path"
  fi
done

blocked=0
for root in "${roots[@]}"; do
  avail=$(df -Pm -- "$root" | awk 'NR==2 {print $4}')
  printf 'HEADROOM %s %s MiB available (minimum %s MiB)\n' "$root" "$avail" "$min_free_mib"
  if ((avail < min_free_mib)); then
    echo "HEADROOM BELOW THRESHOLD: $root" >&2
    blocked=1
  fi
done
exit "$blocked"
