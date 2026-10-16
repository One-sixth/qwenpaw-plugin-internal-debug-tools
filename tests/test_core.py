# -*- coding: utf-8 -*-
"""core.py 单元测试：DebugThread 执行语义 + DebugManager 状态机。

不起宿主、不依赖 contextvar，直接构造线程与注册表。
卡死场景使用短超时（总耗时 ~4s），死循环线程为 daemon，进程退出即清。
"""

import io
import sys
import threading
import time

import pytest

from core import (DEFAULT_EVAL_TIMEOUT, MAX_EVAL_TIMEOUT, MAX_RESULT_CHARS,
                  STATUS_CREATE_FAILED, STATUS_CREATED, STATUS_RUNNING,
                  STATUS_STUCK, DebugManager, DebugThread,
                  _clamp_eval_timeout)

FAKE_KEY = ("agent-t", "user-t", "session-test-core")


def make_thread() -> DebugThread:
    return DebugThread(FAKE_KEY)


def make_manager() -> DebugManager:
    return DebugManager()


# ── 执行语义 ──────────────────────────────────────────────


class TestExecSemantics:
    def test_eval_expression(self):
        dt = make_thread()
        r = dt.eval("1 + 1", timeout=5)
        assert r["ok"] is True
        assert r["result"] == "2"

    def test_statement_and_persistence(self):
        dt = make_thread()
        r1 = dt.eval("x = 41 + 1", timeout=5)
        assert r1["ok"] is True and r1["result"] == "None"
        # 跨调用状态保留
        r2 = dt.eval("x * 2", timeout=5)
        assert r2["ok"] is True and r2["result"] == "84"

    def test_last_expr_captured(self):
        dt = make_thread()
        r = dt.eval("y = 3\ny + 1", timeout=5)
        assert r["ok"] is True and r["result"] == "4"
        # 变量已定义
        assert dt.eval("y", timeout=5)["result"] == "3"

    def test_stdout_captured(self):
        dt = make_thread()
        r = dt.eval('print("hello-debug")', timeout=5)
        assert r["ok"] is True
        assert "hello-debug" in r["stdout"]
        assert r["result"] == "None"

    def test_import_persists(self):
        dt = make_thread()
        r1 = dt.eval("import statistics", timeout=5)
        assert r1["ok"] is True
        r2 = dt.eval("statistics.mean([1, 2, 3])", timeout=5)
        assert r2["result"] == "2"

    def test_builtin_modules_present(self):
        dt = make_thread()
        r = dt.eval(
            "'sys' in globals() and 'os' in globals() and 'gc' in globals()"
            " and 'threading' in globals() and 'asyncio' in globals()"
            " and 'json' in globals()",
            timeout=5,
        )
        assert r["ok"] is True
        assert r["result"] == "True"

    def test_loop_ref_injected(self):
        dt = make_thread()
        sentinel = object()
        r = dt.eval("_loop is not None", timeout=5, loop_ref=sentinel)
        assert r["result"] == "True"

    def test_underscore_last_result(self):
        dt = make_thread()
        dt.eval("7 * 6", timeout=5)
        r = dt.eval("_ + 1", timeout=5)
        assert r["result"] == "43"

    def test_exception_does_not_kill_thread(self):
        dt = make_thread()
        r1 = dt.eval("1 / 0", timeout=5)
        assert r1["ok"] is False
        assert "ZeroDivisionError" in r1["error"]
        # 线程仍存活、仍可继续执行
        assert dt.is_alive() is True
        r2 = dt.eval("40 + 2", timeout=5)
        assert r2["ok"] is True and r2["result"] == "42"

    def test_systemexit_caught(self):
        dt = make_thread()
        r = dt.eval("raise SystemExit(3)", timeout=5)
        assert r["ok"] is False
        assert "SystemExit" in r["error"]
        assert dt.is_alive() is True  # BaseException 也不杀线程

    def test_result_repr_truncated(self):
        dt = make_thread()
        r = dt.eval("'x' * 100000", timeout=5)
        assert r["ok"] is True
        assert r["truncated"] is True
        assert len(r["result"]) <= MAX_RESULT_CHARS + 100
        assert "已截断" in r["result"]

    def test_syntax_error_reported(self):
        dt = make_thread()
        r = dt.eval("def def def", timeout=5)
        assert r["ok"] is False
        assert "SyntaxError" in r["error"]
        assert dt.is_alive() is True

    def test_status_snapshot(self):
        dt = make_thread()
        dt.eval("1", timeout=5)
        s = dt.status()
        assert s["alive"] is True
        assert s["executed"] == 1
        assert s["thread_name"].startswith("internal-debug-")
        assert s["stop_requested"] is False


# ── 超时语义 ──────────────────────────────────────────────


class TestTimeout:
    def test_timeout_clamp(self):
        assert _clamp_eval_timeout(0) == DEFAULT_EVAL_TIMEOUT
        assert _clamp_eval_timeout(-5) == DEFAULT_EVAL_TIMEOUT
        assert _clamp_eval_timeout(None) == DEFAULT_EVAL_TIMEOUT
        assert _clamp_eval_timeout(2000) == MAX_EVAL_TIMEOUT
        assert _clamp_eval_timeout(900) == 900.0
        assert _clamp_eval_timeout(1) == 1.0

    def test_zero_timeout_uses_default_not_infinite(self):
        """≤0 不再无限等待：按默认 30s 上限，正常短任务照常完成。"""
        dt = make_thread()
        r = dt.eval("time.sleep(0.5)\n'ok-after-sleep'", timeout=0)
        assert r["ok"] is True
        assert r["result"] == "'ok-after-sleep'"

    def test_eval_timeout_returns(self):
        dt = make_thread()
        r = dt.eval("time.sleep(5)", timeout=0.5)
        assert r["ok"] is False
        assert r.get("timed_out") is True
        assert dt.is_alive() is True  # 任务仍在跑，线程未死

    def test_timed_out_task_finishes_later(self):
        dt = make_thread()
        dt.eval("_marker = 'pending'\ntime.sleep(1)\n_marker = 'done'",
                timeout=0.3)
        time.sleep(1.5)  # 等任务自然结束
        r = dt.eval("_marker", timeout=5)
        assert r["result"] == "'done'"  # 超时任务的副作用可见

    def test_print_not_hijacked_across_threads(self):
        """回归测试：任务执行期间其他线程的 print 不被劫持。

        曾用 contextlib.redirect_stdout 捕获输出——它是进程级
        sys.stdout 全局替换，任务卡死时宿主所有线程的输出都被
        劫持进无人认领的 StringIO（黑洞）。现改为 namespace 注入
        捕获版 print，sys.stdout 保持宿主原样。
        """
        dt = make_thread()
        captured = io.StringIO()
        real_stdout = sys.stdout
        sys.stdout = captured
        try:
            dt.eval("print('from-task')\nwhile True: pass", timeout=0.3)
            time.sleep(0.2)  # 确保调试线程已进入死循环
            t = threading.Thread(target=lambda: print("outside-thread"))
            t.start()
            t.join()
            print("outside-main")
        finally:
            sys.stdout = real_stdout
        text = captured.getvalue()
        assert "outside-thread" in text
        assert "outside-main" in text


# ── stop / 状态机 ─────────────────────────────────────────


class TestStopAndStateMachine:
    def test_stop_when_never_started(self):
        mgr = make_manager()
        r = mgr.stop(FAKE_KEY, timeout=5)
        assert r["ok"] is True and r.get("already") is True

    def test_stop_normal(self):
        mgr = make_manager()
        dt, created, state = mgr.start(FAKE_KEY)
        assert created is True and state == STATUS_CREATED
        r = mgr.stop(FAKE_KEY, timeout=5)
        assert r["ok"] is True
        assert dt.is_alive() is False

    def test_start_idempotent(self):
        mgr = make_manager()
        dt1, created1, state1 = mgr.start(FAKE_KEY)
        assert created1 is True and state1 == STATUS_CREATED
        dt2, created2, state2 = mgr.start(FAKE_KEY)
        assert created2 is False and state2 == STATUS_RUNNING
        assert dt1 is dt2  # 同一线程复用

    def test_restart_after_thread_death(self):
        mgr = make_manager()
        dt1, _, _ = mgr.start(FAKE_KEY)
        mgr.stop(FAKE_KEY, timeout=5)
        dt2, created, _ = mgr.start(FAKE_KEY)
        assert created is True
        assert dt1 is not dt2  # 旧线程已结束 → 允许新建

    def test_stop_timeout_marks_stuck_and_blocks_start(self):
        mgr = make_manager()
        mgr.start(FAKE_KEY)
        # 制造卡住线程：死循环任务 + eval 超时放弃
        r_eval = mgr.get_ready(FAKE_KEY)[0].eval(
            "while True: pass", timeout=0.5
        )
        assert r_eval.get("timed_out") is True
        # stop 超时 → 卡死
        r_stop = mgr.stop(FAKE_KEY, timeout=2)
        assert r_stop["ok"] is False
        assert r_stop.get("stuck") is True
        # start 被拒绝
        _, created, state = mgr.start(FAKE_KEY)
        assert state == STATUS_STUCK
        assert created is False

    def test_stop_twice_idempotent(self):
        mgr = make_manager()
        mgr.start(FAKE_KEY)
        assert mgr.stop(FAKE_KEY, timeout=5)["ok"] is True
        r2 = mgr.stop(FAKE_KEY, timeout=5)
        assert r2["ok"] is True and r2.get("already") is True

    def test_stop_with_zero_timeout_sets_signal_only(self):
        mgr = make_manager()
        dt, _, _ = mgr.start(FAKE_KEY)
        r = mgr.stop(FAKE_KEY, timeout=0)
        # 不等待，但信号已设置；稍候线程应自行退出
        assert r["ok"] is True
        assert r.get("signal_only") is True
        assert dt.stop_requested is True
        dt._thread.join(5)
        assert dt.is_alive() is False


    def test_queued_timeout_note_mentions_queueing(self):
        """排队超时：前序长任务占线时，note 应说明可能仍在排队。"""
        mgr = make_manager()
        mgr.start(FAKE_KEY)
        dt = mgr.get_ready(FAKE_KEY)[0]
        dt.eval("time.sleep(1.5)", timeout=0.2)  # 占线，超时放弃
        r = dt.eval("42", timeout=0.5)  # 新任务排队，等待计时含排队时间
        assert r.get("timed_out") is True
        assert "排队" in r["note"]

    def test_stuck_self_heals_when_thread_exits_later(self):
        """stuck 自愈：卡住的是长任务时线程会自行退出，start 随后恢复。"""
        mgr = make_manager()
        mgr.start(FAKE_KEY)
        dt = mgr.get_ready(FAKE_KEY)[0]
        dt.eval("time.sleep(1)", timeout=0.2)  # 长任务占线
        dt.stop(timeout=0)  # signal_only：设退出信号，不等待
        _, created, state = mgr.start(FAKE_KEY)
        assert state == STATUS_STUCK  # 线程仍活着 → 拒绝
        time.sleep(1.5)  # 任务结束，线程检查退出信号自行退出
        assert dt.is_alive() is False
        dt2, created, state = mgr.start(FAKE_KEY)
        assert state == STATUS_CREATED and created is True  # 自愈，无需重启


class TestHardening:
    """复审加固项回归：缓冲上限、构造失败折叠。"""

    def test_print_buffer_overflow(self):
        from core import MAX_CAPTURE_BUFFER_CHARS, _make_capturing_print

        buf = io.StringIO()
        p = _make_capturing_print(buf)
        p("x" * (MAX_CAPTURE_BUFFER_CHARS // 2))
        p("y" * (MAX_CAPTURE_BUFFER_CHARS // 2))  # 触发超限
        p("z" * 1000)  # 超限后丢弃
        text = buf.getvalue()
        assert "缓冲上限" in text
        assert "z" * 1000 not in text
        assert len(text) < MAX_CAPTURE_BUFFER_CHARS + 200

    def test_print_single_huge_write_also_capped(self):
        """单次巨量 print 也不能穿透缓冲（第二轮复审 P1）。"""
        from core import MAX_CAPTURE_BUFFER_CHARS, _make_capturing_print

        buf = io.StringIO()
        p = _make_capturing_print(buf)
        p("x" * (MAX_CAPTURE_BUFFER_CHARS * 2))  # 一次写 8MB
        text = buf.getvalue()
        assert len(text) < MAX_CAPTURE_BUFFER_CHARS + 200
        assert "缓冲上限" in text
        p("more")  # 溢出后丢弃
        assert len(buf.getvalue()) == len(text)

    def test_clean_tb_circular_reference(self):
        """异常链循环引用不死循环（第二轮复审覆盖缺口）。"""
        from core import _clean_tb

        a = ValueError("a")
        b = KeyError("b")
        a.__cause__ = b
        b.__context__ = a  # 构造循环
        text = _clean_tb(a)
        assert "ValueError" in text and "KeyError" in text

    def test_format_stopping_and_timed_out_branches(self):
        from tools.eval import _format

        assert _format({"state": "stopping", "note": "stopping-note"}) == \
            "stopping-note"
        assert _format({"timed_out": True, "note": "timeout-note"}) == \
            "timeout-note"

    def test_self_eval_rejected(self):
        """调试线程内调 dt.eval：self_eval 拒绝，不自我等待（与 stop 对称）。"""
        dt = make_thread()
        dt._namespace["me"] = dt
        r = dt.eval("me.eval('1+1', timeout=5)", timeout=5)
        # 外层任务正常完成；内层调用的 self_eval dict 作为表达式值的 repr
        assert r["ok"] is True
        assert "'self_eval'" in r["result"]
        assert "已拒绝" in r["result"]
        dt._stop_event.set()  # 收尾：让线程退出
        dt._queue.put(None)
        dt._thread.join(5)
        assert dt.is_alive() is False

    def test_clamp_rejects_non_numeric(self):
        assert _clamp_eval_timeout("30") == DEFAULT_EVAL_TIMEOUT
        assert _clamp_eval_timeout(None) == DEFAULT_EVAL_TIMEOUT

    def test_start_create_failure_folding(self, monkeypatch):
        """线程创建失败折叠为 create_failed，不向工具协程抛异常。"""
        import core as core_mod

        class Boom:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("can't start new thread")

        monkeypatch.setattr(core_mod.threading, "Thread", Boom)
        mgr = make_manager()
        dt, created, state = mgr.start(FAKE_KEY)
        assert dt is None
        assert created is False
        assert state == STATUS_CREATE_FAILED

    def test_stop_inside_debug_thread_signal_only(self):
        """调试线程内的代码调 stop：不 join 自己，折叠为 signal_only。

        宿主内实测发现：eval("me.stop(1)") 会触发
        RuntimeError: cannot join current thread（已被任务级保护折叠，
        线程不死），修复后应返回 signal_only 且线程随后自行退出。
        """
        dt = make_thread()
        dt._namespace["me"] = dt
        r = dt.eval("me.stop(5)", timeout=5)
        assert r["ok"] is True
        assert "signal_only" in r["result"]
        dt._thread.join(5)
        assert dt.is_alive() is False  # 信号已设，任务完成后线程退出


class TestFormatAndCleanTb:
    """新格式（耗时前置+分区）与 traceback 引擎帧过滤。"""

    def test_clean_tb_filters_engine_frames(self):
        dt = make_thread()
        r = dt.eval("1/0", timeout=5)
        assert "core.py" not in r["error"]
        assert "ZeroDivisionError: division by zero" in r["error"]
        assert 'File "<internal-debug>"' in r["error"]

    def test_clean_tb_keeps_double_chain(self):
        # SyntaxError 回退 exec 再炸：双层链，每层都只留用户帧
        dt = make_thread()
        r = dt.eval('data = {"a": 1}\ndata["b"]', timeout=5)
        assert "core.py" not in r["error"]
        assert "During handling" in r["error"]
        assert r["error"].rstrip().endswith("KeyError: 'b'")

    def test_format_success_minimal(self):
        from tools.eval import _format

        text = _format({"ok": True, "result": "1024", "stdout": "",
                        "truncated": False, "elapsed_ms": 3})
        assert text.splitlines()[0] == "⏱ 3ms"
        assert "——return——\n1024" in text
        assert "——output——" not in text

    def test_format_success_with_stdout(self):
        from tools.eval import _format

        text = _format({"ok": True, "result": "45", "stdout": "line1\nline2\n",
                        "truncated": False, "elapsed_ms": 1})
        assert "——return——\n45\n——output——\nline1\nline2\n" in text

    def test_format_error_sections(self):
        from tools.eval import _format

        err = ('Traceback (most recent call last):\n'
               '  File "<internal-debug>", line 2, in <module>\n'
               "KeyError: 'b'")
        text = _format({"ok": False, "error": err, "stdout": "",
                        "truncated": False, "elapsed_ms": 8})
        assert text.splitlines()[0] == "⏱ 8ms"
        assert "——exception——\nKeyError: 'b'" in text
        assert "——traceback——" in text

    def test_exception_summary(self):
        from tools.eval import _exception_summary

        assert _exception_summary("a\nb\nKeyError: 'b'") == "KeyError: 'b'"
        assert _exception_summary("") == "未知异常"


# ── eval 状态守卫 ─────────────────────────────────────────


class TestEvalGuards:
    def test_get_ready_without_start(self):
        mgr = make_manager()
        dt, err = mgr.get_ready(FAKE_KEY)
        assert dt is None
        assert "internal_debug_tools_start" in err

    def test_eval_rejected_when_stopping(self):
        dt = make_thread()
        dt._stop_event.set()
        r = dt.eval("1", timeout=5)
        assert r["ok"] is False
        assert r.get("state") == "stopping"

    def test_get_ready_after_stop_requested(self):
        mgr = make_manager()
        dt, _, _ = mgr.start(FAKE_KEY)
        dt._stop_event.set()
        _, err = mgr.get_ready(FAKE_KEY)
        assert dt is not None
        assert "退出信号" in err


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
