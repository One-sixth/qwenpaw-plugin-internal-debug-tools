# AGENTS.md — qwenpaw-plugin-internal-debug-tools

QwenPaw 宿主内部调试套件插件。按会话启动专属 Python 调试 REPL 线程，实时 eval 调试宿主运行时内部。

## 架构

- `core.py`：DebugThread（任务队列 + 持久 namespace + BaseException 级保护）+ DebugManager（会话隔离注册表，key = agent/user/session，contextvar 读取同 process-tools 模式）。
- `tools/`：start / eval / stop 三个工具，`asyncio.to_thread` 包裹一切阻塞等待（join / future.result），绝不阻塞宿主事件循环。
- `utils.py`：contextvar 会话定位 + ToolChunk 构造（make_success/make_error）。

## 关键设计决策

- **线程保护**：主循环 get/解包/执行/回传四环节全部 BaseException 级 try/except；用户代码 `raise SystemExit` 也只是折叠为结果 dict，线程不死。`_safe_execute` 外层另有引擎级兜底（format_exception/getvalue 等极端边缘再抛异常也不会杀线程）；`DebugThread` 构造失败（线程资源耗尽）由 `manager.start` 折叠为 create_failed。
- **stop 状态机**（作者确立，v0.1.0）：
  - `stop_event.set()` + 队列哨兵 None 双通道唤醒；`join(timeout)` 默认 60s；
  - 超时未结束 → stuck 状态，本会话 `start` 被拒（STATUS_STUCK）；**stuck 可自愈**——若卡住的是长任务（非死锁）线程最终自行退出，start 直接恢复可用，持续卡死才需重启宿主（复审实证后文案软化）；
  - start 是否新建线程**完全取决于旧线程 is_alive()**，不单独维护 tombstone；
  - `timeout≤0` = 只设信号不等（signal_only），不得与卡死混淆（曾因 join(0) 后立即查 is_alive 误判卡死，已修）。
- **eval 超时语义**：超时只放弃等待，任务继续在调试线程跑（副作用后续可见）；死循环任务最终会转化为 stop 卡死场景。**timeout 上限 900s（15 分钟，作者定稿 2026-10-16）**：≤0/None 不再无限等待、回落默认 30s，clamp 在 core 层 `_clamp_eval_timeout` 统一生效（stop 的 ≤0=signal_only 是独立语义，不受影响）。
- **REPL 语义**：compile 'eval' 失败转 'exec'，ast 判断最后一条 Expr 语句捕获返回值；`_` 存上次成功结果（异常时不覆盖）；`_loop` 每次提交刷新为宿主 running loop。
- **输出捕获边界（作者定稿）**：仅 `print()` 经 namespace 注入捕获；孤儿输出不加缓存；`sys.stdout.write`/`file=sys.stderr` 等透传宿主；结果不再含 stderr 字段（redirect 移除后恒空，已删）；单任务 print 缓冲上限 4MB（`_make_capturing_print` 内 tell() 检查，超限丢弃后续写入，死循环 print 的 OOM 防线）。
- **已知行为（复审 P2 备案）**：① eval 超时放弃后 `to_thread` worker 仍阻塞到任务真正完成（最长 900s），并发多个长任务 eval 可能占满宿主线程池——"任意代码执行、默认关闭、作者自用"定位下可接受；② 并行提交多个 eval 时入队顺序有竞态（`_` 取值依赖执行顺序），串行使用无此问题；③ 注册表死条目在 `get_ready` 时顺手清理。
- **enabled=False**：任意代码执行权限，默认关闭，作者自行开启。
- **工具层错误语义**：用户代码执行出错（ok=False + traceback）经 make_success 返回（REPL 正常工作状态），仅线程状态类问题走 make_error。

## 测试

`tests/test_core.py`：44 项，不起宿主直接构造线程/注册表；卡死场景用短超时（eval 0.5s + stop 2s），daemon 线程进程退出即清。运行：

```bash
D:\Software\miniconda3\envs\normal\python.exe -m pytest tests/ -v
```

## 宿主内实测（2026-10-17，装机 run 树 v0.1.0）

全链路通过：start（namespace 15 变量）→ 看宿主线程（MainThread/19×asyncio/wecom-ws/wechat-poll/debug 线程同框）→ `_loop.is_running()==True` → `run_coroutine_threadsafe` 派协程进宿主循环成功（112ms≈100ms sleep）→ 3s 任务 timeout=1s 准时返回超时提示且副作用可见 → stop/幂等/eval 拒绝/再 start 复活。已知行为：**并行提交多个 eval 时入队顺序有竞态**（`_` 取值依赖执行顺序），串行使用无此问题。

## 热重载（2026-10-17 宿主内实证）

插件模块在宿主内注册为 `plugin_<plugin_id>` 命名空间（`PluginNamespaceFinder` 隔离加载）。**叶子级热重载可行**：

```python
# 在调试 eval 里（改完装机树文件后）：
import importlib, sys
importlib.reload(sys.modules['plugin_qwenpaw_plugin_internal_debug_tools.tools.eval'])
```

原理：reload **原地更新 module dict**，而宿主注册表绑定的旧入口函数 `__globals__` 就是这个 dict → 旧入口执行新叶子逻辑（如 `_format`）。已实证：改 `_format` 文案 → reload → 旧入口函数立即输出新文案 → 还原 reload 恢复。

**边界（实测结论）**：
1. ❌ 入口函数**自身函数体**改动不生效（旧 code object 还是旧字节码）——需重启或重新 register；
2. ⚠️ **core.py 别轻易 reload**：模块级 `_manager = None` 被重置 → 当前调试线程脱离注册表 → 会切断自己的 eval 通道。**恢复路径已实测（2026-10-17）**：reload 完成后当前任务正常返回、下一调用被拒（"尚未启动调试线程"）→ `internal_debug_tools_start` 重连即可，新线程用新类代码，旧线程成无害孤儿 daemon。已用此方式完成 core.py + tools.eval 全套热部署（新格式上线未重启）；
3. ❌ 类改动不影响已实例化对象（旧类实例方法绑定旧类）；
4. ❌ 新增工具需重新 register，会撞 governance ownership claim，勿试；
5. **eval 也有 current_thread 自检**（与 stop 对称，第二轮复审补）：调试线程内调 `dt.eval` 返回 self_eval 拒绝，不会自我等待到超时；
6. `_clean_tb` 有意忽略 `__suppress_context__`（`raise X from None` 仍显示 context 链）——调试 REPL 多显示上下文反而有用；帧过滤按文件名含 "core.py" 子串匹配，极端情况会误伤其他同名 core.py 的宿主帧（作者自用场景可接受，备案）。

## 部署（git 双树）

源码 `D:\Data\github_repo\qwenpaw-plugin-internal-debug-tools`（main）+ 装机 `C:\Users\TWD\.qwenpaw\plugins\qwenpaw-plugin-internal-debug-tools`（run 分支 worktree，同仓库）。更新流程：源码改 → 逐个同步变更文件到装机树（防漏同步）→ 作者重启宿主 → 提交由作者自理；严禁 `qwenpaw plugin install`（会破坏 worktree）。

## 踩坑记录

- **redirect_stdout 进程级劫持（v0.1.0 大坑，实测撞上）**：`contextlib.redirect_stdout` 是全局 `sys.stdout` 替换、非线程局部——调试线程任务卡死时 with 永不退出，宿主所有线程的 print 全部被劫持进无人认领的 StringIO（黑洞），且永不恢复。修复：弃用 redirect，改为每次 `_safe_execute` 往 namespace 注入捕获版 `print`（eval/exec 的 globals 查找先于 builtins，print 自然命中），sys.stdout 原封不动。局限：直接 `sys.stdout.write` 的代码不被捕获（透传宿主，可接受）。
- core.py 顶部漏 `import asyncio`（namespace 预置需要，引擎本体不用）→ NameError 一片，25 测全挂。
- 测试里生成器表达式内 `dir()` 返回的是 genexp 自己的 frame locals，不是 eval 全局命名空间 → 检查预置模块用 `globals()`。
- `statistics.mean([1,2,3])` 返回 int `2` 不是 float——repr 是 `'2'`。
- pwsh 管道下 python 脚本 print 输出可能整段丢失（buffered + 编码组合），跑演示/验证时加 `-X utf8` + env `PYTHONUNBUFFERED=1` + 工具侧 encoding="utf-8"。
- **调试线程内调 stop 会 join 自己（2026-10-17 宿主内实测撞上）**：eval 代码里 `dt.stop()` → `threading.Thread.join` 抛 `RuntimeError: cannot join current thread`（被任务级保护折叠，线程不死）。修复：stop 内 `current_thread` 自检，线程内调用折叠为 signal_only。测试技巧：单测里 `dt._namespace["me"] = dt` 后 eval `"me.stop(5)"`。
