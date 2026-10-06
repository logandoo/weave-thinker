# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""playwright sync API 专用工作线程（2026-10-04 全量 278 红事故根治）。

playwright.sync_api 用 greenlet 在**调用线程**内驱动私有 event loop；用完
该线程的 asyncio running-loop 状态残留（`asyncio._get_running_loop()` 非空），
其后同线程任何 `asyncio.run()` 报 "cannot be called from a running event loop"
——一次导出/数学渲染就毒死整个 pytest 进程。

修法：所有 sync playwright 工作固定在**唯一专用工作线程**执行（该线程永不
asyncio.run，污染封存在其内）；浏览器缓存（notes._BrowserPool 线程局部）随之
单线程全局复用——顺带对齐 60 browser OOM postmortem 的串行化缓解
（docs/POSTMORTEM_20260921_60_browser_oom.md）。

用法：
    @pw_thread
    def render(...): ...          # 整函数在工作线程执行（同步语义不变）

    run_on_pw_thread(fn, *args)   # 同上的显式形式

可重入：已在工作线程上时直接内联执行（防装饰器嵌套死锁）。
"""
import functools
import logging
import queue
import threading

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None
_QUEUE: "queue.SimpleQueue" = queue.SimpleQueue()
_SENTINEL = object()
# A4.9 wave2：调用方等待上限（fail-fast；卡死的工作任务不无限拖住导出/截图线程池）
_JOB_TIMEOUT = float(__import__("os").environ.get("WT_PW_JOB_TIMEOUT", "120"))


def _worker_loop() -> None:
    while True:
        job = _QUEUE.get()
        if job is _SENTINEL:
            return
        fn, box = job
        try:
            box["ret"] = fn()
        except BaseException as exc:  # noqa: BLE001 — 原样回抛调用线程
            box["err"] = exc
        finally:
            box["done"].set()


def _ensure_worker() -> threading.Thread:
    global _WORKER
    with _LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(
                target=_worker_loop, daemon=True, name="pw-sync-worker")
            _WORKER.start()
            logger.info("pw-sync-worker started (tid=%s)", _WORKER.ident)
    return _WORKER


def on_pw_thread() -> bool:
    """当前是否已在 playwright 工作线程（可重入判定）。"""
    return threading.current_thread() is _WORKER


def run_on_pw_thread(fn, /, *args, **kwargs):
    """在专用工作线程同步执行 fn（已在其上则内联），返回/原样回抛结果。"""
    if on_pw_thread():
        return fn(*args, **kwargs)
    worker = _ensure_worker()
    box: dict = {"done": threading.Event()}
    _QUEUE.put((lambda: fn(*args, **kwargs), box))
    box["done"].wait(_JOB_TIMEOUT)
    if not box["done"].is_set():
        raise TimeoutError(f"pw job timed out after {_JOB_TIMEOUT}s: {fn!r}")
    if "err" in box:
        raise box["err"]
    return box.get("ret")


def pw_thread(fn):
    """装饰器形式：整个函数体在工作线程执行（同步语义、异常传播不变）。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return run_on_pw_thread(fn, *args, **kwargs)
    wrapper._pw_kind = "thread"  # 测试在位钉（区分 thread/ephemeral）
    return wrapper


def run_ephemeral(fn, /, *args, **kwargs):
    """一次性线程执行（线程随任务死亡，greenlet loop 状态一并消失）。

    A4.9 wave2 Critical：池化的 `sync_playwright().start()`（notes._BrowserPool）
    会把 loop 标记为 running 永久驻留该线程——之后同线程任何 `with
    sync_playwright()` 都报 "Sync API inside the asyncio loop"。两类会话因此
    **不得同线程**：池化调用走 `pw_thread`（持久工作线程）；自带
    `with sync_playwright()` 的调用（_render_math_with_mathjax /
    visual_critic._screenshot_html）走本一次性线程。
    """
    box: dict = {"done": threading.Event()}
    t = threading.Thread(
        target=lambda: _run_boxed(fn, args, kwargs, box),
        daemon=True, name="pw-ephemeral")
    t.start()
    box["done"].wait(_JOB_TIMEOUT)
    if not box["done"].is_set():
        raise TimeoutError(f"pw ephemeral job timed out after {_JOB_TIMEOUT}s: {fn!r}")
    if "err" in box:
        raise box["err"]
    return box.get("ret")


def _run_boxed(fn, args, kwargs, box):
    try:
        box["ret"] = fn(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001
        box["err"] = exc
    finally:
        box["done"].set()


def pw_ephemeral(fn):
    """装饰器形式：一次性线程执行（见 run_ephemeral）。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return run_ephemeral(fn, *args, **kwargs)
    wrapper._pw_kind = "ephemeral"  # 测试在位钉（区分 thread/ephemeral）
    return wrapper
