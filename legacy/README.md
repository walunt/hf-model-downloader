# legacy —— 历史归档

这里放已经不再维护、但保留备查的一次性脚本。当前主流程**不使用**此目录下的任何文件。

| 文件 | 说明 | 现状 |
|---|---|---|
| `download_dsv4_vision.sh` | 最早为单个 DeepSeek-V4-Flash-Vision 仓库手写的一键下载+校验脚本（硬编码仓库名与 156GB 磁盘阈值） | 已被 `hf_download.sh`（通用下载/重试/校验）+ Web 控制台完全取代 |

通用下载请用仓库根目录的 `hf_download.sh`，或直接启动 `progress_server.py` 用网页看板操作。
两者都不需要本目录。
