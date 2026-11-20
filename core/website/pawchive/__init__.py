from __future__ import annotations

import pathlib as p

from deploy import Env

from core.website.pawchive.api import (  # 单一来源：端点常量只在 api.py 定义
    base,
    creator_posts,
    creators_txt,
    discord_info,
    domain,
    favorites,
    file_domain,
    headers,
    img_domain,
    post,
)


DEFAULT_PAWCHIVE_SAVE_PATH = str(Env.default_acggr_path.joinpath("pawchive"))


def authors_db_path() -> p.Path:
    """作者库（预构建 preset db）路径：``<AppConfig>/Acggr/pawchive.db``。

    运行时唯一 fact：下载落点、就绪判定、读写都从这里取，不再各自拼路径。
    """
    from core.config import conf_dir

    return conf_dir.joinpath("pawchive.db")

PAWCHIVE_TOPIC = """
  ┏┓┏┓┏┓  ┓
  ┃ ┃┓┗┓━━┃┏┏┓┏┳┓┏┓┏┓┏┓
  ┗┛┗┛┗┛  ┛┗┗ ┛┗┗┗┛┛┗┗┛
"""
