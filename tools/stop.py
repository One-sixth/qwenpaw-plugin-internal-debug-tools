# -*- coding: utf-8 -*-
"""internal_debug_tools_stop — 停止本会话的内部调试线程。"""

from __future__ import annotations

import asyncio

try:
    from ..core import DEFAULT_STOP_TIMEOUT, get_manager
    from ..utils import make_error, make_success, session_key
except ImportError:  # 兼容 pytest 直接以项目根导入
    from core import DEFAULT_STOP_TIMEOUT, get_manager
    from utils import make_error, make_success, session_key


async def internal_debug_tools_stop(timeout: float = DEFAULT_STOP_TIMEOUT):
    """停止本会话的内部调试线程（幂等：没有线程时直接成功）。通过退出事件通知线程结束并最多等待 timeout 秒；超时未结束时线程标记为卡死，本会话将无法再次 start，需要重启宿主清除卡住线程。

    Args:
        timeout: 等待线程结束的最长秒数，默认 60；≤0 表示只设置退出信号、不等待。
    """
    key = session_key()
    result = await asyncio.to_thread(get_manager().stop, key, timeout)

    if result.get("ok"):
        return make_success(result.get("note", "调试线程已停止。"))
    return make_error(
        "停止失败：" + result.get("note", "线程未在限时内结束。")
    )
