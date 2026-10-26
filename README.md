# tmux_claude_session

探测每个 tmux 窗口/窗格的工作目录，以及其中运行的 [Claude Code](https://claude.com/claude-code) 会话 ID。

在 tmux 里同时开着很多个 Claude Code 时，用它可以快速看清：哪个窗口在哪个目录、跑的是哪个会话、当前状态如何、多久没有新消息了，方便 `claude --resume <id>` 找回会话，或发现同一会话被重复打开。

## 特性

- 按窗口分组输出；分屏窗口中的每个窗格单独列出，并标注位置（左、右上等）
- 显示会话 ID、状态（如 `idle`）、会话名，以及最后一条消息（用户输入或 Claude 输出）距今多久
- 同一会话在多个窗格中运行时给出提示
- 支持 JSON 输出，便于被其他脚本调用
- 只依赖 Python 标准库，单文件，无需安装

## 环境要求

- Linux（依赖 `/proc` 文件系统）
- Python ≥ 3.7
- tmux（需要有正在运行的 tmux server）

## 用法

```bash
./tmux_probe.py              # 表格输出所有窗格
./tmux_probe.py -c           # 只显示运行 Claude 的窗格
./tmux_probe.py --json       # JSON 输出
```

可以把它链接到 PATH 中方便调用：

```bash
ln -s "$PWD/tmux_probe.py" ~/.local/bin/tmux_probe
```

## 输出示例

```
窗口      窗口名   窗格     工作目录         Claude 会话 ID                         状态    最后消息  会话名
---------------------------------------------------------------------------------------------------------
main:0    claude   .0       ~/projects/foo   0f8c2a1e-5b7d-4c3a-9e21-7d4b6a8c1f02   idle    3m前      foo-refactor
dev:1     work     .0 左    ~/projects/bar   7a3e9b40-21cd-4f6e-8a15-c9d0e2b7f364   idle    12s前     bar-tests
                   .1 右    ~/projects/bar   -                                      -       -         (zsh)

共 2 个窗口（1 个有分屏）、3 个窗格，其中 2 个 Claude 进程
```

## 工作原理

1. 通过 `tmux list-panes -a` 获取所有窗格及其进程 PID
2. 扫描 `/proc` 构建进程树，在每个窗格的进程树中找出最外层的 `claude` 进程
3. 按以下来源（按可靠性排序）确定会话 ID：
   1. `$CLAUDE_CONFIG_DIR/sessions/<pid>.json`：Claude Code 运行时写入，`/clear`、`/resume` 后会更新；会比对进程启动时间，排除 PID 复用导致的过期文件
   2. 进程命令行中的 `--session-id` / `--resume` 参数：仅作兜底，可能已过时，输出中以 `*` 标记
4. 在 `$CLAUDE_CONFIG_DIR/projects/` 下找到对应的 transcript（`<session-id>.jsonl`），从末尾向前查找最后一条 `user` / `assistant` 记录（跳过 `isMeta` 的系统注入内容），以其 `timestamp` 作为“最后消息”时间。不用文件修改时间，是因为 `cost-state`、`mode`、`ai-title` 等元数据记录在没有新对话时也会追加写入

`CLAUDE_CONFIG_DIR` 未设置时默认为 `~/.claude`。
