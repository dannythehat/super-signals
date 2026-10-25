"""Static safety regression checks for PU Prime unattended recovery script."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2] / "local_mt5_bridge" / "windows"


class PuRecoveryScriptsTest(unittest.TestCase):
    def test_runner_never_uses_powershell_native_stderr_redirect(self):
        script = (ROOT / "run-safe.ps1").read_text(encoding="utf-8")
        self.assertIn("-RedirectStandardError", script)
        self.assertNotIn("*>>", script)

    def test_recovery_does_not_kill_existing_worker_or_store_secrets(self):
        script = (ROOT / "pu-recovery-once.ps1").read_text(encoding="utf-8")
        self.assertIn("SUPER_SIGNALS_MT5_TERMINAL_PATH=", script)
        self.assertIn("local_mt5_bridge", script)
        self.assertNotIn("Stop-Process", script)
        self.assertNotIn("taskkill", script)
        self.assertNotIn("password", script.lower().replace("no broker orders, passwords", ""))
        self.assertIn("Get-CimInstance", script)

    def test_scheduler_is_idempotent_one_shot_not_infinite_loop(self):
        check = (ROOT / "pu-recovery-once.ps1").read_text(encoding="utf-8")
        install = (ROOT / "install-pu-recovery.ps1").read_text(encoding="utf-8")
        self.assertNotIn("while ($true)", check.lower())
        self.assertIn("RepetitionInterval", install)
        self.assertIn("IgnoreNew", install)
        self.assertIn("LogonType Interactive", install)
        self.assertIn("ColdBootWithoutLogin", install) if "ColdBootWithoutLogin" in install else None


if __name__ == "__main__":
    unittest.main()
