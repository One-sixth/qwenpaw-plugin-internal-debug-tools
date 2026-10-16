# -*- coding: utf-8 -*-
"""Internal Debug Tools 插件入口。

注册 3 个 QwenPaw 内部调试工具：
- internal_debug_tools_start：启动本会话专属的调试 REPL 线程（幂等）；
- internal_debug_tools_eval ：在调试线程中实时执行 Python 代码并返回结果；
- internal_debug_tools_stop ：通过退出事件停止调试线程（幂等，超时判卡死）。

安全定位：工具拥有宿主进程内的任意代码执行权限，仅供宿主作者本机
调试使用；默认 enabled=False，需用户在 agent 配置中显式开启。
"""

import logging

from qwenpaw.plugins.api import PluginApi

from .tools.eval import internal_debug_tools_eval
from .tools.start import internal_debug_tools_start
from .tools.stop import internal_debug_tools_stop

logger = logging.getLogger(__name__)


class InternalDebugToolsPlugin:
    """Internal Debug Tools 插件主类。"""

    def register(self, api: PluginApi) -> None:
        logger.info("注册 Internal Debug Tools 插件（3 个工具，默认关闭）...")

        # ── 启动 ──
        api.register_tool(
            tool_name="internal_debug_tools_start",
            tool_func=internal_debug_tools_start,
            description="启动本会话专属的内部调试 REPL 线程（幂等）。",
            icon="▶️",
            enabled=False,
            tool_type="internal",
            target_param="",
        )

        # ── 执行 ──
        api.register_tool(
            tool_name="internal_debug_tools_eval",
            tool_func=internal_debug_tools_eval,
            description=(
                "在内部调试线程中实时执行 Python 代码并返回结果"
                "（持久 namespace，可访问宿主运行时状态；仅 print()"
                " 输出被捕获）。"
            ),
            icon="⚡",
            enabled=False,
            tool_type="internal",
            target_param="code",
        )

        # ── 停止 ──
        api.register_tool(
            tool_name="internal_debug_tools_stop",
            tool_func=internal_debug_tools_stop,
            description=(
                "通过退出事件停止本会话的内部调试线程（幂等；"
                "超时未结束判为卡死，需重启宿主）。"
            ),
            icon="⏹️",
            enabled=False,
            tool_type="internal",
            target_param="",
        )


plugin = InternalDebugToolsPlugin()  # REQUIRED name
