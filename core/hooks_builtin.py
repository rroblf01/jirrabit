"""Built-in global hooks. Loaded by CoreConfig.ready()."""
import logging

from .hooks import register_hook

logger = logging.getLogger("jirrabit.hooks")


@register_hook("*")
def log_event(event, **payload):
    keys = ",".join(k for k in payload.keys() if k != "event")
    logger.info("event=%s payload_keys=%s", event, keys)
