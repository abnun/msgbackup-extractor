#!/usr/bin/env bash
# Gate before any push: refuse if personal or private data is about to leave
# this machine. Scans the working tree AND the full commit history, because a
# clean working tree says nothing about what an earlier commit contains.
#
#   scripts/check-sensitive.sh          working tree + history
#   scripts/check-sensitive.sh --tree   working tree only (faster)
#
# Exit 0 = clean, 1 = findings. It never fixes anything: a hit needs a human
# decision. Working-tree hits mean editing files; history hits mean rewriting
# history, which is a different and much larger operation.
set -uo pipefail
cd "$(dirname "$0")/.."

# Deliberately broad. Extend rather than narrow.
PATTERNS=(
  'mark\.mueller@'                       # work address, must not be public
  '[Bb]echtle'                           # employer name
  '/Users/[a-z]+\.[a-z]+/'               # machine-specific absolute paths
  'MobileSync/Backup/[0-9a-fA-F]{8,}'    # a real device backup path
  '[0-9a-f]{8}-[0-9A-F]{16}'             # iPhone UDID, modern form
  'BACKUP_PASSWORD'                      # a password that reached the code
  # Facts about the author's own device and message volume. A public document
  # may show what was verified; it must not show how much mail the author has.
  'iPhone ?[0-9]{1,2}\\b'                  # device model
  'iOS [0-9]{1,2}\\.[0-9]'                 # installed OS version
  '[0-9]+[.,][0-9]+ ?(GB|TB)'            # backup or export volume
  '[0-9]{1,3}[.,][0-9]{3} (Dateien|files|Eintr|entries|Nachrichten|messages)'
)

hits=0

# Known-harmless strings live in an explicit list with a reason, so every
# exception is reviewable. The list also covers history, because an old commit
# cannot be annotated after the fact.
ALLOWLIST=scripts/sensitive-allowlist.txt
drop_allowed() {
  if [ -s "$ALLOWLIST" ]; then
    grep -vFf <(grep -vE '^[[:space:]]*(#|$)' "$ALLOWLIST")
  else
    cat
  fi
}

report() {  # report <label> <pattern> <matches>
  printf '\n  [%s] %s\n' "$1" "$2"
  printf '%s\n' "$3" | head -20 | sed 's/^/      /'
  hits=$((hits + 1))
}

printf 'Working tree\n'
tree_hits=0
for p in "${PATTERNS[@]}"; do
  out=$(git grep -nIiE -- "$p" -- ':!scripts/check-sensitive.sh' 2>/dev/null | drop_allowed) || continue
  [ -n "$out" ] && { report tree "$p" "$out"; tree_hits=1; }
done
[ "$tree_hits" -eq 0 ] && printf '  clean\n'

if [ "${1:-}" != "--tree" ]; then
  printf '\nCommit history (authors, messages, diffs)\n'
  # The pathspec excludes this script and its list from the diffs: they contain
  # the patterns by definition, and once committed they would match themselves
  # forever. Commit messages and authors come from --format and are unaffected.
  # %B is the FULL message, not %s: a figure hidden in a commit body would
  # otherwise slip through unseen.
  log=$(git log --all -p --format='COMMIT %H %an <%ae>%n%B' -- . \
        ':!scripts/check-sensitive.sh' ':!scripts/sensitive-allowlist.txt' 2>/dev/null)
  hist_hits=0
  for p in "${PATTERNS[@]}"; do
    out=$(printf '%s' "$log" | grep -nIiE -- "$p" | drop_allowed) || continue
    [ -n "$out" ] && { report history "$p" "$out"; hist_hits=1; }
  done
  [ "$hist_hits" -eq 0 ] && printf '  clean\n'

  # filter-branch leaves its originals behind. They are unreachable from any
  # branch but still in the object database, and `git push --mirror` would
  # carry them along.
  if [ -d .git-rewrite ] || git show-ref --quiet refs/original 2>/dev/null; then
    printf '\n  [refs] filter-branch leftovers present (.git-rewrite or refs/original)\n'
    printf '        These hold the pre-rewrite objects. Remove them and run\n'
    printf '        git reflog expire --expire=now --all && git gc --prune=now\n'
    hits=$((hits + 1))
  fi
fi

printf '\n'
if [ "$hits" -gt 0 ]; then
  printf 'REFUSED: %d finding(s). Do not push.\n' "$hits"
  exit 1
fi
printf 'OK: nothing sensitive found. Safe to push.\n'
