#!/usr/bin/env python3
# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Docker 部署配置生成器（从模板生成 backend/config.toml 并套用环境覆盖）。

用途（三种调用方共用同一份逻辑）：
  1. 容器入口 `docker/entrypoint.sh`：挂载缺失时从镜像内模板生成；
  2. CI（docker-smoke job）：生成指向 compose `db` 服务的配置；
  3. 有 Python 的宿主用户：`python docker/bootstrap_config.py --from ... --out ...`。

行为契约：
  - **stdlib-only**（tomllib 仅读不需要；本脚本行级编辑，不 import 三方包），
    生成物保留模板注释与键序，用户可继续按注释填写；
  - 输出已存在且未给 `--force` → 打印 `skip:` 并退出 0（幂等）；
  - 输出路径是目录（bind mount 源文件不存在时的典型现象）→ 退出 1；
  - `[security].jwt_secret_key` 为占位/空 → 生成 64 位十六进制随机值；
  - 只改 `[database]` / `[security]` 的显式覆盖项，其余键原样保留。

退出码：0 = 生成成功或按幂等跳过；1 = 输入/输出错误。
"""
import argparse
import re
import secrets
import sys
from pathlib import Path

PLACEHOLDER_MARKERS = ("<your", "your-", "placeholder", "changeme")
SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
KV_RE = re.compile(r"^(\s*)([A-Za-z0-9_\-]+)(\s*=\s*)(.*)$")


def is_placeholder(value) -> bool:
    """占位/空值判定：需要被替换的键返回 True。"""
    if not isinstance(value, str):
        return True
    stripped = value.strip()
    if not stripped:
        return True
    lowered = stripped.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def format_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def apply_section_overrides(text: str, section: str, overrides: dict) -> str:
    """在指定 [section] 内逐行替换键值；键不存在则在段尾补插，段缺失则整段补插。

    保留行内缩进、键序与注释；不改动其他段。
    """
    pending = {k: v for k, v in overrides.items() if v is not None}
    if not pending:
        return text

    lines = text.split("\n")
    out = []
    current = None
    seen_section = False
    for line in lines:
        header = SECTION_RE.match(line)
        if header:
            if current == section and pending:
                out.extend(f"{k} = {format_value(v)}" for k, v in pending.items())
                pending.clear()
            current = header.group(1)
            if current == section:
                seen_section = True
            out.append(line)
            continue
        if current == section:
            kv = KV_RE.match(line)
            if kv and kv.group(2) in pending:
                key = kv.group(2)
                out.append(f"{kv.group(1)}{key}{kv.group(3)}{format_value(pending.pop(key))}")
                continue
        out.append(line)

    if current == section and pending:
        out.extend(f"{k} = {format_value(v)}" for k, v in pending.items())
        pending.clear()
    if not seen_section and pending:
        if out and out[-1].strip():
            out.append("")
        out.append(f"[{section}]")
        out.extend(f"{k} = {format_value(v)}" for k, v in pending.items())
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从模板生成 Weave Thinker 部署配置（Docker / CI / 本地自动化）",
    )
    parser.add_argument("--from", dest="src", required=True, help="模板路径（config.toml.example）")
    parser.add_argument("--out", dest="out", required=True, help="输出路径（backend/config.toml）")
    parser.add_argument("--db-host", default=None, help="覆盖 [database].host（Docker 用 db）")
    parser.add_argument("--db-port", type=int, default=None, help="覆盖 [database].port")
    parser.add_argument("--db-user", default=None, help="覆盖 [database].username")
    parser.add_argument("--db-pass", default=None, help="覆盖 [database].password")
    parser.add_argument("--db-name", default=None, help="覆盖 [database].name")
    parser.add_argument("--jwt-secret", default="", help="显式 JWT 密钥（缺省时占位值自动生成随机串）")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
    return parser


def _access_error(path, exc) -> str:
    import errno as _errno

    if getattr(exc, "errno", None) in (_errno.EACCES, _errno.EPERM):
        return (
            f"错误: 无权限访问 (permission denied): {path} —— {exc}。"
            "容器场景常见原因：SELinux（Fedora/RHEL）的 bind mount 未重打标签；"
            "处置：compose 挂载选项加 :z（.env 设 WT_MOUNT_OPTS=ro,z，改后需重建容器），"
            "或在宿主机执行 chcon -t container_file_t <宿主机上的该文件>。"
        )
    return f"错误: 文件访问失败 (OSError): {path} —— {exc}。"


def main() -> int:
    args = build_parser().parse_args()
    src = Path(args.src)
    out = Path(args.out)
    try:
        return _run(args, src, out)
    except OSError as exc:
        print(_access_error(out, exc), file=sys.stderr)
        return 1


def _run(args, src: Path, out: Path) -> int:
    if out.is_dir():
        print(
            f"错误: 输出路径是目录 (directory)，不是文件: {out} —— 通常因为 compose "
            f"挂载源文件不存在（Docker 会把缺失的 bind mount 源建成目录）；"
            f"请先创建配置文件再启动容器。",
            file=sys.stderr,
        )
        return 1
    if out.exists() and not args.force:
        print(f"skip: {out} 已存在（如需覆盖请加 --force）")
        return 0
    if not src.is_file():
        print(f"错误: 模板文件 not found: {src}", file=sys.stderr)
        return 1

    text = src.read_text(encoding="utf-8")
    applied = []

    db_overrides = {
        "host": args.db_host,
        "port": args.db_port,
        "username": args.db_user,
        "password": args.db_pass,
        "name": args.db_name,
    }
    if any(v is not None for v in db_overrides.values()):
        text = apply_section_overrides(text, "database", db_overrides)
        applied.extend(f"database.{k}" for k, v in db_overrides.items() if v is not None)

    if args.jwt_secret:
        text = apply_section_overrides(text, "security", {"jwt_secret_key": args.jwt_secret})
        applied.append("security.jwt_secret_key(--jwt-secret)")
    elif is_placeholder(_read_section_value(text, "security", "jwt_secret_key")):
        text = apply_section_overrides(
            text, "security", {"jwt_secret_key": secrets.token_hex(32)}
        )
        applied.append("security.jwt_secret_key(随机生成)")

    out.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# 本文件由 docker/bootstrap_config.py 从模板生成；模型端点请编辑 config_model.toml。\n"
    )
    out.write_text(header + text, encoding="utf-8")

    print(f"已生成: {out}")
    if applied:
        print("覆盖项: " + ", ".join(applied))
    return 0


def _read_section_value(text: str, section: str, key: str):
    current = None
    for line in text.split("\n"):
        header = SECTION_RE.match(line)
        if header:
            current = header.group(1)
            continue
        if current != section:
            continue
        kv = KV_RE.match(line)
        if kv and kv.group(2) == key:
            raw = _strip_inline_comment(kv.group(4).strip())
            if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
                return raw[1:-1]
            return raw
    return ""


def _strip_inline_comment(value: str) -> str:
    """去掉 TOML 值后的行内注释（引号外的 # 起注释），引号内 # 保留。"""
    in_double = in_single = False
    for i, ch in enumerate(value):
        if ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == "#" and not in_double and not in_single:
            return value[:i].rstrip()
    return value


if __name__ == "__main__":
    sys.exit(main())
