---
inclusion: always
---

# Git 提交执行指令（AI 必读）

规范正本见 `docs/knowledge/10-提交规范.md`，冲突时以正本为准。

## 触发

用户要求「提交 / commit / 推送 / push」时执行。

## 工作流（AI 生成提交，人工 push）

1. `git status` / `git diff` 确认改动范围。
2. 生成符合 `<type>(<scope>): <subject>` 的提交信息。
3. 只 `git add` 明确涉及的文件，禁止 `git add .` / `-A`。
4. `git commit` 后展示 diff 摘要与提交信息，**绝不自动 `git push`**——除非用户在同一轮明确说了「推送 / push」。
5. 返工/review 修改一律追加新提交；禁止对已 push 的提交 `--amend` / `rebase` 改写历史，仅未 push 的提交信息 typo 可在 push 前 `--amend`。

## 分支命名

`<type>/<负责人>_<说明>_<日期MMDD>`

示例：`feat/meng_admin-date-range_0813`

校验正则：`^(feat|fix|docs|refactor|perf|test|chore|ci|style)/[a-z][a-z0-9]*_.+_([0-9]{4}|[0-9]{8})$`

长期分支 `master` / `main` / `dev` / `release/*` 自动放行。

## 硬性约束

- header（首行）不超过 50 字，结尾不加句号。
- 禁止含糊 subject（优化代码/修改注释/添加日志）。
- 破坏性变更在 body 说明并 footer 标注 `BREAKING CHANGE`。
- type 不驱动版本号，仅用于可读性。
- MR 标题同样遵循该格式。

## scope 词表（InkTime）

`admin` / `analysis` / `render` / `server` / `worker` / `display` / `esp32` / `config` / `db` / `upload` / `trash` / `auth`

## type 速查

```
feat     新功能          fix      修复bug
docs     文档            refactor 重构（不改行为）
perf     性能            test     测试
chore    杂务/依赖        ci       CI配置
style    格式（不改行为）
```
