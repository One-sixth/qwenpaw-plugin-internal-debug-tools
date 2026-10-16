# -*- coding: utf-8 -*-
"""内部调试线程引擎与会话级注册表。

- 每个会话可启动一个专属调试线程（daemon），内含持久 namespace，
  配合 internal_debug_tools_eval 像宿主内 REPL 一样实时执行 Python 代码；
- 任务经 queue.Queue 提交、串行执行，Future 回传结果；
- 执行全程 BaseException 级保护：用户代码抛 SystemExit、裸 except
  甚至引擎自身异常都不会让线程 raise 退出；
- stop 通过 threading.Event 设置退出信号，join 最多 timeout 秒；
  超时未结束 → 线程标记卡死，本会话禁止再 start，只能重启宿主；
- start / stop 均幂等：start 是否新建完全取决于旧线程是否已结束。
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import collections
import concurrent.futures
import gc
import io
import json
import logging
import math
import os
import pprint
import queue
import re
import sys
import threading
import time
import traceback
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────

MAX_RESULT_CHARS = 2000   # result repr 截断上限
MAX_STREAM_CHARS = 4000   # stdout / stderr / traceback 截断上限
QUEUE_POLL_INTERVAL = 0.5  # 主循环空转时检查退出事件的间隔（秒）
DEFAULT_EVAL_TIMEOUT = 30.0
MAX_EVAL_TIMEOUT = 900.0   # eval 等待上限 15 分钟，不允许无限等待
DEFAULT_STOP_TIMEOUT = 60.0

STATUS_CREATED = "created"    # 本次调用新建了线程
STATUS_RUNNING = "running"    # 已有存活线程（幂等）
STATUS_STUCK = "stuck"        # 旧线程 stop 未退出，拒绝新建


def _clamp_eval_timeout(timeout: Optional[float]) -> float:
    """归一化 eval 等待时长：≤0/None 回默认 30s，上限 900s（15 分钟）。"""
    if timeout is None or timeout <= 0:
        return DEFAULT_EVAL_TIMEOUT
    return min(float(timeout), MAX_EVAL_TIMEOUT)


def _clip(text: str, limit: int) -> Tuple[str, bool]:
    """超长文本截断，返回 (文本, 是否截断)。"""
    if len(text) <= limit:
        return text, False
    return (
        text[:limit] + f"\n…[已截断，原文共 {len(text)} 字符]",
        True,
    )


def _safe_repr(value: Any) -> str:
    """repr 的保护版：对象 __repr__ 抛异常时不炸。"""
    try:
        return repr(value)
    except BaseException as e:  # noqa: BLE001
        try:
            return f"<repr 失败: {type(value).__name__!r} raised {e!r}>"
        except BaseException:  # noqa: BLE001
            return "<repr 失败>"


def _make_capturing_print(buf_out: io.StringIO):
    """构造写入 buf 的捕获版 print（注入 namespace 用）。

    eval/exec 的 globals 查找先于 builtins，注入后用户代码的 print
    自然命中本函数。不用 contextlib.redirect_stdout：那是进程级
    sys.stdout 全局替换，多线程下会劫持宿主其他线程的输出，且任务
    卡死时劫持永不恢复。file= 参数透传真实 print。
    """

    def _print(*args, sep=" ", end="\n", file=None, flush=False):
        if file is not None:
            builtins.print(*args, sep=sep, end=end, file=file, flush=flush)
            return
        buf_out.write(sep.join(str(a) for a in args) + end)

    return _print


def _make_namespace() -> Dict[str, Any]:
    """构建调试线程的持久 namespace，预置常用模块。"""
    return {
        "__name__": "internal_debug",
        "__builtins__": builtins,
        "sys": sys,
        "os": os,
        "gc": gc,
        "time": time,
        "threading": threading,
        "asyncio": asyncio,
        "json": json,
        "re": re,
        "math": math,
        "pprint": pprint,
        "traceback": traceback,
        "collections": collections,
        "concurrent": concurrent,
    }


# ── 调试线程 ──────────────────────────────────────────────


class DebugThread:
    """单个会话的调试 REPL 线程。"""

    def __init__(self, key: Tuple[str, str, str]) -> None:
        self._key = key
        self._queue: "queue.Queue" = queue.Queue()
        self._namespace = _make_namespace()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._executed = 0
        self._started_at = time.time()
        ident = format(abs(hash(key)) % 0xFFFF, "04x")
        self._thread = threading.Thread(
            target=self._run,
            name=f"internal-debug-{ident}",
            daemon=True,
        )
        self._thread.start()

    # ── 对外接口（工具函数经 asyncio.to_thread 调用，可阻塞）──

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def status(self) -> Dict[str, Any]:
        """状态快照。"""
        return {
            "alive": self.is_alive(),
            "stop_requested": self.stop_requested,
            "thread_name": self._thread.name,
            "thread_id": self._thread.ident,
            "started_at": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self._started_at)
            ),
            "alive_seconds": round(time.time() - self._started_at, 1),
            "executed": self._executed,
            "namespace_vars": len(self._namespace),
        }

    def eval(self, code: str, timeout: float, loop_ref: Any = None) -> Dict[str, Any]:
        """提交一段代码到调试线程执行并等待结果。

        timeout 归一化：≤0/None 回默认 30s，上限 900s（15 分钟）。
        超时只放弃等待，任务仍在调试线程里继续执行。
        """
        if self._stop_event.is_set():
            return {
                "ok": False,
                "state": "stopping",
                "note": (
                    "调试线程已收到退出信号（正在退出或卡死），无法执行。"
                    "请调用 internal_debug_tools_stop 确认状态；"
                    "若线程卡死需重启宿主。"
                ),
            }
        timeout = _clamp_eval_timeout(timeout)
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._queue.put((code, future, loop_ref))
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            return {
                "ok": False,
                "timed_out": True,
                "note": (
                    f"等待 {timeout}s 超时，代码仍在调试线程中继续执行"
                    "（可能死循环/阻塞）。可再次 eval 查看进展，"
                    "或 stop 停止线程。"
                ),
            }

    def stop(self, timeout: float) -> Dict[str, Any]:
        """设置退出事件并等待线程结束（幂等）。

        timeout>0：等待至多 timeout 秒，超时未结束判为卡死；
        timeout≤0：只设置退出信号、不等待（signal_only）。
        """
        if not self.is_alive():
            return {
                "ok": True,
                "already": True,
                "note": "调试线程已结束，无需停止（幂等）。",
            }
        self._stop_event.set()
        self._queue.put(None)  # 哨兵：让阻塞在 get() 的线程立刻醒来
        if not (timeout and timeout > 0):
            return {
                "ok": True,
                "stopped": False,
                "signal_only": True,
                "note": (
                    "已设置退出信号（timeout≤0，未等待线程结束）。"
                    "可再次调用 stop 并传 timeout>0 确认线程是否退出。"
                ),
            }
        self._thread.join(timeout)
        if self.is_alive():
            return {
                "ok": False,
                "stopped": False,
                "stuck": True,
                "note": (
                    "已设置退出信号，但线程尚未结束，调试线程可能在死锁或"
                    "繁忙。本会话已无法启动新调试线程，推荐手动重启宿主，"
                    "让卡住的线程随进程消失后再使用。"
                ),
            }
        return {
            "ok": True,
            "stopped": True,
            "executed": self._executed,
            "note": "调试线程已停止。",
        }

    # ── 线程体 ───────────────────────────────────────────

    def _run(self) -> None:
        """主循环：所有环节 BaseException 级保护，绝不让线程 raise 退出。"""
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=QUEUE_POLL_INTERVAL)
            except queue.Empty:
                continue
            except BaseException:  # noqa: BLE001
                logger.exception("internal-debug 队列读取异常，继续循环")
                continue
            if item is None:  # stop 哨兵
                break
            try:
                code, future, loop_ref = item
            except BaseException:  # noqa: BLE001
                logger.exception("internal-debug 任务解包异常")
                continue
            result = self._safe_execute(code, loop_ref)
            try:
                if not future.done():
                    future.set_result(result)
            except BaseException:  # noqa: BLE001
                logger.exception("internal-debug 回传结果异常")

    def _safe_execute(self, code: str, loop_ref: Any) -> Dict[str, Any]:
        """执行一段代码，任何异常都折叠为结果 dict，绝不向外抛。"""
        t0 = time.monotonic()
        result: Dict[str, Any] = {
            "ok": True,
            "result": None,
            "stdout": "",
            "truncated": False,
        }
        buf_out = io.StringIO()
        value: Any = None
        try:
            # 宿主主事件循环引用每次提交时刷新，供 run_coroutine_threadsafe 用
            if loop_ref is not None:
                self._namespace["_loop"] = loop_ref
            try:
                # 注入捕获版 print（globals 查找优先于 builtins）。
                # 不用 redirect_stdout：进程级全局替换，多线程不安全。
                self._namespace["print"] = _make_capturing_print(buf_out)
                compiled = compile(code, "<internal-debug>", "eval")
                value = eval(compiled, self._namespace)  # noqa: S307
            except SyntaxError:
                value = self._exec_capture(code)
        except BaseException as e:  # noqa: BLE001  用户代码异常也不能杀线程
            result["ok"] = False
            tb = "".join(
                traceback.format_exception(type(e), e, e.__traceback__)
            )
            tb_clipped, trunc = _clip(tb, MAX_STREAM_CHARS)
            result["error"] = tb_clipped
            result["truncated"] = result["truncated"] or trunc
        finally:
            out, t1 = _clip(buf_out.getvalue(), MAX_STREAM_CHARS)
            result["stdout"] = out
            result["truncated"] = result["truncated"] or t1
            if result["ok"]:
                rep, t3 = _clip(_safe_repr(value), MAX_RESULT_CHARS)
                result["result"] = rep
                result["truncated"] = result["truncated"] or t3
                # REPL `_`：只记录成功结果，异常时保留上次成功值
                self._namespace["_"] = value
            result["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
            with self._lock:
                self._executed += 1
        return result

    def _exec_capture(self, code: str) -> Any:
        """exec 语句；若最后一条是表达式语句，捕获并返回其值（REPL 语义）。"""
        tree = ast.parse(code, "<internal-debug>", "exec")
        last = tree.body[-1] if tree.body else None
        if isinstance(last, ast.Expr):
            tree.body = tree.body[:-1]
            if tree.body:
                exec(  # noqa: S102
                    compile(tree, "<internal-debug>", "exec"),
                    self._namespace,
                )
            return eval(  # noqa: S307
                compile(
                    ast.Expression(last.value),
                    "<internal-debug>",
                    "eval",
                ),
                self._namespace,
            )
        exec(compile(tree, "<internal-debug>", "exec"), self._namespace)  # noqa: S102
        return None


# ── 会话级注册表 ──────────────────────────────────────────


class DebugManager:
    """按 (agent_id, user_id, session_id) 隔离的调试线程注册表。"""

    def __init__(self) -> None:
        self._threads: Dict[Tuple[str, str, str], DebugThread] = {}
        self._lock = threading.Lock()

    def start(
        self, key: Tuple[str, str, str]
    ) -> Tuple[Optional[DebugThread], bool, str]:
        """启动（或幂等复用）会话调试线程。

        返回 (线程, 是否新建, 状态)。状态：
        - created：新建成功；
        - running：已有存活线程，幂等返回；
        - stuck：旧线程收到退出信号但未结束（卡死），拒绝新建。
        """
        with self._lock:
            old = self._threads.get(key)
            if old is not None and old.is_alive():
                if old.stop_requested:
                    return old, False, STATUS_STUCK
                return old, False, STATUS_RUNNING
            dt = DebugThread(key)
            self._threads[key] = dt
            return dt, True, STATUS_CREATED

    def get_ready(
        self, key: Tuple[str, str, str]
    ) -> Tuple[Optional[DebugThread], Optional[str]]:
        """取可执行任务的线程；不可用时返回 (None, 错误说明)。"""
        with self._lock:
            dt = self._threads.get(key)
        if dt is None:
            return None, (
                "本会话尚未启动调试线程，请先调用 internal_debug_tools_start。"
            )
        if not dt.is_alive():
            return None, (
                "调试线程已结束，请重新调用 internal_debug_tools_start 启动。"
            )
        if dt.stop_requested:
            return None, (
                "调试线程已收到退出信号（正在退出或卡死），无法执行任务。"
                "请调用 internal_debug_tools_stop 确认状态；若线程卡死，"
                "推荐重启宿主。"
            )
        return dt, None

    def stop(self, key: Tuple[str, str, str], timeout: float) -> Dict[str, Any]:
        """停止会话调试线程（幂等）。"""
        with self._lock:
            dt = self._threads.get(key)
        if dt is None or not dt.is_alive():
            return {
                "ok": True,
                "already": True,
                "note": "本会话没有正在运行的调试线程，无需停止（幂等）。",
            }
        return dt.stop(timeout)


# ── 进程级单例 ────────────────────────────────────────────

_manager: Optional[DebugManager] = None
_manager_lock = threading.Lock()


def get_manager() -> DebugManager:
    """获取进程级 DebugManager 单例。"""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = DebugManager()
        return _manager
