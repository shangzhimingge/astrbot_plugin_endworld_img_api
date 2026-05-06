# 🖼️ 随机图片 (Random Image)

[![Version](https://img.shields.io/badge/version-v6.1.1-blue.svg)](https://github.com/YourUsername/astrbot_plugin_endworld_img_api) [![License](https://img.shields.io/badge/license-GPLv3-green.svg)](LICENSE) [![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.11.4-orange.svg)](https://github.com/Soulter/AstrBot) [![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

一个为 AstrBot 打造的高性能、强安全的自定义 API 随机图片分发插件。

<img width="1408" height="768" alt="Plugin Logo" src="https://github.com/user-attachments/assets/8a5c7712-ff32-44d3-b051-7b881f3e16a2" />

## 🌟 核心特性一览

* **⚡ 异步线程池与性能释放**：内置智能大图压缩（Pillow 引擎），且所有 CPU 密集型任务均通过 `asyncio.to_thread` 委派至独立线程池，**彻底告别高并发发图导致的机器人卡顿与事件循环阻塞**。
* **🚀 轮询池架构与批量获取**：支持 `指令 + 空格 + 数量` 批量出图。底层采用高效的 Round-Robin（轮询）算法调度多个图源 API，在遭遇风控拦截或死链时智能无缝切换并重新抽卡。
* **🛡️ SSRF 防御体系**：不仅拦截常规的 `127.0.0.1`，更深度接管 HTTP `30x` 重定向，利用原生 `ipaddress` 完美识别十进制/十六进制等异形 IP 绕过攻击，同时完美兼容 Docker 与旁路由代理环境。
* **🌐 全能 API 兼容与破除缓存**：内置智能 JSON 寻址；独家动态时间戳与浏览器 UA 伪装，彻底解决 CDN 缓存导致的“每次都抽到同一张图”的痛点。
* **🎛️ 丰富的插件配置**：多组指令、分群黑白名单、撤回时间均可独立配置。结合数据惰性清理机制，极致优化大群聊场景下的内存开销。
* **👻 完美双重撤回机制**：绕过封装直达底层协议，发送与撤回如丝般顺滑。撤回提示文字独立发送，杜绝“合并卡片无法撤回”的牛皮癣问题。

---

## 📦 安装方法

1. 下载插件代码压缩包，解压后重命名文件夹为 `astrbot_plugin_endworld_img_api`。
2. 将文件夹放置于 AstrBot 的 `data/plugins/` 目录下。
3. 安装该插件必需的依赖库（在终端运行）：

```bash
pip install aiofiles aiohttp Pillow
```

4. 重启 AstrBot 即可自动加载并在插件配置面板生成高级配置项。

---

## 🚀 使用说明 & 配置指南

### 1. 添加与管理图源

进入 AstrBot 管理后台 -> 插件配置 -> 找到 `astrbot_plugin_endworld_img_api`。

* **触发指令**：输入想要的触发词，多个请分行或使用列表（如 `壁纸`, `来点图`）。
* **API 地址**：**强烈建议填写多个 API**，插件会在前一个失败时自动在池中尝试下一个。
* **使用合并转发发送**：勾选后，该图源的单张图片也会以合并转发卡片发出，极致防风控。
* **分群管理**：按需下拉选择 `无限制`、`黑名单` 或 `白名单`，并填入适用群号。
* **自动撤回时间**：填入 `30` 代表 30秒后撤回；`0` 则不撤回。

### 2. 用户日常交互

* **单张获取**：发送配置的触发词（如 `涩涩`）。
* **批量获取**：发送 `触发词 + 空格 + 数量`（如 `涩涩 5`），机器人将一次性发送多张图片（根据配置决定是否自动转为合并转发）。

---

## ⚙️ 核心全局配置项

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| **批量获取强制合并转发** | `false` | 只要是批量请求，都会被打包成合并记录防刷屏（不影响独立图源中的设置）。 |
| **批量合并转发阈值** | `3` | 当一次请求数量 **≥** 此值时，自动打包为合并转发卡片。 |
| **发送失败重试次数** | `3` | 图片被风控或死链时的“最大换图重试次数”。 |
| **开启大图自动压缩** | `true` | 是否允许插件压缩超大图片（防发送超时）。 |
| **压缩阈值 / 质量** | `5MB / 85` | 图片大于 5MB 时触发独立线程池压缩，质量为 85。 |

---

## ⚠️ 注意事项 & 法律警告

* **合规声明**：本插件仅作为网络图片 API 的纯净分发与转发工具，不内置任何图源。
* **法律风险**：请严格遵守所在地法律法规及各大平台（QQ/微信）的运营规范，**严禁在公开群聊对接与传播非法、违禁或色情图片源**。使用者需自行承担因图源配置不当引发的封号或法律风险。

---

## 🛠️ 开发维护

* **版本号**：v6.1.1 (Pro Edition)
* **衍生自**：[mccloud_img](https://github.com/MCYUNIDC/mccloud_img) (Author: MC云)
* **当前重构作者**：殇之冥歌
* **核心依赖**：`AstrBot Core v4.11.4+`

🎉 **Enjoy your high-quality random images!** 🌸
