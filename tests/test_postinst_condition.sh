#!/usr/bin/env bash

# nano test_postinst_condition.sh
# chmod +x ./test_postinst_condition.sh
# ./test_postinst_condition.sh

set -euo pipefail

should_cleanup() {
  local old="${1:-}" new="${2:-}"
  if dpkg --compare-versions "$new" ge "1.6.0~"; then
    if [ -z "$old" ] || dpkg --compare-versions "$old" lt "1.6.0" || [ "$old" = "1.6.2-wb100" ]; then
      return 0
    fi
  fi
  return 1
}

printf "%-20s %-28s %-6s %-6s\n" "OLD" "NEW" "RESULT" "EXP"
printf "%-20s %-28s %-6s %-6s\n" "--------------------" "----------------------------" "------" "------"

pass=0; fail=0
while read -r old new exp _; do
  [ -z "${old:-}" ] && continue
  case "$old" in \#*) continue;; esac
  [ "$old" = "-" ] && old=""

  if should_cleanup "$old" "$new"; then res="YES"; else res="NO"; fi
  printf "%-20s %-28s %-6s %-6s\n" "${old:-<empty>}" "$new" "$res" "$exp"
  if [ "$res" = "$exp" ]; then pass=$((pass+1)); else fail=$((fail+1)); fi
done <<'CASES'
# OLD            NEW                          EXP
1.6.2-wb100      1.6.0~exp~PR+57~4~g7c0ae31    YES
1.6.2-wb100      1.6.0                         YES
1.5.14           1.6.0~exp~PR+57~4~g7c0ae31    YES
1.5.14           1.6.0                         YES
-                1.6.0                         YES
-                1.5.14                        NO
1.6.2-wb100      1.5.14                        NO
1.5.11           1.5.14                        NO
1.6.0            1.6.1                         NO
CASES

echo
echo "Summary: PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
