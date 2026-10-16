# Changelog

## 0.1.0

初版。

- `internal_debug_tools_start`：启动本会话专属调试 REPL 线程（幂等；卡死线程拒绝新建）。
- `internal_debug_tools_eval`：实时执行 Python 代码（表达式求值/语句+末尾表达式捕获；持久 namespace；`_loop` 宿主事件循环引用；stdout/stderr/traceback 捕获；超时只放弃等待）。
- `internal_debug_tools_stop`：退出事件 + join 等待（默认 60s），超时判卡死并建议重启宿主；幂等。
- 线程主循环与任务执行全程 BaseException 级保护，用户代码异常不杀线程。
- stdout 捕获经 namespace 注入捕获版 print 实现（不用 redirect_stdout：进程级全局替换，多线程下会劫持宿主输出）。
- eval timeout 上限 900s（15 分钟）：≤0/None 回落默认 30s，不再允许无限等待；clamp 在 core 层统一生效。
- 会话隔离注册表（agent_id, user_id, session_id）；28 项单元测试全通过（含跨线程 print 劫持回归测试）；`qwenpaw plugin validate` 通过。
