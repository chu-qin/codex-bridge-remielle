# 蕾米 AI 助手 (Remielle Codex Bridge)

> 桌面 AI 任务状态监控宠，实时反映 Codex / Claude Code 的工作状态

为了更方便追踪项目进度 ~~摸鱼(bushi)~~ 的任务监控桌宠

<img src=".\assets\gif\连续绘制.gif" style="zoom:50%;" />

然而codex额度蹬完暂时没法验证了

等重置.jpg

<img src=".\assets\gif\期待.gif" style="zoom:50%;" />

## 功能

- **实时状态监控**：根据 AI 工具的活动自动切换动画
- **系统托盘驻留**：关闭窗口后托盘保留，左键唤回，右键菜单
- **拖拽 + 缩放**：鼠标拖拽移动位置，滚轮调整大小
- **开机自启**：菜单一键安装/卸载

## 状态说明

| 动画 | 状态 | 触发条件 |
|------|------|---------|
| 待机 | idle | 无任务，常驻显示 |
| 思考 | thinking | 任务活跃但长时间无输出 |
| 连续绘制 | running | 任务活跃，持续产出 |
| 间歇绘制 | running_intermittent | 任务活跃，间歇产出 |
| 拿笔待机 | ready | 有未读消息等待查看 |
| 得意 | review | 任务完成待查看 |
| 期待 | 拖拽中 | 鼠标拖拽窗口时 |

<img src=".\assets\gif\思考.gif" style="zoom:50%;" />

## 用法

### 启动

双击 `启动蕾米Codex助手.vbs`

### 托盘图标

| 操作 | 行为 |
|------|------|
| 左键图标 | 显示 / 隐藏桌宠 |
| 右键图标 | 弹出菜单（状态信息、显示/隐藏、自检、大小、退出等） |

### 窗口操作

| 操作 | 行为 |
|------|------|
| 右键桌宠 | 弹出菜单 |
| 左键拖拽 | 移动位置 |
| 滚轮 | 缩放大小 |
| 关闭按钮 | 隐藏到托盘（不退出） |

## 开发

### 运行自检

```bash
python remielle_codex_bridge.pyw --self-test
```

### 运行单元测试

```bash
python -m unittest remielle.tests.test_state_machine -v
```

### 演示模式

```bash
python remielle_codex_bridge.pyw --demo
```

## 依赖

- Python 3.12+
- Pillow（`pip install Pillow`）

## 配置

首次运行自动生成 `config.json`，可自定义：

- `activity_thresholds`：状态切换的时间阈值
- `poll_interval_ms`：轮询间隔
- `recent_session_days`：扫描最近 N 天的 session
- `default_scale`：默认缩放
