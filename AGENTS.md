# AGENTS.md — qwenpaw-plugin-internal-debug-tools

QwenPaw 宿主内部调试套件插件。按会话启动专属 Python 调试 REPL 线程，实时 eval 调试宿主运行时内部。

## 架构

- `core.py`：DebugThread（任务队列 + 持久 namespace + BaseException 级保护）+ DebugManager（会话隔离注册表，key = agent/user/session，contextvar 读取同 process-tools 模式）。
- `tools/`：start / eval / stop 三个工具，`asyncio.to_thread` 包裹一切阻塞等待（join / future.result），绝不阻塞宿主事件循环。
- `utils.py`：contextvar 会话定位 + ToolChunk 构造（make_success/make_error）。

## 关键设计决策

- **线程保护**：主循环 get/解包/执行/回传四环节全部 BaseException 级 try/except；用户代码 `raise SystemExit` 也只是折叠为结果 dict，线程不死。
- **stop 状态机**（作者确立，v0.1.0）：
  - `stop_event.set()` + 队列哨兵 None 双通道唤醒；`join(timeout)` 默认 60s；
  - 超时未结束 → 卡死状态，本会话 `start` 被拒（STATUS_STUCK），唯一出路是重启宿主；
  - start 是否新建线程**完全取决于旧线程 is_alive()**，不单独维护 tombstone；
  - `timeout≤0` = 只设信号不等（signal_only），不得与卡死混淆（曾因 join(0) 后立即查 is_alive 误判卡死，已修）。
- **eval 超时语义**：超时只放弃等待，任务继续在调试线程跑（副作用后续可见）；死循环任务最终会转化为 stop 卡死场景。**timeout 上限 900s（15 分钟，作者定稿 2026-10-16）**：≤0/None 不再无限等待、回落默认 30s，clamp 在 core 层 `_clamp_eval_timeout` 统一生效（stop 的 ≤0=signal_only 是独立语义，不受影响）。
- **REPL 语义**：compile 'eval' 失败转 'exec'，ast 判断最后一条 Expr 语句捕获返回值；`_` 存上次成功结果（异常时不覆盖）；`_loop` 每次提交刷新为宿主 running loop。
- **输出捕获边界（作者定稿）**：仅 `print()` 经 namespace 注入捕获；孤儿输出不加缓存；`sys.stdout.write`/`file=sys.stderr` 等透传宿主；结果不再含 stderr 字段（redirect 移除后恒空，已删）。
- **enabled=False**：任意代码执行权限，默认关闭，作者自行开启。
- **工具层错误语义**：用户代码执行出错（ok=False + traceback）经 make_success 返回（REPL 正常工作状态），仅线程状态类问题走 make_error。

## 测试

`tests/test_core.py`：25 项，不起宿主直接构造线程/注册表；卡死场景用短超时（eval 0.5s + stop 2s），daemon 线程进程退出即清。运行：

```bash
D:\Software\miniconda3\envs\normal\python.exe -m pytest tests/ -v
```

## 踩坑记录

- **redirect_stdout 进程级劫持（v0.1.0 大坑，实测撞上）**：`contextlib.redirect_stdout` 是全局 `sys.stdout` 替换、非线程局部——调试线程任务卡死时 with 永不退出，宿主所有线程的 print 全部被劫持进无人认领的 StringIO（黑洞），且永不恢复。修复：弃用 redirect，改为每次 `_safe_execute` 往 namespace 注入捕获版 `print`（eval/exec 的 globals 查找先于 builtins，print 自然命中），sys.stdout 原封不动。局限：直接 `sys.stdout.write` 的代码不被捕获（透传宿主，可接受）。
- core.py 顶部漏 `import asyncio`（namespace 预置需要，引擎本体不用）→ NameError 一片，25 测全挂。
- 测试里生成器表达式内 `dir()` 返回的是 genexp 自己的 frame locals，不是 eval 全局命名空间 → 检查预置模块用 `globals()`。
- `statistics.mean([1,2,3])` 返回 int `2` 不是 float——repr 是 `'2'`。
- pwsh 管道下 python 脚本 print 输出可能整段丢失（buffered + 编码组合），跑演示/验证时加 `-X utf8` + env `PYTHONUNBUFFERED=1` + 工具侧 encoding="utf-8"。
