# agent-ui 的测试（审计 2026-11-18 #185）。
#
# 跑法（在仓库根）：
#   python3 -I -B -m unittest discover -s .github/tests
# （不加 -t .：.github 不是合法的包名，加了 unittest 会报 Start directory is not importable。）
#
# 只用标准库，不连 D-Bus、不调 niri：gi 换成测试里写的假模块（假 AT-SPI 树），
# niri_info 换成桩。HOME 指到临时目录。
# 放在 .github/tests/ 而不是 tests/：打包会跳过 .github，测试不会进包。

import importlib.machinery
import importlib.util
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "skills" / "niri-agent" / "agent-ui"

# 只会 raise ImportError 的 gi：模拟没装 PyGObject。
NO_GI = 'raise ImportError("simulated: no PyGObject")\n'

# 够 agent-ui 跑一遍的假 AT-SPI。
FAKE_GI_INIT = "def require_version(name, version):\n    pass\n"
FAKE_GI_REPOSITORY = textwrap.dedent('''
    class _Enum:
        def __init__(self, *names):
            for n in names:
                setattr(self, n, n)

    class Atspi:
        StateType = _Enum("CHECKED", "SELECTED", "PRESSED", "EXPANDED", "FOCUSED",
                          "ENABLED", "SHOWING", "VISIBLE")
        CoordType = _Enum("WINDOW", "SCREEN")
        desktop = None

        @staticmethod
        def get_desktop(i):
            return Atspi.desktop

        class Text:
            @staticmethod
            def get_text(t, a, b):
                return ""
''')


class Rect:
    def __init__(self, x, y, w, h):
        self.x, self.y, self.width, self.height = x, y, w, h


class StateSet:
    def __init__(self, *states):
        self.states = set(states)

    def contains(self, st):
        return st in self.states


class Node:
    """假的可访问对象。vanish_after=N：get_state_set 第 N 次以后抛异常（节点中途消失）。"""

    def __init__(self, role, name="", rect=None, children=(), pid=None, vanish_after=None):
        self.role, self.name, self.rect = role, name, rect
        self.children, self.pid = list(children), pid
        self.vanish_after, self.state_calls = vanish_after, 0

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_process_id(self):
        return self.pid

    def get_state_set(self):
        self.state_calls += 1
        if self.vanish_after is not None and self.state_calls > self.vanish_after:
            raise RuntimeError("The application no longer exists")
        return StateSet("SHOWING", "VISIBLE", "ENABLED")

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, i):
        return self.children[i]

    def get_component_iface(self):
        if self.rect is None:
            return None
        rect = self.rect

        class Comp:
            def get_extents(self, coord):
                return rect
        return Comp()

    def get_text_iface(self):
        return None

    def get_value_iface(self):
        return None


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("MIYU_", "XDG_"))}
        env["HOME"] = str(self.base / "home")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)


class HelpWithoutGi(Sandbox):
    # 审计 #185-2：顶层就 import gi，没装 PyGObject 时连 --help 都看不了（ImportError）。
    # 应先 argparse，用到 AT-SPI 时才 import gi。
    def test_help_works_without_pygobject(self):
        fake = self.base / "nogi"
        fake.mkdir()
        (fake / "gi.py").write_text(NO_GI)
        code = ("import runpy, sys; sys.path.insert(0, sys.argv[1]); "
                "sys.argv = [sys.argv[2], '--help']; runpy.run_path(sys.argv[0], run_name='__main__')")
        p = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(fake), str(SCRIPT)],
                           capture_output=True, text=True, timeout=10)
        self.assertNotIn("ImportError", p.stderr)
        self.assertEqual(p.returncode, 0, p.stderr[-500:])
        self.assertIn("--id", p.stdout)


class VanishingNode(Sandbox):
    # 审计 #185-1：emit() 里列表推导对每个 STATES 项调一次 obj.get_state_set()，没包 try
    # （同一函数后面那次包了，walk 里那次也包了）。节点在 walk 取完状态后消失，
    # emit 抛异常，整棵树的转储中断。应照常列出（复用 walk 拿到的状态，或包 try）。
    def load_with_fake_gi(self):
        fake = self.base / "fakegi"
        (fake / "gi").mkdir(parents=True)
        (fake / "gi" / "__init__.py").write_text(FAKE_GI_INIT)
        (fake / "gi" / "repository.py").write_text(FAKE_GI_REPOSITORY)
        saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "gi" or k.startswith("gi.")}
        sys.path.insert(0, str(fake))
        try:
            sys.dont_write_bytecode = True
            loader = importlib.machinery.SourceFileLoader("agent_ui_under_test", str(SCRIPT))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            mod = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
        finally:
            sys.path.remove(str(fake))
            for k in [k for k in sys.modules if k == "gi" or k.startswith("gi.")]:
                del sys.modules[k]
            sys.modules.update(saved)
        return mod

    def test_node_vanishing_mid_dump_still_lists(self):
        mod = self.load_with_fake_gi()
        button = Node("push button", "OK", Rect(10, 10, 80, 30), vanish_after=1)
        stable = Node("push button", "Cancel", Rect(100, 10, 80, 30))
        frame = Node("frame", "Win", Rect(0, 0, 800, 600), [button, stable])
        app = Node("application", "app", None, [frame], pid=42)
        mod.Atspi.desktop = Node("desktop", "", None, [app])
        mod.niri_info = lambda wid: {"pid": 42, "surface_offset": (0, 0), "size": (800, 600)}
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["agent-ui", "--id", "1"]), redirect_stdout(out):
            mod.main()
        text = out.getvalue()
        self.assertIn("push button 'OK'", text)
        self.assertIn("push button 'Cancel'", text)


if __name__ == "__main__":
    unittest.main()
