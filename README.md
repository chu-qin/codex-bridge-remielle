# 蕾米 Codex 桥 · Remilia Codex Bridge

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Platform: Windows](https://img.shields.io/badge/platform-Windows%2010%2B-lightgrey)

把 Claude Code（Codex）的任务状态映射为桌面上的蕾米桌宠动画——Codex 思考时她会歪头，写代码时动笔作画，完成后得意地笑。

**零网络、零 API Key、纯本地运行。右键桌宠即可管理全部功能。**

---

## 功能

- **8 种显示状态**：启动 → 待机 ⇄ 等待输入 ⇄ 思考 ⇄ 连续绘制 ⇄ 间歇绘制 ⇄ 任务完成 → 隐藏
- **常驻桌面**：默认始终可见（idle 动画），可在右键菜单切换「完成后自动隐藏」
- **7 个原始 GIF**：期待、待机、拿笔待机、思考、连续绘制、间歇绘制、得意
- **右键菜单管理全部**：测试动作 / 演示 / 自检 / 大小 / 常驻开关 / 开机自启 / 退出（不再需要额外脚本）
- **可交互**：左键拖拽移动、鼠标滚轮缩放
- **轻量**：稳态轮询 ~0.8ms/次，约占单核 0.2%

---

## 使用

| 操作 | 说明 |
|------|------|
| 双击 `启动蕾米Codex助手.vbs` | 启动桌宠（无控制台窗口），播放启动动画后进入待机状态 |
| **右键桌宠** | 打开管理菜单（测试动作、调整大小、开关设置、开机自启、退出） |
| 左键拖拽 | 移动桌宠位置 |
| 鼠标滚轮 | 缩放（40%~250%） |

### 右键菜单

```
状态：等待 Codex 任务
─────────────────
测试待机动作
测试思考动作
测试准备动作
测试画画动作
测试间歇动作
测试完成动作
─────────────────
演示全部动作        → 依次展示全部 7 个状态
运行自检            → 检查 GIF 资源和 Codex 连接
─────────────────
大小 ▸ 50%/75%/100%/125%/150%
重置位置和大小
─────────────────
☑ 常驻显示          → 关闭后桌宠仅在 Codex 活动时出现
☐ 完成后自动隐藏    → 开启后任务完成查看后自动隐藏
─────────────────
安装开机自启        → 创建启动文件夹快捷方式 / 卸载
─────────────────
退出状态桥
```

---

## 状态说明

| 状态 | GIF | 触发条件 |
|------|-----|---------|
| startup | 期待.gif | 程序启动（播放一次） |
| idle | 待机.gif | 无任务、无未读消息 |
| ready | 拿笔待机.gif | 有未读消息 |
| thinking | 思考.gif | Codex 思考中（无近期输出） |
| running | 连续绘制.gif | Codex 正在输出 |
| running_intermittent | 间歇绘制.gif | 输出间隔 > 3 秒 |
| review | 得意.gif | 任务完成、等待查看 |
| hidden | — | 非持久模式且无事件 |

---

## 配置

编辑 `config.json`：

```json
{
  "actions": {
    "startup": "期待.gif",
    "idle": "待机.gif",
    "thinking": "思考.gif",
    "ready": "拿笔待机.gif",
    "running": "连续绘制.gif",
    "running_intermittent": "间歇绘制.gif",
    "complete": "得意.gif"
  },
  "display": {
    "persistent": true,
    "auto_hide_after_complete": false,
    "clickthrough_on_idle": false,
    "clickthrough_on_startup": true
  },
  "activity_thresholds": {
    "thinking_timeout_seconds": 10.0,
    "intermittent_timeout_seconds": 3.0
  },
  "poll_interval_ms": 350,
  "unread_settle_ms": 2500,
  "recent_session_days": 2,
  "default_scale": 0.75,
  "topmost": true
}
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `display.persistent` | 常驻显示（idle 时也显示） | `true` |
| `display.auto_hide_after_complete` | 完成后自动隐藏 | `false` |
| `display.clickthrough_on_idle` | 待机时鼠标穿透 | `false` |
| `activity_thresholds.thinking_timeout_seconds` | 多久无输出判定为 thinking | `10.0` |
| `activity_thresholds.intermittent_timeout_seconds` | 输出间隔多少秒切间歇动画 | `3.0` |
| `poll_interval_ms` | 状态轮询间隔 | `350` |
| `recent_session_days` | 扫描几天内的会话 | `2` |
| `default_scale` | 默认缩放 | `0.75` |

### 自定义 GIF

把 GIF 放到 `assets/gif/` 目录，在 `assets/坐标配置.json` 中添加坐标偏移，然后在 `config.json` 的 `actions` 中引用。

---

## 工作原理

```
Codex 会话 JSONL 文件
        │
        ▼
CodexSessionWatcher  ──  每 350ms 增量读取新行
        │
        ├── "task_started"   →  active_turns +1
        ├── "task_complete"  →  pending_reviews +1
        ├── tool_use / assistant_message → 更新 last_activity_time
        └── ...
        │
        ▼
CodexUnreadWatcher  ──  监听 .codex-global-state.json
        │
        └── thread_id 从未读变为已读 → 清除 pending_review
        │
        ▼
BridgeApp 状态机  ──  8 种状态，中央调度 _transition_to()
```

---

## 项目结构

```
remilia-codex-bridge/
├── remilia_codex_bridge.pyw    # 主程序（单文件，~1100 行）
├── config.json                 # 配置文件
├── settings.json               # 窗口位置/缩放（自动保存）
├── 启动蕾米Codex助手.vbs        # 启动脚本（自动查找 Python）
├── LICENSE                     # MIT
├── README.md
└── assets/
    ├── gif/
    │   ├── 待机.gif
    │   ├── 得意.gif
    │   ├── 思考.gif
    │   ├── 拿笔待机.gif
    │   ├── 期待.gif
    │   ├── 连续绘制.gif
    │   └── 间歇绘制.gif
    └── 坐标配置.json
```

---

## CLI 参数

```
python remilia_codex_bridge.pyw --self-test   # 检查资源和 Codex 状态
python remilia_codex_bridge.pyw --status      # 仅显示当前任务状态
python remilia_codex_bridge.pyw --demo        # 演示模式（依次展示全部状态）
python remilia_codex_bridge.pyw               # 正常模式
```

---

## 常见问题

### 双击 VBS 没反应？

1. 确保 Claude Code 已安装
2. 设置环境变量 `CODEX_PYTHON` 指向你的 `pythonw.exe` 路径
3. VBS 会自动搜索 Codex 自带的 Python 运行时

### 提示「另一个实例已在运行」？

同一时间只能运行一个状态桥实例。如果确认没有运行，可能是上次异常退出遗留的互斥锁——重启电脑即可清除。

### 桌宠不显示？

检查 `config.json` 中 `display.persistent` 是否为 `true`。如果为 `false`，桌宠仅在 Codex 活动时出现。

### 右键菜单闪？

已在 v2.0 中通过 Windows 原生 Popup Menu API（`TrackPopupMenu`）彻底解决。旧版本的 tkinter `tk_popup` 与透明窗口存在 z-order 冲突。

---

## 许可证

MIT License — 详见 [LICENSE](LICENSE) 文件。

原始桌宠 GIF 资源版权归原作者所有。本项目仅提供状态桥接和动画播放功能。
