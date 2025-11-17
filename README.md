# Recoil Controller

一个基于 Python 的 Windows 自动压枪与连点控制台。项目提供插件化武器配置、可视化控制面板、可自定义快捷键/触发键以及音效反馈，可直接运行源码或使用 PyInstaller 打包后的可执行文件。

![UI screenshot](./assets/1.jpg "")

> 注：若未提供截图，可将 `assets/1.jpg` 替换为实际截图，或删除上述行。

## 功能特点

- **双列可视化面板**：左侧集中武器、开关、连点和快捷键设置，右侧展示实时日志以及插件刷新 / 程序重启按钮。
- **插件化武器配置**：通过 `plugins/weapons.yaml`（或 JSON）定义任意数量的武器压枪参数，热加载即刻生效。
- **可自定义触发键**：压枪前自动按下的键默认为 `P`，可在 UI 中自定义并绑定快捷键（默认 `F10`），同时配有 `tone_on/off_bx.mp3` 提示音。
- **快捷键完全可换绑**：压枪总开关（默认 `F8`）、触发键控制（`F10`）、F→I 关联（`F9`）、连点开关（`=`）以及武器切换（`Alt+←/→`）均可在界面上重新绑定。
- **稳定连点**：连点线程直接读取物理鼠标状态，避免“松键未识别”导致的持续点击；支持设定基础延迟与随机浮动。
- **音效反馈与日志**：所有开关操作都会在右侧日志区记录，并可选播放提示音（无需额外依赖）。
- **托盘驻留**：点击窗口右上角关闭按钮时自动最小化到系统托盘（可在托盘菜单中恢复或退出）。

## 目录结构

```text
app.py                 # 主界面逻辑（Tkinter）与状态管理
core/
  engine.py            # 压枪循环 & 自动连点线程
  input_backend.py     # WinAPI/pynput 输入后端封装
  plugin_api.py        # 武器配置 dataclass
plugins/
  weapons.yaml         # 默认武器参数示例
assets/
  tone_*.mp3           # 开关提示音（包含新加的 tone_on/off_bx.mp3）
```

## 环境要求

- Windows 10/11（需要 WinAPI 输入接口）
- Python 3.9+（建议 3.10+）
- 依赖库见 
equirements.txt（含 keyboard, pynput，以及按需的 pyyaml/pyinstaller）

安装依赖（可根据自身需求决定是否安装 YAML/打包/托盘相关库）：

`powershell
pip install -r requirements.txt
`

## 快速开始（源码模式）

```powershell
git clone https://github.com/WandeF/recoil-controller.git
cd recoil-controller
python app.py
```

启动后：

1. 左侧“武器选择”会列出 `plugins/weapons.yaml` 中的条目。
2. 勾选“压枪总开关”即可启用压枪逻辑。
3. “自动按触发键”代表是否在压枪开始时自动按下你配置的键位（默认为 `P`）。
4. “连点参数”中设置基准延迟与随机浮动，点击“应用”后立即生效。
5. 右侧日志窗口显示所有状态变化；下方按钮用于刷新插件或重启程序。

## 武器配置

默认读取顺序：

1. `plugins/weapons.yaml`
2. 若 YAML 不存在或解析失败，回退到 `plugins/weapons.json`

YAML 示例（同仓库提供的示例一致）：

```yaml
weapons:
  - name: M14b
    defaultPull: 4.0
    initialDuration: 0.2
    steadyPull: 2.2
    sleepTime: 8
    acceleration: 200.0
  - name: VKT
    defaultPull: 2.0
    initialDuration: 0.5
    steadyPull: 1.8
    sleepTime: 8
    acceleration: 200.0
```

字段含义：

- `defaultPull`: 初段压枪拉力
- `initialDuration`: 初段持续时间（秒）
- `steadyPull`: 平稳期压枪拉力
- `sleepTime`: 每次循环的最小延迟（毫秒）
- `acceleration`: 计数与拉力的加速度因子

> 若需要更复杂的自定义算法，可在插件加载后返回自定义 `calc` 函数（可在 `core/engine.py` 中查看接口）。

## 默认快捷键

| 功能             | 默认键 | 说明                     |
|------------------|--------|--------------------------|
| 压枪总开关       | F8     | 启动/停止压枪主循环      |
| 自动按触发键     | F10    | 控制是否自动按触发键     |
| F→I 关联         | F9     | 按下 F 时自动发送 I      |
| 连点开关         | =      | 开启/关闭连点            |
| 上一/下一把武器 | Alt+← / Alt+→ | 切换选中武器 |

所有快捷键都可以在 UI 的“快捷键”卡片中重新绑定，并实时生效。

## 自定义触发键

“自动按触发键”开启时，压枪开始后会自动按下配置的键位（默认 `P`）。

1. 在“触发键设置”中输入任意单字符（例如 `o`、`;`、`[` 等）。
2. 点击“应用”后立即生效，同时触发键提示音会反馈状态（tone_on/off_bx.mp3）。
3. 若清空输入并点击“应用”，会自动还原为 `P`。

## 打包为可执行文件

项目提供了 `app.spec`，可直接使用 PyInstaller 打包：

```powershell
pyinstaller app.spec
# 或自定义：pyinstaller -F -w app.py
```

打包完成的可执行文件位于 `dist/` 目录，可与 `plugins/`、`assets/` 一同分发。程序运行时会优先使用 exe 同目录下的 `plugins` 覆盖内置配置，方便更新武器脚本。

## 常见问题

- **“ImportError: cannot import name 'WinAPIBackend'”**：确认 `core/input_backend.py` 存在，且在 Windows 上运行。
- **运行需要管理员权限？**：部分情况下 `keyboard` 库监听底层键盘需要管理员权限，请按需以管理员方式启动。
- **连点未停止**：新版连点逻辑会检测物理鼠标状态，如仍出现异常，可在 UI 中关闭“连点开关”或调整延迟。

## 许可证

仓库未显式声明许可证时，默认保留版权所有权。若需要开源协议，请在仓库根目录补充 `LICENSE` 文件。

---

欢迎提交 Issue / PR，一起完善 UI、插件生态以及更多游戏适配能力。
