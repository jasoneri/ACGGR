# -*- coding: utf-8 -*-
"""macOS launcher specifics for acggr.

Forked from the upstream launcher's mac module. The font replacement now
targets ``ui`` files instead of ``GUI/uic`` (the acggr layout keeps the UI
infrastructure copies in ``ui/``).
"""

import pathlib
import re
import subprocess

# 对应`冬青黑体简体中文`，想要换其他字体可聚焦搜索`字体册`，在目标字体右键`访达中显示`，可以看到字体文件，把字体名替换掉`font`的值即可
# 字体册仅支持能访达/系统alias能搜索出的字体，如果是下载的字体文件，可以看`macOS.font_replace _repl`
font = "Hiragino Sans GB"


class macOS:
    default_sv_path = pathlib.Path.home().joinpath("Downloads/Acggr")
    default_acggr_path = pathlib.Path.home().joinpath("Downloads/Acggr")
    default_clip_db = pathlib.Path.home().joinpath(
        "Library/Containers/org.p0deje.Maccy/Data/Library/Application Support/Maccy/Storage.sqlite")
    clip_sql = "SELECT `ZTITLE` FROM `ZHISTORYITEM` order by `Z_PK` desc"
    shell = "zsh"
    aira2_platform_id = "macos-arm"
    aira2_binary_name = "aria2c"

    def __init__(self, _p):
        self.proj_p = _p

    @staticmethod
    def open_folder(_p):
        subprocess.run(['open', _p])

    @staticmethod
    def open_file(_f):
        subprocess.run(['open', _f])

    def env_init(self):
        # 1. 更换字体
        self.font_replace()

    def font_replace(self):
        def _repl(content):
            new_content = re.sub(r'font\.setFamily\(".*?"\)', f'font.setFamily("{font}")', content)
            return new_content

        # acggr 布局：Qt Designer 产出的 UI 副本在 ui/core/browser/（无 conf_dia / ui_mainwindow）
        uic_p = self.proj_p.joinpath("ui", "core", "browser")
        for _f in ["ui_browser.py"]:
            self.file_content_replace(uic_p.joinpath(_f), _repl)

    @staticmethod
    def file_content_replace(file, repl_func):
        with open(file, 'r+', encoding='utf-8') as fp:
            content = fp.read()
            new_content = repl_func(content)
            fp.seek(0)
            fp.truncate()
            fp.write(new_content)
