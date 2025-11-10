# core/engine.py
"""
RecoilEngine - 压枪引擎
- 通过 `get_state()` 获取运行时状态（dict: fire_enabled, right_hold, left_hold, flash_mode 可选）
- 通过 `get_weapon()` 获取当前 WeaponConfig
- 通过 `get_calc()` 获取可选的自定义 pull 计算函数: (elapsed_time: float, count: int) -> float
- 使用输入后端（WinAPIBackend 或 PynputBackend）执行鼠标移动和按键操作
"""
import random
import ctypes
import time
import threading
from typing import Callable, Optional

import random, ctypes, time, threading
# 尝试导入后端实现（如果项目中有 core/input_backend.py）
try:
    from core.input_backend import WinAPIBackend, PynputBackend
    _HAS_BACKENDS = True
except Exception:
    # 如果没有实现文件，后续创建一个简易回退使用 pynput（若也不可用则仅做日志）
    _HAS_BACKENDS = False

from core.plugin_api import WeaponConfig


class RecoilEngine:
    """
    RecoilEngine 控制逻辑：
      - start()/stop() 控制后台线程
      - 每次循环读取 get_state/get_weapon/get_calc 返回值决定行为
      - 在射击开始时按住 'p'（key_down），在结束时释放 'p'（key_up）
      - 鼠标下移使用 backend.move_mouse(0, int(pull))
    """

    def __init__(
        self,
        get_state: Callable[[], dict],
        get_weapon: Callable[[], WeaponConfig],
        get_calc: Callable[[], Optional[Callable[[float, int], float]]],
        log: Callable[[str], None] = lambda s: None,
        backend: str = "winapi",  # "winapi" or "pynput" or "auto"
    ):
        self._get_state = get_state
        self._get_weapon = get_weapon
        self._get_calc = get_calc
        self._log = log

        self._running = False
        self._thread: Optional[threading.Thread] = None

        # 选择输入后端
        self._backend_name = backend
        self._backend = self._choose_backend(backend)
        self._trigger_key = 'p'
        self._trigger_held = False

    def _choose_backend(self, backend: str):
        """选择输入后端。优先 WinAPI（更兼容游戏），回退到 pynput。"""
        if _HAS_BACKENDS:
            try:
                if backend == "winapi":
                    return WinAPIBackend()
                elif backend == "pynput":
                    return PynputBackend()
                else:  # auto
                    # 优先 winapi，失败回退 pynput
                    try:
                        return WinAPIBackend()
                    except Exception:
                        return PynputBackend()
            except Exception as e:
                self._log(f"[Engine] backend init failed: {e}")
        # 回退：尽量使用 pynput if available, else a dummy backend
        try:
            from pynput.mouse import Controller as PMouse
            from pynput.keyboard import Controller as PKey
            class _SimplePynputBackend:
                def __init__(self):
                    self.m = PMouse()
                    self.k = PKey()
                def move_mouse(self, dx: int, dy: int) -> None:
                    try:
                        self.m.move(dx, dy)
                    except Exception:
                        pass
                def key_down(self, ch: str) -> None:
                    try:
                        self.k.press(ch)
                    except Exception:
                        pass
                def key_up(self, ch: str) -> None:
                    try:
                        self.k.release(ch)
                    except Exception:
                        pass
                def key_tap(self, ch: str) -> None:
                    try:
                        self.key_down(ch); self.key_up(ch)
                    except Exception:
                        pass
            return _SimplePynputBackend()
        except Exception:
            # 最后回退到一个不执行动作的占位 backend，仅做日志（防止崩溃）
            class _DummyBackend:
                def move_mouse(self, dx: int, dy: int) -> None:
                    pass
                def key_down(self, ch: str) -> None:
                    pass
                def key_up(self, ch: str) -> None:
                    pass
                def key_tap(self, ch: str) -> None:
                    pass
            self._log("[Engine] Warning: no real input backend available, using dummy backend.")
            return _DummyBackend()

    # ----------------------------
    # lifecycle
    # ----------------------------
    def start(self) -> None:
        """启动后台线程"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._log("[Engine] started with backend=" + str(self._backend_name))

    def stop(self) -> None:
        """停止后台线程"""
        self._running = False
        self.release_trigger()
        if self._thread is not None:
            try:
                self._thread.join(timeout=1.0)
            except Exception:
                pass
        self._log("[Engine] stopped")

    def set_trigger_key(self, key: str) -> None:
        """更新内部按压的触发键"""
        if not key:
            key = 'p'
        key = str(key)[0]
        if not key.strip():
            key = 'p'
        key = key.lower()
        if key == self._trigger_key:
            return
        was_held = self._trigger_held
        if was_held:
            self.release_trigger()
        self._trigger_key = key
        if was_held:
            self._ensure_trigger_down()
        self._log(f"[Engine] trigger key -> '{self._trigger_key}'")

    def _ensure_trigger_down(self) -> None:
        if self._trigger_held:
            return
        try:
            self._backend.key_down(self._trigger_key)
            self._trigger_held = True
        except Exception as e:
            self._log(f"[Engine] key_down '{self._trigger_key}' failed: {e}")

    def release_trigger(self) -> None:
        """确保自动按下的触发键被释放"""
        if not self._trigger_held:
            return
        try:
            self._backend.key_up(self._trigger_key)
        except Exception as e:
            self._log(f"[Engine] key_up '{self._trigger_key}' failed: {e}")
        finally:
            self._trigger_held = False

    # ----------------------------
    # 内部逻辑
    # ----------------------------
    def _press_key_once(self, ch: str) -> None:
        """按一次键（tap）"""
        try:
            self._backend.key_tap(ch)
        except Exception:
            try:
                # 保险回退：按下再释放
                self._backend.key_down(ch)
                time.sleep(0.01)
                self._backend.key_up(ch)
            except Exception:
                pass

    def _loop(self) -> None:
        """
        主循环：轮询 get_state()，并在需要时执行压枪动作。
        轮询频率以微等待为主（1ms），但实际 sleep 会被 sleepTime 控制。
        """
        count = 0
        shooting = False
        start_time = 0.0

        while self._running:
            try:
                st = self._get_state() or {}
                cfg = self._get_weapon() or WeaponConfig(name="Default")
                calc = self._get_calc()
            except Exception as e:
                self._log(f"[Engine] get_state/get_weapon/get_calc error: {e}")
                st = {}
                cfg = WeaponConfig(name="Default")
                calc = None

            # 基本状态判定
            fire_enabled = bool(st.get("fire_enabled", True))
            right_hold = bool(st.get("right_hold", False))
            left_hold = bool(st.get("left_hold", False))
            press_key_enabled = bool(st.get("press_key_enabled", True))

            should_fire = fire_enabled and right_hold and left_hold
            if not press_key_enabled and self._trigger_held:
                self.release_trigger()

            # 射击开始
            if should_fire and not shooting:
                shooting = True
                count = 0
                start_time = time.perf_counter()
                # 按住 P（射击期间保持）
                if press_key_enabled:
                    self._ensure_trigger_down()
                self._log("[Engine] shooting start")

            # 射击循环
            if shooting and should_fire:
                if press_key_enabled:
                    self._ensure_trigger_down()
                else:
                    self.release_trigger()
                count += 1
                elapsed = time.perf_counter() - start_time
                try:
                    if callable(calc):
                        pull = float(calc(elapsed, count))
                    else:
                        # 默认计算：初始阶段动态加量，之后稳态
                        if elapsed < float(getattr(cfg, "initialDuration", 0.5)):
                            pull = float(getattr(cfg, "defaultPull", 2.0)) + (count / float(getattr(cfg, "acceleration", 200.0)))
                        else:
                            pull = float(getattr(cfg, "steadyPull", 1.6))
                except Exception as e:
                    self._log(f"[Engine] calculate pull error: {e}")
                    pull = float(getattr(cfg, "steadyPull", 1.6))

                # 执行鼠标下移：MoveMouse(0, int(pull))
                try:
                    self._backend.move_mouse(0, int(pull))
                except Exception as e:
                    self._log(f"[Engine] move_mouse error: {e}")

                # 简要日志每若干次打印一次
                if count % 10 == 0:
                    try:
                        self._log(f"[Engine] t={elapsed:.3f}s count={count} pull={int(pull)}")
                    except Exception:
                        pass

                # sleep cfg.sleepTime 毫秒
                sleep_ms = int(getattr(cfg, "sleepTime", 8))
                # 最低保障 sleep，避免零延迟烧 CPU
                time.sleep(max(0.001, sleep_ms / 1000.0))

            # 射击结束
            if shooting and not should_fire:
                shooting = False
                count = 0
                start_time = 0.0
                self.release_trigger()
                self._log("[Engine] shooting end")

            # 当未在射击且也不需要高频时，短暂让步
            if not shooting:
                # 保证主循环不会过热，但保持较短延迟以响应按键
                time.sleep(0.002)

        # 线程退出时确保释放按键（若还按着 p）
        self.release_trigger()


class AutoClickThread(threading.Thread):
    def __init__(self, get_state, log, delay_ms=20, rand_ms=20,
                 begin_synth=lambda: None, end_synth=lambda: None):
        super().__init__(daemon=True)
        self._get_state = get_state
        self._log = log
        self._delay_ms = delay_ms
        self._rand_ms = rand_ms
        self._running = True
        self._clicking = False
        self._synthing = False
        self._last_synth_ts = 0.0
        self._hold_shadow = False

        def _wrap_begin():
            self._synthing = True
            try:
                begin_synth()
            except Exception:
                self._synthing = False
                raise

        def _wrap_end():
            try:
                end_synth()
            finally:
                self._synthing = False

        self._begin_synth = _wrap_begin
        self._end_synth = _wrap_end

        self.user32 = ctypes.windll.user32
        self.MOUSEEVENTF_LEFTDOWN = 0x0002
        self.MOUSEEVENTF_LEFTUP   = 0x0004
        self.VK_LBUTTON = 0x01

    def click_once(self):
        try:
            self._begin_synth()
            # 发合成 down/up（监听会被屏蔽，不改 left_hold）
            self.user32.mouse_event(self.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            self.user32.mouse_event(self.MOUSEEVENTF_LEFTUP,   0, 0, 0, 0)
        finally:
            # 给监听一点点时间过滤当前这帧的事件
            time.sleep(0.002)
            self._end_synth()
            self._last_synth_ts = time.perf_counter()

    def stop(self):
        self._running = False
        self.clear_click_state()

    def clear_click_state(self):
        """External hook to reset click status when UI turns the feature off."""
        if self._clicking:
            self._clicking = False
            self._log("[Clicker] 状态已重置")
        self._hold_shadow = False

    def _physical_left_pressed(self) -> bool:
        """Check the hardware mouse state to avoid missing release events."""
        try:
            return bool(self.user32.GetAsyncKeyState(self.VK_LBUTTON) & 0x8000)
        except Exception:
            return False

    def run(self):
        while self._running:
            st = self._get_state()
            auto_enabled = bool(st.get("auto_click_enabled", False))
            user_hold = bool(st.get("left_hold", False))
            physical_hold = self._physical_left_pressed()
            now = time.perf_counter()

            if user_hold:
                self._hold_shadow = True
            elif (not physical_hold) and (not self._synthing) and (now - self._last_synth_ts > 0.03):
                self._hold_shadow = False

            left_hold = physical_hold or self._hold_shadow

            if auto_enabled and left_hold:
                if not self._clicking:
                    self._clicking = True
                    self._log("[Clicker] 连点开启")
                self.click_once()
                delay = max(1, self._delay_ms + random.randint(-self._rand_ms, self._rand_ms))
                time.sleep(delay / 1000.0)
            else:
                if self._clicking:
                    self._clicking = False
                    self._log("[Clicker] 连点结束")
                time.sleep(0.01)
