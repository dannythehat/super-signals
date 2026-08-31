"""Super Signals API package.

Production behaviour is owned by explicitly constructed canonical services and classes.
The package also installs a narrow logging safety guard for third-party websocket
transport libraries. Those libraries can emit every MetaAPI price tick at INFO level,
which is high-volume diagnostic noise rather than an application event.
"""

import logging


# MetaAPI uses python-engineio/python-socketio underneath. Their client loggers can
# print entire XAUUSD tick/synchronization payloads many times per second when the
# application's normal INFO logging is enabled. Disable only those raw transport
# loggers; Super Signals application, broker, health, warning and error logging remains
# available through the application's own loggers.
for _transport_logger_name in ("engineio.client", "socketio.client"):
    logging.getLogger(_transport_logger_name).disabled = True
