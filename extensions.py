from flask_login import LoginManager
import logging
from collections import deque
from flask_login import LoginManager

login_manager = LoginManager()


# ----------------------------
# LOGIN MANAGER
# ----------------------------
login_manager = LoginManager()

# ----------------------------
# UI LOG BUFFER
# ----------------------------
LOG_BUFFER = deque(maxlen=200)

class UILogHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        LOG_BUFFER.append(msg)

logger = logging.getLogger("vendor_portal")
logger.setLevel(logging.INFO)

ui_handler = UILogHandler()
ui_handler.setFormatter(logging.Formatter(
    "[%(asctime)s] %(levelname)s — %(message)s",
    "%H:%M:%S"
))

logger.addHandler(ui_handler)
