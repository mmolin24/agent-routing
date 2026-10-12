#!/bin/bash
# Claude Code status line: shows plan usage ("5h 23% · 7d 41%") and keeps the latest
# figures in ~/.claude/usage/rate_limits.json for agent-routing's dashboard.
# Claude Code passes `rate_limits` (Pro/Max plans) in the JSON on stdin; it's the only
# supported source of plan usage. Never fails: a non-zero exit would blank the line.
input=$(cat)
out="$HOME/.claude/usage/rate_limits.json"

if printf '%s' "$input" | jq -e '.rate_limits | (.five_hour // .seven_day)' >/dev/null 2>&1; then
  mkdir -p "$(dirname "$out")" 2>/dev/null
  tmp=$(mktemp "${out}.XXXXXX" 2>/dev/null) && {
    printf '%s' "$input" | jq -c --argjson t "$(date +%s)" \
      '{captured_at: $t, session_id, version, rate_limits}' >"$tmp" 2>/dev/null \
      && mv -f "$tmp" "$out" 2>/dev/null || rm -f "$tmp" 2>/dev/null
  }
fi

line=$(printf '%s' "$input" | jq -r '
  def pct(w): if w then "\(w.used_percentage | floor)%" else empty end;
  [ (if .rate_limits.five_hour then "5h " + pct(.rate_limits.five_hour) else empty end),
    (if .rate_limits.seven_day then "7d " + pct(.rate_limits.seven_day) else empty end) ]
  | join(" · ")' 2>/dev/null)
model=$(printf '%s' "$input" | jq -r '.model.display_name // empty' 2>/dev/null)
printf '%s\n' "${model:+$model · }${line:-usage n/a}"
exit 0
