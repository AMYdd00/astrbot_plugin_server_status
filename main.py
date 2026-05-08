"""
AstrBot 服务器状态监控插件 (astrbot_plugin_server_status)

功能：
- 后台每60秒自动采集硬件数据（CPU/内存/Swap/GPU/磁盘/网络/温度）
- 保留最近30条数据（约30分钟趋势）
- 使用 /checkstatus 命令生成 Apple PPT 风格状态卡片图片
- 跨平台支持 Windows / Linux / Docker
- 优雅降级：无GPU/无温度传感器时自动跳过对应模块

数据流：
  asyncio 后台采集循环 -> 环形缓冲区 (deque maxlen=30)
    -> /checkstatus 命令 -> Jinja2 渲染 HTML -> Playwright 截图 -> 发送图片
"""

import asyncio
import json
import os
import platform
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import psutil

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger


PLUGIN_NAME = "astrbot_plugin_server_status"
DEFAULT_INTERVAL = 60
DEFAULT_MAX_POINTS = 30


class DataPoint:
    """单次采样数据点"""
    __slots__ = (
        "timestamp", "cpu_percent", "cpu_temp", "cpu_freq",
        "mem_percent", "mem_used", "mem_total", "mem_available",
        "swap_percent", "swap_used", "swap_total",
        "gpu_percent", "gpu_mem_percent", "gpu_mem_used", "gpu_mem_total",
        "gpu_temp", "gpu_name",
        "net_sent", "net_recv",
    )

    def __init__(self):
        self.timestamp = time.time()
        self.cpu_percent = 0.0
        self.cpu_temp = None
        self.cpu_freq = 0.0
        self.mem_percent = 0.0
        self.mem_used = ""
        self.mem_total = ""
        self.mem_available = ""
        self.swap_percent = 0.0
        self.swap_used = ""
        self.swap_total = ""
        self.gpu_percent = None
        self.gpu_mem_percent = None
        self.gpu_mem_used = ""
        self.gpu_mem_total = ""
        self.gpu_temp = None
        self.gpu_name = ""
        self.net_sent = 0
        self.net_recv = 0


@register("astrbot_plugin_server_status", "AMYdd00", "服务器状态监控插件", "1.0.0")
class ServerStatusPlugin(Star):
    """Apple PPT 风格服务器状态监控插件"""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self.logger = logger

        # 环形缓冲区
        self._data_points: deque = deque(maxlen=DEFAULT_MAX_POINTS)

        # 后台采集任务
        self._collect_task: Optional[asyncio.Task] = None
        self._running = False

        # 上一次网络计数
        self._last_net_counters: Optional[Tuple[int, int, float]] = None

        # GPU 状态
        self._gpu_available = False
        self._nvml_initialized = False
        self._gpu_device_count = 0

        # Playwright
        self._playwright_browser = None
        self._playwright = None

        # 系统信息缓存
        self._sysinfo_hostname = platform.node()
        self._sysinfo_arch = platform.machine()
        self._sysinfo_python = platform.python_version()
        self._sysinfo_os, self._sysinfo_kernel = self._get_os_info()

        # 模板路径
        self._template_dir = Path(__file__).parent / "templates"

    def _cfg(self, key: str, default=None):
        if not self.config:
            return default
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return getattr(self.config, key, default)

    @property
    def _collect_interval(self) -> int:
        return int(self._cfg("collect_interval_seconds", DEFAULT_INTERVAL))

    @property
    def _max_points(self) -> int:
        return int(self._cfg("max_data_points", DEFAULT_MAX_POINTS))

    @property
    def _enable_gpu(self) -> bool:
        return bool(self._cfg("enable_gpu_monitor", True))

    @property
    def _enable_temp(self) -> bool:
        return bool(self._cfg("enable_temperature_monitor", True))

    @property
    def _theme_mode(self) -> str:
        return str(self._cfg("theme_mode", "dark"))

    # ========== 系统信息获取 ==========
    @staticmethod
    def _get_os_info() -> Tuple[str, str]:
        """获取操作系统名称和内核版本"""
        import subprocess
        system = platform.system()
        if system == "Windows":
            release = platform.release()
            version = platform.version()
            try:
                result = subprocess.run(
                    ["wmic", "os", "get", "Caption", "/value"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        if "Caption=" in line:
                            os_name = line.split("=", 1)[1].strip()
                            return os_name, f"{release} (build {version.split('.')[2]})"
            except Exception:
                pass
            return f"Windows {release}", version
        elif system == "Linux":
            os_name = "Linux"
            try:
                result = subprocess.run(
                    ["cat", "/etc/os-release"],
                    capture_output=True, text=True, timeout=3
                )
                if result.returncode == 0:
                    name = ""
                    version = ""
                    for line in result.stdout.splitlines():
                        if line.startswith("PRETTY_NAME="):
                            os_name = line.split("=", 1)[1].strip().strip('"').strip("'")
                            break
                        if line.startswith("NAME="):
                            name = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if line.startswith("VERSION_ID="):
                            version = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if not os_name or os_name == "Linux":
                        os_name = f"{name} {version}" if name else "Linux"
            except Exception:
                pass
            return os_name, platform.release()
        elif system == "Darwin":
            return f"macOS {platform.mac_ver()[0]}", platform.release()
        else:
            return system, platform.release()

    # ========== 格式化工具 ==========
    @staticmethod
    def _format_bytes(b: int) -> str:
        if b < 1024:
            return f"{b} B"
        elif b < 1024 ** 2:
            return f"{b / 1024:.1f} KB"
        elif b < 1024 ** 3:
            return f"{b / 1024 ** 2:.1f} MB"
        elif b < 1024 ** 4:
            return f"{b / 1024 ** 3:.2f} GB"
        else:
            return f"{b / 1024 ** 4:.2f} TB"

    @staticmethod
    def _format_speed(bps: float) -> str:
        if bps < 1024:
            return f"{bps:.1f} B"
        elif bps < 1024 ** 2:
            return f"{bps / 1024:.1f} KB"
        elif bps < 1024 ** 3:
            return f"{bps / 1024 ** 2:.1f} MB"
        else:
            return f"{bps / 1024 ** 3:.2f} GB"

    @staticmethod
    def _format_uptime(seconds: float) -> str:
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        minutes = int((seconds % 3600) // 60)
        parts = []
        if days > 0:
            parts.append(f"{days}天")
        if hours > 0:
            parts.append(f"{hours}时")
        if minutes > 0:
            parts.append(f"{minutes}分")
        if not parts:
            parts.append("不到1分钟")
        return " ".join(parts)

    # ========== GPU 初始化 ==========
    def _init_gpu(self):
        if not self._enable_gpu:
            self._gpu_available = False
            return
        try:
            import pynvml
            pynvml.nvmlInit()
            self._gpu_device_count = pynvml.nvmlDeviceGetCount()
            if self._gpu_device_count > 0:
                self._gpu_available = True
                self._nvml_initialized = True
                self.logger.info(f"GPU监控已初始化，检测到 {self._gpu_device_count} 个GPU设备")
            else:
                self._nvml_initialized = True
                self._gpu_available = False
                self.logger.info("未检测到NVIDIA GPU设备")
        except Exception as e:
            self._gpu_available = False
            self._nvml_initialized = False
            self.logger.info(f"GPU监控未启用 (pynvml: {e})")

    def _collect_gpu_info(self, dp: DataPoint):
        if not self._gpu_available or not self._nvml_initialized:
            return
        try:
            import pynvml
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            dp.gpu_percent = util.gpu
            mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            dp.gpu_mem_total = self._format_bytes(mem_info.total)
            dp.gpu_mem_used = self._format_bytes(mem_info.used)
            dp.gpu_mem_percent = round((mem_info.used / mem_info.total) * 100, 1) if mem_info.total > 0 else 0
            try:
                name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode('utf-8')
                dp.gpu_name = name.strip()
            except Exception:
                dp.gpu_name = "NVIDIA GPU"
            if self._enable_temp:
                try:
                    temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                    dp.gpu_temp = temp
                except Exception:
                    dp.gpu_temp = None
        except Exception:
            pass

    # ========== 数据采集 ==========
    async def _collect_data_point(self) -> DataPoint:
        dp = DataPoint()
        dp.timestamp = time.time()
        try:
            # CPU
            dp.cpu_percent = psutil.cpu_percent(interval=0.3)
            dp.cpu_freq = round(psutil.cpu_freq().current) if psutil.cpu_freq() else 0

            # CPU 温度
            if self._enable_temp:
                try:
                    temps = psutil.sensors_temperatures()
                    for key in ("coretemp", "cpu_thermal", "k10temp", "zenpower", "acpitz"):
                        if key in temps:
                            dp.cpu_temp = round(temps[key][0].current)
                            break
                except Exception:
                    pass

            # 内存
            mem = psutil.virtual_memory()
            dp.mem_percent = round(mem.percent, 1)
            dp.mem_total = self._format_bytes(mem.total)
            dp.mem_used = self._format_bytes(mem.used)
            dp.mem_available = self._format_bytes(mem.available)

            # Swap
            swap = psutil.swap_memory()
            dp.swap_percent = round(swap.percent, 1)
            dp.swap_total = self._format_bytes(swap.total)
            dp.swap_used = self._format_bytes(swap.used)

            # GPU
            self._collect_gpu_info(dp)

            # 网络
            net = psutil.net_io_counters()
            now = time.time()
            if self._last_net_counters:
                last_sent, last_recv, last_time = self._last_net_counters
                elapsed = now - last_time
                if elapsed > 0:
                    dp.net_sent = max(0, (net.bytes_sent - last_sent) / elapsed)
                    dp.net_recv = max(0, (net.bytes_recv - last_recv) / elapsed)
            self._last_net_counters = (net.bytes_sent, net.bytes_recv, now)
        except Exception as e:
            self.logger.exception(f"数据采集失败: {e}")
        return dp

    async def _collect_loop(self):
        """后台数据采集循环"""
        self.logger.info("服务器状态监控：后台采集已启动")
        while self._running:
            try:
                dp = await self._collect_data_point()
                self._data_points.append(dp)
                max_pts = self._max_points
                if self._data_points.maxlen != max_pts:
                    self._data_points = deque(self._data_points, maxlen=max_pts)
                self.logger.debug(f"[采集] CPU={dp.cpu_percent}% MEM={dp.mem_percent}% 数据点={len(self._data_points)}/{max_pts}")
            except Exception as e:
                self.logger.exception(f"采集循环异常: {e}")
            await asyncio.sleep(self._collect_interval)

    # ========== 插件生命周期 ==========
    async def initialize(self):
        self.logger.info("服务器状态监控插件初始化中...")
        self._init_gpu()
        self._data_points = deque(maxlen=self._max_points)
        self._running = True
        self._collect_task = asyncio.create_task(self._collect_loop())
        self.logger.info(f"初始化完成 (采集间隔={self._collect_interval}s, 保留={self._max_points}条)")

    async def terminate(self):
        self.logger.info("服务器状态监控插件卸载中...")
        self._running = False
        if self._collect_task:
            self._collect_task.cancel()
            try:
                await self._collect_task
            except asyncio.CancelledError:
                pass
            self._collect_task = None
        await self._close_playwright()
        if self._nvml_initialized:
            try:
                import pynvml
                pynvml.nvmlShutdown()
            except Exception:
                pass
        self._data_points.clear()
        self.logger.info("卸载完成")

    # ========== Playwright ==========
    async def _get_playwright_browser(self):
        if self._playwright_browser is None:
            try:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
                self._playwright_browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        '--no-sandbox',
                        '--disable-setuid-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-gpu',
                        '--single-process',
                    ]
                )
                self.logger.info("Playwright 浏览器已启动")
            except Exception as e:
                self.logger.exception(f"Playwright 启动失败: {e}")
                raise
        return self._playwright_browser

    async def _close_playwright(self):
        if self._playwright_browser:
            try:
                await self._playwright_browser.close()
            except Exception:
                pass
            self._playwright_browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    # ========== HTML 渲染 + 截图 ==========
    async def _render_and_capture(self) -> Optional[Path]:
        data = self._prepare_template_data()
        if not data:
            return None

        try:
            from jinja2 import Environment, FileSystemLoader
            env = Environment(loader=FileSystemLoader(str(self._template_dir)))
            template = env.get_template("status_template.html")
            html_content = template.render(**data)
        except Exception as e:
            self.logger.exception(f"HTML 模板渲染失败: {e}")
            return None

        browser = await self._get_playwright_browser()
        if not browser:
            return None

        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        os.makedirs(data_dir, exist_ok=True)
        output_path = Path(data_dir) / f"status_{int(time.time())}.png"
        self._cleanup_old_screenshots(data_dir, keep=5)

        try:
            page = await browser.new_page(
                viewport={"width": 840, "height": 1},
                device_scale_factor=2,
            )
            await page.set_content(html_content, wait_until="networkidle")
            await asyncio.sleep(0.3)
            page_height = await page.evaluate("document.documentElement.scrollHeight")
            await page.set_viewport_size({"width": 840, "height": page_height})
            await asyncio.sleep(0.2)
            await page.screenshot(path=str(output_path), full_page=True, type="png")
            await page.close()
            self.logger.info(f"状态卡片已生成: {output_path}")
            return output_path
        except Exception as e:
            self.logger.exception(f"截图生成失败: {e}")
            return None

    def _cleanup_old_screenshots(self, data_dir: str, keep: int = 5):
        try:
            files = []
            for f in os.listdir(data_dir):
                if f.startswith("status_") and f.endswith(".png"):
                    fp = os.path.join(data_dir, f)
                    files.append((os.path.getmtime(fp), fp))
            files.sort(key=lambda x: x[0], reverse=True)
            for _, fp in files[keep:]:
                try:
                    os.remove(fp)
                except Exception:
                    pass
        except Exception:
            pass

    # ========== 模板数据准备 ==========
    def _prepare_template_data(self) -> Optional[dict]:
        points = list(self._data_points)
        if not points:
            return None
        latest = points[-1]

        timestamps = [datetime.fromtimestamp(p.timestamp).strftime("%H:%M") for p in points]
        time_start = datetime.fromtimestamp(points[0].timestamp).strftime("%H:%M")
        time_end = datetime.fromtimestamp(points[-1].timestamp).strftime("%H:%M")

        # CPU
        cpu_values = [p.cpu_percent for p in points]
        cpu_avg = round(sum(cpu_values) / len(cpu_values), 1) if cpu_values else 0
        cpu_max = round(max(cpu_values), 1) if cpu_values else 0
        cpu_model = platform.processor() or "未知"
        if platform.system() == "Linux":
            try:
                with open("/proc/cpuinfo", "r") as f:
                    for line in f:
                        if "model name" in line:
                            cpu_model = line.split(":", 1)[1].strip()
                            break
            except Exception:
                pass
        cpu_data = {
            "model": cpu_model,
            "cores": psutil.cpu_count(logical=False) or "N/A",
            "threads": psutil.cpu_count(logical=True) or "N/A",
            "freq": f"{latest.cpu_freq:.0f}" if latest.cpu_freq else "N/A",
            "current": round(latest.cpu_percent, 1),
            "avg": cpu_avg,
            "max": cpu_max,
            "temp": round(latest.cpu_temp) if latest.cpu_temp is not None else None,
            "data": json.dumps(cpu_values),
        }

        # 内存
        mem_values = [p.mem_percent for p in points]
        mem_data = {
            "total": latest.mem_total,
            "used": latest.mem_used,
            "available": latest.mem_available,
            "percent": latest.mem_percent,
            "data": json.dumps(mem_values),
        }

        # GPU
        gpu_data = None
        if self._gpu_available and any(p.gpu_percent is not None for p in points):
            gpu_values = [p.gpu_percent or 0 for p in points]
            gpu_data = {
                "name": latest.gpu_name or "NVIDIA GPU",
                "mem_total": latest.gpu_mem_total,
                "mem_used": latest.gpu_mem_used,
                "percent": latest.gpu_percent or 0,
                "temp": round(latest.gpu_temp) if latest.gpu_temp is not None else None,
                "data": json.dumps(gpu_values),
            }

        # Swap
        swap_data = {
            "total": latest.swap_total,
            "used": latest.swap_used,
            "free": "—",
            "percent": latest.swap_percent,
        }
        try:
            swap = psutil.swap_memory()
            swap_data["free"] = self._format_bytes(swap.free)
        except Exception:
            pass

        # 磁盘
        disks = []
        try:
            for part in psutil.disk_partitions():
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    skip_fs = ("proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "fusectl",
                               "cgroup_root", "cgroup", "debugfs", "securityfs", "pstore",
                               "bpf", "autofs", "mqueue", "hugetlbfs", "configfs", "efivarfs", "overlay")
                    if part.fstype in skip_fs:
                        continue
                    disk_temp = None
                    if self._enable_temp and "nvme" in part.device.lower():
                        try:
                            import glob
                            for temp_file in glob.glob("/sys/class/nvme/nvme*/temperature"):
                                with open(temp_file, "r") as tf:
                                    temp_val = int(tf.read().strip()) // 1000
                                    if 0 < temp_val < 100:
                                        disk_temp = temp_val
                                        break
                        except Exception:
                            pass
                    disks.append({
                        "mountpoint": part.mountpoint,
                        "total": self._format_bytes(usage.total),
                        "used": self._format_bytes(usage.used),
                        "free": self._format_bytes(usage.free),
                        "percent": min(round(usage.percent, 1), 100.0),
                        "fstype": part.fstype,
                        "device": part.device,
                        "temp": disk_temp,
                    })
                except (PermissionError, OSError):
                    continue
        except Exception:
            disks = []

        # 网络
        net_down_values = [p.net_recv for p in points]
        net_up_values = [p.net_sent for p in points]
        net_data = {
            "download": self._format_speed(latest.net_recv) if latest.net_recv else "0 B",
            "upload": self._format_speed(latest.net_sent) if latest.net_sent else "0 B",
            "down_data": json.dumps(net_down_values),
            "up_data": json.dumps(net_up_values),
        }

        # 系统信息
        uptime_seconds = time.time() - psutil.boot_time() if hasattr(psutil, 'boot_time') else 0
        sysinfo_data = {
            "os": self._sysinfo_os,
            "kernel": self._sysinfo_kernel,
            "arch": self._sysinfo_arch,
            "uptime": self._format_uptime(uptime_seconds),
            "python": self._sysinfo_python,
        }

        return {
            "theme": self._theme_mode,
            "hostname": self._sysinfo_hostname,
            "time_range": f"{time_start} ~ {time_end}",
            "timestamps": json.dumps(timestamps),
            "cpu": cpu_data,
            "mem": mem_data,
            "gpu": gpu_data,
            "swap": swap_data,
            "disks": disks,
            "net": net_data,
            "sysinfo": sysinfo_data,
        }

    # ========== 命令处理器 ==========
    @filter.command("checkstatus")
    async def check_status(self, event: AstrMessageEvent):
        """生成服务器状态卡片"""
        if not self._data_points:
            yield event.plain_result("⚠️ 数据采集中，请稍后再试...")
            return

        yield event.plain_result("⏳ 正在生成服务器状态卡片，请稍候...")

        try:
            fresh_dp = await self._collect_data_point()
            self._data_points.append(fresh_dp)

            output_path = await self._render_and_capture()

            if output_path and output_path.exists():
                from astrbot.api.message_components import Image as ImageComp
                from astrbot.api.event import MessageChain
                msg_chain = MessageChain()
                msg_chain.chain.append(ImageComp(file=str(output_path)))
                await self.context.send_message(event.unified_msg_origin, msg_chain)
                self.logger.info(f"✅ 服务器状态卡片已发送")
            else:
                yield event.plain_result(
                    "❌ 状态卡片生成失败，请检查 Playwright 是否正确安装。\n"
                    "可尝试执行: playwright install chromium"
                )
        except Exception as e:
            self.logger.exception(f"生成状态卡片失败: {e}")
            yield event.plain_result(f"❌ 生成失败: {str(e)}")
