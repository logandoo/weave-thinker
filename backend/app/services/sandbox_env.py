# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""沙箱共享工具环境（F1）——把 playwright 浏览器注册表钉到服务端共享目录。

背景：terminal / code_execution / process 三条工具沙箱链路都把 HOME 重定向进
用户工作区（隔离，见各调用点）。副作用：playwright 按 `$HOME/.cache/ms-playwright`
（macOS `~/Library/Caches/ms-playwright`）解析浏览器注册表，在沙箱里即工作区内部，
于是每个首次用浏览器的工作区都复制一份 ~641M 的浏览器（2026-10-26 在 60 生产实测
3 份）。F1：沙箱 env 统一注入 ``PLAYWRIGHT_BROWSERS_PATH`` 指向**服务进程**的
共享注册表，浏览器只装一份。

解析优先级（`resolve_shared_browsers_path`）：
    配置 ``[browser].shared_browsers_path``（显式路径=覆盖，``off``=不注入，空=auto）
    > 服务进程自身 env ``PLAYWRIGHT_BROWSERS_PATH``
    > 平台默认注册表（以服务进程 HOME 为 ``~``）。

信任边界（A4.9 I3 裁决）：共享注册表对沙箱可写（playwright install 落这里），
与沙箱同 uid（部署形态 root 单用户）——同一信任域的既有事实；HOME/工作区隔离
是卫生不是安全边界。跨 uid 部署前需把注册表改为沙箱只读并预装浏览器。
"""
import os
import sys
from typing import Dict, Mapping, Optional

BROWSERS_ENV_VAR = "PLAYWRIGHT_BROWSERS_PATH"


def resolve_shared_browsers_path(
    config_path: Optional[str],
    *,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[str] = None,
    platform: Optional[str] = None,
) -> Optional[str]:
    """返回应注入沙箱的注册表目录；None 表示不注入（配置为 off）。
    路径一律归一为绝对路径（A4.9 M1：相对路径会按沙箱 cwd 重定位 → 复制体回归）。"""
    cfg = (config_path or "").strip()
    if cfg.lower() == "off":
        return None
    if cfg:
        return os.path.abspath(os.path.expanduser(cfg))
    env = os.environ if environ is None else environ
    existing = (env.get(BROWSERS_ENV_VAR) or "").strip()
    if existing:
        return os.path.abspath(os.path.expanduser(existing))
    plat = (platform or sys.platform).lower()
    user_home = os.path.expanduser("~") if home is None else home
    if plat == "darwin":
        return os.path.join(user_home, "Library", "Caches", "ms-playwright")
    if plat in ("win32", "cygwin"):
        return os.path.join(user_home, "AppData", "Local", "ms-playwright")
    return os.path.join(user_home, ".cache", "ms-playwright")


def playwright_browsers_env() -> Dict[str, str]:
    """给沙箱 env 追加的键值对：``{PLAYWRIGHT_BROWSERS_PATH: <共享注册表>}`` 或 ``{}``。"""
    try:
        from app.core.config import get_config
        cfg = str(getattr(get_config(), "browser_shared_browsers_path", "") or "")
    except Exception:  # 配置不可用时退回默认解析，绝不因注入失败而阻塞工具执行
        cfg = ""
    path = resolve_shared_browsers_path(cfg)
    return {BROWSERS_ENV_VAR: path} if path else {}
