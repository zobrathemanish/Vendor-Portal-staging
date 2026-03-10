import sys
import os
from datetime import datetime

class TerminalLogger:

    def __init__(self, base_dir, vendor, domain, submission_id):
        self.terminal = sys.stdout

        log_dir = os.path.join(
            base_dir,
            "logs",
            f"vendor={vendor}",
            domain,
            f"submission={submission_id}"
        )

        os.makedirs(log_dir, exist_ok=True)

        self.log_path = os.path.join(log_dir, "pipeline.log")

        self.log = open(self.log_path, "a", encoding="utf-8")

    def write(self, message):

        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        if message.strip() != "":
            formatted = f"[{timestamp}] {message.rstrip()}\n"

            self.terminal.write(formatted)
            self.log.write(formatted)
        else:
            self.terminal.write(message)
            self.log.write(message)

    def flush(self):
        self.terminal.flush()
        self.log.flush()