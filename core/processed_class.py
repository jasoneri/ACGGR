#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Pagination URL helper for the standalone repo.

Only ``Url`` lives here: mainline preview states, JS execution and clipboard
helpers are out of scope, and ``core.website.core`` builds paginated preview
URLs with this class.
"""
import re
import urllib.parse as up


class Url(str):
    """class for next page
    do not use fstring"""
    page = 2  # if use it, always start on page2
    step = 1
    next_suffix = NotImplementedError("not support for next page")
    replace_format: str = None
    info = None

    def __init__(self, _url):
        self.url = _url

    def set_next(self, *info):
        """info support(keep sort):
        next_suffix, replace_format, step
        """
        self.info = info
        if info and len(info) == 2:
            self.next_suffix, self.replace_format = info
        elif info and len(info) == 3:
            self.next_suffix, _, self.step = info
        else:
            self.next_suffix = info[0]
        return self

    def __str__(self):
        return self.url

    def __add__(self, _str):  # must before next/prev/jump
        return Url(f"{self.url}{_str}").set_next(*self.info)

    @property
    def next(self):
        return self.turn_page(func=lambda page: page + self.step)

    @property
    def prev(self):
        return self.turn_page(func=lambda page: page - self.step)

    def jump(self, p):
        return self.turn_page(_p=p)

    def turn_page(self, func=None, _p: int = None, match_replace: str = None):
        is_str = isinstance(self.next_suffix, str)
        if_next = re.search(self.next_suffix, self) if is_str else self.next_suffix.search(self)
        if bool(if_next):
            match = if_next.group()
            if match_replace:
                _url = self.url.replace(match, match_replace)
            else:
                current_page = int(re.search(r"\d+", match).group())
                if self.step == 1 and current_page <= 0:
                    raise ValueError("current page is less than zero")
                new = match.replace(str(current_page), str(_p or func(current_page)))
                _url = self.url.replace(match, new)
        else:
            page2 = (self.next_suffix if is_str else str(self.next_suffix.pattern)).replace(r"\d+",
                                                                                            str(_p or self.page))
            query = up.urlparse(self).query
            if self.replace_format:
                new = self.replace_format % page2
                _url = self.replace(self.replace_format.replace("%s", ""), new)
            else:
                _url = f'{self}{"&" if query else "?"}{match_replace or page2}'
        return Url(_url).set_next(*self.info)
