# 贡献指南 Contributing

感谢你愿意改进 Acggr。参与方式不止写代码：报一个能复现的 bug、补一条翻译、修一段文档，同样有价值。

[English](contribute_en.md) · **简体中文**

| 我想… | 去哪里 |
| --- | --- |
| 报告 bug | [新建 Bug 报告](https://github.com/jasoneri/acggr/issues/new?template=bug-report.yml) |
| 提功能需求 | [新建 Feature Request](https://github.com/jasoneri/acggr/issues/new?template=feature-request.yml) |
| 提交代码 | PR 到 `pyapp` 分支，见[贡献代码](#贡献代码) |
| 补翻译 | 见[贡献翻译](#贡献翻译i18n) |
| 改文档 | `docs/` 下的 markdown，走同一个 PR 流程 |

## 提交 Bug

先搜一遍 [Issues](https://github.com/jasoneri/acggr/issues)，确认没有人报过。报告里请带上：

1. **版本**：主窗口标题栏的 `ACGGR-py v...`。
2. **系统**：Windows 10/11、macOS 或其它。
3. **复现步骤**：从启动到出错的最小操作序列。
4. **日志**：直接上传日志文件作为附件，不要把整段日志粘进正文。
5. **配置**：`%LOCALAPPDATA%\Acggr\conf.yml` 的相关片段或配置窗口截图，私人信息先打码。
6. **截图/录屏**：界面问题优先给图，能省掉一半来回沟通。

一个 issue 只报一个问题；多个问题混在一起时取最严重的一个当标题。

## 提交功能需求

先搜 [Issues](https://github.com/jasoneri/acggr/issues) 确认没有重复。写清楚使用场景、期望行为，以及你考虑过的替代方案。如果涉及新的站点适配，请附上站点地址、需要抓取的内容和登录要求。

## 贡献代码

### 环境

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync                 # 安装依赖
uv run acggr.py         # 启动 GUI
```

无显示器环境用 `QT_QPA_PLATFORM=offscreen`；只想做导入冒烟时用 `uv run acggr.py --check`（等价于 CI 的 `ACGG_CHECK_MODE=1`）。

### 分支与 PR

- 从 `pyapp` 切分支，命名 `pyapp/<站点>-<功能>`，例如 `pyapp/danbooru-pin-tabs`、`pyapp/pawchive-grid`。
- PR 目标分支一律是 `pyapp`；`main` 只是发布快照，不开功能 PR。
- 一个 PR 只做一件事，附带说明「改了什么、为什么、怎么验证」。
- 新增功能先开 issue 对齐方向，避免做完被拒。

### 代码约定

分层是硬约束：`app -> ui -> core -> res`，`ui`/`core`/`res` 不得 import `app`，`core`/`res` 不得 import `ui`。

界面文案只用语义 key：

```python
label.setText(qtTrId("browser.toolbar.copy_unfinished"))   # 对
label.setText("复制未完成")                                  # 错
```

约定速查：

- 文案的 id 与译文分家：代码里写 `qtTrId("<语义 id>")`，译文在 `res/i18n/<lang>.yml`。
- 资源只跟踪 `res/material_ct.py` 与 `res/i18n/<lang>.yml`；`.qrc`、原始 svg/png/ico 不进库，`material_ct.py` 不要手改。
- 页面脚本（注入浏览器的 JS）放 `ui/core/browser/js/*.js`，不要内嵌进 Python 字符串。
- docstring / 注释只写契约和约束，不写历史沿革和实测故事。
- 不要提交 `test/`、本地探针或调试脚本；功能 PR 只包含功能代码。

### 提交前自查

```bash
uv run acggr.py --check      # 必须通过
uv run ruff check .          # 本地自查；历史告警不要求一次清完
```

涉及界面或主题的改动，请附截图（亮/暗各一张），并确认两档布局一致。

## 贡献翻译（i18n）

Acggr 的界面文案是 **id-based** 的：代码里只有语义 id，译文全部放在 `res/i18n/<lang>.yml`。目前提供 `en_US` 与 `zh_CN` 两种。

### 文件结构

```yaml
# res/i18n/en_US.yml
browser:
  toolbar:
    copy_unfinished: copy unfinished
```

`res/i18n/zh_CN.yml` 结构相同，填对应语言的译文。key 的层级对应 id 的点分结构：
`browser.toolbar.copy_unfinished`。

### 修改或新增文案

1. 代码里使用 `qtTrId("feature.widget.element")`，id 由小写点分三段以上组成。
2. 运行同步命令，把新 id 补进所有语言表：

   ```bash
   uv run python res/i18n/build.py --sync     # 补新 key、删不再使用的 key
   ```

3. 在 `res/i18n/<lang>.yml` 里填写每个语言的译文，不允许留空。
4. 校验：

   ```bash
   uv run python res/i18n/build.py --check    # 报告缺 key、死 key、空译文，并试编译 .qm
   ```

### 规则

- **id 是契约**：已有 id 不要改名。改名等于换 key，会让所有语言表同时失效。
- **英文不是免费的**：两种语言都维护各自的 yml，不存在「源语言直接显示」的回退。
- 语言相关的站点值、协议值不要写进代码，放到 `res/lang_data.py`。
- 新增语言：添加 `res/i18n/<lang>.yml`，并在 `res/lang_data.py` 的 `SUPPORTED_LANGUAGES` 中登记；内嵌 AriaNg 页面还需要在 `ui/core/browser/aria2_bridge.py` 的 `ARIANG_LANGUAGE_KEYS` 里补映射。
- 译文缺失时界面会直接显示 id 本身，这是有意为之，便于发现漏翻。

### 运行时行为

启动时 `res.i18n` 会把 yml 编译成 `.qm` 并装进 `QApplication`，`.qm`、`.ts`、缓存戳都不进库。切换界面语言会重启进程（Qt 的 `qtTrId` 在控件构造时求值）；内嵌的 AriaNg 页面例外，会就地热切换。

## 贡献文档

- 文档放在 `docs/`，只接受 markdown。
- 遵循 [中文文案排版指北](https://github.com/sparanoid/chinese-copywriting-guidelines)。
- PR 描述写清「哪一段为什么改」，不要只写「update docs」。

## 提交信息

一句话说明改动对象和内容，必要时加一行原因：

```text
danbooru: fix viewer wheel paging
pawchive: keep selection across tab switches
```

## 安全

发现安全问题时不要开公开 issue，请通过仓库主页的邮箱私下联系维护者。
