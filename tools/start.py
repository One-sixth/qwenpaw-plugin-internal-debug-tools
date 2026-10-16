# -*- coding: utf-8 -*-
"""internal_debug_tools_start — 启动本会话的内部调试线程。"""

from __future__ import annotations

import asyncio

try:
    from ..core import (STATUS_CREATE_FAILED, STATUS_RUNNING, STATUS_STUCK,
                        get_manager)
    from ..utils import make_error, make_success, session_key
except ImportError:  # 兼容 pytest 直接以项目根导入
    from core import (STATUS_CREATE_FAILED, STATUS_RUNNING, STATUS_STUCK,
                      get_manager)
    from utils import make_error, make_success, session_key


async def internal_debug_tools_start():
    """启动本会话专属的内部调试 REPL 线程（幂等）。线程内持有一个持久 namespace，配合 internal_debug_tools_eval 实时执行 Python 代码调试 QwenPaw 宿主内部；已在运行时只返回状态不新建；上次 stop 超时未退出的线程会拒绝启动——线程退出后可直接重新 start，持续卡死才需重启宿主。

    Returns:
        成功时返回线程状态（存活/线程名/启动时间/累计执行次数/namespace 变量数）。
    """
    key = session_key()
    manager = get_manager()
    dt, created, state = await asyncio.to_thread(manager.start, key)

    if state == STATUS_STUCK:
        return make_error(
            "启动调试线程失败：上一次 stop 已设置退出信号，但旧线程尚未结束"
            "（可能正在执行长任务或死锁），线程退出前无法启动新调试线程。"
            "可稍后重试 start（若旧线程自行退出即可成功）；持续卡死需重启宿主。"
        )
    if state == STATUS_CREATE_FAILED or dt is None:
        return make_error(
            "启动调试线程失败：线程创建异常（系统线程资源可能耗尽），"
            "请稍后重试或重启宿主。"
        )

    status = dt.status()  # 非阻塞快照，无需丢线程池
    action = "已新建" if created else "复用运行中"
    lines = [
        f"调试线程{action}（会话级）。",
        f"- 线程: {status['thread_name']} (tid={status['thread_id']})",
        f"- 启动时间: {status['started_at']}（已存活 {status['alive_seconds']}s）",
        f"- 累计执行: {status['executed']} 次；namespace 变量数: {status['namespace_vars']}",
    ]
    if not created and state == STATUS_RUNNING:
        lines.append("（线程此前已在运行，本次为幂等调用，namespace 状态保持不变）")
    lines.append("现在可以用 internal_debug_tools_eval 实时执行 Python 代码了。")
    return make_success("\n".join(lines))
