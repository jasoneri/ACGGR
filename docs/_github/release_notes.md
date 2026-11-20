# Release notes

## acggr 0.1.0

首个独立发布：scriptWin 工具集从上游项目抽离为可独立安装的 `acggr`。

- 独立 `conf_dir`（`<AppConfigLocation>/Acggr`），与上游项目的配置目录互不共享
- 入口命令 `acggr`
- 依赖 `PySide6` / `PySide6-Fluent-Widgets` / `httpx` / `redis` / `pandas` /
  `loguru` / `PyYAML` / `colorama` / `tqdm`

安装：`uv tool install acggr`
