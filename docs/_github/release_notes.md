# Release notes

## acggr-py 0.2.0-beta.0

首个独立发布：scriptWin 工具集从上游项目抽离为可独立安装的 `acggr-py`。

- 独立 `conf_dir`（`<AppConfigLocation>/Acggr`），与上游项目的配置目录互不共享
- 入口命令 `acggr`（GUI）、`acggr-cli --check`（导入自检）
- 模块：danbooru / pawchive / cbg / jsoneri / aria2 监控
- 应用内更新：轮询 updater release 的 `update.json`（preview 通道）
- 依赖 `PySide6` / `PySide6-Fluent-Widgets` / `httpx` / `redis` / `pandas` /
  `loguru` / `PyYAML` / `colorama` / `tqdm`

安装：`uvx acggr-py`
