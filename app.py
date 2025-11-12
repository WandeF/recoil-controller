# app.py
import os
import sys
import subprocess
import json
import time
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Any
import queue
import ctypes
from ctypes import wintypes

try:
    import yaml  # pip install pyyaml
except Exception:
    yaml = None

import tkinter as tk
from tkinter import ttk, messagebox

import keyboard
from pynput import mouse

from core.engine import RecoilEngine, AutoClickThread
from core.plugin_api import WeaponConfig


DEFAULT_KEY_BINDINGS = {
    "fire": "f8",
    "flash": "f9",
    "auto_click": "=",
    "press_key": "f10",
}


# -------- 路径基准：允许 EXE 同目录外部 plugins 覆盖 --------
def _runtime_base() -> Path:
    if getattr(sys, 'frozen', False):
        exe_dir = Path(sys.executable).parent
        ext_plugins = exe_dir / "plugins"
        if ext_plugins.exists():
            return exe_dir
        if hasattr(sys, "_MEIPASS"):
            return Path(sys._MEIPASS)
    return Path(__file__).parent


BASE_DIR = _runtime_base()
PLUG_DIR = BASE_DIR / "plugins"
os.makedirs(PLUG_DIR, exist_ok=True)
SETTINGS_PATH = BASE_DIR / "user_settings.json"


class PluginManager:
    def __init__(self, folder: Path):
        self.folder = folder
        self.weapons: List[WeaponConfig] = []

    def _load_yaml(self, path: Path):
        if yaml is None:
            raise RuntimeError("未安装 pyyaml，请执行: pip install pyyaml")
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _load_json(self, path: Path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def load(self):
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).parent
            external = exe_dir / "plugins"
            if external.exists():
                self.folder = external

        print(f"[Plugin] 当前插件目录: {self.folder}")
        self.weapons.clear()
        cfg = None

        yml = self.folder / "weapons.yaml"
        jsn = self.folder / "weapons.json"

        if yml.exists():
            try:
                cfg = self._load_yaml(yml)
                print(f"[Plugin] Loaded YAML: {yml}")
            except Exception as e:
                print(f"[Plugin] YAML 加载失败 ({e})，尝试 JSON...")
                if jsn.exists():
                    cfg = self._load_json(jsn)
                    print(f"[Plugin] Loaded JSON: {jsn}")
        elif jsn.exists():
            cfg = self._load_json(jsn)
            print(f"[Plugin] Loaded JSON: {jsn}")
        else:
            print("[Plugin] ⚠ 未找到 plugins/weapons.yaml 或 weapons.json")
            return

        items = (cfg.get("weapons") or []) if isinstance(cfg, dict) else []
        if not items:
            print(f"[Plugin] ⚠ 文件中未找到有效的 'weapons' 列表: {self.folder}")
            return

        for it in items:
            try:
                w = WeaponConfig(
                    name=str(it["name"]),
                    defaultPull=float(it.get("defaultPull", 2.0)),
                    initialDuration=float(it.get("initialDuration", 0.5)),
                    steadyPull=float(it.get("steadyPull", 1.6)),
                    sleepTime=int(it.get("sleepTime", 8)),
                    acceleration=float(it.get("acceleration", 200.0)),
                )
                self.weapons.append(w)
                print(f"[Plugin] ✅ {w.name}")
            except Exception as e:
                print(f"[Plugin] ⚠ 解析失败: {it} -> {e}")

        if not self.weapons:
            print("[Plugin] ⚠ 未成功加载任何武器配置！")

    def get_calc(self, name: str):
        return None


class State:
    def __init__(self):
        self.fire_enabled = True
        self.flash_mode = False
        self.auto_click_enabled = False
        self.press_key_enabled = True
        self.press_key_char = "p"
        self.current_weapon: Optional[WeaponConfig] = None

        self.right_hold = False
        self.left_hold = False
        self._synth_guard = 0

        self.key_bindings = DEFAULT_KEY_BINDINGS.copy()

    def begin_synth(self):
        self._synth_guard += 1

    def end_synth(self):
        if self._synth_guard > 0:
            self._synth_guard -= 1

    def is_synth(self) -> bool:
        return self._synth_guard > 0

    def as_dict(self):
        return {
            "fire_enabled": self.fire_enabled,
            "flash_mode": self.flash_mode,
            "auto_click_enabled": self.auto_click_enabled,
            "press_key_enabled": self.press_key_enabled,
            "right_hold": self.right_hold,
            "left_hold": self.left_hold,
        }


# ===== Windows MCI-based MP3 player (no extra deps) =====
ASSET_DIR = BASE_DIR / "assets"

winmm = ctypes.windll.winmm
mciSendStringW = winmm.mciSendStringW
mciSendStringW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.UINT, wintypes.HANDLE]
mciSendStringW.restype = wintypes.UINT

_AUDIO_ALIASES: Dict[str, str] = {}


def _mci(cmd: str) -> int:
    buf = ctypes.create_unicode_buffer(256)
    rc = mciSendStringW(cmd, buf, 255, 0)
    return rc


def _ensure_open(alias: str, filepath: str) -> bool:
    rc = _mci(f'status {alias} mode')
    if rc == 0:
        return True
    rc = _mci(f'open "{filepath}" type mpegvideo alias {alias}')
    return rc == 0


def _stop(alias: str):
    _mci(f'stop {alias}')


def _seek_start(alias: str):
    _mci(f'seek {alias} to start')


def _play(alias: str):
    _mci(f'play {alias}')


def _close(alias: str):
    _mci(f'close {alias}')


def stop_all_audio():
    for alias in list(_AUDIO_ALIASES.values()):
        try:
            _stop(alias)
            _close(alias)
        except Exception:
            pass
    _AUDIO_ALIASES.clear()


def play_tone(name: str):
    path = ASSET_DIR / f"{name}.mp3"
    if not path.exists():
        print(f"[Tone] ⚠ 未找到音效文件 {path}")
        return

    alias = _AUDIO_ALIASES.get(name) or f"alias_{name}"
    _AUDIO_ALIASES[name] = alias

    try:
        if not _ensure_open(alias, str(path)):
            print(f"[Tone] ⚠ 打开失败: {path}")
            return
        _stop(alias)
        _seek_start(alias)
        _play(alias)
    except Exception as e:
        print(f"[Tone] 播放失败: {e}")


def tone_on_fire():
    play_tone("tone_on_fire")


def tone_off_fire():
    play_tone("tone_off_fire")


def tone_on_flash():
    play_tone("tone_on_flash")


def tone_off_flash():
    play_tone("tone_off_flash")


def tone_on_ac():
    play_tone("tone_on_ac")


def tone_off_ac():
    play_tone("tone_off_ac")


def tone_on_bx():
    play_tone("tone_on_bx")


def tone_off_bx():
    play_tone("tone_off_bx")


class HotkeyManager:
    def __init__(self, root: tk.Tk, log: Callable[[str], None]):
        self._root = root
        self._log = log
        self._bindings: Dict[str, Dict[str, Any]] = {}
        self._paused = False
        self._pressed: set[str] = set()
        self._hook = keyboard.hook(self._handle_event, suppress=False)

    def register(self, action: str, key: str, callback: Callable[[], None]):
        self._bindings[action] = {"key": None, "callback": callback}
        self.update_key(action, key)

    def update_key(self, action: str, key: str):
        info = self._bindings.get(action)
        if not info:
            return
        norm = (key or "").strip().lower()
        info["key"] = norm
        if norm:
            self._log(f"[Hotkey] {action} -> {norm}")
        else:
            self._log(f"[Hotkey] {action} 已清除")

    def pause_all(self):
        if self._paused:
            return
        self._paused = True
        self._pressed.clear()

    def resume_all(self):
        if not self._paused:
            return
        self._paused = False
        self._pressed.clear()

    def shutdown(self):
        if self._hook is not None:
            try:
                keyboard.unhook(self._hook)
            except Exception:
                pass
            self._hook = None
        self._pressed.clear()
        self._paused = True

    def _handle_event(self, event):
        if self._paused:
            return
        name = (event.name or "").lower()
        if not name:
            return
        if event.event_type == "down":
            if name in self._pressed:
                return
            self._pressed.add(name)
            for info in self._bindings.values():
                if info.get("key") == name and callable(info.get("callback")):
                    self._root.after(0, info["callback"])
        elif event.event_type == "up":
            self._pressed.discard(name)


class ControlPanel:
    def __init__(self, app: "RecoilControllerApp"):
        self.app = app
        self.root = tk.Tk()
        self.root.title("Recoil Controller")
        self.root.geometry("1180x936")
        self.root.configure(bg="#0f111d")
        self.style = ttk.Style()
        self._setup_theme()

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.binding_labels: Dict[str, ttk.Label] = {}
        self.capture_action: Optional[str] = None
        self.capture_hook = None

        self._build_layout()
        self.root.protocol("WM_DELETE_WINDOW", self.app.shutdown)
        self.root.after(150, self._flush_logs)

    def _setup_theme(self):
        try:
            self.style.theme_use("clam")
        except Exception:
            pass
        palette = {
            "bg": "#0f111d",
            "panel": "#1b1f32",
            "text": "#f5f5f5",
            "accent": "#3b82f6",
        }
        self.root.configure(bg=palette["bg"])
        for element in ("TFrame", "TLabelframe", "TLabelframe.Label", "TLabel"):
            self.style.configure(element, background=palette["panel"], foreground=palette["text"])
        self.style.configure("TCheckbutton", background=palette["panel"], foreground=palette["text"])
        self.style.configure("Accent.TButton", foreground=palette["text"], background=palette["accent"])

    def _build_layout(self):
        container = ttk.Frame(self.root, padding=12)
        container.pack(fill="both", expand=True)

        header = ttk.Label(container, text="自动压枪控制面板", font=("Segoe UI", 18, "bold"))
        header.pack(anchor="w", pady=(0, 8))

        body = ttk.Frame(container)
        body.pack(fill="both", expand=True)

        left_col = ttk.Frame(body)
        left_col.pack(side="left", fill="both", expand=True, padx=(0, 10))

        right_col = ttk.Frame(body, width=360)
        right_col.pack(side="right", fill="both", expand=False)
        right_col.pack_propagate(False)

        switches = ttk.Labelframe(left_col, text="功能开关")
        switches.pack(fill="x", pady=10)

        self.fire_var = tk.BooleanVar(value=self.app.state.fire_enabled)
        self.flash_var = tk.BooleanVar(value=self.app.state.flash_mode)
        self.auto_var = tk.BooleanVar(value=self.app.state.auto_click_enabled)
        self.press_key_var = tk.BooleanVar(value=self.app.state.press_key_enabled)
        self.press_key_value = tk.StringVar(value=self.app.state.press_key_char.upper())

        ttk.Checkbutton(switches, text="压枪总开关", variable=self.fire_var,
                        command=lambda: self.app.set_fire_enabled(self.fire_var.get())).grid(row=0, column=0, padx=6, pady=4, sticky="w")
        ttk.Checkbutton(switches, text="F → I 关联", variable=self.flash_var,
                        command=lambda: self.app.set_flash_mode(self.flash_var.get())).grid(row=0, column=1, padx=6, pady=4, sticky="w")
        ttk.Checkbutton(switches, text="连点开关", variable=self.auto_var,
                        command=lambda: self.app.set_auto_click_enabled(self.auto_var.get())).grid(row=0, column=2, padx=6, pady=4, sticky="w")
        ttk.Checkbutton(switches, text="自动按触发键", variable=self.press_key_var,
                        command=lambda: self.app.set_press_key_enabled(self.press_key_var.get())).grid(row=0, column=3, padx=6, pady=4, sticky="w")

        trigger_frame = ttk.Labelframe(left_col, text="触发键设置")
        trigger_frame.pack(fill="x", pady=6)
        ttk.Label(trigger_frame, text="自动按压的键：").grid(row=0, column=0, padx=6, pady=4, sticky="w")
        ttk.Entry(trigger_frame, textvariable=self.press_key_value, width=6).grid(row=0, column=1, padx=4, pady=4)
        ttk.Button(trigger_frame, text="应用", command=self._apply_press_key_char).grid(row=0, column=2, padx=6, pady=4)
        ttk.Label(trigger_frame, text="仅取首个字符，留空恢复为 P").grid(row=0, column=3, padx=6, pady=4, sticky="w")

        autoc_frame = ttk.Labelframe(left_col, text="连点参数")
        autoc_frame.pack(fill="x", pady=10)
        self.click_delay_var = tk.StringVar(value=str(self.app.click_delay))
        self.click_rand_var = tk.StringVar(value=str(self.app.click_rand))
        ttk.Label(autoc_frame, text="基准延迟 (ms)").grid(row=0, column=0, padx=6, pady=4, sticky="w")
        ttk.Entry(autoc_frame, textvariable=self.click_delay_var, width=8).grid(row=0, column=1, padx=4, pady=4)
        ttk.Label(autoc_frame, text="随机范围 ± (ms)").grid(row=0, column=2, padx=6, pady=4, sticky="w")
        ttk.Entry(autoc_frame, textvariable=self.click_rand_var, width=8).grid(row=0, column=3, padx=4, pady=4)
        ttk.Button(autoc_frame, text="应用", command=self._apply_click_config).grid(row=0, column=4, padx=8, pady=4)

        weapon_frame = ttk.Labelframe(left_col, text="武器选择")
        weapon_frame.pack(fill="both", expand=True, pady=10)
        self.current_weapon_var = tk.StringVar(value=self.app.state.current_weapon.name if self.app.state.current_weapon else "None")
        ttk.Label(weapon_frame, text="当前武器：").pack(anchor="w", pady=(4, 0))
        self.current_weapon_label = ttk.Label(weapon_frame, textvariable=self.current_weapon_var, font=("Segoe UI", 12, "bold"))
        self.current_weapon_label.pack(anchor="w")

        list_frame = ttk.Frame(weapon_frame)
        list_frame.pack(fill="both", expand=True, pady=6)
        self.weapon_list = tk.Listbox(list_frame, height=8, exportselection=False, bg="#111425", fg="#f5f5f5",
                                      selectbackground="#3b82f6", relief="flat")
        self.weapon_list.pack(side="left", fill="both", expand=True)
        self.weapon_list.bind("<<ListboxSelect>>", self._on_weapon_selected)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.weapon_list.yview)
        scroll.pack(side="right", fill="y")
        self.weapon_list.configure(yscrollcommand=scroll.set)

        nav_frame = ttk.Frame(weapon_frame)
        nav_frame.pack(fill="x", pady=4)
        ttk.Button(nav_frame, text="上一把", command=lambda: self.app.step_weapon(-1)).pack(side="left", padx=4)
        ttk.Button(nav_frame, text="下一把", command=lambda: self.app.step_weapon(1)).pack(side="left", padx=4)

        bindings = ttk.Labelframe(left_col, text="快捷键")
        bindings.pack(fill="x", pady=10)
        binding_rows = (
            ("fire", "压枪开关"),
            ("press_key", "自动按键"),
            ("flash", "F→I"),
            ("auto_click", "连点开关"),
        )
        for row, (action, label) in enumerate(binding_rows):
            frame = ttk.Frame(bindings)
            frame.grid(row=row, column=0, sticky="w", pady=4)
            ttk.Label(frame, text=label, width=12).pack(side="left")
            key_label = ttk.Label(frame, text=self._format_key(self.app.state.key_bindings.get(action)))
            key_label.pack(side="left", padx=6)
            self.binding_labels[action] = key_label
            ttk.Button(frame, text="更改", command=lambda a=action: self._begin_key_capture(a)).pack(side="left", padx=4)
            ttk.Button(frame, text="恢复默认", command=lambda a=action: self.app.update_hotkey(a, DEFAULT_KEY_BINDINGS[a])).pack(side="left", padx=4)

        self.capture_status = ttk.Label(bindings, text="", foreground="#93c5fd")
        self.capture_status.grid(row=len(binding_rows), column=0, sticky="w", pady=(6, 0))

        log_frame = ttk.Labelframe(right_col, text="日志")
        log_frame.pack(fill="both", expand=True, pady=(0, 10))
        self.log_view = tk.Text(log_frame, height=12, bg="#0b0e17", fg="#d1d5db", insertbackground="#f5f5f5",
                                state="disabled", wrap="word", relief="flat")
        self.log_view.pack(fill="both", expand=True)

        footer = ttk.Frame(right_col)
        footer.pack(fill="x", pady=6)
        ttk.Button(footer, text="刷新插件", command=self.app.refresh_plugins).pack(side="left", padx=4)
        ttk.Button(footer, text="重启程序", command=self.app.restart_program).pack(side="left", padx=4)

    def set_click_fields(self, base: int, rand: int):
        self.click_delay_var.set(str(base))
        self.click_rand_var.set(str(rand))

    def _format_key(self, key: Optional[str]) -> str:
        return key.upper() if key else "未绑定"

    def update_key_label(self, action: str, key: Optional[str]):
        label = self.binding_labels.get(action)
        if label:
            label.configure(text=self._format_key(key))

    def _begin_key_capture(self, action: str):
        if self.capture_hook:
            keyboard.unhook(self.capture_hook)
            self.capture_hook = None
        self.capture_action = action
        self.capture_status.configure(text=f"正在为 {action} 捕获按键（Esc 取消）")
        self.app.hotkeys.pause_all()
        self.capture_hook = keyboard.hook(self._handle_capture_event)

    def _handle_capture_event(self, event):
        if event.event_type != "down" or not self.capture_action:
            return
        action = self.capture_action
        key_name = (event.name or "").lower()
        cancel = key_name == "esc"
        try:
            if self.capture_hook:
                keyboard.unhook(self.capture_hook)
        except Exception:
            pass
        self.capture_hook = None
        self.capture_action = None
        self.capture_status.configure(text="")
        self.app.hotkeys.resume_all()
        if cancel:
            return
        if not key_name:
            messagebox.showwarning("绑定失败", "无法识别该按键，请重试")
            return
        self.app.update_hotkey(action, key_name)

    def sync_fire(self, value: bool):
        self.fire_var.set(bool(value))

    def sync_flash(self, value: bool):
        self.flash_var.set(bool(value))

    def sync_auto(self, value: bool):
        self.auto_var.set(bool(value))

    def sync_press(self, value: bool):
        self.press_key_var.set(bool(value))

    def sync_press_key_char(self, value: str):
        self.press_key_value.set((value or "P").upper())

    def refresh_weapon_list(self, names: List[str], selected_index: int):
        self.weapon_list.configure(state=tk.NORMAL)
        self.weapon_list.delete(0, tk.END)
        if not names:
            self.weapon_list.insert(tk.END, "插件目录为空")
            self.weapon_list.configure(state=tk.DISABLED)
            return
        for name in names:
            self.weapon_list.insert(tk.END, name)
        if 0 <= selected_index < len(names):
            self.weapon_list.selection_set(selected_index)
            self.weapon_list.see(selected_index)

    def highlight_weapon(self, index: int):
        if self.weapon_list.cget("state") == tk.DISABLED:
            return
        self.weapon_list.selection_clear(0, tk.END)
        if index >= 0:
            self.weapon_list.selection_set(index)
            self.weapon_list.see(index)

    def update_current_weapon(self, name: Optional[str]):
        self.current_weapon_var.set(name or "None")

    def _on_weapon_selected(self, _event=None):
        if self.weapon_list.cget("state") == tk.DISABLED:
            return
        sel = self.weapon_list.curselection()
        if not sel:
            return
        if sel[0] == self.app.current_weapon_index():
            return
        self.app.switch_weapon(sel[0])

    def _apply_click_config(self):
        try:
            base = int(self.click_delay_var.get())
            rand = int(self.click_rand_var.get())
        except ValueError:
            messagebox.showwarning("参数错误", "请输入整数数值")
            return
        self.app.update_click_params(base, rand)

    def _apply_press_key_char(self):
        value = self.press_key_value.get()
        self.app.update_press_key_char(value)

    def enqueue_log(self, line: str):
        self.log_queue.put(line)

    def _flush_logs(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_view.configure(state="normal")
                self.log_view.insert("end", line + "\n")
                self.log_view.see("end")
                self.log_view.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(150, self._flush_logs)

    def post(self, func: Callable[[], None]):
        self.root.after(0, func)

    def close(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


class RecoilControllerApp:
    def __init__(self):
        self.pm = PluginManager(PLUG_DIR)
        self.pm.load()
        self.state = State()
        if self.pm.weapons:
            self.state.current_weapon = self.pm.weapons[0]

        self.click_delay = 20
        self.click_rand = 20
        self._settings_cache = {}
        self._load_settings()

        self.ui = ControlPanel(self)
        self.ui.refresh_weapon_list([w.name for w in self.pm.weapons], self.current_weapon_index())
        self.ui.update_current_weapon(self.state.current_weapon.name if self.state.current_weapon else "None")
        self.ui.set_click_fields(self.click_delay, self.click_rand)
        self.ui.sync_press_key_char(self.state.press_key_char)

        self.hotkeys = HotkeyManager(self.ui.root, self.log)
        self._register_hotkeys()
        self.nav_hotkeys: List[Optional[int]] = []
        self._register_navigation_hotkeys()

        self.flash_hook_id = None
        self.mouse_listener = mouse.Listener(on_click=self._on_mouse)
        self.mouse_listener.start()

        self.engine = RecoilEngine(
            get_state=lambda: self.state.as_dict(),
            get_weapon=lambda: self.state.current_weapon or WeaponConfig(name="Default"),
            get_calc=lambda: self.pm.get_calc(self.state.current_weapon.name) if self.state.current_weapon else None,
            log=self.log,
            backend="winapi",
        )
        self.engine.set_trigger_key(self.state.press_key_char)
        self.engine.start()

        self.click_thread = AutoClickThread(
            get_state=lambda: self.state.as_dict(),
            log=self.log,
            delay_ms=self.click_delay,
            rand_ms=self.click_rand,
            begin_synth=self.state.begin_synth,
            end_synth=self.state.end_synth,
        )
        self.click_thread.start()

        self._shutting_down = False
        self._restart_args: Optional[List[str]] = None

    def log(self, msg: str):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line)
        if self.ui:
            self.ui.enqueue_log(line)
    def _load_settings(self):
        data = {}
        try:
            if SETTINGS_PATH.exists():
                data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[Settings] 加载失败: {exc}")
            data = {}
        self._settings_cache = data or {}

        def _bool(name, default):
            return bool(self._settings_cache.get(name, default))

        self.state.fire_enabled = _bool("fire_enabled", self.state.fire_enabled)
        self.state.flash_mode = _bool("flash_mode", self.state.flash_mode)
        self.state.auto_click_enabled = _bool("auto_click_enabled", self.state.auto_click_enabled)
        self.state.press_key_enabled = _bool("press_key_enabled", self.state.press_key_enabled)

        key_bindings = self._settings_cache.get("key_bindings")
        if isinstance(key_bindings, dict):
            for action, key in key_bindings.items():
                if action in self.state.key_bindings and isinstance(key, str):
                    self.state.key_bindings[action] = key.strip().lower()

        self.click_delay = int(self._settings_cache.get("click_delay", self.click_delay))
        self.click_rand = int(self._settings_cache.get("click_rand", self.click_rand))

        char = self._settings_cache.get("press_key_char")
        if isinstance(char, str) and char:
            self.state.press_key_char = char[0].lower()

        weapon_name = self._settings_cache.get("current_weapon")
        if weapon_name and self.pm.weapons:
            for w in self.pm.weapons:
                if w.name == weapon_name:
                    self.state.current_weapon = w
                    break

    def _persist_state(self):
        data = {
            "fire_enabled": self.state.fire_enabled,
            "flash_mode": self.state.flash_mode,
            "auto_click_enabled": self.state.auto_click_enabled,
            "press_key_enabled": self.state.press_key_enabled,
            "press_key_char": self.state.press_key_char,
            "click_delay": self.click_delay,
            "click_rand": self.click_rand,
            "key_bindings": self.state.key_bindings,
            "current_weapon": self.state.current_weapon.name if self.state.current_weapon else None,
        }
        self._settings_cache = data
        try:
            SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"[Settings] 保存失败: {exc}")

    def _register_hotkeys(self):
        self.hotkeys.register("fire", self.state.key_bindings["fire"], self._toggle_fire_hotkey)
        self.hotkeys.register("press_key", self.state.key_bindings["press_key"], self._toggle_press_key_hotkey)
        self.hotkeys.register("flash", self.state.key_bindings["flash"], self._toggle_flash_hotkey)
        self.hotkeys.register("auto_click", self.state.key_bindings["auto_click"], self._toggle_auto_hotkey)

    def _register_navigation_hotkeys(self):
        try:
            self.nav_hotkeys.append(keyboard.add_hotkey("alt+left", lambda: self.ui.post(lambda: self.step_weapon(-1))))
            self.nav_hotkeys.append(keyboard.add_hotkey("alt+right", lambda: self.ui.post(lambda: self.step_weapon(1))))
        except Exception as exc:
            self.log(f"[Hotkey] 注册 Alt+方向 键失败: {exc}")

    def _toggle_fire_hotkey(self):
        self.set_fire_enabled(not self.state.fire_enabled, source="hotkey")

    def _toggle_press_key_hotkey(self):
        self.set_press_key_enabled(not self.state.press_key_enabled, source="hotkey")

    def _toggle_flash_hotkey(self):
        self.set_flash_mode(not self.state.flash_mode, source="hotkey")

    def _toggle_auto_hotkey(self):
        self.set_auto_click_enabled(not self.state.auto_click_enabled, source="hotkey")

    def set_fire_enabled(self, value: bool, source: str = "ui"):
        value = bool(value)
        if self.state.fire_enabled == value:
            self.ui.sync_fire(value)
            return
        self.state.fire_enabled = value
        if value:
            tone_on_fire()
            self.log("[Fire] 已开启")
        else:
            tone_off_fire()
            self.log("[Fire] 已关闭")
            if self.engine:
                self.engine.release_trigger()
        self.ui.sync_fire(value)
        self._persist_state()

    def set_flash_mode(self, value: bool, source: str = "ui"):
        value = bool(value)
        if self.state.flash_mode == value:
            self.ui.sync_flash(value)
            return
        self.state.flash_mode = value
        if value:
            tone_on_flash()
            self.log("[Assoc] F→I 已开启")
            self._enable_flash_hook()
        else:
            tone_off_flash()
            self.log("[Assoc] F→I 已关闭")
            self._disable_flash_hook()
        self.ui.sync_flash(value)
        self._persist_state()

    def _enable_flash_hook(self):
        if self.flash_hook_id is not None:
            return
        try:
            self.flash_hook_id = keyboard.hook_key(
                'f',
                lambda e: (e.event_type == 'down' and keyboard.send('i')),
                suppress=True
            )
        except Exception as exc:
            self.flash_hook_id = None
            self.log(f"[Assoc] 注册 F→I 失败: {exc}")

    def _disable_flash_hook(self):
        if self.flash_hook_id is not None:
            try:
                keyboard.unhook(self.flash_hook_id)
            except Exception:
                pass
            self.flash_hook_id = None

    def set_auto_click_enabled(self, value: bool, source: str = "ui"):
        value = bool(value)
        if self.state.auto_click_enabled == value:
            self.ui.sync_auto(value)
            return
        self.state.auto_click_enabled = value
        if value:
            tone_on_ac()
            self.log("[Clicker] 连点开启")
        else:
            tone_off_ac()
            self.click_thread.clear_click_state()
            self.log("[Clicker] 连点关闭")
        self.ui.sync_auto(value)
        self._persist_state()

    def set_press_key_enabled(self, value: bool, source: str = "ui"):
        value = bool(value)
        if self.state.press_key_enabled == value:
            self.ui.sync_press(value)
            return
        self.state.press_key_enabled = value
        if value:
            tone_on_bx()
            self.log("[Trigger] 自动按键功能已开启")
        else:
            tone_off_bx()
            if self.engine:
                self.engine.release_trigger()
            self.log("[Trigger] 自动按键功能已关闭")
        self.ui.sync_press(value)
        self._persist_state()

    def update_click_params(self, base: int, rand: int):
        self.click_delay = max(1, base)
        self.click_rand = max(0, rand)
        if self.click_thread:
            self.click_thread._delay_ms = self.click_delay
            self.click_thread._rand_ms = self.click_rand
        self.log(f"[Clicker] 连点延迟 {self.click_delay} ± {self.click_rand} ms")
        self._persist_state()

    def update_press_key_char(self, value: str):
        raw = (value or "").strip()
        if not raw:
            raw = "p"
        char = raw[0].lower()
        changed = (self.state.press_key_char != char)
        self.state.press_key_char = char
        if self.engine:
            self.engine.set_trigger_key(char)
        if self.ui:
            self.ui.sync_press_key_char(char)
        if changed:
            self.log(f"[Trigger] 自动按键设置为 {char.upper()}")
        self._persist_state()

    def current_weapon_index(self) -> int:
        if not self.state.current_weapon or not self.pm.weapons:
            return -1
        for i, w in enumerate(self.pm.weapons):
            if w.name == self.state.current_weapon.name:
                return i
        return -1

    def switch_weapon(self, index: int):
        if not self.pm.weapons:
            self.log("[Weapon] 插件目录为空")
            return
        index = max(0, min(index, len(self.pm.weapons) - 1))
        self.state.current_weapon = self.pm.weapons[index]
        self.ui.update_current_weapon(self.state.current_weapon.name)
        self.ui.highlight_weapon(index)
        self.log(f"[Weapon] {self.state.current_weapon.name}")
        self._persist_state()

    def step_weapon(self, delta: int):
        idx = self.current_weapon_index()
        if idx == -1:
            return
        self.switch_weapon(idx + delta)

    def refresh_plugins(self):
        self.pm.load()
        if self.pm.weapons:
            names = [w.name for w in self.pm.weapons]
            if not self.state.current_weapon or self.state.current_weapon.name not in names:
                self.state.current_weapon = self.pm.weapons[0]
        else:
            names = []
            self.state.current_weapon = None
        self.ui.refresh_weapon_list(names, self.current_weapon_index())
        self.ui.update_current_weapon(self.state.current_weapon.name if self.state.current_weapon else "None")
        self.log("[Plugin] 插件列表已刷新")
        self._persist_state()

    def update_hotkey(self, action: str, key: str):
        if action not in self.state.key_bindings:
            return
        norm = (key or "").strip().lower()
        self.state.key_bindings[action] = norm
        self.hotkeys.update_key(action, norm)
        self.ui.update_key_label(action, norm)
        self._persist_state()

    def restart_program(self):
        args = [sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, __file__]
        self._restart_args = args
        self.log("[System] 正在重启程序...")
        self.shutdown()

    def _on_mouse(self, _x, _y, btn, pressed):
        if self.state.is_synth():
            return
        if btn == mouse.Button.right:
            self.state.right_hold = pressed
        elif btn == mouse.Button.left:
            self.state.left_hold = pressed

    def shutdown(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        self.log("[System] 正在退出...")
        if self.hotkeys:
            self.hotkeys.shutdown()
        for handle in self.nav_hotkeys:
            if handle is not None:
                try:
                    keyboard.remove_hotkey(handle)
                except Exception:
                    pass
        self.nav_hotkeys.clear()
        self._disable_flash_hook()
        if self.mouse_listener:
            self.mouse_listener.stop()
        if self.click_thread:
            self.click_thread.stop()
        if self.engine:
            self.engine.stop()
        stop_all_audio()
        if self.ui:
            self.ui.close()

    def run(self):
        try:
            self.ui.run()
        finally:
            if self._restart_args:
                subprocess.Popen(self._restart_args)


def main():
    app = RecoilControllerApp()
    app.run()


if __name__ == "__main__":
    main()
