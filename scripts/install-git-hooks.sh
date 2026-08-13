#!/usr/bin/env bash
# 将 core.hooksPath 指向仓库内的 scripts/git-hooks，使本地提交自动校验。
set -euo pipefail
repo_root="$(git rev-parse --show-toplevel)"
hooks_dir="$repo_root/scripts/git-hooks"
[ -d "$hooks_dir" ] || { echo "❌ 未找到 $hooks_dir" >&2; exit 1; }

# core.hooksPath 需 git >= 2.9.0
git_ver="$(git --version | awk '{print $3}')"
if ! awk -v v="$git_ver" 'BEGIN{split(v,a,"."); if (a[1]>2 || (a[1]==2 && a[2]>=9)) exit 0; exit 1}'; then
  echo "❌ 当前 git 版本 $git_ver 不支持 core.hooksPath（需 >= 2.9.0）" >&2
  exit 1
fi

chmod +x "$hooks_dir"/* 2>/dev/null || true
git -C "$repo_root" config core.hooksPath "$hooks_dir"
echo "✅ 已安装：core.hooksPath -> $hooks_dir"
