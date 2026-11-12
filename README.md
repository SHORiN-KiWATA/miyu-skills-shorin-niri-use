# 操作 niri 桌面（shorin-niri-use）

[Miyu](https://github.com/miyu-agent/miyu) 的社区技能包，带一份技能 `niri-agent`：让 AI 在后台操作 niri 桌面上的图形界面应用（截窗口、点、
输入、按键、开应用），不动你的光标和焦点，能用命令行、D-Bus 办的先用它们。

```sh
miyu pkg install shorin-niri-use
```

装上以后 AI 平时只看到技能的名字和一句说明，要操作桌面时自己读正文；你也可以在输入框里写 `$niri-agent` 点名用它。

## 要什么

- **[SHORiN-KiWATA/niri](https://github.com/SHORiN-KiWATA/niri)**：`niri msg agent`（agent seat，第二套指针和键盘）是这个分支加的，
  官方 niri 没有，装了也用不上。
- `agent-ui`（读窗口的无障碍树）要 PyGObject 和 AT-SPI：`python3 -c "import gi; gi.require_version('Atspi', '2.0')"` 不报错就行。
- Miyu 核心要从桌面会话里起（拿得到 niri 的套接字和会话的 D-Bus）。

## 包里有什么

| 文件 | 是什么 |
|---|---|
| `skills/niri-agent/SKILL.md` | 技能正文：先找命令行接口、选窗口、定位 → 动作 → 看回执、注意事项和报错怎么办 |
| `skills/niri-agent/agent-ui` | 列出窗口里的控件和点哪里（AT-SPI），比截图快、准 |
| `skills/niri-agent/agent-firefox` | 用你当前的登录状态开一个 agent 自己的 Firefox |

技能正文、两个脚本照 niri 分支里的 `resources/agent/skill/` 改写：工具名换成 Miyu 的（`shell`、`read`、`web_fetch`），脚本照技能
所在的文件夹找。niri 那边改了，这里跟着改。

## 许可证

GPL-3.0-or-later，同 niri。
