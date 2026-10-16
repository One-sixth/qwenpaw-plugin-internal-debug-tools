# Changelog

## 0.1.0

初版。

- `internal_debug_tools_start`：启动本会话专属调试 REPL 线程（幂等；卡死线程拒绝新建）。
- `internal_debug_tools_eval`：实时执行 Python 代码（表达式求值/语句+末尾表达式捕获；持久 namespace；`_loop` 宿主事件循环引用；仅 print() 输出捕获 + traceback 返回；超时只放弃等待，上限 15 分钟）。
- `internal_debug_tools_stop`：退出事件 + join 等待（默认 60s），超时未结束期间禁止再 start（线程自行退出后可直接 start，持续卡死才需重启宿主）；幂等。
- 线程主循环与任务执行全程 BaseException 级保护，用户代码异常不杀线程。
- stdout 捕获经 namespace 注入捕获版 print 实现（不用 redirect_stdout：进程级全局替换，多线程下会劫持宿主输出）。
- eval timeout 上限 900s（15 分钟）：≤0/None 回落默认 30s，不再允许无限等待；clamp 在 core 层统一生效。
- 会话隔离注册表（agent_id, user_id, session_id）；`qwenpaw plugin validate` 通过。
- 无上下文子 agent 复审后加固：eval 超时提示区分"stop 竞态丢弃/排队或执行中"两场景；stuck 文案按实证软化（长任务卡住可自愈）；print 捕获缓冲上限 4MB；`_safe_execute` 引擎级兜底；`DebugThread` 构造失败折叠为 create_failed；注册表死条目顺手清理。
- eval 返回格式改版（作者定稿）：耗时 `⏱ Xms` 前置，区段制 `——return——`/`——output——`/`——exception——`/`——traceback——`；`_clean_tb` 剔除 traceback 引擎帧（core.py），异常链逐层过滤只留用户帧，异常摘要前置。
- 全套通过**热重载**部署（reload core + tools.eval，未重启宿主）：通道切断 → start 重连的恢复路径实测可行。
- 第二轮复审加固：print 缓冲上限补单次大写入截断（8MB 单次 print 不再穿透）；eval 增加 `current_thread` 自检（线程内 eval 自我等待，拒绝返回 self_eval）；`_clamp_eval_timeout`/stop 的 timeout 非数值防御；README/AGENTS/CHANGELOG 旧文案与测试数同步。
