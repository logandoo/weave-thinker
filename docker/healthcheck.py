#!/usr/bin/env python3
# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""容器健康检查：探测本机 8158（或 config.toml [server].port）的登录页。

HTTP / HTTPS 双协议自适应（挂载证书后仍可用）；失败返回 1 供 Docker 标记
unhealthy。只读、无副作用。
"""
import ssl
import sys
import urllib.request

PORT = 8158
try:
    import tomllib

    with open("/app/backend/config.toml", "rb") as fh:
        PORT = int(tomllib.load(fh).get("server", {}).get("port", 8158))
except Exception:
    pass

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

for scheme in ("http", "https"):
    try:
        resp = urllib.request.urlopen(
            f"{scheme}://127.0.0.1:{PORT}/app/frontend/login",
            timeout=4,
            context=CTX,
        )
        if resp.status < 500:
            sys.exit(0)
    except Exception:
        continue

sys.exit(1)
