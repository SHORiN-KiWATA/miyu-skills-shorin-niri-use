<p align="center">
  <a href="README.md">English</a> | 简体中文
</p>

# 操作 niri 桌面（shorin-niri-use）

> <sub>本文档由 Anthropic 的 Claude Fable 5.2 生成。</sub>

这是 [Miyu](https://github.com/miyu-agent/miyu) 的一个社区技能包，包含一份技能 `niri-agent`。这份技能让 AI 在后台操作 niri 桌面上的图形界面应用（截取窗口、点击、输入、按键、打开应用），不会移动你的光标，也不会改变焦点；能用命令行或 D-Bus 完成的操作，优先使用命令行或 D-Bus。

运行以下命令安装：

```sh
miyu pkg install shorin-niri-use
```

安装以后，AI 平时只看到技能的名字和一句说明，需要操作桌面时才会自己读取技能正文；你也可以在输入框里写 `$niri-agent`，点名让 AI 使用这份技能。

## 依赖

使用这份技能需要满足以下条件：

- **[SHORiN-KiWATA/niri](https://github.com/SHORiN-KiWATA/niri)**：`niri msg agent`（代理座位 agent seat，也就是第二套指针和键盘）是这个分支添加的功能。官方的 niri 没有这个功能，所以安装官方的 niri 也无法使用这份技能。
- `agent-firefox` 同步网站数据（`storage/`）时需要 `rsync`。
- `agent-ui`（读取窗口的无障碍树）需要 PyGObject 和 AT-SPI：运行 `python3 -c "import gi; gi.require_version('Atspi', '2.0')"`，如果不报错，就说明已经满足条件。
- Miyu 核心需要从桌面会话里启动，这样核心才能拿到 niri 的套接字和会话的 D-Bus。

## 包里有什么

这个技能包里有以下三个文件：

| 文件 | 内容 |
|---|---|
| `skills/niri-agent/SKILL.md` | 技能正文：先寻找命令行接口，然后选择窗口、定位 → 执行动作 → 查看回执，最后是注意事项和遇到报错时的处理方法 |
| `skills/niri-agent/agent-ui` | 列出窗口里的控件以及应该点击的位置（AT-SPI），比截图更快、更准确 |
| `skills/niri-agent/agent-firefox` | 用你当前的登录状态打开一个 Agent 专用的 Firefox |

技能正文和两个脚本按照 niri 分支里的 `resources/agent/skill/` 改写而来：工具名换成了 Miyu 的工具名（`shell`、`read`、`web_fetch`），脚本按照技能所在的文件夹查找。niri 分支那边修改以后，这里也会跟着修改。

## 许可证

这个技能包使用 GPL-3.0-or-later 许可证，和 niri 相同。
