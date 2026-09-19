#!/usr/bin/env bash
# plan レビューの marker を書く唯一の経路。
#   plan-approve.sh <plan ファイル> <Codex の出力ファイル>
#   plan-approve.sh --skip <plan ファイル>     人が skip を承認した trivial な plan
# Codex の出力に BLOCKING の verdict が1つでもあれば、解釈によらず拒否する。
# 出力が「その plan」に対するものかは、冒頭の PLAN-SHA 行で確認する。
set -u

marker="$HOME/.claude/.zaitaku-mealpace-plan-codex-reviewed"
faces=(制約 評価 設計)

die() { echo "plan-approve.sh: $1" >&2; exit 1; }

skip=0
if [ "${1:-}" = "--skip" ]; then skip=1; shift; fi
plan="${1:-}"
[ -n "$plan" ] && [ -s "$plan" ] || die "plan ファイルがありません: ${plan:-<未指定>}"

# plan-gate.sh と同じ計算。ファイルをそのままハッシュする。text: は hook の入力に
# planFilePath が無い場合の照合用で、末尾の空白と改行を落とした本文のハッシュ。
h=$(sha256sum < "$plan" | cut -c1-16)
text=$(cat "$plan")
text="${text%"${text##*[![:space:]]}"}"
ht=$(printf '%s' "$text" | sha256sum | cut -c1-16)

if [ "$skip" -eq 0 ]; then
  out="${2:-}"
  [ -n "$out" ] && [ -s "$out" ] || die "Codex の出力ファイルがありません: ${out:-<未指定>}"
  grep -qE "^[[:space:]*\`-]*PLAN-SHA:[[:space:]]*${h}([^0-9a-f]|\$)" "$out" \
    || die "出力に現在の plan と一致する PLAN-SHA 行がありません (期待: PLAN-SHA: ${h})。別の plan か、直す前の plan へのレビューなので承認しません"
  # 行頭の装飾（- * ` 空白）は許す
  verdicts=$(grep -E '^[[:space:]*`-]*VERDICT:' "$out" || true)
  for f in "${faces[@]}"; do
    printf '%s\n' "$verdicts" | grep -qE "VERDICT:[[:space:]]*${f}[^:]*:[[:space:]]*(LGTM|CONCERN|BLOCKING)" \
      || die "「${f}」面の VERDICT 行がありません。出力形式が崩れているので承認しません"
  done
  if printf '%s\n' "$verdicts" | grep -q 'BLOCKING'; then
    printf '%s\n' "$verdicts" >&2
    die "BLOCKING があります。plan を直して再レビューしてください"
  fi
fi

{
  echo "$h"
  echo "text: $ht"
  echo "plan: $plan"
  [ "$skip" -eq 1 ] && echo "skip: 人の承認による" || echo "codex: $out"
  date -u +%Y-%m-%dT%H:%M:%SZ
} > "$marker"
echo "承認: $h ($plan)"
