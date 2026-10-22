#!/bin/sh
# Fail if any commit message in the range, or any annotated tag message,
# carries an attribution line. Lightweight tags have no message of their own
# and are skipped. Usage: check_commit_trailers.sh [range]
range="${1:-origin/master..HEAD}"
pattern='co-authored-by|claude-session|generated with|claude code|anthropic'
found=$(git log --format='commit %h%n%B' "$range" | grep -inE "$pattern")
for t in $(git for-each-ref refs/tags --format='%(objecttype):%(refname:short)' | sed -n 's/^tag://p'); do
  hit=$(git cat-file tag "$t" | sed '1,/^$/d' | grep -iE "$pattern")
  [ -n "$hit" ] && found="$found
tag $t: $hit"
done
if [ -n "$found" ]; then
  echo "attribution lines found:"
  echo "$found"
  exit 1
fi
echo "clean: no attribution lines in $range or in annotated tag messages"
exit 0
