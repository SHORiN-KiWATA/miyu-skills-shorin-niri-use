# agent-firefox 的测试（审计 2026-11-18）。
#
# 跑法（在仓库根）：
#   python3 -I -B -m unittest discover -s .github/tests
# （不加 -t .：.github 不是合法的包名，加了 unittest 会报 Start directory is not importable。）
#
# 只用标准库，不联网、不起 Firefox、不调 niri。HOME 指到临时目录，假 profile 全在里面造，
# 加载后先断言脚本的 HOME 在临时目录下——绝不碰真实的 ~/.mozilla。
# 放在 .github/tests/ 而不是 tests/：打包会跳过 .github，测试不会进包。

import importlib.machinery
import importlib.util
import os
import pathlib
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "skills" / "niri-agent" / "agent-firefox"


def lz4_file(payload):
    """只有一个字面量序列的 .jsonlz4（payload 长度 15..269）。"""
    return (b"mozLz40\0" + struct.pack("<I", len(payload))
            + bytes([0xF0, len(payload) - 15]) + payload)


COOKIES = b'{"cookies":[{"host":"a.example","name":"k","value":"1"}]}'


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
        sys.dont_write_bytecode = True
        loader = importlib.machinery.SourceFileLoader("agent_firefox_under_test", str(SCRIPT))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.mod = importlib.util.module_from_spec(spec)
        loader.exec_module(self.mod)
        # HOME 在 import 时取；确认没有指到真实家目录。
        self.assertTrue(str(self.mod.HOME).startswith(str(self.base)), self.mod.HOME)

    def profile(self, name, recovery=None):
        p = self.base / name
        (p / "sessionstore-backups").mkdir(parents=True)
        if recovery is not None:
            (p / "sessionstore-backups" / "recovery.jsonlz4").write_bytes(recovery)
        return p


class FreshAgentProfile(Sandbox):
    # 审计 #183-1：源 profile 没有 cookies.sqlite、只有 recovery.jsonlz4（新建或清过的 profile），
    # agent 的 profile 也还没有 cookies.sqlite（第一次用）时，add_session_cookies 对
    # sqlite3.connect 新建的空库直接 INSERT，抛 OperationalError: no such table: moz_cookies，
    # agent-firefox 整个退出。应照常同步完（建表写入，或跳过会话 cookie）。
    def test_sync_without_source_cookies_db(self):
        src = self.profile("src", lz4_file(COOKIES))
        dst = self.base / "agent"
        n = self.mod.sync(src, dst)
        self.assertIn(n, (0, 1))
        self.assertTrue((dst / "user.js").is_file(), "同步没走完")


class BrokenSessionStore(Sandbox):
    # 审计 #183-2a：截断/损坏的 lz4 让 lz4_block 越界抛 IndexError，session_cookies
    # 只接 OSError/ValueError/JSONDecodeError，于是整个脚本崩。应当作读不出，返回 []。
    def test_truncated_recovery_gives_no_cookies(self):
        truncated = b"mozLz40\0" + struct.pack("<I", 10) + bytes([0x1F]) + b"A" + bytes([0x05])
        src = self.profile("src", truncated)
        self.assertEqual(self.mod.session_cookies(src), [])

    def test_truncated_block_raises_value_error(self):
        # 字面量 'A' 之后偏移量只剩一个字节。
        with self.assertRaises(ValueError):
            self.mod.lz4_block(bytes([0x1F]) + b"A" + bytes([0x05]), 10)

    # 审计 #183-2b：偏移 0、偏移大于已解出的长度都是非法 LZ4；现在前者 IndexError，
    # 后者用负下标静默取错字节（交回 b'ABCCABCC'）。都应抛 ValueError。
    def test_offset_zero_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.mod.lz4_block(bytes([0x10]) + b"A" + struct.pack("<H", 0), 5)

    def test_offset_beyond_output_raises_value_error(self):
        # 字面量 ABC（3 字节），匹配长度 1+4=5、偏移 4 > 3。
        with self.assertRaises(ValueError):
            self.mod.lz4_block(bytes([0x31]) + b"ABC" + struct.pack("<H", 4), 8)

    # 防改坏（现在就绿）：正常的块照常解开，session_cookies 读得出 cookie。
    def test_good_block_still_decodes(self):
        data = lz4_file(COOKIES)
        self.assertEqual(self.mod.lz4_block(data[12:], len(COOKIES)), COOKIES)
        src = self.profile("src", data)
        self.assertEqual(self.mod.session_cookies(src),
                         [{"host": "a.example", "name": "k", "value": "1"}])

    def test_overlapping_match_still_decodes(self):
        # 字面量 'A'，偏移 1、长度 4 的重叠拷贝 → AAAAA。
        self.assertEqual(self.mod.lz4_block(bytes([0x10]) + b"A" + struct.pack("<H", 1), 5),
                         b"AAAAA")


class RsyncDependency(Sandbox):
    # 审计 #184-1：源 profile 有 storage/ 时 sync 硬调 rsync（check=True），没装 rsync 就
    # FileNotFoundError；README「要什么」和 SKILL.md 都没写 rsync。修法二选一：文档写上 rsync，
    # 或改用 shutil.copytree 不再依赖它。文档没写时，PATH 里没有 rsync 也得同步得了。
    def test_rsync_documented_or_not_needed(self):
        root = SCRIPT.parents[2]
        docs = (root / "README.md").read_text(encoding="utf-8") + \
            (SCRIPT.parent / "SKILL.md").read_text(encoding="utf-8")
        if "rsync" in docs:
            return
        src = self.profile("src")
        (src / "storage" / "default" / "site").mkdir(parents=True)
        (src / "storage" / "default" / "site" / "ls.sqlite").write_bytes(b"x")
        dst = self.base / "agent"
        empty = self.base / "empty-bin"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            self.mod.sync(src, dst)
        self.assertEqual((dst / "storage" / "default" / "site" / "ls.sqlite").read_bytes(), b"x")


class LockPid(Sandbox):
    # 审计 #184-2：is_running 只看 lock 链接里的 pid 在 /proc 下存不存在。pid 被别的进程
    # 复用（这里 pid 1 是 init）或者链接写的是别的主机，也当成 agent 的 Firefox 在跑，
    # 于是跳过登录同步、给 firefox 加 --new-window。应核对进程确实是 firefox（主机名对不上
    # 也当作没在跑）。
    def test_lock_pointing_at_non_firefox_pid_is_not_running(self):
        for target in ("otherhost:+1", f"{os.uname().nodename}:+1", f"{os.uname().nodename}:+{os.getpid()}"):
            with self.subTest(lock=target):
                p = self.base / ("prof-" + str(abs(hash(target))))
                p.mkdir()
                os.symlink(target, p / "lock")
                self.assertFalse(self.mod.is_running(p))

    # 防改坏（现在就绿）：没有 lock 就是没在跑。
    def test_no_lock_is_not_running(self):
        p = self.base / "prof"
        p.mkdir()
        self.assertFalse(self.mod.is_running(p))


if __name__ == "__main__":
    unittest.main()
