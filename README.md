# qwenpaw-plugin-internal-debug-tools

QwenPaw 宿主内部调试套件：按会话启动专属 Python 调试 REPL 线程，实时执行代码查看/修改 QwenPaw 宿主进程的运行时状态。

## ⚠️ 安全定位

工具拥有**宿主进程内任意代码执行权限**，仅供宿主作者本机调试使用。三个工具默认 `enabled=False`，需在 agent 配置（UI 工具开关）中显式开启后才可用。

## 工具

| 工具 | 功能 |
|------|------|
| `internal_debug_tools_start` | 启动本会话专属的调试 REPL 线程（幂等；已在运行只返回状态；上次 stop 未退出的卡死线程会拒绝启动） |
| `internal_debug_tools_eval` | 在调试线程中实时执行一段 Python 代码并返回结果（超时只放弃等待，代码继续执行） |
| `internal_debug_tools_stop` | 通过退出事件停止调试线程（幂等；默认最多等 60s，超时判卡死，需重启宿主） |

## 架构

```
工具协程（宿主 asyncio）            调试线程（daemon，每会话一个）
──────────────────────            ──────────────────────────────
eval(code, timeout) ──queue──▶  串行执行 exec/eval(code, ns)
      │                                │
      └── Future.result(timeout) ◀── 结果 dict
                                   持久 namespace（导入/变量跨调用保留）
```

- **持久 namespace**：预置 `sys/os/gc/time/threading/asyncio/json/re/math/pprint/traceback/collections/concurrent`；`_loop` 指向宿主主事件循环（配合 `asyncio.run_coroutine_threadsafe` 派协程），`_` 为上次成功结果。
- **执行语义**：表达式直接求值；语句序列执行并捕获最后一条表达式语句的值（REPL 语义）；**只有 `print()` 的输出会被捕获返回**——直接 `sys.stdout.write()`、`file=sys.stderr` 等不经过捕获、直接透传宿主控制台；异常折叠为结果，**绝不杀死线程**（含 `SystemExit` 等 BaseException）。
- **保护**：线程主循环每个环节 BaseException 级保护；`stop` 用 `threading.Event` + 队列哨兵双通道通知退出，`join(timeout)` 超时未结束判为卡死——本会话禁止再 `start`，只能重启宿主。
- **会话隔离**：注册表 key = `(agent_id, user_id, session_id)`（读 QwenPaw 内核 contextvar），不同会话的调试线程互不可见。
- **截断**：result repr 限 2000 字符，stdout/stderr/traceback 限 4000 字符。

## 典型用法

```text
internal_debug_tools_start()
internal_debug_tools_eval("[t.name for t in threading.enumerate()]")
internal_debug_tools_eval("sys.modules['qwenpaw'].__version__")
internal_debug_tools_eval("asyncio.run_coroutine_threadsafe(my_coro(), _loop)")
internal_debug_tools_stop()
```

## 安装与测试

```bash
qwenpaw plugin validate ./qwenpaw-plugin-internal-debug-tools
qwenpaw plugin install ./qwenpaw-plugin-internal-debug-tools   # 或 zip 包
# 安装后在 agent 配置中手动开启三个工具

D:\Software\miniconda3\envs\normal\python.exe -m pytest tests/ -v
```

纯标准库实现，无第三方依赖。
