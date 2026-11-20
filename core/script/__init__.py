#!/usr/bin/python
# -*- coding: utf-8 -*-
import json
import typing as t
import pathlib as p
from functools import partial

from redis import asyncio as aioredis
from core import conf
from core.config import ScriptConf
from core.core import ILLEGAL_FILENAME_RE
from core.script.entry_migrate import move_entry_identity

__all__ = ["conf", "ScriptConf", "AioRClient", "BlackList", "folder_sub", "redis_conf", "redis_reachable", "move_entry_identity"]

# Legacy in-file site rename (kemono -> pawchive) still runs here; the
# conf_img.yml -> conf.yml fold lives in core.config.legacy_conf, before Conf()
# reads the yaml.
move_entry_identity()

#: ``conf.yml``'s ``redis`` section, over the code defaults. Merged rather than
#: taken verbatim so a file that predates a key (or has no ``redis`` block at
#: all) cannot turn the ``redis_conf["host"]`` index accesses below into
#: ``KeyError`` -- redis is an optional external service, not a required input.
from core.config import DEFAULT_REDIS_CONF as _DEFAULT_REDIS_CONF

redis_conf: dict = {**_DEFAULT_REDIS_CONF, **(conf.redis or {})}


folder_sub = ILLEGAL_FILENAME_RE

#: 本地 TCP 探针超时。redis 是可选外部服务，探测不得拖慢开窗。
REDIS_PROBE_TIMEOUT_S = 0.15


def redis_reachable(*, timeout_s: float = REDIS_PROBE_TIMEOUT_S) -> bool:
    """redis 是否在 ``redis_conf`` 指定的地址上监听。

    端口可达性判定复用 aria2 侧的 ``port_answering`` —— 同一概念不保留两份实现。
    """
    from core.script.aria2.lock import port_answering

    return port_answering(redis_conf["host"], int(redis_conf["port"]), timeout_s=timeout_s)


class AioRClient(aioredis.Redis):
    """
    conf of ./conf.yml
    ```yaml
    redis:
      host: 127.0.0.1
      port: 6379
      db: 0
      password:
    ```
    """

    def __init__(self):
        """redis address from ``redis_conf`` (conf.yml over the code defaults)"""
        super(AioRClient, self).__init__(host=redis_conf['host'], port=redis_conf['port'], db=redis_conf['db'])

    async def hgetall(self, name):
        """already decode && json.loads"""
        result = await super(AioRClient, self).hgetall(name)
        try:
            return {key.decode(): json.loads(value) for key, value in result.items()}
        except (json.decoder.JSONDecodeError, TypeError):
            return {key.decode(): value.decode() for key, value in result.items()}

    async def hget(self, name, key):
        """already json.loads"""
        result = await super(AioRClient, self).hget(name, key)
        try:
            return json.loads(result)
        except (json.decoder.JSONDecodeError, TypeError):
            return result

    async def rpush(self, name, *values):
        _values = tuple(map(partial(json.dumps, ensure_ascii=False), values))
        return await super(AioRClient, self).rpush(name, *_values)

    async def lpop(self, name: str, count: t.Optional[int] = None) -> list:
        results = await super(AioRClient, self).lpop(name, count)
        if isinstance(results, str):
            results = [results]
        elif results is None:
            results = []
        return list(map(json.loads, results))


class BlackList:
    def __init__(self, file: p.Path):
        self.file = file

    def read(self):
        if not self.file.exists():
            return []
        with open(self.file, 'r', encoding='utf-8') as f:
            _ = json.load(f)
        return _ or []

    def save(self, new_data):
        with open(self.file, 'w', encoding='utf-8') as f:
            json.dump(new_data, f, ensure_ascii=False)
