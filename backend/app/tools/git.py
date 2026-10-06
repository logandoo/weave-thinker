# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""agent `git` 工具 — 受控 git 子命令（vibeweaver 集成 P0 / B1）。

在用户工作区的真实 git 仓库上执行白名单子命令，为 vibeweaver 工作流提供
COV-9 基线提交、`git diff --stat` 变更统计、§V6 `git restore` 回滚与
commit hash 读取能力。

安全边界（与 terminal 互补——terminal._command_accesses_outside_workspace
不覆盖 git 参数，故这里自建收容）：
- 子命令白名单：status/diff/log/rev-parse（只读）+ add/commit/restore/stash/init；
  其余一律拒绝（reset/clean/push/fetch/checkout/config…）。
- 参数过滤：拒绝 NUL、拒绝改写 git 定位的选项（-C/-c/--git-dir/--work-tree…）、
  拒绝越界路径参数（绝对路径 / `..` 段 / `~`）。
- 分级权限：只读免审；add/commit/stash/init 走 `git_execution`（默认允许）；
  restore 走 `workspace_restore`（默认拒绝，会覆盖工作区文件）。
- 影子 git（.snapshots）与工作区真实 `.git` 互不干扰；`.git/` 写保护由
  workspace_write 侧继续兜底，本工具经 git 二进制写入属受控通道。

执行体走 app/services/git_binary.run_git_command（缺失 git = 127 降级，
不抛异常）。返回 JSON：{success, subcommand, exit_code, stdout, stderr[, truncated]}。
"""
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.tools.registry import registry

_READ_ONLY_SUBCOMMANDS = {"status", "diff", "log", "rev-parse"}
_MUTATING_SUBCOMMANDS = {"add", "commit", "restore", "stash", "init"}
_ALLOWED_SUBCOMMANDS = _READ_ONLY_SUBCOMMANDS | _MUTATING_SUBCOMMANDS

# 会改写 git 定位/配置/远端通道、或携带文件副作用（任意读写/模板钩子植入）的
# 选项一律拒绝。A4.9 C1：仅校验选项名不够——`--output=/abs` 这类「=值」形态
# 是任意文件写原语（diff --output 可在工作区外落盘），必须整名拉黑。
_BLOCKED_OPTION_PATTERN = re.compile(
    r"^(-C|--git-dir|--work-tree|--exec-path|--config-env|--namespace"
    r"|--super-prefix|--upload-pack|--receive-pack|-c"
    r"|--output|--file|--template|--pathspec-from-file|--pathspec-file-nul"
    r"|--separate-git-dir)$"
)

# 前缀拒绝用的长选项名单（与上表的长选项一致；缩写匹配方向见 _validate_args）
_BLOCKED_LONG_NAMES = (
    "--git-dir", "--work-tree", "--exec-path", "--config-env", "--namespace",
    "--super-prefix", "--upload-pack", "--receive-pack",
    "--output", "--file", "--template", "--pathspec-from-file",
    "--pathspec-file-nul", "--separate-git-dir",
)

_PATH_SEGMENT_ESCAPE = re.compile(r"(^|[\\/])\.\.([\\/]|$)")


def check_git_tool() -> bool:
    try:
        from app.core.config import get_config
        return bool(get_config().git_tool_enabled)
    except Exception:
        return True


def _payload_error(message: str, **extra: Any) -> str:
    payload: Dict[str, Any] = {"success": False, "error": message}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _validate_args(subcommand: str, raw_args: List[str]) -> Tuple[Optional[str], List[str]]:
    """返回 (拒绝理由, 净化后的参数列表)。"""
    cleaned: List[str] = []
    for arg in raw_args:
        if not isinstance(arg, str):
            return "args 只能是字符串", []
        if "\x00" in arg:
            return "参数包含非法字符", []
        if arg.startswith("-"):
            if arg in ("--",):
                cleaned.append(arg)
                continue
            name, eq, value = arg.partition("=")
            # A4.9 C1（R2 残余）：git 长选项支持唯一前缀缩写（`--tem=` ≡
            # `--template=`）——黑名单必须按前缀拒绝：任何被拉黑长选项 B 满足
            # B.startswith(N) 即拒（过度拒绝无害，git 自身也拒歧义缩写）。
            if _BLOCKED_OPTION_PATTERN.match(name) or (
                name.startswith("--")
                and any(b.startswith(name) for b in _BLOCKED_LONG_NAMES)
            ):
                return f"禁止的 git 选项（含缩写形态）: {name}", []
            if not re.match(r"^--[a-zA-Z0-9][a-zA-Z0-9-]*$|^-{1,2}[a-zA-Z0-9]+$", name):
                return f"无法解析的 git 选项: {arg}", []
            # A4.9 C1：`=值` 形态的值侧同样收容——路径样值（绝对/`~`/`..`）
            # 拒绝（选项名黑名单之外的纵深防御）。
            if eq and (
                value.startswith("/") or value.startswith("~")
                or _PATH_SEGMENT_ESCAPE.search(value)
            ):
                return f"git 选项值不得是越界/绝对路径: {arg}", []
            cleaned.append(arg)
            continue
        # 非选项参数按路径/引用收容：拒绝绝对路径、`..` 段、`~` 展开。
        if arg.startswith("/") or arg.startswith("~"):
            return f"路径参数必须是工作区相对路径: {arg}", []
        if _PATH_SEGMENT_ESCAPE.search(arg):
            return f"路径参数不得越出工作区: {arg}", []
        cleaned.append(arg)
    return None, cleaned


def _truncate(text: str, limit: int) -> Tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]", True


async def git(args: Dict[str, Any], **kwargs) -> str:
    subcommand = str(args.get("subcommand") or "").strip()
    if subcommand not in _ALLOWED_SUBCOMMANDS:
        return _payload_error(
            f"不支持的 git 子命令: {subcommand or '(空)'}；"
            f"允许: {sorted(_ALLOWED_SUBCOMMANDS)}",
        )

    raw_args = args.get("args") or []
    if not isinstance(raw_args, list):
        return _payload_error("args 必须是字符串数组")
    reason, git_args = _validate_args(subcommand, [str(a) for a in raw_args])
    if reason:
        return _payload_error(reason)

    message = args.get("message")
    if subcommand == "commit":
        if not message or not str(message).strip():
            return _payload_error("commit 需要非空 message 参数")
        git_args = ["commit", "-m", str(message)] + git_args
    else:
        git_args = [subcommand] + git_args
    # ---- 工作区定位 ----
    workspace_root = str(kwargs.get("workspace_path") or "")
    user = kwargs.get("user")
    db = kwargs.get("db")
    if not workspace_root:
        if user is None or db is None:
            return _payload_error("用户上下文缺失，无法定位工作区")
        from app.services.workspace_service import ensure_user_workspace
        workspace = await ensure_user_workspace(db, user.id, getattr(user, "username", None))
        workspace_root = str(Path(workspace.root_path).resolve())
    else:
        workspace_root = str(Path(workspace_root).resolve())

    # ---- 分级权限（_permission_needed 协议；registry 级 key 不分粒度，
    # 故 mutating 子命令在此按 notes.py 模式逐动作放行/请求）----
    if subcommand not in _READ_ONLY_SUBCOMMANDS:
        if not args.get("_permission_granted"):
            from app.services.agent_permissions import (
                is_permission_allowed,
                permission_description,
            )
            perm_key = "workspace_restore" if subcommand == "restore" else "git_execution"
            if not (user is not None and is_permission_allowed(user, perm_key)):
                return json.dumps(
                    {
                        "success": False,
                        "_permission_needed": True,
                        "_permission_key": perm_key,
                        "_command": "git " + " ".join([subcommand] + git_args[1:]),
                        "_permission_description": permission_description(perm_key),
                    },
                    ensure_ascii=False,
                )

    from app.core.config import get_config
    from app.services.git_binary import resolve_git_path, run_git_command

    config = get_config()
    timeout = max(5.0, float(getattr(config, "git_tool_timeout", 30.0)))
    out_cap = max(1000, int(getattr(config, "git_tool_max_output", 20000)))

    code, out, err = await run_git_command(git_args, cwd=workspace_root, timeout=timeout)
    if code != 0 and subcommand == "commit" and "Please tell me who you are" in (err or ""):
        # 全新仓库缺 user.identity：一次性注入仅本次生效的默认身份（不写
        # 用户仓库 config，不覆盖既有身份）。
        git_args = [
            "-c", "user.name=Weave Thinker",
            "-c", "user.email=agent@weave.local",
        ] + git_args
        code, out, err = await run_git_command(git_args, cwd=workspace_root, timeout=timeout)

    if code == 127:
        return _payload_error(
            "git executable not found —— 运行环境缺失 git 可执行"
            "（打包环境检查 Resources/bin/git；裸机安装 Git 或 Xcode CLT）",
            exit_code=127,
        )
    if code in (124, 125, 126):
        return _payload_error(err or f"git 执行失败({code})", exit_code=code)

    out, out_trunc = _truncate(out, out_cap)
    err, err_trunc = _truncate(err, out_cap // 2)
    payload: Dict[str, Any] = {
        "success": code == 0,
        "subcommand": subcommand,
        "exit_code": code,
        "git_path": resolve_git_path(),
        "stdout": out,
        "stderr": err,
    }
    if out_trunc or err_trunc:
        payload["truncated"] = True
    return json.dumps(payload, ensure_ascii=False)


registry.register(
    name="git",
    toolset="files",
    schema={
        "name": "git",
        "description": (
            "在工作区 git 仓库上执行受控子命令（白名单：status/diff/log/rev-parse/"
            "add/commit/restore/stash/init）。用于变更审计（git diff --stat）、"
            "基线提交（git add -A + commit）、回滚（restore）与提交哈希读取"
            "（rev-parse HEAD）。路径参数必须是工作区相对路径；reset/clean/push/"
            "checkout 等破坏性/外联子命令不可用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subcommand": {
                    "type": "string",
                    "enum": sorted(_ALLOWED_SUBCOMMANDS),
                    "description": "git 子命令",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "子命令附加参数（如 ['--stat']、['HEAD~1']、['src/']）。"
                        "拒绝越界路径与危险选项。"
                    ),
                },
                "message": {
                    "type": "string",
                    "description": "提交说明（subcommand=commit 时必填）",
                },
            },
            "required": ["subcommand"],
        },
    },
    handler=git,
    is_async=True,
    check_fn=check_git_tool,
    description="受控 git 子命令（变更审计/基线提交/回滚/哈希）",
    emoji="",
)
