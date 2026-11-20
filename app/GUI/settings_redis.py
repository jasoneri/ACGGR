from __future__ import annotations

from PySide6.QtCore import qtTrId
from qfluentwidgets import FluentIcon as FIF, LineEdit

from core.config import DEFAULT_REDIS_CONF
from core.script import conf as script_conf
from ui.qfluent.components.elided_card import ElidedGroupHeaderCardWidget

#: Redis default endpoint text, ``host:port`` -- the form the row edits.
DEFAULT_REDIS_ADDRESS = f"{DEFAULT_REDIS_CONF['host']}:{DEFAULT_REDIS_CONF['port']}"


def parse_redis_address(text: object) -> dict:
    """``host:port`` -> ``{"host": ..., "port": ...}``; raise ``ValueError``."""
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("redis address is empty (expected host:port)")
    host, separator, port_text = raw.rpartition(":")
    host = host.strip()
    if not separator or not host:
        raise ValueError(f"redis address must be host:port, got {raw!r}")
    try:
        port = int(port_text.strip())
    except ValueError as exc:
        raise ValueError(f"redis port must be an integer, got {port_text!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"redis port out of range: {port}")
    return {"host": host, "port": port}


def redis_conf_stored() -> dict:
    """The ``redis`` section as stored, falling back to the code defaults."""
    section = getattr(script_conf, "redis", None)
    if not isinstance(section, dict):
        section = {}
    return {
        "host": str(section.get("host") or DEFAULT_REDIS_CONF["host"]),
        "port": section.get("port") or DEFAULT_REDIS_CONF["port"],
        "password": str(section.get("password") or ""),
    }


def redis_address_text() -> str:
    """Stored endpoint as ``host:port``."""
    stored = redis_conf_stored()
    return f"{stored['host']}:{stored['port']}"


class RedisGroupCard(ElidedGroupHeaderCardWidget):
    """Redis endpoint + password, own card on the pawchive tab.

    The queue this endpoint serves is pawchive's, so the rows live next to the
    pawchive entry card. ``db`` stays a code default: a second database index
    would only ever be changed by whoever renamed the key too.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setting_interface = parent
        self.setObjectName("RedisGroupCard")
        self.setTitle(qtTrId("settings.redis.title"))

        stored = redis_conf_stored()

        self.redisEdit = LineEdit(self)
        self.redisEdit.setPlaceholderText(DEFAULT_REDIS_ADDRESS)
        self.redisEdit.setMinimumWidth(240)
        self.redisEdit.setClearButtonEnabled(True)
        self.redisEdit.setText(redis_address_text())
        self.redisEdit.editingFinished.connect(self._commit_redis_address)
        self.addGroup(
            FIF.CONNECT, qtTrId("settings.redis.address_label"),
            qtTrId("settings.redis.address_hint"), self.redisEdit,
        )

        self.passwordEdit = LineEdit(self)
        self.passwordEdit.setPlaceholderText(qtTrId("settings.redis.password_placeholder"))
        self.passwordEdit.setMinimumWidth(240)
        self.passwordEdit.setClearButtonEnabled(True)
        self.passwordEdit.setEchoMode(LineEdit.Password)
        self.passwordEdit.setText(stored["password"])
        self.passwordEdit.editingFinished.connect(self._commit_password)
        self.addGroup(
            FIF.CERTIFICATE, qtTrId("settings.redis.password_label"),
            qtTrId("settings.redis.password_hint"), self.passwordEdit,
        )

    def set_redis_address(self, text: object = None) -> None:
        """Backfill from conf.yml without re-issuing the commit."""
        blocked = self.redisEdit.blockSignals(True)
        try:
            self.redisEdit.setText(str(text or DEFAULT_REDIS_ADDRESS))
        finally:
            self.redisEdit.blockSignals(blocked)

    def set_password(self, text: object = None) -> None:
        """Backfill from conf.yml without re-issuing the commit."""
        blocked = self.passwordEdit.blockSignals(True)
        try:
            self.passwordEdit.setText(str(text or ""))
        finally:
            self.passwordEdit.blockSignals(blocked)

    def get_password(self) -> str:
        return self.passwordEdit.text()

    def _stored_section(self) -> dict:
        section = getattr(script_conf, "redis", None)
        return dict(section) if isinstance(section, dict) else {}

    def _report_invalid(self, error: BaseException) -> None:
        setting_interface = self.setting_interface
        if setting_interface is not None and hasattr(setting_interface, "_present_toast"):
            setting_interface._present_toast(
                f"{qtTrId('settings.redis.invalid')}: {error}", level="error")
            return
        from loguru import logger

        logger.warning(f"[Settings] {qtTrId('settings.redis.invalid')}: {error}")

    def _commit(self, key: str, value: object, render, revert) -> None:
        """Persist one redis field; a no-op when nothing actually changed."""
        section = self._stored_section()
        if section.get(key) == value:
            return
        section[key] = value
        try:
            script_conf.update(redis=section)
        except Exception as error:
            self._report_invalid(error)
            revert()
            return
        render()

    def _commit_redis_address(self) -> None:
        """Validate the endpoint and persist it; a bad value is reverted.

        Commit point only (``editingFinished``): ``save_conf`` runs on a 600 ms
        autosave debounce, so validating there would fire once per typing pause
        while the user is still halfway through the address.
        """
        raw = self.redisEdit.text().strip()
        if not raw:
            self.redisEdit.setText(redis_address_text())
            return
        try:
            parsed = parse_redis_address(raw)
        except ValueError as error:
            self._report_invalid(error)
            self.redisEdit.setText(redis_address_text())
            return
        self._commit(
            "host", parsed["host"],
            render=lambda: self.redisEdit.setText(f"{parsed['host']}:{parsed['port']}"),
            revert=lambda: self.redisEdit.setText(redis_address_text()),
        )
        # ``port`` rides along with the same commit; persist it separately so a
        # host-only change still records both halves of the endpoint.
        self._commit(
            "port", parsed["port"],
            render=lambda: None,
            revert=lambda: None,
        )

    def _commit_password(self) -> None:
        """Persist the password as typed; empty clears it (no-auth redis)."""
        self._commit(
            "password", self.passwordEdit.text(),
            render=lambda: None,
            revert=lambda: self.passwordEdit.setText(redis_conf_stored()["password"]),
        )
