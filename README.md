# 🌟 AstrBot 服务器状态监控插件

**Apple PPT 风格**服务器状态监控插件，为 AstrBot 提供精美的硬件状态图片报告。

## ✨ 功能特点

- 📊 **命令触发**：发送 `/checkstatus` 即可生成状态卡片
- 🖥️ **CPU 监控**：型号、核心数、使用率、频率、温度（折线图）
- 💾 **内存监控**：总量、已用、可用、使用率（折线图）
- 🎮 **GPU 监控**：型号、显存、使用率、温度（折线图，NVIDIA GPU）
- 🔄 **Swap 监控**：总量、已用、使用率
- 💽 **磁盘监控**：各挂载点容量、使用率（进度条）
- 🌐 **网络监控**：上行/下行实时速率（折线图）
- 📋 **系统信息**：OS 版本、内核、架构、运行时间
- 🌡️ **温度监控**：CPU、GPU、NVMe SSD 温度（支持时显示）
- 🎨 **Apple PPT 风格**：磨砂玻璃卡片、深色渐变背景、高级感设计
- 🔄 **30 分钟趋势**：后台每 60 秒采集一次，保留 30 条数据

## 📷 效果预览

生成的卡片图片包含以下板块（从上到下）：

1. 🖥️ **CPU** — 折线图 + 型号/核心/频率/温度
2. 💾 **内存** — 折线图 + 容量信息
3. 🎮 **GPU** — 折线图（检测到 NVIDIA 显卡时显示）
4. 🔄 **Swap** — 容量信息
5. 💽 **存储** — 进度条列表
6. 🌐 **网络** — 双折线图（上下行）
7. 📋 **系统信息** — OS/内核/架构/运行时间

## 🚀 安装方法

### 方法一：AstrBot 面板安装

1. 将插件文件夹复制到 AstrBot 的 `addons/` 目录
2. 在 AstrBot 管理面板中启用插件
3. 重启 AstrBot

### 方法二：手动安装

```bash
# 1. 进入 AstrBot 插件目录
cd /path/to/AstrBot/addons

# 2. 克隆或复制插件
git clone https://github.com/AMYdd00/astrbot_plugin_server_status.git

# 3. 安装依赖
cd astrbot_plugin_server_status
pip install -r requirements.txt

# 4. 安装 Playwright 浏览器
playwright install chromium

# 5. 重启 AstrBot
```

## ⚙️ 配置说明

可在 AstrBot 管理面板中配置以下参数：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `collect_interval_seconds` | 60 | 数据采集间隔（秒） |
| `max_data_points` | 30 | 最大保留数据条数（决定折线图时间跨度） |
| `enable_gpu_monitor` | true | 是否启用 GPU 监控 |
| `enable_temperature_monitor` | true | 是否启用温度监控 |
| `theme_mode` | dark | 主题模式（dark/light） |

## 🎯 使用命令

```
/checkstatus    — 生成服务器状态卡片图片
```

## 🐳 Docker 部署

如果 AstrBot 运行在 Docker 中，需要确保 Playwright 可以正常使用：

```dockerfile
# 在 Dockerfile 中添加
RUN pip install playwright && playwright install chromium
RUN apt-get update && apt-get install -y \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2
```

## 🖥️ 跨平台支持

| 平台 | CPU | 内存 | GPU | 温度 | 磁盘温度 |
|------|-----|------|-----|------|---------|
| ✅ Windows | ✔️ | ✔️ | ✔️ (NVIDIA) | ⚠️ (需管理员) | ❌ |
| ✅ Linux | ✔️ | ✔️ | ✔️ (NVIDIA) | ✔️ | ✔️ (NVMe) |
| ✅ Docker | ✔️ | ✔️ | ✔️ (NVIDIA) | ✔️ | ✔️ (NVMe) |

## 📦 依赖

- `psutil` — 系统信息采集（跨平台）
- `jinja2` — HTML 模板渲染
- `pynvml` — NVIDIA GPU 监控（可选）
- `playwright` — HTML 转图片

## 📝 更新日志

### v1.0.0

- 初始版本发布
- 支持 CPU/内存/GPU/Swap/磁盘/网络/系统信息
- Apple PPT 风格磨砂玻璃卡片设计
- 后台 30 分钟趋势数据采集
- 跨平台兼容 Windows/Linux/Docker

## 📄 许可证

MIT License
