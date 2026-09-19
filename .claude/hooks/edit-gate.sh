#!/usr/bin/env bash
# Edit / Write の PreToolUse gate。
# 制約領域のファイルは、このセッションで承認済みの plan があるときだけ編集できる。
# plan mode を通らずに手を動かした場合を止める。marker は SessionStart で消える。
# 判定できないとき（jq が無い、プロジェクトのルートが取れない）は block する。
set -u

marker="$HOME/.claude/.zaitaku-mealpace-plan-codex-reviewed"
restricted='^(analysis/|firmware/|tools/record\.py$|docs/evaluation\.md$|docs/data-schema\.md$)'

command -v jq >/dev/null || { echo '{"decision":"block","reason":"edit-gate.sh: jq が見つかりません"}'; exit 2; }

input=$(cat)
path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // .tool_input.notebook_path // empty')
[ -n "$path" ] || { echo '{"decision":"block","reason":"edit-gate.sh: hook の入力から編集対象のパスを取れません。入力形式が変わった可能性があります。"}'; exit 2; }

root="${CLAUDE_PROJECT_DIR:-$(printf '%s' "$input" | jq -r '.cwd // empty')}"
[ -n "$root" ] || { jq -cn --arg r "edit-gate.sh: CLAUDE_PROJECT_DIR も cwd も取れず、$path が制約領域かどうか判定できません。" '{decision:"block",reason:$r}'; exit 2; }
rel="${path#"$root"/}"

printf '%s' "$rel" | grep -qE "$restricted" || exit 0
[ -s "$marker" ] && exit 0

jq -cn --arg r "$rel は制約領域です。承認済みの plan がありません。plan mode で plan を書き、codex-review skill でレビューしてから編集してください (docs/workflow.md「レビュー（Codex）」)。" '{decision:"block",reason:$r}'
exit 2
