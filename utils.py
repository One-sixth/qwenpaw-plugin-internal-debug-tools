# -*- coding: utf-8 -*-
"""通用工具：会话上下文定位 + ToolChunk 返回构造。"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ── 会话上下文定位 ────────────────────────────────────────


def get_current_context() -> Tuple[str, str, str]:
    """获取当前工具调用的 (agent_id, user_id, session_id) 上下文。

    读取 QwenPaw 内核 contextvar（由 ContextVarsSetupHook 每次请求注入）。
    工具必须在请求处理协程内调用本函数。取不到时回落为 "default"。
    """
    try:
        from qwenpaw.app.agent_context import (
            get_current_agent_id,
            get_current_session_id,
            get_current_user_id,
        )

        agent_id = get_current_agent_id() or "default"
        session_id = get_current_session_id() or "default"
        user_id = get_current_user_id() or "default"
        return str(agent_id), str(user_id), str(session_id)
    except Exception:  # noqa: BLE001
        return "default", "default", "default"


def session_key(
    agent_id: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Tuple[str, str, str]:
    """构建调试线程注册表的会话隔离键。

    不传参时自动读当前 contextvar。键内任一维度缺失回 "default"，
    保证不同会话的调试线程互不可见、互不影响。
    """
    if agent_id is None or user_id is None or session_id is None:
        d_agent, d_user, d_session = get_current_context()
        agent_id = agent_id or d_agent
        user_id = user_id or d_user
        session_id = session_id or d_session
    return (str(agent_id), str(user_id), str(session_id))


# ── ToolChunk 返回构造 ───────────────────────────────────

_ToolChunk = None
_ToolResultState = None
_TextBlock = None
_imported = False


def _import_toolchunk() -> None:
    """懒加载 agentscope ToolChunk（在宿主进程内运行时才可用）。"""
    global _ToolChunk, _ToolResultState, _TextBlock, _imported
    if _imported:
        return
    _imported = True
    try:
        from agentscope.message import ToolResultState, TextBlock
        from agentscope.tool import ToolChunk

        _ToolChunk = ToolChunk
        _ToolResultState = ToolResultState
        _TextBlock = TextBlock
    except Exception as e:  # noqa: BLE001
        logger.warning("agentscope ToolChunk 导入失败: %s", e)


def make_success(text: str):
    """构建成功结果 ToolChunk。"""
    _import_toolchunk()
    if _ToolChunk is None:
        return text
    return _ToolChunk(
        is_last=True,
        state=_ToolResultState.SUCCESS,
        content=[_TextBlock(type="text", text=text)],
    )


def make_error(text: str):
    """构建错误结果 ToolChunk。"""
    _import_toolchunk()
    if _ToolChunk is None:
        return text
    return _ToolChunk(
        is_last=True,
        state=_ToolResultState.ERROR,
        content=[_TextBlock(type="text", text=text)],
    )
