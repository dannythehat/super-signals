"""Long-running Windows worker for Super Signals' local MT5 transport."""

from __future__ import annotations

import logging
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .journal import CommandJournal
from .mt5_adapter import LocalMt5Error, Mt5Adapter

LOG = logging.getLogger("super-signals-local-bridge")
CAPABILITIES = [
    "account",
    "positions",
    "orders",
    "history",
    "quotes",
    "margin",
    "market",
    "pending",
    "modify",
    "partial-close",
]


@dataclass(frozen=True, slots=True)
class Settings:
    api_url: str
    bridge_token: str
    worker_id: str
    profile: str
    expected_account_id: str
    terminal_path: str
    login: int
    password: str
    server: str
    magic: int
    deviation: int
    poll_seconds: float
    journal_path: Path

    @classmethod
    def from_environment(cls) -> Settings:
        required = {
            name: os.getenv(name, "").strip()
            for name in (
                "SUPER_SIGNALS_API_URL",
                "SUPER_SIGNALS_LOCAL_BRIDGE_TOKEN",
                "SUPER_SIGNALS_LOCAL_BRIDGE_ACCOUNT_ID",
                "SUPER_SIGNALS_MT5_TERMINAL_PATH",
                "SUPER_SIGNALS_MT5_LOGIN",
                "SUPER_SIGNALS_MT5_PASSWORD",
                "SUPER_SIGNALS_MT5_SERVER",
            )
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError("Missing required settings: " + ", ".join(missing))
        if len(required["SUPER_SIGNALS_LOCAL_BRIDGE_TOKEN"]) < 32:
            raise RuntimeError("SUPER_SIGNALS_LOCAL_BRIDGE_TOKEN must be at least 32 characters")
        data_root = Path(os.getenv("PROGRAMDATA", str(Path.home()))) / "SuperSignals" / "bridge"
        return cls(
            api_url=required["SUPER_SIGNALS_API_URL"].rstrip("/"),
            bridge_token=required["SUPER_SIGNALS_LOCAL_BRIDGE_TOKEN"],
            worker_id=os.getenv(
                "SUPER_SIGNALS_LOCAL_BRIDGE_WORKER_ID",
                f"super-signals-{socket.gethostname().lower()}",
            ).strip(),
            profile=os.getenv("SUPER_SIGNALS_LOCAL_BRIDGE_PROFILE", "super-signals").strip(),
            expected_account_id=required["SUPER_SIGNALS_LOCAL_BRIDGE_ACCOUNT_ID"],
            terminal_path=required["SUPER_SIGNALS_MT5_TERMINAL_PATH"],
            login=int(required["SUPER_SIGNALS_MT5_LOGIN"]),
            password=required["SUPER_SIGNALS_MT5_PASSWORD"],
            server=required["SUPER_SIGNALS_MT5_SERVER"],
            magic=int(os.getenv("SUPER_SIGNALS_MT5_MAGIC", "2601001")),
            deviation=int(os.getenv("SUPER_SIGNALS_MT5_DEVIATION", "20")),
            poll_seconds=max(
                0.25, float(os.getenv("SUPER_SIGNALS_LOCAL_BRIDGE_POLL_SECONDS", "0.5"))
            ),
            journal_path=Path(
                os.getenv(
                    "SUPER_SIGNALS_LOCAL_BRIDGE_JOURNAL",
                    str(data_root / "command-journal.sqlite3"),
                )
            ),
        )


class BridgeWorker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.Client(
            base_url=settings.api_url,
            headers={"Authorization": f"Bearer {settings.bridge_token}"},
            timeout=httpx.Timeout(35.0),
        )
        self.journal = CommandJournal(settings.journal_path)
        self.mt5 = Mt5Adapter(
            terminal_path=settings.terminal_path,
            login=settings.login,
            password=settings.password,
            server=settings.server,
            magic=settings.magic,
            deviation=settings.deviation,
        )

    def run(self) -> None:
        self.mt5.connect()
        LOG.info(
            "Bridge online worker=%s profile=%s account_ref=%s",
            self.settings.worker_id,
            self.settings.profile,
            self.settings.expected_account_id,
        )
        while True:
            try:
                command = self._claim()
                if command is None:
                    time.sleep(self.settings.poll_seconds)
                    continue
                self._process(command)
            except KeyboardInterrupt:
                return
            except httpx.HTTPError as exc:
                LOG.warning("Render connection unavailable: %s", type(exc).__name__)
                time.sleep(min(max(self.settings.poll_seconds * 4, 2.0), 15.0))
            except Exception:
                LOG.exception("Bridge loop failed safely")
                time.sleep(2.0)

    def close(self) -> None:
        self.mt5.close()
        self.journal.close()
        self.client.close()

    def _identity(self) -> dict[str, Any]:
        return {
            "worker_id": self.settings.worker_id,
            "profile": self.settings.profile,
            "version": __version__,
            "capabilities": CAPABILITIES,
        }

    def _claim(self) -> dict[str, Any] | None:
        response = self.client.post(
            "/internal/local-bridge/v1/claim",
            json={**self._identity(), "lease_seconds": 45},
        )
        response.raise_for_status()
        return response.json().get("command")

    def _process(self, command: dict[str, Any]) -> None:
        command_id = str(command["id"])
        if str(command["account_id"]) != self.settings.expected_account_id:
            self._complete(
                command,
                status="failed",
                error_code="local_bridge_account_mismatch",
            )
            return

        journal_status, saved = self.journal.begin(command_id)
        if journal_status == "completed" and saved is not None:
            self._complete(command, **saved)
            return
        if journal_status == "started":
            # The previous process stopped after beginning the command. It is unsafe to
            # resubmit a mutation blindly; canonical broker reconciliation resolves it.
            self._complete(
                command,
                status="ambiguous",
                error_code="local_bridge_interrupted_command",
            )
            return

        completion: dict[str, Any]
        try:
            result = self.mt5.execute(str(command["operation"]), dict(command.get("payload") or {}))
            completion = {"status": "succeeded", "result": result}
        except LocalMt5Error as exc:
            LOG.warning("MT5 command failed id=%s code=%s", command_id, exc.code)
            completion = {
                "status": "failed",
                "error_code": exc.code,
                "error_message": str(exc),
            }
        except Exception as exc:
            LOG.exception("MT5 command crashed id=%s", command_id)
            completion = {
                "status": "failed",
                "error_code": "local_bridge_command_failed",
                "error_message": type(exc).__name__,
            }
        self.journal.finish(command_id, completion)
        self._complete(command, **completion)

    def _complete(self, command: dict[str, Any], **completion: Any) -> None:
        response = self.client.post(
            f"/internal/local-bridge/v1/commands/{command['id']}/complete",
            json={
                "worker_id": self.settings.worker_id,
                "lease_token": command["lease_token"],
                **completion,
            },
        )
        response.raise_for_status()


def main() -> int:
    logging.basicConfig(
        level=os.getenv("SUPER_SIGNALS_LOCAL_BRIDGE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        worker = BridgeWorker(Settings.from_environment())
    except (RuntimeError, ValueError, LocalMt5Error) as exc:
        LOG.error("Bridge configuration failed: %s", exc)
        return 2
    try:
        worker.run()
        return 0
    finally:
        worker.close()


if __name__ == "__main__":
    sys.exit(main())
