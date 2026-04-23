import sys
from datetime import datetime
from utils.status_helper import append_log


class TerminalLogger:

    def __init__(self, base_dir, vendor, domain, submission_id):
        # store original stdout safely
        self.terminal = sys.__stdout__

        self.vendor = vendor
        self.domain = domain
        self.submission_id = submission_id

    def write(self, message):

        if not message:
            return

        # always print raw message to terminal first
        try:
            self.terminal.write(message)
        except Exception:
            pass

        # skip empty / newline-only messages for blob logging
        if message.strip() == "":
            return

        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        formatted = f"[{timestamp}] {message.strip()}"

        # safe blob logging (never break pipeline)
        try:
            append_log(
                self.vendor,
                self.domain,
                self.submission_id,
                formatted
            )
        except Exception as e:
            # DO NOT recurse into logger → use original stdout
            try:
                self.terminal.write(f"[LOGGER ERROR] {e}\n")
            except Exception:
                pass

    def flush(self):
        try:
            self.terminal.flush()
        except Exception:
            pass