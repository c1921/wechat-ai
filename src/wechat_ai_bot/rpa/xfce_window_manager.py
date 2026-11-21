"""Openbox/X11 window manager using xdotool + mss."""

import logging
import re
import subprocess
import time
from threading import RLock
from pathlib import Path
from typing import Any, Optional

import wechat_ai_bot.utils.mouse as pyautogui
from wechat_ai_bot.rpa.linux_window_manager import WindowTypeEnum
from wechat_ai_bot.rpa.image_processor import ImageProcessor
from wechat_ai_bot.rpa.ocr_processor import OCRProcessor
from wechat_ai_bot.utils.helpers import get_center_point, set_clipboard_text
from wechat_ai_bot.utils.mouse import human_like_mouse_move
from wechat_ai_bot.utils.size_config import suggest_size


def _xdotool(*args):
    """Run xdotool command."""
    subprocess.run(["xdotool"] + list(args), capture_output=True, timeout=5)


def _run(*args) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=5)


def _position() -> tuple:
    r = subprocess.run(["xdotool", "getmouselocation"], capture_output=True, text=True, timeout=3)
    import re
    x = int(re.search(r"x:(\d+)", r.stdout).group(1))
    y = int(re.search(r"y:(\d+)", r.stdout).group(1))
    return (x, y)


class XdotoolWindow:
    """Small pyautogui-like wrapper around an X11 window id."""

    def __init__(self, wid: str, title: str, geom: dict):
        self.id = str(wid)
        self.title = title
        self.left = int(geom.get("x", 0))
        self.top = int(geom.get("y", 0))
        self.width = int(geom.get("width", 0))
        self.height = int(geom.get("height", 0))

    def close(self) -> None:
        _run("xdotool", "windowclose", self.id)


class XFCEWindowManager:
    """X11 window manager using xdotool + mss."""

    MIN_MAIN_WINDOW_WIDTH = 500
    MIN_MAIN_WINDOW_HEIGHT = 400
    MIN_EDITOR_INTERIOR_HEIGHT = 40
    RECOVERY_INTERVAL = 15
    LOGIN_GUIDANCE = {
        "login_required": "请打开容器桌面，重新登录微信",
        "scan_required": "请打开容器桌面，用手机微信扫码登录",
        "confirmation_required": "请在手机微信上确认登录",
    }

    def __init__(self, image_processor: ImageProcessor, ocr_processor: OCRProcessor, rpa_config: dict = None):
        self.logger = logging.getLogger(__name__)
        self.rpa_config = rpa_config or {}
        self.size_config = suggest_size(self.rpa_config)
        self.weixin_windows = {}
        self.current_window = None
        # Shared with the controller for the entire visual action, including cleanup.
        self.ui_lock = RLock()
        self._startup_empty_frames = 0
        self._startup_retry_at = 0.0
        self._startup_pending_session = ""
        self._startup_pending_until = 0.0
        self._resize_retry_at = 0.0
        self._entry_retry_at = 0.0
        self.MSG_TOP_X = self.MSG_TOP_Y = self.MSG_WIDTH = self.MSG_HEIGHT = 0
        self.SIDE_BAR_WIDTH = self.SESSION_LIST_WIDTH = self.TITLE_BAR_HEIGHT = 0
        self.ROOM_SIDE_BAR_WIDTH = int(self.rpa_config.get("room_side_bar_width", 360))
        self.target_window_size = (self.size_config.width, self.size_config.height)
        self.actual_window_geometry = {}
        self.window_aligned = False
        self.window_id: Optional[str] = None
        self.last_window_state = "not_initialized"
        self._readiness = {
            "wechat_process_running": None,
            "login_state": "unknown",
            "ui_send_ready": False,
            "reason": self.last_window_state,
            "guidance": "",
            "checked_at": None,
        }

        self.ICON_CONFIGS = {
            "send_button": {"name": "send", "color": "red", "position": None},
            "search_icon": {"name": "search", "color": "yellow", "position": None},
        }

        self.image_processor = image_processor
        self.ocr_processor = ocr_processor
        self.last_switch_session = None
        self.last_switch_session_time = None
        self.current_session_name = ""
        self.action_delay = self.rpa_config.get("action_delay", 0.3)
        self.side_bar_delay = self.rpa_config.get("side_bar_delay", 3)
        self.scroll_delay = self.rpa_config.get("scroll_delay", 1)
        self.switch_contact_delay = self.rpa_config.get("switch_contact_delay", 0.3)
        self.window_show_delay = self.rpa_config.get("window_show_delay", 1.5)
        self.window_margin = self.rpa_config.get("window_margin", 20)
        self.room_action_offset = tuple(self.rpa_config.get("room_action_offset", (0, -30)))
        self.search_contact_offset = tuple(self.rpa_config.get("search_contact_offset", (0, 40)))

    def _is_main_wechat_window(self, geom: dict) -> bool:
        return (
            geom["width"] >= self.MIN_MAIN_WINDOW_WIDTH
            and geom["height"] >= self.MIN_MAIN_WINDOW_HEIGHT
        )

    def _is_wechat_popup_window(self, window: XdotoolWindow) -> bool:
        if window.title.lower() not in {"wechat", "weixin", "微信"}:
            return False
        if window.width >= self.size_config.width or window.height >= self.size_config.height:
            return False
        return window.width >= 220 and window.height >= 120

    def _room_sidebar_region(self) -> list[int]:
        width = int(self.ROOM_SIDE_BAR_WIDTH or self.rpa_config.get("room_side_bar_width", 360))
        width = max(260, min(width, int(self.size_config.width) // 2))
        top = int(self.TITLE_BAR_HEIGHT or 52)
        return [
            int(self.size_config.width) - width,
            top,
            width,
            int(self.size_config.height) - top,
        ]

    def _is_room_sidebar_open(self) -> bool:
        region = self._room_sidebar_region()
        try:
            screenshot = self.image_processor.take_screenshot(
                region=region,
                save_path="/config/runtime_images/sidebar_state_ocr.png",
            )
            if screenshot is None:
                return False
            results = self.ocr_processor.process_image(image=screenshot)
        except Exception as exc:
            self.logger.debug("Failed to OCR room sidebar state: %s", exc)
            return False

        labels = [str(item.get("label") or "").strip() for item in results]
        normalized_labels = ["".join(label.split()) for label in labels if label]
        markers = (
            "搜索群成员",
            "群聊名称",
            "群公告",
            "消息免打扰",
            "我在本群的昵称",
            "查找聊天内容",
            "显示群成员昵称",
            "退出群聊",
        )
        matched_markers = set()
        for label in normalized_labels:
            for marker in markers:
                if marker in label or label in marker:
                    matched_markers.add(marker)
        if len(matched_markers) >= 2:
            return True
        self.logger.debug("Room sidebar markers not found: labels=%s region=%s", labels, region)
        return False

    def _managed_window_ids(self) -> set[str]:
        """Return top-level windows known by the window manager."""
        try:
            r = _run("xprop", "-root", "_NET_CLIENT_LIST")
        except Exception:
            return set()
        if r.returncode != 0:
            return set()
        ids = set()
        for token in re.findall(r"0x[0-9a-fA-F]+", r.stdout):
            try:
                ids.add(str(int(token, 16)))
            except ValueError:
                continue
        return ids

    def _find_wechat_window(self, *, log: bool = True) -> Optional[str]:
        candidates = []
        seen = set()
        managed_ids = self._managed_window_ids()
        for query in (
            ("search", "--class", "wechat"),
            ("search", "--name", "WeChat"),
            ("search", "--name", "Weixin"),
            ("search", "--name", "微信"),
        ):
            r = _run("xdotool", *query)
            for wid in r.stdout.strip().split():
                if wid in seen:
                    continue
                seen.add(wid)
                geom = self._window_geometry(wid)
                if not geom:
                    continue
                name = _run("xdotool", "getwindowname", wid).stdout.strip()
                # WeChatAppEx browser windows can have the same title and size
                # as the chat window. Never send keystrokes to a known helper.
                if self._is_main_process_window(wid) is not True:
                    continue
                area = geom["width"] * geom["height"]
                candidates.append((area, wid, geom, name, wid in managed_ids))

        visible = [
            item for item in candidates
            if item[3].lower() in {"wechat", "weixin", "微信"}
            and item[2]["width"] >= 220
            and item[2]["height"] >= 120
        ]
        if not visible:
            if log:
                self.logger.warning("No visible WeChat window candidates: %s", candidates)
            return None

        visible.sort(reverse=True, key=lambda item: (item[4], item[0]))
        area, wid, geom, name, managed = visible[0]
        if log:
            self.logger.info(
                "Selected WeChat window %s '%s' at %s,%s %sx%s managed=%s",
                wid,
                name,
                geom["x"],
                geom["y"],
                geom["width"],
                geom["height"],
                managed,
            )
        return wid

    def _is_main_process_window(self, wid: str) -> Optional[bool]:
        try:
            result = _run("xdotool", "getwindowpid", wid)
            pid = result.stdout.strip()
            if result.returncode != 0 or not pid.isdigit():
                return None
            executable = Path(f"/proc/{pid}/exe").resolve(strict=True).name.lower()
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return None
        return executable in {"wechat", "weixin"}

    def _all_managed_windows(self) -> list[XdotoolWindow]:
        windows: list[XdotoolWindow] = []
        for wid in self._managed_window_ids():
            geom = self._window_geometry(wid)
            if not geom:
                continue
            name = _run("xdotool", "getwindowname", wid).stdout.strip()
            if not name:
                continue
            windows.append(XdotoolWindow(wid, name, geom))
        return windows

    def _all_wechat_windows(self) -> list[XdotoolWindow]:
        windows_by_id: dict[str, XdotoolWindow] = {
            window.id: window for window in self._all_managed_windows()
        }
        for query in (
            ("search", "--class", "wechat"),
            ("search", "--name", "WeChat"),
            ("search", "--name", "Weixin"),
            ("search", "--name", "wechat"),
            ("search", "--name", "微信"),
        ):
            result = _run("xdotool", *query)
            for wid in result.stdout.strip().split():
                if not wid or wid in windows_by_id:
                    continue
                geom = self._window_geometry(wid)
                if not geom:
                    continue
                name = _run("xdotool", "getwindowname", wid).stdout.strip()
                if not name:
                    continue
                windows_by_id[wid] = XdotoolWindow(wid, name, geom)
        return list(windows_by_id.values())

    def _active_window(self) -> Optional[XdotoolWindow]:
        result = _run("xdotool", "getactivewindow")
        wid = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        if not wid:
            return None
        geom = self._window_geometry(wid)
        if not geom:
            return None
        name = _run("xdotool", "getwindowname", wid).stdout.strip()
        if not name:
            return None
        return XdotoolWindow(wid, name, geom)

    def get_window(
        self,
        window_type: WindowTypeEnum,
        all_windows: bool = False,
    ) -> Optional[XdotoolWindow]:
        windows = self._all_wechat_windows() if window_type == WindowTypeEnum.MenuWindow else self._all_managed_windows()
        wechat_titles = {"wechat", "weixin", "微信"}
        if window_type == WindowTypeEnum.MainWindow:
            return next((w for w in windows if w.title.lower() in wechat_titles), None)
        if window_type == WindowTypeEnum.PublicAnnouncementWindow:
            return next((w for w in windows if "群公告" in w.title), None)
        if window_type == WindowTypeEnum.InviteMemberWindow:
            return next((w for w in windows if "添加群成员" in w.title or "邀请" in w.title), None)
        if window_type == WindowTypeEnum.RemoveMemberWindow:
            return next((w for w in windows if "移出群成员" in w.title or "删除成员" in w.title), None)
        if window_type == WindowTypeEnum.MenuWindow:
            candidates = [
                w
                for w in windows
                if w.title.lower() in wechat_titles
                and 80 <= w.width <= 600
                and 100 <= w.height <= 800
                and w.width < int(self.size_config.width) * 0.8
                and w.height < int(self.size_config.height) * 0.8
            ]
            candidates.sort(key=lambda w: w.width * w.height, reverse=True)
            return candidates[0] if candidates else None
        if window_type in (
            WindowTypeEnum.InviteConfirmWindow,
            WindowTypeEnum.InviteResonWindow,
            WindowTypeEnum.RoomInputConfirmBox,
        ):
            active = self._active_window()
            if active and self._is_wechat_popup_window(active):
                return active
            popup_windows = [
                w for w in self._all_wechat_windows() if self._is_wechat_popup_window(w)
            ]
            popup_windows.sort(key=lambda w: w.width * w.height, reverse=True)
            return popup_windows[0] if popup_windows else None
        return None

    def wait_for_window(
        self,
        window_type: WindowTypeEnum,
        all: bool = False,
        timeout: int = 5,
    ) -> Optional[XdotoolWindow]:
        start = time.time()
        while time.time() - start < timeout:
            window = self.get_window(window_type, all)
            if window:
                return window
            time.sleep(0.2)
        self.logger.warning("Window %s not found within %ss", window_type.value, timeout)
        return None

    def _read_login_prompt(self, screenshot) -> tuple[str, Optional[dict]]:
        if screenshot is None:
            return "unknown", None
        rows = {
            self._startup_label(row.get("label", "")): row
            for row in self.ocr_processor.process_image(image=screenshot, local_only=True)
        }
        if any(
            text == "扫码登录" or "微信扫码登录" in text or "扫描二维码登录" in text
            or "使用手机微信扫描二维码" in text
            or text in {"scanqrcodetologin", "scantheqrcodetologin"}
            for text in rows
        ):
            return "scan_required", None
        if any(
            "手机" in text
            and any(prompt in text for prompt in ("确认登录", "确认后登录", "完成登录"))
            for text in rows
        ):
            return "confirmation_required", None
        if any("请重新登录" in text or "你已退出微信" in text for text in rows):
            return "login_required", rows.get("我知道了")
        if "进入微信" in rows:
            return "login_required", rows["进入微信"]
        if (
            screenshot.width < self.MIN_MAIN_WINDOW_WIDTH
            and "登录" in rows
            and {"切换账号", "切换帐号"} & rows.keys()
        ):
            return "login_required", rows["登录"]
        return "unknown", None

    def _inspect_login_window(self, wid: str, geom: dict, *, recover: bool) -> str:
        region = [geom["x"], geom["y"], geom["width"], geom["height"]]
        screenshot = self.image_processor.take_screenshot(region=region)
        login_state, button = self._read_login_prompt(screenshot)
        if button and recover and time.monotonic() >= self._entry_retry_at:
            bbox = button.get("pixel_bbox", ())
            if len(bbox) == 4:
                x1, y1, x2, y2 = bbox
                if 0 <= x1 < x2 <= geom["width"] and 0 <= y1 < y2 <= geom["height"]:
                    if self._active_window_id() == wid and self._is_main_process_window(wid) is True:
                        self._entry_retry_at = time.monotonic() + self.RECOVERY_INTERVAL
                        x = geom["x"] + int((x1 + x2) / 2)
                        y = geom["y"] + int((y1 + y2) / 2)
                        _run("xdotool", "mousemove", str(x), str(y), "click", "1")
        return login_state

    def _display_size(self) -> Optional[tuple[int, int]]:
        r = _run("xdotool", "getdisplaygeometry")
        parts = r.stdout.strip().split()
        if r.returncode == 0 and len(parts) == 2 and all(part.isdigit() for part in parts):
            return int(parts[0]), int(parts[1])
        return None

    def _window_geometry(self, wid: str) -> Optional[dict]:
        try:
            r = _run("xdotool", "getwindowgeometry", "--shell", wid)
            if r.returncode != 0:
                return None
            geom = {}
            for line in r.stdout.splitlines():
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key in {"X", "Y", "WIDTH", "HEIGHT"}:
                    geom[key.lower()] = int(value)
            if {"x", "y", "width", "height"} <= set(geom):
                return geom
        except Exception as e:
            self.logger.warning(f"Failed to read window geometry: {e}")
        return None

    def _target_window_size(self) -> tuple[int, int]:
        size = suggest_size(self.rpa_config)
        self.target_window_size = (size.width, size.height)
        display = self._display_size()
        if display is None:
            self.last_window_state = "display_unavailable"
            return 0, 0
        if display[0] < size.width or display[1] < size.height:
            self.last_window_state = "display_too_small"
            return 0, 0
        self.size_config = size
        return self.target_window_size

    def _geometry_matches_target(
        self,
        geom: Optional[dict],
        target_w: Optional[int] = None,
        target_h: Optional[int] = None,
    ) -> bool:
        if not geom:
            return False
        target_w = target_w or self.target_window_size[0]
        target_h = target_h or self.target_window_size[1]
        tolerance = int(self.rpa_config.get("window", {}).get("size_tolerance", 8))
        return (
            abs(geom["width"] - target_w) <= tolerance
            and abs(geom["height"] - target_h) <= tolerance
            and abs(geom["x"]) <= tolerance
            and abs(geom["y"]) <= tolerance
        )

    def _clear_session_cache(self) -> None:
        self.last_switch_session = None
        self.last_switch_session_time = None
        self.current_session_name = ""

    def _invalidate_layout(self) -> None:
        self.current_window = None
        self.weixin_windows.pop("WeChat", None)
        self.MSG_TOP_X = self.MSG_TOP_Y = self.MSG_WIDTH = self.MSG_HEIGHT = 0
        self.SIDE_BAR_WIDTH = self.SESSION_LIST_WIDTH = self.TITLE_BAR_HEIGHT = 0
        self.ICON_CONFIGS["send_button"]["position"] = None
        self.window_aligned = False
        self.last_window_state = "layout_unavailable"
        self._clear_session_cache()

    def _input_height_is_valid(self, height: int, input_top: int) -> bool:
        layout_config = self.rpa_config.get("layout", {})
        minimum = int(layout_config.get("min_input_height", 100))
        maximum = int(layout_config.get("max_input_height", 200))
        input_height = height - input_top
        if not 0 < minimum <= input_height <= maximum < height:
            self.logger.debug(
                "Invalid WeChat input height: %s (expected %s..%s), input_top=%s",
                input_height, minimum, maximum, input_top,
            )
            return False
        return True

    def _set_current_layout(self, w: int, h: int) -> None:
        self.weixin_windows["WeChat"] = {
            "MSG_TOP_X": self.MSG_TOP_X,
            "MSG_TOP_Y": self.MSG_TOP_Y,
            "MSG_WIDTH": self.MSG_WIDTH,
            "MSG_HEIGHT": self.MSG_HEIGHT,
            "region": [0, 0, w, h],
        }
        self.current_window = self.weixin_windows["WeChat"]
        self.window_aligned = True

    def refresh_window_geometry(self) -> dict:
        """Read current geometry without changing calibration or readiness."""
        wid = self.window_id
        if not wid or self._is_main_process_window(wid) is not True:
            return {}
        return self._window_geometry(wid) or {}

    def get_readiness(self) -> dict:
        """Return a snapshot without X11 I/O or acquiring the action lock."""
        return dict(self._readiness)

    def _publish_readiness(self, running: Optional[bool], reason: str, login: str) -> bool:
        ready = reason == "ready"
        if not ready:
            self._invalidate_layout()
        guidance = self.LOGIN_GUIDANCE.get(login, "")
        previous = self._readiness
        self.last_window_state = reason
        self._readiness = {
            "wechat_process_running": running,
            "login_state": login,
            "ui_send_ready": ready,
            "reason": reason,
            "guidance": guidance,
            "checked_at": int(time.time()),
        }
        if login == "scan_required" and previous["login_state"] != login:
            self.logger.warning("%s", guidance)
        elif reason != previous["reason"]:
            self.logger.info("WeChat window state: %s (login: %s)", reason, login)
        return ready

    def _active_window_id(self) -> Optional[str]:
        result = _run("xdotool", "getactivewindow")
        wid = result.stdout.strip()
        return wid if result.returncode == 0 and wid.isdigit() else None

    def _window_above(self, wid: str, geometry: dict) -> Optional[str]:
        result = _run("xprop", "-root", "_NET_CLIENT_LIST_STACKING")
        ids = [str(int(token, 16)) for token in re.findall(r"0x[0-9a-fA-F]+", result.stdout)]
        if result.returncode != 0 or wid not in ids:
            return None
        for other in reversed(ids[ids.index(wid) + 1:]):
            bounds = self._window_geometry(other)
            if not bounds:
                continue
            if any(
                max(geometry[pos], bounds[pos]) >= min(
                    geometry[pos] + geometry[length], bounds[pos] + bounds[length],
                )
                for pos, length in (("x", "width"), ("y", "height"))
            ):
                continue
            info = _run("xwininfo", "-id", other)
            if info.returncode == 0 and "IsViewable" in info.stdout:
                return other
        return None

    def _normalize_dpi(self) -> None:
        """Keep the WeChat/RPA window in physical pixels instead of Selkies HiDPI."""
        try:
            Path("/config/.Xresources").write_text("Xft.dpi: 96\n", encoding="utf-8")
            _run("xrdb", "/config/.Xresources")
        except Exception as exc:
            self.logger.debug("Failed to normalize Xft.dpi: %s", exc)

    def _resize_wechat_window(self, wid: str, target_w: int, target_h: int) -> Optional[dict]:
        """Move and resize WeChat, waiting for the WM to report the requested size."""
        tolerance = int(self.rpa_config.get("window", {}).get("size_tolerance", 8))
        for attempt in range(1, 4):
            _run("xdotool", "windowactivate", wid)
            time.sleep(self.action_delay)
            try:
                _run("wmctrl", "-ir", wid, "-b", "remove,maximized_vert,maximized_horz")
            except FileNotFoundError:
                self.logger.debug("wmctrl not available; resizing with xdotool only")
            time.sleep(self.action_delay)
            _run("xdotool", "windowmove", "--sync", wid, "0", "0")
            _run("xdotool", "windowsize", "--sync", wid, str(target_w), str(target_h))
            time.sleep(max(self.action_delay, 0.5))
            geom = self._window_geometry(wid)
            if not geom:
                continue
            width_ok = abs(geom["width"] - target_w) <= tolerance
            height_ok = abs(geom["height"] - target_h) <= tolerance
            x_ok = abs(geom["x"]) <= tolerance
            y_ok = abs(geom["y"]) <= tolerance
            if width_ok and height_ok and x_ok and y_ok:
                return geom
            self.logger.warning(
                "WeChat resize attempt %s did not reach target %sx%s: got x=%s y=%s %sx%s",
                attempt,
                target_w,
                target_h,
                geom["x"],
                geom["y"],
                geom["width"],
                geom["height"],
            )
        return self._window_geometry(wid)

    def ensure_action_ready(self, action: Any = None) -> bool:
        """Check the live window before an action, without waiting for recovery."""
        with self.ui_lock:
            return self._check_window_locked(activate=True, recover=False)

    def monitor_window(self) -> None:
        """Observe and recover in the background when no RPA action owns the UI."""
        if not self.ui_lock.acquire(blocking=False):
            return
        try:
            self._check_window_locked(activate=False, recover=True)
        finally:
            self.ui_lock.release()

    def _reset_session_recovery(self) -> None:
        self._startup_empty_frames = 0
        self._startup_pending_session = ""
        self._startup_pending_until = 0.0

    def _check_window_locked(self, *, activate: bool, recover: bool) -> bool:
        running = None
        try:
            process = _run("pgrep", "-x", "wechat|weixin")
            if process.returncode not in {0, 1}:
                return self._publish_readiness(None, "process_check_failed", "unknown")
            running = process.returncode == 0 and bool(process.stdout.strip())
            wid = self._find_wechat_window(log=False) if running else None
            if wid != self.window_id:
                self._reset_session_recovery()
                self._resize_retry_at = 0.0
                self._entry_retry_at = 0.0
                self._startup_retry_at = 0.0
                self.window_id = wid
                if wid and self._readiness["ui_send_ready"]:
                    self._publish_readiness(running, "window_changed", "unknown")
                else:
                    self._invalidate_layout()
            self.actual_window_geometry = {}
            if not running:
                return self._publish_readiness(False, "wechat_not_running", "unknown")
            if not wid:
                return self._publish_readiness(True, "window_not_found", "unknown")

            geometry = self._window_geometry(wid)
            if not geometry:
                return self._publish_readiness(True, "window_geometry_unavailable", "unknown")
            self.actual_window_geometry = geometry
            reason, login = self._inspect_window(wid, geometry, activate=activate, recover=recover)
            if login in self.LOGIN_GUIDANCE:
                self._reset_session_recovery()
            if reason not in {"ready", "welcome_pane"}:
                self._startup_empty_frames = 0
            return self._publish_readiness(True, reason, login)
        except Exception:
            if self._readiness["reason"] != "window_check_failed":
                self.logger.exception("WeChat window check failed")
            self._reset_session_recovery()
            return self._publish_readiness(running, "window_check_failed", "unknown")

    def _inspect_window(
        self, wid: str, geometry: dict, *, activate: bool, recover: bool,
    ) -> tuple[str, str]:
        active = self._active_window_id()
        if active and active != wid and self._is_main_process_window(active) is True:
            # An owned foreground dialog can cover the selected chat window.
            popup_geometry = self._window_geometry(active)
            if popup_geometry:
                login = self._inspect_login_window(active, popup_geometry, recover=recover)
                if login != "unknown":
                    return login, login
            return "window_obscured", "unknown"

        if active != wid and activate:
            result = _run("xdotool", "windowactivate", "--sync", wid)
            if result.returncode == 0:
                active = self._active_window_id()
        if active != wid:
            return "window_not_focused", "unknown"

        above = self._window_above(wid, geometry)
        if above:
            if self._is_main_process_window(above) is True:
                bounds = self._window_geometry(above)
                login = self._inspect_login_window(above, bounds, recover=False) if bounds else "unknown"
                if login != "unknown":
                    return login, login
            return "window_obscured", "unknown"

        if not self._is_main_wechat_window(geometry):
            login = self._inspect_login_window(wid, geometry, recover=recover)
            return (login if login != "unknown" else "login_window_unrecognized"), login

        w, h = self._target_window_size()
        if not w or not h or not self._geometry_matches_target(geometry, w, h):
            reason = self.last_window_state if not w or not h else "geometry_drift"
            # Look at the original window before attempting a resize.
            shot = self.image_processor.take_screenshot(
                region=[geometry["x"], geometry["y"], geometry["width"], geometry["height"]],
            )
            login = self._login_from_unready_frame(shot)
            if login in self.LOGIN_GUIDANCE:
                return login, login
            if not w or not h or not recover or time.monotonic() < self._resize_retry_at:
                return reason, login
            self._publish_readiness(True, reason, login)
            geometry = self._recover_window_geometry(wid, w, h)
            if not self._geometry_matches_target(geometry, w, h):
                return "resize_mismatch", login
            time.sleep(self.window_show_delay)
            if self._active_window_id() != wid:
                return "window_not_focused", "unknown"

        shot = self.image_processor.take_screenshot(region=[0, 0, w, h])
        if shot is None or shot.size != (w, h):
            return "screenshot_unavailable", "unknown"
        reason, login = self._check_layout(shot, recover=recover)
        if reason == "ready":
            if self._active_window_id() != wid:
                return "window_not_focused", "unknown"
            live_geometry = self._window_geometry(wid)
            if live_geometry != geometry:
                self.actual_window_geometry = live_geometry or {}
                return "geometry_drift", login
        elif reason == "layout_unavailable" and login == "logged_in" and recover:
            if time.monotonic() >= self._resize_retry_at and self._active_window_id() == wid:
                self._publish_readiness(True, reason, login)
                if not self._geometry_matches_target(self._recover_window_geometry(wid, w, h), w, h):
                    return "resize_mismatch", login
        return reason, login

    def _recover_window_geometry(self, wid: str, width: int, height: int) -> Optional[dict]:
        self._resize_retry_at = time.monotonic() + self.RECOVERY_INTERVAL
        self._normalize_dpi()
        geometry = self._resize_wechat_window(wid, width, height)
        self.actual_window_geometry = geometry or {}
        return geometry

    def _find_vertical_boundary(
        self,
        screenshot,
        x_min: int,
        x_max: int,
        y_min: int,
        y_max: int,
    ) -> int:
        pixels = screenshot.load()
        width, height = screenshot.size
        x_min = max(1, min(x_min, width - 2))
        x_max = max(x_min + 1, min(x_max, width - 1))
        y_min = max(1, min(y_min, height - 2))
        y_max = max(y_min + 1, min(y_max, height - 1))
        best_x = x_min
        best_score = -1
        for x in range(x_min, x_max):
            score = 0
            samples = 0
            for y in range(y_min, y_max, 24):
                left = pixels[x - 1, y]
                right = pixels[x, y]
                score += sum(abs(int(right[i]) - int(left[i])) for i in range(3))
                samples += 1
            score = score / max(samples, 1)
            if score > best_score:
                best_score = score
                best_x = x
        return best_x

    def _find_horizontal_boundary(
        self,
        screenshot,
        y_min: int,
        y_max: int,
        x_min: int,
        x_max: int,
    ) -> Optional[int]:
        pixels = screenshot.load()
        width, height = screenshot.size
        x_min = max(1, min(x_min, width - 2))
        x_max = max(x_min + 1, min(x_max, width - 1))
        y_min = max(1, min(y_min, height - 2))
        y_max = max(y_min + 1, min(y_max, height - 1))
        best_y = y_min
        best_score = -1
        for y in range(y_min, y_max):
            score = 0
            samples = 0
            for x in range(x_min, x_max, 24):
                top = pixels[x, y - 1]
                bottom = pixels[x, y]
                score += sum(abs(int(bottom[i]) - int(top[i])) for i in range(3))
                samples += 1
            score = score / max(samples, 1)
            if score > best_score:
                best_score = score
                best_y = y
        # A flat or nearly flat frame has no usable boundary. Returning y_min
        # here turns a partially rendered window into a plausible cached layout.
        return best_y if best_score >= 6 else None

    def _find_full_width_horizontal_boundary(
        self,
        screenshot,
        y_min: int,
        y_max: int,
        x_min: int,
        x_max: int,
        diff_threshold: int = 18,
        min_strong_ratio: float = 0.55,
        min_avg_diff: float = 20,
    ) -> Optional[int]:
        pixels = screenshot.load()
        width, height = screenshot.size
        x_min = max(1, min(x_min, width - 2))
        x_max = max(x_min + 1, min(x_max, width - 1))
        y_min = max(1, min(y_min, height - 2))
        y_max = max(y_min + 1, min(y_max, height - 1))

        best_y = None
        best_score = -1
        for y in range(y_min, y_max):
            total = 0
            strong = 0
            samples = 0
            for x in range(x_min, x_max, 4):
                top = pixels[x, y - 1]
                bottom = pixels[x, y]
                diff = sum(abs(int(bottom[i]) - int(top[i])) for i in range(3))
                total += diff
                samples += 1
                if diff > diff_threshold:
                    strong += 1
            if not samples:
                continue
            strong_ratio = strong / samples
            avg_diff = total / samples
            if strong_ratio < min_strong_ratio or avg_diff < min_avg_diff:
                continue
            score = strong_ratio * 1000 + avg_diff
            if score > best_score:
                best_score = score
                best_y = y
        return best_y

    def init_chat_window(self) -> bool:
        with self.ui_lock:
            return self._init_chat_window_locked()

    def _init_chat_window_locked(self) -> bool:
        return self._check_window_locked(activate=True, recover=True)

    @staticmethod
    def _startup_label(text: str) -> str:
        return "".join(str(text).split()).casefold()

    def _chat_session_list_visible(self, screenshot, left: int) -> bool:
        rows = self.ocr_processor.process_image(
            image=screenshot.crop((65, 35, left, min(screenshot.height, 650))),
            local_only=True,
        )
        search_visible = any(
            self._startup_label(row.get("label", "")) in {"搜索", "search", "q搜索", "qsearch"}
            and row.get("pixel_bbox", [0, 999])[1] < 40
            for row in rows
        )
        session_visible = any(
            row.get("label", "").strip() and row.get("pixel_bbox", [0, 0])[1] > 50
            for row in rows
        )
        return search_visible and session_visible

    def _empty_startup_pane(self, screenshot, left: int) -> bool:
        """Recognize the centered watermark on an otherwise uniform welcome pane."""
        w, h = screenshot.size
        pixels = screenshot.load()
        background = pixels[w - 20, 120][:3]
        if min(background) < 230 or max(background) - min(background) > 8:
            return False
        cx, cy = (left + w) // 2, (32 + h) // 2
        marker = outside = changed = 0
        for y in range(40, h - 8, 8):
            for x in range(left + 8, w - 8, 8):
                rgb = pixels[x, y][:3]
                diff = max(abs(int(rgb[i]) - int(background[i])) for i in range(3))
                if abs(x - cx) <= 65 and abs(y - cy) <= 55:
                    marker += diff >= 12 and max(rgb) - min(rgb) < 8
                else:
                    outside += 1
                    changed += diff > 8
        return 15 <= marker <= 150 and bool(outside) and changed / outside <= 0.002

    def _login_from_unready_frame(self, screenshot) -> str:
        if screenshot is None:
            return "unknown"
        w, h = screenshot.size
        if w >= 500 and h >= 400:
            left = self._find_vertical_boundary(screenshot, 260, 380, 40, h - 40)
            if self._chat_session_list_visible(screenshot, left):
                return "logged_in"
        return self._read_login_prompt(screenshot)[0]

    def _startup_title_matches(self, screenshot, target: str) -> bool:
        left = self.MSG_TOP_X
        shot = screenshot.crop((left + 10, 35, screenshot.width - 10, 80))
        rows = self.ocr_processor.process_image(image=shot, local_only=True)
        return any(self._startup_label(row.get("label", "")) == self._startup_label(target)
                   for row in rows)

    def _click_startup_candidate(self, wid: str, target: str, region: list[int], box) -> bool:
        x1, y1, x2, y2 = box
        if not (0 <= x1 < x2 <= region[2] and 0 <= y1 < y2 <= region[3]):
            return False
        if self._active_window_id() != wid or self._is_main_process_window(wid) is not True:
            return False
        x = region[0] + int((x1 + x2) / 2)
        y = region[1] + int((y1 + y2) / 2)
        if _run("xdotool", "mousemove", str(x), str(y), "click", "1").returncode:
            return False
        self._startup_pending_session = target
        self._startup_pending_until = time.monotonic() + self.RECOVERY_INTERVAL
        self.logger.info("Opened startup session candidate; waiting for title and layout verification")
        return True

    def _startup_search_matches(self, rows: list[dict], target: str) -> list:
        functions = {"功能", "functions"}
        chats = {"最近", "最近使用", "recent", "聊天", "chats", "联系人", "contacts", "群聊", "groupchats"}
        other_sections = {"聊天记录", "chathistory", "网络搜索", "搜索网络", "网页", "websearch", "小程序", "公众号"}
        sections = functions | chats | other_sections
        allowed = functions if target in {"文件传输助手", "filetransferassistant"} else chats
        current = ""
        section_bottom = 0
        matches = []
        for row in sorted(rows, key=lambda item: item["pixel_bbox"][1]):
            label = self._startup_label(row.get("label", ""))
            box = row["pixel_bbox"]
            if label in sections:
                current, section_bottom = label, box[3]
            elif label == target and current in allowed and box[1] >= section_bottom:
                # The file helper occupies the first row in the Functions section.
                if current not in functions or box[1] - section_bottom <= 80:
                    matches.append(box)
        return matches

    def _open_startup_session(self, target: str, screenshot, left: int) -> bool:
        """Prefer the visible chat list, then look for a labeled local search result."""
        wid = self.window_id
        if not wid or self._active_window_id() != wid or self._is_main_process_window(wid) is not True:
            return False
        expected = self._startup_label(target)
        sidebar = [65, 85, left - 65, min(screenshot.height, 800) - 85]
        shot = screenshot.crop((sidebar[0], sidebar[1], left, sidebar[1] + sidebar[3]))
        matches = [row["pixel_bbox"] for row in self.ocr_processor.process_image(image=shot, local_only=True)
                   if self._startup_label(row.get("label", "")) == expected]
        if matches:
            return len(matches) == 1 and self._click_startup_candidate(wid, target, sidebar, matches[0])

        search_started = selected = False
        try:
            if _run("xdotool", "key", "ctrl+f").returncode:
                return False
            search_started = True
            time.sleep(self.action_delay)
            if not set_clipboard_text(target):
                return False
            if self._active_window_id() != wid:
                return False
            if _run("xdotool", "key", "ctrl+a").returncode:
                return False
            if _run("xdotool", "key", "ctrl+v").returncode:
                return False
            time.sleep(max(self.switch_contact_delay, 0.5))
            region = [45, 65, min(420, screenshot.width - 45), min(740, screenshot.height - 65)]
            shot = self.image_processor.take_screenshot(region=region)
            if shot is None:
                return False
            rows = self.ocr_processor.process_image(image=shot, local_only=True)
            matches = self._startup_search_matches(rows, expected)
            if len(matches) != 1:
                self.logger.warning("Startup session search has no unique exact local match")
                return False
            selected = self._click_startup_candidate(wid, target, region, matches[0])
            return selected
        finally:
            # Once a result opens, Escape hides the main WeChat window.
            if search_started and not selected and self._active_window_id() == wid:
                _run("xdotool", "key", "Escape")

    def _check_layout(self, screenshot, *, recover: bool) -> tuple[str, str]:
        now = time.monotonic()
        if self._init_window_part_size(screenshot):
            pending = self._startup_pending_session
            if pending and not self._startup_title_matches(screenshot, pending):
                if now >= self._startup_pending_until:
                    self._reset_session_recovery()
                return "session_title_unconfirmed", "logged_in"
            self._reset_session_recovery()
            self._set_current_layout(*screenshot.size)
            return "ready", "logged_in"

        login = self._login_from_unready_frame(screenshot)
        if login in self.LOGIN_GUIDANCE:
            return login, login

        if self._startup_pending_session:
            if now < self._startup_pending_until:
                return "session_title_unconfirmed", "logged_in"
            self._reset_session_recovery()

        if login != "logged_in":
            return "layout_unavailable", login

        _, h = screenshot.size
        left = self._find_vertical_boundary(screenshot, 260, 380, 40, h - 40)
        if not self._empty_startup_pane(screenshot, left):
            return "layout_unavailable", login

        target = str(self.rpa_config.get("layout", {}).get("startup_session", "文件传输助手") or "").strip()
        if not recover or not target or any(char in target for char in "\r\n\t"):
            return "welcome_pane", login
        self._startup_empty_frames += 1
        if self._startup_empty_frames >= 2 and now >= self._startup_retry_at:
            self._startup_empty_frames = 0
            self._startup_retry_at = now + self.RECOVERY_INTERVAL
            self._publish_readiness(True, "welcome_pane", login)
            if self._open_startup_session(target, screenshot, left):
                return "session_title_unconfirmed", login
        return "welcome_pane", login

    def _init_window_part_size(self, screenshot=None) -> bool:
        w, h = self.size_config.width, self.size_config.height
        if screenshot is None:
            screenshot = self.image_processor.take_screenshot(region=[0, 0, w, h])
        layout = self._detect_layout(screenshot, w, h)
        if layout is None:
            self._invalidate_layout()
            return False

        sidebar, session_right, title_bottom, input_top = layout
        msg_width, msg_height = w - session_right, input_top - title_bottom
        changed = (self.MSG_TOP_X, self.MSG_TOP_Y, self.MSG_WIDTH, self.MSG_HEIGHT) != (
            session_right, title_bottom, msg_width, msg_height,
        )
        if changed:
            self._invalidate_layout()
        self.SIDE_BAR_WIDTH = sidebar
        self.SESSION_LIST_WIDTH = session_right - sidebar
        self.MSG_TOP_X = session_right
        self.TITLE_BAR_HEIGHT = self.MSG_TOP_Y = title_bottom
        self.MSG_WIDTH = msg_width
        self.MSG_HEIGHT = msg_height
        configured_room_sidebar_width = int(self.rpa_config.get("room_side_bar_width", 360))
        self.ROOM_SIDE_BAR_WIDTH = max(260, min(configured_room_sidebar_width, w // 2))
        self.ICON_CONFIGS["send_button"]["position"] = [
            max(self.MSG_TOP_X + 80, w - 116),
            max(input_top + 20, h - 50),
            w - 18,
            h - 14,
        ]
        if changed:
            self.logger.info(
                "Layout OK: sidebar=%s sessions=%s msg=(%s,%s %sx%s) input_top=%s",
                self.SIDE_BAR_WIDTH, self.SESSION_LIST_WIDTH, self.MSG_TOP_X,
                self.MSG_TOP_Y, self.MSG_WIDTH, self.MSG_HEIGHT, input_top,
            )
            self._save_layout_debug(screenshot)
        return True

    def _detect_layout(self, screenshot, w: int, h: int) -> Optional[tuple[int, int, int, int]]:
        if screenshot is None or screenshot.size != (w, h):
            return None

        sidebar = self._find_vertical_boundary(screenshot, 45, 80, 40, h - 40)
        session_right = self._find_vertical_boundary(screenshot, 260, 380, 40, h - 40)
        if not 45 <= sidebar <= 80:
            sidebar = 60
        if session_right <= sidebar + 180:
            session_right = min(w - 300, sidebar + 265)

        title_bottom = self._find_full_width_horizontal_boundary(
            screenshot,
            52,
            82,
            session_right + 10,
            w - 10,
            diff_threshold=8,
            min_strong_ratio=0.45,
            min_avg_diff=6,
        )
        if title_bottom is None:
            title_bottom = self._find_horizontal_boundary(
                screenshot,
                52,
                82,
                session_right + 10,
                w - 10,
            )
        if title_bottom is None or not 52 <= title_bottom <= 82:
            return None

        input_top = self._find_full_width_horizontal_boundary(
            screenshot,
            max(title_bottom + 200, h - 360),
            h - 80,
            session_right + 10,
            w - 10,
        )
        if input_top is None:
            input_top = self._find_horizontal_boundary(
                screenshot,
                max(title_bottom + 200, h - 360),
                h - 80,
                session_right + 10,
                w - 10,
            )
        if input_top is None or not self._input_height_is_valid(h, input_top):
            return None
        if h - 50 - input_top < self.MIN_EDITOR_INTERIOR_HEIGHT:
            return None

        msg_width = w - session_right
        msg_height = input_top - title_bottom
        if msg_width < 300 or msg_height < 300:
            self.logger.error(
                "Invalid WeChat layout: msg region %sx%s at %s,%s",
                msg_width,
                msg_height,
                session_right,
                title_bottom,
            )
            return None

        return sidebar, session_right, title_bottom, input_top

    def _save_layout_debug(self, screenshot):
        try:
            output = "/config/runtime_images/layout_debug.png"
            boxes = [
                {
                    "pixel_bbox": [0, 0, self.SIDE_BAR_WIDTH, self.size_config.height],
                    "content": "side_bar",
                    "label": "side_bar",
                },
                {
                    "pixel_bbox": [
                        self.SIDE_BAR_WIDTH,
                        0,
                        self.SIDE_BAR_WIDTH + self.SESSION_LIST_WIDTH,
                        self.size_config.height,
                    ],
                    "content": "session_list",
                    "label": "session_list",
                },
                {
                    "pixel_bbox": [
                        self.MSG_TOP_X,
                        self.MSG_TOP_Y,
                        self.MSG_TOP_X + self.MSG_WIDTH,
                        self.MSG_TOP_Y + self.MSG_HEIGHT,
                    ],
                    "content": "message_region",
                    "label": "message_region",
                },
                {
                    "pixel_bbox": self.ICON_CONFIGS["send_button"]["position"],
                    "content": "send_button",
                    "label": "send_button",
                },
            ]
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            self.image_processor.draw_boxes_on_screen(screenshot.copy(), boxes, output_path=output)
            self.logger.info("Layout debug image saved: %s", output)
        except Exception as e:
            self.logger.warning(f"Failed to save layout debug image: {e}")

    def close_all_windows(self):
        for window in self._all_wechat_windows():
            if window.id == self.window_id:
                continue
            is_wechat_popup = self._is_wechat_popup_window(window)
            is_named_dialog = any(
                text in window.title
                for text in ("群公告", "添加群成员", "邀请", "移出群成员", "删除成员")
            )
            if not is_wechat_popup and not is_named_dialog:
                continue
            try:
                window.close()
                time.sleep(0.1)
            except Exception:
                continue
        return True

    def open_close_sidebar(self, close: bool = False):
        try:
            w = int(self.size_config.width)
            opened = self._is_room_sidebar_open()
            if close and not opened:
                return True
            if not close and opened:
                return True
            x = max(0, w - 30)
            y = max(36, int(self.TITLE_BAR_HEIGHT or 52) - 10)
            human_like_mouse_move(x, y)
            pyautogui.click()
            time.sleep(self.side_bar_delay)
            return True
        except Exception as exc:
            self.logger.warning("Failed to toggle room sidebar: %s", exc)
            return False

    def get_message_region(self) -> Optional[list]:
        if self.current_window:
            return [self.current_window.get(k) for k in ["MSG_TOP_X","MSG_TOP_Y","MSG_WIDTH","MSG_HEIGHT"]]
        return None

    def switch_session(self, target: str) -> bool:
        self.logger.info("Switching session to: %s", target)
        time.sleep(self.action_delay); _xdotool("key", "ctrl+f")
        time.sleep(self.action_delay)
        if not set_clipboard_text(target): return False
        time.sleep(self.action_delay); _xdotool("key", "ctrl+v")
        time.sleep(self.action_delay); _xdotool("key", "Return")
        time.sleep(self.switch_contact_delay)
        self.last_switch_session = target
        self.last_switch_session_time = time.time()
        self.current_session_name = target
        return True

    def get_current_session_name(self) -> str:
        return self.current_session_name or self.last_switch_session or ""

    def get_current_session_id(self) -> str:
        return self.get_current_session_name()

    def activate_input_box(self, offset_x: int = 0) -> bool:
        """Focus the editor interior, above the bottom toolbar and send button."""
        try:
            send_button = self.get_icon_position("send_button")
            if not send_button:
                return False
            top = self.MSG_TOP_Y + self.MSG_HEIGHT
            bottom = int(send_button[1])
            if self.MSG_WIDTH <= 100 or bottom - top < self.MIN_EDITOR_INTERIOR_HEIGHT:
                self.logger.warning("Cannot locate a safe editor interior")
                return False
            x = self.MSG_TOP_X + max(20, min(80 + offset_x, self.MSG_WIDTH - 20))
            y = (top + bottom) // 2
            # The old (MSG_TOP_X + 50, send_y) point lies on the toolbar;
            # a small layout shift can turn it into an attachment/icon click.
            result = _run("xdotool", "mousemove", str(x), str(y), "click", "1")
            if result.returncode != 0:
                return False
            time.sleep(self.action_delay)
            return True
        except Exception as exc:
            self.logger.warning("Failed to focus editor: %s", exc)
            return False

    def get_icon_position(self, icon_name: str) -> Optional[list]:
        return self.ICON_CONFIGS.get(icon_name, {}).get("position")
