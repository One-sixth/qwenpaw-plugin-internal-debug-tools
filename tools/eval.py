# -*- coding: utf-8 -*-
"""internal_debug_tools_eval — 在调试线程中实时执行 Python 代码。"""

from __future__ import annotations

import asyncio

try:
    from ..core import DEFAULT_EVAL_TIMEOUT, get_manager
    from ..utils import make_error, make_success, session_key
except ImportError:  # 兼容 pytest 直接以项目根导入
    from core import DEFAULT_EVAL_TIMEOUT, get_manager
    from utils import make_error, make_success, session_key


def _format(result: dict) -> str:
    """把执行结果 dict 格式化为人类可读文本。"""
    if result.get("state") == "stopping":
        return result.get("note", "调试线程正在退出，无法执行。")
    if result.get("timed_out"):
        return result.get("note", "执行超时。")

    parts = ["✅ 执行成功" if result.get("ok") else "❌ 执行出错"]
    if result.get("ok"):
        parts.append(f"结果: {result.get('result')}")
    else:
        parts.append(f"异常 traceback:\n{result.get('error', '')}")
    if result.get("stdout"):
        parts.append(f"print 输出:\n{result['stdout']}")
    parts.append(f"耗时: {result.get('elapsed_ms')}ms")
    if result.get("truncated"):
        parts.append("（输出过长已截断）")
    return "\n".join(parts)


async def internal_debug_tools_eval(code: str, timeout: float = DEFAULT_EVAL_TIMEOUT):
    """在内部调试线程中实时执行一段 Python 代码并返回结果（需先 internal_debug_tools_start）。表达式直接求值返回；语句则执行并捕获最后一条表达式语句的值；异常折叠为 traceback 返回且不会杀死线程。注意：只有 print() 的输出会被捕获返回，直接 sys.stdout.write() / file=sys.stderr 等不经过捕获、直接透传宿主控制台。namespace 跨调用持久（导入与变量保留），预置 sys/os/gc/threading/asyncio/json/re 等模块，_loop 指向宿主主事件循环，_ 为上次成功结果。timeout 超时只放弃等待，代码仍在内部线程继续执行。

    Args:
        code: 要执行的 Python 代码（表达式或语句序列）。
        timeout: 等待执行结果的最长秒数，默认 30，上限 900（15 分钟）；≤0 视为默认 30。超时后代码仍在调试线程中继续执行。
    """
    if not code or not code.strip():
        return make_error("code 不能为空。")

    key = session_key()
    dt, err = get_manager().get_ready(key)
    if dt is None:
        return make_error(err)

    loop_ref = None
    try:
        loop_ref = asyncio.get_running_loop()
    except RuntimeError:
        pass  # 极少：无运行循环时提交，namespace 里就没有 _loop

    result = await asyncio.to_thread(dt.eval, code, timeout, loop_ref)
    # 用户代码执行出错属于 REPL 正常工作状态（ok=False + traceback），
    # 不视为工具级错误，统一经成功通道返回格式化结果。
    return make_success(_format(result))
