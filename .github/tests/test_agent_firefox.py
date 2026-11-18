# agent-firefox 的测试（审计 2026-11-18）。
#
# 跑法（在仓库根）：
#   python3 -I -B -m unittest discover -s .github/tests
# （不加 -t .：.github 不是合法的包名，加了 unittest 会报 Start directory is not importable。）
#
# 只用标准库，不联网、不起 Firefox、不调 niri。HOME 指到临时目录，假 profile 全在里面造，
# 加载后先断言脚本的 HOME 在临时目录下——绝不碰真实的 ~/.mozilla。
# 放在 .github/tests/ 而不是 tests/：打包会跳过 .github，测试不会进包。

import errno
import hashlib
import importlib.machinery
import importlib.util
import io
import os
import pathlib
import shutil
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


def tree(root):
    """整树快照：每一项的类型；文件的大小、mtime、内容哈希；链接指向哪儿。"""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames + filenames):
            p = pathlib.Path(dirpath) / name
            st = os.lstat(p)
            rel = str(p.relative_to(root))
            if p.is_symlink():
                out[rel] = ("link", os.readlink(p))
            elif p.is_dir():
                out[rel] = ("dir",)
            else:
                out[rel] = ("file", st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


class StaleLoginFiles(Sandbox):
    # 审计收尾（#183 顺带发现，合并人定了扩大）：文件头说登录状态「每次整份替换」，但源 profile
    # 没有 key4.db、permissions.sqlite、logins.json、logins-backup.json、containers.json
    # （SQLITE_FILES、PLAIN_FILES 两张表里的）时，agent 侧上次同步来的那份留着：用户清掉的已存
    # 密码，agent 那边还解得开。源没有 storage/ 时 agent 侧旧的 storage/（网站数据）也留着
    # （rsync 带 --delete，但源没有 storage/ 时根本不调 rsync）。
    # 源没有的，agent 侧的那份（sqlite 连同 -wal、-shm、-journal）应删掉；只删表里的名字、只删
    # agent 侧目录里的；是符号链接的只删链接本身；源那边一个字节不碰；agent 自己的文件不碰。
    SUFFIXES = ("", "-wal", "-shm", "-journal")

    def synced_names(self):
        sqlite_files, plain = self.mod.SQLITE_FILES, self.mod.PLAIN_FILES
        for name in ("cookies.sqlite", "key4.db", "permissions.sqlite"):
            self.assertIn(name, sqlite_files)
        for name in ("logins.json", "logins-backup.json", "containers.json"):
            self.assertIn(name, plain)
        return [n + x for n in sqlite_files for x in self.SUFFIXES] + list(plain)

    def stale_agent(self, name="agent"):
        dst = self.base / name
        (dst / "storage" / "default" / "https+++old.example" / "ls").mkdir(parents=True)
        (dst / "storage" / "default" / "https+++old.example" / "ls" / "data.sqlite").write_bytes(b"old site")
        for name in self.synced_names():
            (dst / name).write_bytes(b"stale from last sync")
        return dst

    def test_files_missing_in_source_removed_from_agent(self):
        src = self.profile("src")
        dst = self.stale_agent()
        self.mod.sync(src, dst)
        for name in self.synced_names() + ["storage"]:
            with self.subTest(name=name):
                self.assertFalse(os.path.lexists(dst / name), f"源没有 {name}，agent 侧上次同步来的还留着")
        self.assertTrue((dst / "user.js").is_file(), "同步没走完")

    # 防改坏（现在就绿）：agent 侧不是同步来的文件都留着。
    def test_agent_own_files_kept(self):
        # Firefox 自己建的、名字和同步的那几个像的，都不是同步来的，一个不许删。
        own = ["prefs.js", "places.sqlite", "places.sqlite-wal", "formhistory.sqlite", "key4.db.bak",
               "logins.json~", "cookies.sqlite.corrupt", "storage.sqlite", "extensions/a.xpi",
               "storage-sync-v2.sqlite"]
        for has_source in (False, True):
            with self.subTest(source_has_files=has_source):
                src = self.profile(f"src-{has_source}")
                if has_source:
                    cookie_db(src / "cookies.sqlite", ["fresh.example"])
                    (src / "logins.json").write_text("{}")
                    (src / "prefs.js").write_text("source prefs")
                dst = self.base / f"agent-{has_source}"
                for name in own:
                    (dst / name).parent.mkdir(parents=True, exist_ok=True)
                    (dst / name).write_text("agent's own " + name)
                self.mod.sync(src, dst)
                for name in own:
                    self.assertEqual((dst / name).read_text(), "agent's own " + name, name)

    def test_symlink_in_agent_only_link_removed(self):
        outside = self.base / "outside"
        (outside / "site").mkdir(parents=True)
        (outside / "key4.db").write_bytes(b"target file")
        (outside / "site" / "data").write_bytes(b"target dir content")
        dst = self.base / "agent"
        dst.mkdir()
        (dst / "key4.db").symlink_to(outside / "key4.db")
        (dst / "logins.json").symlink_to(outside / "key4.db")
        (dst / "storage").symlink_to(outside / "site", target_is_directory=True)
        self.mod.sync(self.profile("src"), dst)
        for name in ("key4.db", "logins.json", "storage"):
            with self.subTest(name=name):
                self.assertFalse(os.path.lexists(dst / name), f"agent 侧的链接 {name} 还在")
        self.assertEqual((outside / "key4.db").read_bytes(), b"target file", "链接指向的文件被删/改了")
        self.assertEqual((outside / "site" / "data").read_bytes(), b"target dir content",
                         "链接指向的目录被删/改了")

    # 防改坏（现在就绿）：同步前后源 profile 整树一样（源里有那些文件、没有两种）。
    def test_source_profile_untouched(self):
        for has_files in (False, True):
            with self.subTest(source_has_files=has_files):
                src = self.profile(f"src-{has_files}", lz4_file(COOKIES))
                if has_files:
                    cookie_db(src / "cookies.sqlite", ["fresh.example"])
                    for name in ("key4.db", "permissions.sqlite"):
                        db = sqlite3.connect(src / name)
                        db.execute("CREATE TABLE t (x)")
                        db.commit()
                        db.close()
                    for name in self.mod.PLAIN_FILES:
                        (src / name).write_text("{}")
                    (src / "storage" / "default" / "site").mkdir(parents=True)
                    (src / "storage" / "default" / "site" / "ls.sqlite").write_bytes(b"x")
                    (src / "prefs.js").write_text("source prefs")
                dst = self.stale_agent(f"agent-{has_files}")
                before = tree(src)
                self.mod.sync(src, dst)
                self.assertEqual(tree(src), before, "源 profile 被改了")
                if has_files:
                    self.assertEqual((dst / "storage" / "default" / "site" / "ls.sqlite").read_bytes(), b"x")
                    self.assertTrue((dst / "key4.db").is_file())


def snap(path):
    """一个文件或目录的快照：文件看大小、mtime_ns、sha256；目录看整树（tree）。"""
    if path.is_dir():
        return ("dir", os.lstat(path).st_mtime_ns, tree(path))
    st = os.lstat(path)
    return ("file", st.st_size, st.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())


def plain_db(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE t (x)")
    db.execute("INSERT INTO t VALUES ('source')")
    db.commit()
    db.close()


class AgentSideLinks(Sandbox):
    # 审计收尾：往 agent 侧写的地方，目标名字是符号链接时会顺着链接写到 agent 目录外：
    # PLAIN_FILES 的 shutil.copy2、user.js 的 write_text 写进链接指的文件；storage 是链接时
    # rsync --delete 写进链接指的目录、把那里原有的东西删掉；copy_sqlite 的 shutil.move
    # 在同一个文件系统上 rename 会换掉链接本身，但跨文件系统（/tmp 是 tmpfs、家目录在别的盘，
    # 真机上就是这样）时退回 copy2，写进链接指的文件；链接指的是目录时 move 把库挪进那个目录。
    # 应先删掉链接本身再写：链接指的东西一个字节不变，agent 侧那个名字成了普通文件/目录、内容是源的。

    def outside(self):
        out = self.base / "outside"
        (out / "dir").mkdir(parents=True)
        (out / "dir" / "keep").write_bytes(b"target dir content")
        (out / "pdir").mkdir()
        (out / "pdir" / "keep").write_bytes(b"target dir content")
        for name in ("logins.json", "user.js", "key4.db", "cookies.sqlite",
                     "key4.db-wal", "key4.db-shm", "key4.db-journal"):
            (out / name).write_bytes(b"target " + name.encode())
        return out

    def source(self):
        src = self.profile("src", lz4_file(COOKIES))
        cookie_db(src / "cookies.sqlite", ["fresh.example"])
        for name in ("key4.db", "permissions.sqlite"):
            plain_db(src / name)
        (src / "logins.json").write_text("source logins")
        (src / "storage" / "default" / "site").mkdir(parents=True)
        (src / "storage" / "default" / "site" / "ls.sqlite").write_bytes(b"source site")
        return src

    def linked_agent(self, out):
        dst = self.base / "agent"
        dst.mkdir()
        links = {"logins.json": out / "logins.json", "user.js": out / "user.js",
                 "key4.db": out / "key4.db", "cookies.sqlite": out / "cookies.sqlite",
                 "key4.db-wal": out / "key4.db-wal", "key4.db-shm": out / "key4.db-shm",
                 "key4.db-journal": out / "key4.db-journal",
                 "permissions.sqlite": out / "pdir", "storage": out / "dir"}
        for name, target in links.items():
            (dst / name).symlink_to(target, target_is_directory=target.is_dir())
        return dst

    def targets(self, out):
        return {p.name: snap(p) for p in sorted(out.iterdir())}

    def check_agent(self, dst):
        for name in ("logins.json", "user.js", "key4.db", "cookies.sqlite", "permissions.sqlite"):
            with self.subTest(agent=name):
                self.assertFalse((dst / name).is_symlink(), f"agent 侧的 {name} 还是链接")
                self.assertTrue((dst / name).is_file(), f"agent 侧的 {name} 不是普通文件")
        for name in ("key4.db-wal", "key4.db-shm", "key4.db-journal"):
            with self.subTest(agent=name):
                self.assertFalse(os.path.lexists(dst / name), f"agent 侧的 {name} 还在")
        self.assertEqual((dst / "logins.json").read_text(), "source logins")
        self.assertEqual((dst / "user.js").read_text(), self.mod.USER_JS)
        for name in ("key4.db", "permissions.sqlite"):
            db = sqlite3.connect(dst / name)
            self.assertEqual(db.execute("SELECT x FROM t").fetchall(), [("source",)], name)
            db.close()
        db = sqlite3.connect(dst / "cookies.sqlite")
        hosts = sorted(r[0] for r in db.execute("SELECT host FROM moz_cookies"))
        db.close()
        self.assertEqual(hosts, ["a.example", "fresh.example"], "会话 cookie 该加进 agent 自己的库")

    def run_sync(self, cross_device):
        out = self.outside()
        src = self.source()
        dst = self.linked_agent(out)
        before = self.targets(out)
        source_before = tree(src)
        real_rename = os.rename

        def rename(a, b, *args, **kw):
            # 跨文件系统：从 agent-firefox 的临时目录挪出来时 rename 失败，shutil.move 退回拷贝。
            if cross_device and "agent-firefox-" in str(a):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_rename(a, b, *args, **kw)

        with mock.patch("os.rename", rename):
            self.mod.sync(src, dst)
        self.assertEqual(self.targets(out), before, "链接指向的东西被改了")
        self.assertEqual(tree(src), source_before, "源 profile 被改了")
        self.check_agent(dst)
        self.assertEqual((dst / "storage" / "default" / "site" / "ls.sqlite").read_bytes(), b"source site")
        self.assertFalse((dst / "storage").is_symlink())

    @unittest.skipUnless(shutil.which("rsync"), "本机没有 rsync")
    def test_links_not_followed_same_filesystem(self):
        self.run_sync(cross_device=False)

    @unittest.skipUnless(shutil.which("rsync"), "本机没有 rsync")
    def test_links_not_followed_cross_filesystem(self):
        self.run_sync(cross_device=True)

    # rsync 换成桩：调 rsync 的那一刻 agent 侧的 storage 已经不是链接。
    def test_storage_link_gone_before_rsync(self):
        out = self.outside()
        src = self.source()
        dst = self.linked_agent(out)
        before = snap(out / "dir")
        seen = []

        def run(cmd, **kw):
            if cmd[0] == "rsync":
                seen.append(os.path.islink(dst / "storage"))
            return mock.Mock(returncode=0)

        with mock.patch.object(self.mod.subprocess, "run", run):
            self.mod.sync(src, dst)
        self.assertEqual(seen, [False], "调 rsync 时 agent 侧的 storage 还是链接（或没调 rsync）")
        self.assertEqual(snap(out / "dir"), before)

    # 防改坏（现在就绿）：storage/ 里更深一层的链接 rsync 自己不跟（没带 --keep-dirlinks）：
    # 源也有的那项，链接被换成目录；源没有的，--delete 只删链接本身。
    @unittest.skipUnless(shutil.which("rsync"), "本机没有 rsync")
    def test_deeper_storage_links_not_followed(self):
        out = self.outside()
        src = self.source()
        dst = self.base / "agent"
        (dst / "storage" / "default").mkdir(parents=True)
        (dst / "storage" / "default" / "site").symlink_to(out / "dir", target_is_directory=True)
        (dst / "storage" / "default" / "gone").symlink_to(out / "pdir", target_is_directory=True)
        before = self.targets(out)
        self.mod.sync(src, dst)
        self.assertEqual(self.targets(out), before, "链接指向的目录被改了")
        site = dst / "storage" / "default" / "site"
        self.assertFalse(site.is_symlink())
        self.assertEqual((site / "ls.sqlite").read_bytes(), b"source site")
        self.assertFalse(os.path.lexists(dst / "storage" / "default" / "gone"))


class DanglingProfileLink(Sandbox):
    # 审计收尾：agent 的 profile 目录本身是符号链接是允许的（有人把它放到别的盘）。但链接指向的
    # 目录不存在时，mkdir(exist_ok=True) 抛 FileExistsError、打 traceback。应说清是哪个链接、
    # 指向哪儿、该怎么办，退出码 1，不起 Firefox，也不替人去建链接指向的目录。
    def test_dangling_profile_link_friendly_error(self):
        src = self.profile("src")
        target = self.base / "elsewhere" / "profile"
        dst = self.base / "agent"
        dst.symlink_to(target)
        argv = ["agent-firefox", "--source", str(src), "--profile", str(dst)]
        err = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(self.mod.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
                mock.patch("sys.stderr", err):
            try:
                self.mod.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
            except OSError as exc:
                self.fail(f"悬空链接时抛了 {type(exc).__name__}: {exc}")
        self.assertEqual(code, 1, err.getvalue())
        self.assertIn(str(dst), err.getvalue())
        self.assertIn(str(target), err.getvalue())
        run.assert_not_called()
        self.assertTrue(dst.is_symlink(), "链接本身不动")
        self.assertFalse(target.exists(), "不替人建链接指向的目录")

    # 防改坏（现在就绿）：链接指向一个在的目录时照常同步，写进链接指的目录。
    def test_profile_link_to_existing_folder_still_works(self):
        src = self.profile("src")
        target = self.base / "elsewhere"
        target.mkdir()
        dst = self.base / "agent"
        dst.symlink_to(target)
        argv = ["agent-firefox", "--source", str(src), "--profile", str(dst)]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(self.mod.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
                mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as done:
                self.mod.main()
        self.assertEqual(done.exception.code, 0)
        self.assertTrue((target / "user.js").is_file())
        self.assertTrue(dst.is_symlink())
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
