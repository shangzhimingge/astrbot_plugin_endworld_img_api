<div align="center">
  
# 🖼️ 随机图片 (Random Image)

<img src="https://count.getloli.com/@astrbot_plugin_endworld_img_api?name=astrbot_plugin_endworld_img_api&theme=booru-jaypee&padding=7&offset=0&align=center&scale=1&pixelated=0&darkmode=auto" alt="count" />

[![Version](https://img.shields.io/badge/version-v6.5.1-blue.svg)](https://github.com/shangzhimingge/astrbot_plugin_endworld_img_api) [![License](https://img.shields.io/badge/license-GPLv3-green.svg)](LICENSE) [![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.24.1-orange.svg)](https://github.com/AstrBotDevs/AstrBot) [![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

一个为 AstrBot 打造的**高并发、强安全**的自定义 API 随机图片分发插件。

<img width="1408" height="768" alt="Plugin Logo" src="https://github.com/user-attachments/assets/8a5c7712-ff32-44d3-b051-7b881f3e16a2" />
</div>

## 🌟 核心特性一览

* **⚡ 并发革命与连接池复用**：彻底抛弃阻塞式串行下载。底层采用全局单例 `ClientSession` 维持 TCP 连接池，多图请求由 `asyncio.gather` 并发执行。**现在，获取 10 张图的速度与获取 1 张图完全相同！**
* **🚀 轮询池架构与防拦截**：支持 `指令 + 空格 + 数量` 批量出图。采用高效的 Round-Robin 算法调度多个图源，遭遇风控或死链时智能无缝切换并重新抽卡。
* **🛡️ 军工级 SSRF 防御体系**：接管 HTTP `30x` 重定向，原生 `ipaddress` 完美识别各种异形 IP 绕过。同时严格限制 DNS 慢速解析超时，并**完美兼容 Clash/Surge 等代理的 Fake-IP (198.18.x.x) 环境**。
* **🧠 极致性能释放 (拯救主线程)**：所有 CPU 密集型任务（Pillow 图片压缩）均委派至独立线程池 (`asyncio.to_thread`)，彻底告别高并发发图导致的机器人卡顿。内置 `mimetypes` 精准探测文件扩展名。
* **🎛️ 跨平台底层协议兼容**：完美支持 QQ 平台 (OneBot/NapCat) 的原生合并转发与底层撤回机制，而在 TG/微信 等其他平台也能优雅降级为框架标准发送，真正做到全平台制霸。
* **👻 完美内存与协程管理**：严苛的惰性缓存清理策略，外加后台任务强引用池设计，彻底杜绝 Python 垃圾回收 (GC) 导致的异步任务意外中断与资源泄漏。

---

## 📦 安装方法

1. 下载插件代码压缩包，解压后重命名文件夹为 `astrbot_plugin_endworld_img_api`。
2. 将文件夹放置于 AstrBot 的 `data/plugins/` 目录下。
3. 安装该插件必需的依赖库（在终端运行）：

```bash
pip install aiofiles aiohttp Pillow
```

4. 重启 AstrBot 即可自动加载并在 Web 面板生成高级配置项。

---

## 🚀 使用说明 & 配置指南

### WebUI 配置中心

在 AstrBot WebUI 中打开“插件”详情，进入本插件的 **settings / 配置中心** Page。该页面可直接完成全部插件配置并同步到运行中的插件：

* **图源管理**：新增、删除和排序图源，编辑触发词、API 地址、群号名单、发送方式与撤回时间。
* **行内 API 检测**：每个 API 输入框旁都有“检测”按钮；检测使用输入框当前值，无需先保存，并显示 HTTP 状态、内容类型与耗时。
* **全局设置**：配置批量转发、最大张数、失败重试、冷却、图片压缩、SSL 验证与回复风格。
* **状态概览**：查看插件版本、图源数量、冷却记录、网络会话与最近保存时间。
* **导入与导出**：导入 UTF-8 JSON 后先显示变更预览，确认后才写入；也可下载当前完整配置。
* **保存保护**：后端会再次校验所有字段，保存失败时恢复原配置；离开页面前会提示尚未保存的更改。
* **升级兼容**：旧版多余字段会自动清理，新增字段按 schema 默认值补齐；已填写字段仍执行严格校验。
* **稳定编辑体验**：列表增删和图源排序不会折叠已展开卡片；新图源直接删除，已保存图源使用页面内确认对话框。

页面使用中等饱和度蓝粉渐变主题，跟随 AstrBot 的亮色/暗色模式，并适配窄屏与键盘操作。新增 Page 后需重载一次插件；静态页面更新通常刷新即可。

### 1. 添加与管理图源

进入 AstrBot 管理后台 -> 插件配置 -> 找到 `astrbot_plugin_endworld_img_api`。

* **触发指令**：输入想要的触发词，多个请分行或使用列表（如 `壁纸`, `来点图`）。
* **API 地址**：**强烈建议填写多个 API**，插件会在前一个失败时自动在池中尝试下一个。
* **使用合并转发发送**：勾选后，该图源的单张图片也会以合并转发卡片发出，极致防风控。
* **分群管理**：按需下拉选择 `无限制`、`黑名单` 或 `白名单`，并填入适用群号。
* **自动撤回时间**：填入 `30` 代表 30秒后撤回；`0` 则不撤回。

### 2. 用户日常交互

* **单张获取**：发送配置的触发词（如 `涩涩`）。
* **批量获取**：发送 `触发词 + 空格 + 数量`（如 `涩涩 5`），机器人将一次性并发获取多张图片（根据配置决定是否自动转为合并转发）。

---

## ⚙️ 核心全局配置项

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| **批量获取强制合并转发** | `false` | 只要是批量请求，都会被打包成合并记录防刷屏（不影响独立图源中的设置）。 |
| **批量合并转发阈值** | `3` | 当一次请求数量 **≥** 此值时，自动打包为合并转发卡片。 |
| **发送失败重试次数** | `3` | 图片被风控或死链时的“最大换图重试次数”。 |
| **开启大图自动压缩** | `true` | 是否允许插件在独立线程池中压缩超大图片（防超时）。 |
| **压缩阈值 / 质量** | `5MB / 85` | 图片大于 5MB 时触发压缩，质量为 85。 |

---

## ⚠️ 注意事项 & 法律警告

* **合规声明**：本插件仅作为网络图片 API 的纯净分发与并发工具，不内置任何图源。
* **法律风险**：请严格遵守所在地法律法规及各大平台（QQ/微信）的运营规范，**严禁在公开群聊对接与传播非法、违禁或色情图片源**。使用者需自行承担因图源配置不当引发的封号或法律风险。

---

## 🛠️ 开发维护

* **版本号**：v6.5.1
* **衍生自**：[mccloud_img](https://github.com/MCYUNIDC/mccloud_img) (Author: MC云)
* **重构作者**：殇之冥歌
* **核心依赖**：`AstrBot Core v4.24.1+`

🎉 **Enjoy your high-quality random images!** 🌸
