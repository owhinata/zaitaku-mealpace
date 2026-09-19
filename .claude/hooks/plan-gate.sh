#!/usr/bin/env bash
# ExitPlanMode の PreToolUse gate。
# 制約領域に触れる plan は、codex-review でレビュー済みの plan と本文が一致するときだけ通す。
# marker には plan-approve.sh が plan ファイルの sha256（先頭16桁）を書く。
# ハッシュは plan-approve.sh と同じ計算にする: planFilePath が取れればファイルをそのまま、
# tool_input.plan しか無ければ末尾の空白と改行を落とした本文（marker の text: 行と照合）。
set -u

marker="$HOME/.claude/.zaitaku-mealpace-plan-codex-reviewed"
# 制約領域のパスとキーワード。plan 本文にどれも無ければレビューを要求しない
# （書かずに触った場合は edit-gate.sh が止める）。
scope='analysis/|firmware/|data/|models/|tools/record|frame\.h|data-schema|evaluation\.md|evaluat|split|LED|p1|Edge Impulse|量子化|特徴量|波形|生データ|評価|分割|センサ|目安'

block() {
  jq -cn --arg r "$1" '{decision:"block",reason:$r}'
  exit 2
}

command -v jq >/dev/null || { echo '{"decision":"block","reason":"plan-gate.sh: jq が見つかりません"}'; exit 2; }

input=$(cat)
planfile=$(printf '%s' "$input" | jq -r '.tool_input.planFilePath // empty')
plan_of() {
  if [ -n "$planfile" ] && [ -f "$planfile" ]; then
    cat "$planfile"
  else
    printf '%s' "$input" | jq -j '.tool_input.plan // empty'
  fi
}

[ -n "$(plan_of)" ] || block "plan-gate.sh: hook の入力から plan 本文を取れません（tool_input.plan も planFilePath も無い）。Claude Code の入力形式が変わった可能性があります。.claude/hooks/plan-gate.sh を確認してください。"

plan_of | grep -qiE "$scope" || exit 0

if [ -n "$planfile" ] && [ -f "$planfile" ]; then
  h=$(sha256sum < "$planfile" | cut -c1-16)
  approved=$([ -f "$marker" ] && head -n1 "$marker")
else
  text=$(plan_of)
  text="${text%"${text##*[![:space:]]}"}"
  h=$(printf '%s' "$text" | sha256sum | cut -c1-16)
  approved=$([ -f "$marker" ] && sed -n 's/^text: //p' "$marker")
fi
[ -n "$approved" ] && [ "$approved" = "$h" ] && exit 0

block "この plan は制約領域に触れますが、レビュー済みの plan と一致しません (hash $h)。codex-review skill でレビューし、.claude/hooks/plan-approve.sh で承認してください (docs/workflow.md「レビュー（Codex）」)。plan を一文字でも変えたら再レビューが要ります。ゲートは BLOCKING だけで、CONCERN は人が採否を決めます。trivial な plan で skip する場合は、人の承認を得てから plan-approve.sh --skip ${planfile:-<plan ファイル>}"
