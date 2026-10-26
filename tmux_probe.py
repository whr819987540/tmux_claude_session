#!/usr/bin/env python3
"""探测每个 tmux 窗口/窗格的工作目录，以及其中运行的 Claude Code 会话 ID。

输出按窗口分组；分屏窗口中的每个窗格单独列出，并标注其位置（左、右上等）。

会话 ID 的来源（按可靠性排序）:
  1. $CLAUDE_CONFIG_DIR/sessions/<pid>.json —— Claude Code 运行时写入，/clear、/resume 后会更新
  2. 进程命令行中的 --session-id / --resume 参数 —— 仅作兜底，可能已过时（输出中以 * 标记）

用法:
  tmux_probe.py              表格输出
  tmux_probe.py -c           只显示运行 Claude 的窗格
  tmux_probe.py --json       JSON 输出
"""
import argparse
import datetime
import glob
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
HOME = os.path.expanduser("~")

PANE_FIELDS = ["session_name", "window_index", "window_id", "window_name", "window_active",
               "window_panes", "pane_index", "pane_id", "pane_pid", "pane_active",
               "pane_at_left", "pane_at_right", "pane_at_top", "pane_at_bottom",
               "pane_current_path", "pane_current_command"]


# ---------- tmux ----------

def list_panes():
    fmt = "\t".join("#{%s}" % f for f in PANE_FIELDS)
    try:
        out = subprocess.run(["tmux", "list-panes", "-a", "-F", fmt],
                             capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        sys.exit("未找到 tmux 命令")
    except subprocess.CalledProcessError as e:
        sys.exit(f"tmux 调用失败（tmux server 是否在运行？）: {e.stderr.strip()}")
    panes = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != len(PANE_FIELDS):
            continue
        p = dict(zip(PANE_FIELDS, parts))
        p["pane_pid"] = int(p["pane_pid"])
        panes.append(p)
    return panes


def pane_position(p):
    """根据窗格是否贴靠窗口边缘给出分屏位置，如 左、右下；未分屏时为空串。"""
    def axis(at_start, at_end, start, end):
        if at_start == at_end == "1":
            return ""
        return start if at_start == "1" else end if at_end == "1" else "中"
    h = axis(p["pane_at_left"], p["pane_at_right"], "左", "右")
    v = axis(p["pane_at_top"], p["pane_at_bottom"], "上", "下")
    return "中" if h == v == "中" else h + v


# ---------- /proc ----------

def read_proc(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
        with open(f"/proc/{pid}/comm") as f:
            comm = f.read().strip()
    except OSError:
        return None
    # comm 可能包含空格或括号，从最后一个 ')' 之后切分；rest[1]=ppid, rest[19]=starttime
    rest = stat[stat.rindex(")") + 2:].split()
    return {"pid": pid, "ppid": int(rest[1]), "starttime": rest[19], "comm": comm, "argv": argv}


def process_table():
    procs, children = {}, {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            info = read_proc(int(name))
            if info:
                procs[info["pid"]] = info
                children.setdefault(info["ppid"], []).append(info["pid"])
    return procs, children


def is_claude(proc):
    exe = proc["argv"][0] if proc["argv"] else ""
    return (proc["comm"] == "claude" or os.path.basename(exe) == "claude"
            or "/claude/versions/" in exe)


def find_claude_procs(root, procs, children):
    """返回窗格进程树中最外层的 claude 进程（不再深入其子进程）。"""
    found, stack = [], [root]
    while stack:
        pid = stack.pop()
        proc = procs.get(pid)
        if proc is None:
            continue
        if is_claude(proc):
            found.append(proc)
            continue
        stack.extend(children.get(pid, []))
    return sorted(found, key=lambda p: p["pid"])


# ---------- Claude Code ----------

def load_pid_files():
    result = {}
    for path in glob.glob(os.path.join(CONFIG_DIR, "sessions", "*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data.get("pid"), int):
            result[data["pid"]] = data
    return result


def session_from_argv(argv):
    args = argv[1:]
    # --fork-session 时 --resume 指向的是源会话，因此 --session-id 优先
    for flag in ("--session-id", "--resume", "-r"):
        for i, a in enumerate(args):
            value = None
            if a == flag and i + 1 < len(args):
                value = args[i + 1]
            elif a.startswith(flag + "="):
                value = a.split("=", 1)[1]
            m = UUID_RE.search(value or "")
            if m:
                return m.group(0)
    return None


def find_transcript(sid, cwd):
    if cwd:
        path = os.path.join(CONFIG_DIR, "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd), sid + ".jsonl")
        if os.path.exists(path):
            return path
    hits = glob.glob(os.path.join(CONFIG_DIR, "projects", "*", sid + ".jsonl"))
    return hits[0] if hits else None


def message_time(line):
    """若该行是 user/assistant 消息则返回其时间戳（epoch 秒），否则返回 None。"""
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("type") not in ("user", "assistant") or d.get("isMeta"):
        return None
    try:
        # Python < 3.11 的 fromisoformat 不认识末尾的 Z
        return datetime.datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, AttributeError, ValueError):
        return None


def last_message_time(path, chunk=1 << 16):
    """从 transcript 末尾向前查找最后一条消息的时间。

    文件 mtime 不可靠：mode、ai-title 等元数据记录也会追加写入。
    """
    with open(path, "rb") as f:
        pos = f.seek(0, os.SEEK_END)
        head = b""
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            lines = (f.read(step) + head).split(b"\n")
            head = lines.pop(0)  # 可能是被块边界截断的行，并入下一块
            for line in reversed(lines):
                ts = message_time(line)
                if ts is not None:
                    return ts
        return message_time(head)


def describe_claude(proc, pid_files):
    info = {"pid": proc["pid"], "argv": proc["argv"], "session_id": None, "source": None,
            "status": None, "name": None, "kind": None, "cwd": None, "version": None,
            "transcript": None, "last_message_time": None}
    data = pid_files.get(proc["pid"])
    # procStart 与 /proc 中的启动时间不一致说明 pid 已被复用，该文件已过期
    if data and str(data.get("procStart", proc["starttime"])) == proc["starttime"]:
        info.update(session_id=data.get("sessionId"), source="pidfile", status=data.get("status"),
                    name=data.get("name"), kind=data.get("kind"), cwd=data.get("cwd"),
                    version=data.get("version"))
    else:
        sid = session_from_argv(proc["argv"])
        if sid:
            info.update(session_id=sid, source="cmdline")
        try:
            info["cwd"] = os.readlink(f"/proc/{proc['pid']}/cwd")
        except OSError:
            pass
    if info["session_id"]:
        path = find_transcript(info["session_id"], info["cwd"])
        if path:
            info["transcript"] = path
            try:
                info["last_message_time"] = last_message_time(path)
            except OSError:
                pass
    return info


# ---------- 输出 ----------

def disp_width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def fit(s, width):
    """按显示宽度截断并右侧补齐（兼容中文字符）。"""
    if disp_width(s) > width:
        while disp_width(s) > width - 1:
            s = s[:-1]
        s += "…"
    return s + " " * (width - disp_width(s))


def short_path(path):
    if path and (path == HOME or path.startswith(HOME + "/")):
        return "~" + path[len(HOME):]
    return path or ""


def ago(ts):
    if ts is None:
        return "-"
    sec = int(time.time() - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if sec >= n:
            return f"{sec // n}{unit}前"
    return f"{sec}s前"


def collect(claude_only):
    """按窗口分组返回：[{窗口信息..., "panes": [{窗格信息..., "claude": [...]}]}]"""
    procs, children = process_table()
    pid_files = load_pid_files()
    windows = {}
    for pane in list_panes():
        claudes = [describe_claude(p, pid_files) for p in find_claude_procs(pane["pane_pid"], procs, children)]
        if claude_only and not claudes:
            continue
        target = f"{pane['session_name']}:{pane['window_index']}"
        # 以 会话:窗口 为键而非 window_id：会话组中多个会话共享同一 window_id
        win = windows.setdefault(target, {
            "target": target,
            "session": pane["session_name"],
            "window_index": int(pane["window_index"]),
            "window_id": pane["window_id"],
            "window_name": pane["window_name"],
            "active": pane["window_active"] == "1",
            "pane_count": int(pane["window_panes"]),
            "panes": [],
        })
        win["panes"].append({
            "target": f"{target}.{pane['pane_index']}",
            "pane_index": int(pane["pane_index"]),
            "pane_id": pane["pane_id"],
            "pane_pid": pane["pane_pid"],
            "active": pane["pane_active"] == "1",
            "position": pane_position(pane),
            "cwd": pane["pane_current_path"],
            "command": pane["pane_current_command"],
            "claude": claudes,
        })
    return list(windows.values())


def pane_label(p):
    return f".{p['pane_index']}" + (f" {p['position']}" if p["position"] else "")


def print_table(windows):
    panes = [p for w in windows for p in w["panes"]]

    def width(header, values, cap):
        return min(max([disp_width(header)] + [disp_width(v) for v in values]), cap)

    cols = [("窗口", width("窗口", [w["target"] for w in windows], 36)),
            ("窗口名", width("窗口名", [w["window_name"] for w in windows], 16)),
            ("窗格", width("窗格", [pane_label(p) for p in panes], 10)),
            ("工作目录", width("工作目录", [short_path(p["cwd"]) for p in panes], 40)),
            ("Claude 会话 ID", 37), ("状态", 6), ("最后消息", 8)]

    def emit(cells, tail):
        print("  ".join(fit(c, w) for c, (_, w) in zip(cells, cols)) + "  " + fit(tail, 40).rstrip())

    emit([t for t, _ in cols], "会话名")
    print("-" * (sum(w for _, w in cols) + 2 * len(cols) + 12))

    sid_targets, footnote = {}, False
    for w in windows:
        # 同一窗口只在首行显示窗口列，同一窗格只在首行显示窗格列
        win_cells = [w["target"], w["window_name"]]
        for p in w["panes"]:
            pane_cells = [pane_label(p), short_path(p["cwd"])]
            if not p["claude"]:
                emit(win_cells + pane_cells + ["-", "-", "-"], f"({p['command']})")
                win_cells = ["", ""]
            for c in p["claude"]:
                if c["session_id"]:
                    sid = c["session_id"] + ("*" if c["source"] == "cmdline" else "")
                    footnote |= c["source"] == "cmdline"
                    sid_targets.setdefault(c["session_id"], []).append(p["target"])
                else:
                    sid = "-"
                name = c["name"] or ("(" + " ".join(["claude"] + c["argv"][1:]) + ")")
                if c["cwd"] and c["cwd"] != p["cwd"]:
                    name += f"  [claude cwd: {short_path(c['cwd'])}]"
                emit(win_cells + pane_cells + [sid, c["status"] or "-", ago(c["last_message_time"])], name)
                win_cells, pane_cells = ["", ""], ["", ""]

    n_claude = sum(len(p["claude"]) for p in panes)
    n_split = sum(1 for w in windows if w["pane_count"] > 1)
    print(f"\n共 {len(windows)} 个窗口（{n_split} 个有分屏）、{len(panes)} 个窗格，其中 {n_claude} 个 Claude 进程")
    if footnote:
        print("* 未找到 sessions/<pid>.json，会话 ID 取自命令行参数，/clear 或 /resume 后可能已失效")
    for sid, targets in sid_targets.items():
        if len(targets) > 1:
            print(f"注意: 会话 {sid} 同时在 {', '.join(targets)} 中运行")


def main():
    ap = argparse.ArgumentParser(description="探测 tmux 窗格的工作目录与 Claude Code 会话 ID")
    ap.add_argument("-c", "--claude-only", action="store_true", help="只显示运行 Claude 的窗格")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = ap.parse_args()
    if not os.path.isdir("/proc"):
        sys.exit("需要 Linux /proc 文件系统")
    windows = collect(args.claude_only)
    if args.json:
        json.dump(windows, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print_table(windows)


if __name__ == "__main__":
    main()
