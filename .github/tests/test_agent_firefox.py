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


def cookie_db(path, hosts):
    """造一份带 moz_cookies 表的 cookies.sqlite（列照 add_session_cookies 要写的那些）。"""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, originAttributes TEXT, name TEXT,"
               " value TEXT, host TEXT, path TEXT, expiry INTEGER, lastAccessed INTEGER,"
               " creationTime INTEGER, isSecure INTEGER, isHttpOnly INTEGER, sameSite INTEGER,"
               " schemeMap INTEGER, isPartitionedAttributeSet INTEGER, updateTime INTEGER,"
               " UNIQUE (name, host, path, originAttributes))")
    db.executemany("INSERT INTO moz_cookies (originAttributes, name, value, host, path) VALUES ('', 'old', '1', ?, '/')",
                   [(h,) for h in hosts])
    db.commit()
    db.close()


class StaleAgentCookies(Sandbox):
    # 审计收尾（#183 顺带发现）：文件头说登录状态「每次整份替换」，但源 profile 没有
    # cookies.sqlite 时 copy_sqlite 直接 return，agent 那边上次同步来的 cookies.sqlite
    # （连同 -wal、-shm）原样留着：用户已经清掉的登录态，agent 的 Firefox 还登着；
    # 会话 cookie 还被加进这份旧库。应删掉 agent 侧的这几个文件，会话 cookie 没库可加就跳过。
    def test_source_without_cookies_db_clears_agent_copy(self):
        src = self.profile("src", lz4_file(COOKIES))
        dst = self.base / "agent"
        dst.mkdir()
        cookie_db(dst / "cookies.sqlite", ["stale.example"])
        (dst / "cookies.sqlite-wal").write_bytes(b"old wal")
        (dst / "cookies.sqlite-shm").write_bytes(b"old shm")
        n = self.mod.sync(src, dst)
        for name in ("cookies.sqlite", "cookies.sqlite-wal", "cookies.sqlite-shm"):
            with self.subTest(name=name):
                self.assertFalse((dst / name).exists(), f"源没有 cookies.sqlite，agent 侧的 {name} 还留着")
        self.assertEqual(n, 0, "没有 cookie 库时会话 cookie 应跳过")
        self.assertTrue((dst / "user.js").is_file(), "同步没走完")

    # 防改坏（现在就绿）：源有 cookies.sqlite 时照旧整份替换，再加会话 cookie。
    def test_source_with_cookies_db_replaces_agent_copy(self):
        src = self.profile("src", lz4_file(COOKIES))
        cookie_db(src / "cookies.sqlite", ["fresh.example"])
        dst = self.base / "agent"
        dst.mkdir()
        cookie_db(dst / "cookies.sqlite", ["stale.example"])
        self.assertEqual(self.mod.sync(src, dst), 1)
        db = sqlite3.connect(dst / "cookies.sqlite")
        hosts = sorted(r[0] for r in db.execute("SELECT host FROM moz_cookies"))
        db.close()
        self.assertEqual(hosts, ["a.example", "fresh.example"])

    # 防改坏（现在就绿）：agent 的 Firefox 在跑时整个不同步，它正在用的 cookies.sqlite 不动。
    def test_running_agent_firefox_skips_sync(self):
        src = self.profile("src")
        dst = self.base / "agent"
        dst.mkdir()
        cookie_db(dst / "cookies.sqlite", ["stale.example"])
        before = (dst / "cookies.sqlite").read_bytes()
        argv = ["agent-firefox", "--source", str(src), "--profile", str(dst)]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(self.mod, "is_running", return_value=True), \
                mock.patch.object(self.mod.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
                mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                self.mod.main()
        self.assertEqual((dst / "cookies.sqlite").read_bytes(), before)
        self.assertFalse((dst / "user.js").exists(), "在跑时不该同步")
        self.assertIn("--new-window", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
