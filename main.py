"""
AstrBot 服务器状态监控插件 (astrbot_plugin_server_status)
"""

import asyncio
import json
import os
import platform
import subprocess
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

_IS_DOCKER = os.path.exists("/.dockerenv")
_HOST_ROOT = "/host" if _IS_DOCKER else ""


class DataPoint:
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

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self.logger = logger

        self._data_points: deque = deque(maxlen=DEFAULT_MAX_POINTS)
        self._collect_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_net: Optional[Tuple[int, int, float]] = None

        self._gpu_available = False
        self._nvml_init = False
        self._gpu_via_smi = False

        self._pw_browser = None
        self._pw = None
        self._render_lock = asyncio.Lock()
        self._checkstatus_lock = asyncio.Lock()

        self._hostname = platform.node()
        self._arch = platform.machine()
        self._py_ver = platform.python_version()
        self._os_name, self._os_kernel = self._get_os_info()
        self._template_dir = Path(__file__).parent / "templates"

    def _cfg(self, key: str, default=None):
        if not self.config:
            return default
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return getattr(self.config, key, default)

    @property
    def _interval(self) -> int:
        return int(self._cfg("collect_interval_seconds", DEFAULT_INTERVAL))
    @property
    def _max_pts(self) -> int:
        return int(self._cfg("max_data_points", DEFAULT_MAX_POINTS))
    @property
    def _gpu_on(self) -> bool:
        return bool(self._cfg("enable_gpu_monitor", True))
    @property
    def _temp_on(self) -> bool:
        return bool(self._cfg("enable_temperature_monitor", True))

    @property
    def _theme(self) -> str:
        mode = str(self._cfg("theme_mode", "dark"))
        if mode != "auto":
            return mode
        now = datetime.now()
        try:
            lt = str(self._cfg("theme_switch_light_time", "06:00"))
            dt = str(self._cfg("theme_switch_dark_time", "18:00"))
            lh, lm = map(int, lt.split(":"))
            dh, dm = map(int, dt.split(":"))
            ln = lh * 60 + lm
            dn = dh * 60 + dm
            nn = now.hour * 60 + now.minute
            return "light" if ln <= nn < dn else "dark"
        except Exception:
            return "dark"

    @staticmethod
    def _get_os_info() -> Tuple[str, str]:
        system = platform.system()
        dp = "🐳 " if _IS_DOCKER else ""
        if system == "Windows":
            r = platform.release()
            v = platform.version()
            try:
                res = subprocess.run(["wmic","os","get","Caption","/value"], capture_output=True, text=True, timeout=5)
                if res.returncode == 0:
                    for line in res.stdout.splitlines():
                        if "Caption=" in line:
                            return f"{dp}{line.split('=',1)[1].strip()}", f"{r} (build {v.split('.')[2]})"
            except Exception:
                pass
            return f"{dp}Windows {r}", v
        elif system == "Linux":
            osn = "Linux"
            try:
                res = subprocess.run(["cat","/etc/os-release"], capture_output=True, text=True, timeout=3)
                if res.returncode == 0:
                    name, ver = "", ""
                    for line in res.stdout.splitlines():
                        if line.startswith("PRETTY_NAME="):
                            osn = line.split("=",1)[1].strip().strip('"').strip("'"); break
                        if line.startswith("NAME="): name = line.split("=",1)[1].strip().strip('"').strip("'")
                        if line.startswith("VERSION_ID="): ver = line.split("=",1)[1].strip().strip('"').strip("'")
                    if not osn or osn == "Linux": osn = f"{name} {ver}" if name else "Linux"
            except Exception:
                pass
            return f"{dp}{osn}", platform.release()
        elif system == "Darwin":
            return f"macOS {platform.mac_ver()[0]}", platform.release()
        return dp + system, platform.release()

    @staticmethod
    def _fmt(b: int) -> str:
        if b < 1024: return f"{b} B"
        if b < 1024**2: return f"{b/1024:.1f} KB"
        if b < 1024**3: return f"{b/1024**2:.1f} MB"
        if b < 1024**4: return f"{b/1024**3:.2f} GB"
        return f"{b/1024**4:.2f} TB"

    @staticmethod
    def _spd(bps: float) -> str:
        if bps < 1024: return f"{bps:.1f} B"
        if bps < 1024**2: return f"{bps/1024:.1f} KB"
        if bps < 1024**3: return f"{bps/1024**2:.1f} MB"
        return f"{bps/1024**3:.2f} GB"

    @staticmethod
    def _upt(sec: float) -> str:
        d, r = divmod(int(sec), 86400)
        h, m = divmod(r, 3600)
        m, _ = divmod(m, 60)
        parts = []
        if d: parts.append(f"{d}天")
        if h: parts.append(f"{h}时")
        if m: parts.append(f"{m}分")
        return " ".join(parts) or "不到1分钟"

    def _init_gpu(self):
        if not self._gpu_on:
            self._gpu_available = False
            return
        try:
            import pynvml
            pynvml.nvmlInit()
            if pynvml.nvmlDeviceGetCount() > 0:
                self._gpu_available = True
                self._nvml_init = True
                self._gpu_via_smi = False
                self.logger.info("GPU监控已初始化 (pynvml)")
                return
            self._nvml_init = True
        except Exception:
            pass
        if _IS_DOCKER:
            try:
                res = subprocess.run(["nvidia-smi","--query-gpu=index,name","--format=csv,noheader"], capture_output=True, text=True, timeout=10)
                if res.returncode == 0 and res.stdout.strip():
                    self._gpu_available = True
                    self._gpu_via_smi = True
                    self.logger.info("GPU监控已初始化 (nvidia-smi)")
                    return
            except Exception:
                pass
        self._gpu_available = False
        self.logger.info("未检测到NVIDIA GPU")

    def _collect_gpu(self, dp: DataPoint):
        if not self._gpu_available:
            return
        if self._gpu_via_smi:
            self._collect_gpu_smi(dp)
        else:
            self._collect_gpu_nvml(dp)

    def _collect_gpu_nvml(self, dp: DataPoint):
        try:
            import pynvml
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            dp.gpu_percent = pynvml.nvmlDeviceGetUtilizationRates(h).gpu
            mi = pynvml.nvmlDeviceGetMemoryInfo(h)
            dp.gpu_mem_total = self._fmt(mi.total)
            dp.gpu_mem_used = self._fmt(mi.used)
            dp.gpu_mem_percent = round(mi.used/mi.total*100, 1)
            try:
                n = pynvml.nvmlDeviceGetName(h)
                dp.gpu_name = n.decode('utf-8') if isinstance(n, bytes) else n.strip()
            except Exception:
                dp.gpu_name = "NVIDIA GPU"
            if self._temp_on:
                try:
                    dp.gpu_temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
                except Exception:
                    pass
        except Exception:
            pass

    def _collect_gpu_smi(self, dp: DataPoint):
        try:
            res = subprocess.run(["nvidia-smi","--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu","--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
            if res.returncode != 0 or not res.stdout.strip():
                return
            p = [x.strip() for x in res.stdout.splitlines()[0].split(", ")]
            if len(p) >= 6:
                dp.gpu_name = p[1]
                dp.gpu_percent = int(p[2]) if p[2].isdigit() else 50
                mu = int(p[3]) if p[3].isdigit() else 0
                mt = int(p[4]) if p[4].isdigit() else 0
                dp.gpu_mem_total = self._fmt(mt * 1024 * 1024)
                dp.gpu_mem_used = self._fmt(mu * 1024 * 1024)
                dp.gpu_mem_percent = round(mu/mt*100, 1) if mt else 0
                if self._temp_on and p[5].isdigit():
                    dp.gpu_temp = int(p[5])
        except Exception:
            pass

    async def _collect(self) -> DataPoint:
        dp = DataPoint()
        dp.timestamp = time.time()
        try:
            dp.cpu_percent = psutil.cpu_percent(interval=0.3)
            dp.cpu_freq = round(psutil.cpu_freq().current) if psutil.cpu_freq() else 0
            if self._temp_on:
                try:
                    for k in ("coretemp","cpu_thermal","k10temp","zenpower","acpitz"):
                        t = psutil.sensors_temperatures().get(k)
                        if t: dp.cpu_temp = round(t[0].current); break
                except Exception:
                    pass
            m = psutil.virtual_memory()
            dp.mem_percent = round(m.percent, 1)
            dp.mem_total = self._fmt(m.total)
            dp.mem_used = self._fmt(m.used)
            dp.mem_available = self._fmt(m.available)
            s = psutil.swap_memory()
            dp.swap_percent = round(s.percent, 1)
            dp.swap_total = self._fmt(s.total)
            dp.swap_used = self._fmt(s.used)
            self._collect_gpu(dp)
            n = psutil.net_io_counters()
            now = time.time()
            if self._last_net:
                ls, lr, lt = self._last_net
                el = now - lt
                if el > 0:
                    dp.net_sent = max(0, (n.bytes_sent - ls) / el)
                    dp.net_recv = max(0, (n.bytes_recv - lr) / el)
            self._last_net = (n.bytes_sent, n.bytes_recv, now)
        except Exception as e:
            self.logger.exception(f"采集失败: {e}")
        return dp

    async def _loop(self):
        self.logger.info("后台采集已启动")
        while self._running:
            try:
                self._data_points.append(await self._collect())
                ml = self._max_pts
                if self._data_points.maxlen != ml:
                    self._data_points = deque(self._data_points, maxlen=ml)
            except Exception as e:
                self.logger.exception(f"循环异常: {e}")
            await asyncio.sleep(self._interval)

    async def initialize(self):
        self.logger.info("初始化中...")
        self._init_gpu()
        self._data_points = deque(maxlen=self._max_pts)
        self._running = True
        self._collect_task = asyncio.create_task(self._loop())
        tag = " (Docker)" if _IS_DOCKER else ""
        self.logger.info(f"初始化完成{tag}")

    async def terminate(self):
        self.logger.info("卸载中...")
        self._running = False
        if self._collect_task:
            self._collect_task.cancel()
            try: await self._collect_task
            except asyncio.CancelledError: pass
            self._collect_task = None
        await self._close_pw()
        if self._nvml_init:
            try:
                import pynvml; pynvml.nvmlShutdown()
            except Exception: pass
        self._data_points.clear()

    async def _get_pw(self):
        if self._pw_browser is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            self._pw_browser = await self._pw.chromium.launch(
                headless=True,
                args=['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage','--disable-gpu','--single-process']
            )
            self.logger.info("Playwright 已启动")
        return self._pw_browser

    async def _close_pw(self):
        if self._pw_browser:
            try: await self._pw_browser.close()
            except Exception: pass
            self._pw_browser = None
        if self._pw:
            try: await self._pw.stop()
            except Exception: pass
            self._pw = None

    async def _render(self) -> Optional[Path]:
        async with self._render_lock:
            data = self._prepare_data()
            if not data:
                return None
            try:
                from jinja2 import Environment, FileSystemLoader
                tpl = Environment(loader=FileSystemLoader(str(self._template_dir))).get_template("status_template.html")
                html = tpl.render(**data)
            except Exception as e:
                self.logger.exception(f"模板渲染失败: {e}")
                return None

            browser = await self._get_pw()
            if not browser:
                return None

            data_dir = StarTools.get_data_dir(PLUGIN_NAME)
            os.makedirs(data_dir, exist_ok=True)
            out = Path(data_dir) / f"status_{int(time.time())}.png"
            self._cleanup(data_dir, keep=5)

            context = await browser.new_context(
                viewport={"width": 720, "height": 1},
                device_scale_factor=2,
            )
            try:
                page = await context.new_page()
                await page.set_content(html, wait_until="load")
                await asyncio.sleep(0.5)
                h = await page.evaluate("document.body.scrollHeight")
                await page.set_viewport_size({"width": 720, "height": h})
                await asyncio.sleep(0.3)
                await page.screenshot(path=str(out), full_page=True, type="png")
                self.logger.info(f"✅ 卡片已生成: {out}")
                return out
            except Exception as e:
                self.logger.exception(f"截图失败: {e}")
                return None
            finally:
                try:
                    await context.close()
                except Exception:
                    pass

    def _cleanup(self, d: str, keep: int = 5):
        try:
            files = sorted(
                [(os.path.getmtime(os.path.join(d, f)), os.path.join(d, f)) for f in os.listdir(d) if f.startswith("status_") and f.endswith(".png")],
                key=lambda x: x[0], reverse=True
            )
            for _, fp in files[keep:]:
                try: os.remove(fp)
                except Exception: pass
        except Exception:
            pass

    def _prepare_data(self) -> Optional[dict]:
        pts = list(self._data_points)
        if not pts:
            return None
        l = pts[-1]

        tss = [datetime.fromtimestamp(p.timestamp).strftime("%H:%M") for p in pts]
        t0 = datetime.fromtimestamp(pts[0].timestamp).strftime("%H:%M")
        t1 = datetime.fromtimestamp(pts[-1].timestamp).strftime("%H:%M")

        cv = [p.cpu_percent for p in pts]
        cpu_m = "未知"
        if platform.system() == "Linux":
            try:
                with open("/proc/cpuinfo") as f:
                    for line in f:
                        if "model name" in line: cpu_m = line.split(":",1)[1].strip(); break
            except Exception: pass
        else:
            cpu_m = platform.processor() or "未知"

        cpu_d = {
            "model": cpu_m,
            "cores": psutil.cpu_count(logical=False) or "N/A",
            "threads": psutil.cpu_count(logical=True) or "N/A",
            "freq": f"{l.cpu_freq:.0f}" if l.cpu_freq else "N/A",
            "current": round(l.cpu_percent, 1),
            "avg": round(sum(cv)/len(cv), 1) if cv else 0,
            "max": round(max(cv), 1) if cv else 0,
            "temp": round(l.cpu_temp) if l.cpu_temp is not None else None,
            "data": json.dumps(cv),
        }

        mv = [p.mem_percent for p in pts]
        mem_d = {
            "total": l.mem_total, "used": l.mem_used, "available": l.mem_available,
            "percent": l.mem_percent, "data": json.dumps(mv),
        }

        gpu_d = None
        if self._gpu_available and any(p.gpu_percent is not None for p in pts):
            gv = [p.gpu_percent or 0 for p in pts]
            gpu_d = {
                "name": l.gpu_name or "NVIDIA GPU",
                "mem_total": l.gpu_mem_total, "mem_used": l.gpu_mem_used,
                "percent": l.gpu_percent or 0,
                "temp": round(l.gpu_temp) if l.gpu_temp is not None else None,
                "data": json.dumps(gv),
            }

        sv = [p.swap_percent for p in pts]
        swap_d = {"total": l.swap_total, "used": l.swap_used, "free": "—", "percent": l.swap_percent}
        try:
            sw = psutil.swap_memory()
            swap_d["free"] = self._fmt(sw.free)
        except Exception: pass

        disks = self._get_disks_docker_friendly()

        ndv = [p.net_recv for p in pts]
        nuv = [p.net_sent for p in pts]
        net_d = {
            "download": self._spd(l.net_recv) if l.net_recv or l.net_sent else "0 B",
            "upload": self._spd(l.net_sent) if l.net_recv or l.net_sent else "0 B",
            "down_data": json.dumps(ndv),
            "up_data": json.dumps(nuv),
        }

        upt = time.time() - psutil.boot_time() if hasattr(psutil, 'boot_time') else 0
        sys_d = {
            "os": self._os_name, "kernel": self._os_kernel,
            "arch": self._arch, "uptime": self._upt(upt),
            "python": self._py_ver, "is_docker": _IS_DOCKER,
        }

        return {
            "theme": self._theme, "hostname": self._hostname,
            "time_range": f"{t0} ~ {t1}", "timestamps": json.dumps(tss),
            "cpu": cpu_d, "mem": mem_d, "gpu": gpu_d, "swap": swap_d,
            "disks": disks, "net": net_d, "sysinfo": sys_d,
        }

    @staticmethod
    def _get_disks_docker_friendly():
        """获取磁盘信息，兼容 Docker Desktop on Windows（overlay文件系统）"""
        disks = []
        seen = set()
        # 只跳过明显是虚拟/伪文件系统的类型，保留 overlay（Docker Desktop）
        skip_fs = ("proc","sysfs","devtmpfs","devpts","tmpfs","fusectl",
                   "cgroup_root","cgroup","debugfs","securityfs","pstore",
                   "bpf","autofs","mqueue","hugetlbfs","configfs","efivarfs")
        for part in psutil.disk_partitions():
            if part.fstype in skip_fs:
                continue
            # 跳过 /dev 下的各种虚拟设备，但保留 overlay 和真实磁盘
            if part.fstype in ("squashfs", "ramfs"):
                continue
            key = f"{part.device}:{part.mountpoint}"
            if key in seen:
                continue
            seen.add(key)
            try:
                u = psutil.disk_usage(part.mountpoint)
                disks.append({
                    "mountpoint": part.mountpoint, "total": ServerStatusPlugin._fmt(u.total),
                    "used": ServerStatusPlugin._fmt(u.used), "free": ServerStatusPlugin._fmt(u.free),
                    "percent": min(round(u.percent, 1), 100.0),
                })
            except (PermissionError, OSError):
                continue

        # 排序：/ 在第一个，然后按挂载点字母序
        disks.sort(key=lambda d: (0 if d["mountpoint"] == "/" else 1, d["mountpoint"]))
        return disks

    @filter.command("checkstatus")
    async def check_status(self, event: AstrMessageEvent):
        if not self._data_points:
            yield event.plain_result("⚠️ 数据采集中，请稍后再试...")
            return

        yield event.plain_result("⏳ 正在生成服务器状态卡片，请稍候...")

        try:
            self._data_points.append(await self._collect())
            out = await self._render()
            if out and out.exists():
                from astrbot.api.message_components import Image as ImageComp
                from astrbot.api.event import MessageChain
                mc = MessageChain()
                mc.chain.append(ImageComp(file=str(out)))
                await self.context.send_message(event.unified_msg_origin, mc)
                self.logger.info("✅ 状态卡片已发送")
            else:
                yield event.plain_result("❌ 生成失败，请检查 Playwright 是否安装。\n可执行: playwright install chromium")
        except Exception as e:
            self.logger.exception(f"失败: {e}")
            yield event.plain_result(f"❌ 失败: {str(e)}")
