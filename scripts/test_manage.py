from pathlib import Path
import hashlib
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

import psutil
import manage


class StopServicesTests(unittest.TestCase):
    def test_process_exit_does_not_skip_other_service(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "services.json").write_text("{}", encoding="utf-8")
            process = Mock()
            services = {"comfy": {"pid": 10}, "web": {"pid": 20}}
            with patch.object(manage, "STATE", state), patch.object(manage, "owned_process", side_effect=[None, process]):
                manage.stop_services(services)
            process.terminate.assert_called_once()
            process.wait.assert_called_once_with(timeout=15)
            self.assertFalse((state / "services.json").exists())

    def test_access_denied_keeps_identity_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            services = {"comfy": {"pid": 10, "created": 123, "script": "scripts/run_comfy.py"}}
            with patch.object(manage, "STATE", state), patch.object(manage, "owned_process", side_effect=RuntimeError("无权核对")):
                with self.assertRaises(RuntimeError):
                    manage.stop_services(services)
            self.assertEqual(json.loads((state / "services.json").read_text(encoding="utf-8")), services)

    def test_unknown_script_or_reused_pid_is_never_stopped(self):
        with patch("psutil.Process") as constructor:
            self.assertIsNone(manage.owned_process({"pid": 10, "created": 1, "script": "other.py"}))
            constructor.assert_not_called()
        process = Mock()
        process.exe.return_value = str(manage.PYTHON)
        process.create_time.return_value = 200
        with patch("psutil.Process", return_value=process):
            self.assertIsNone(manage.owned_process({"pid": 10, "created": 100, "script": "app/server.py"}))
        process.terminate.assert_not_called()


class EnvironmentAssetTests(unittest.TestCase):
    def test_same_size_model_without_verified_snapshot_is_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "_state"
            state.mkdir()
            (root / "locks").mkdir()
            model = root / "model.bin"
            model.write_bytes(b"test")
            asset = {"destination": "model.bin", "size": 4, "sha256": hashlib.sha256(b"test").hexdigest()}
            (root / "locks/models.json").write_text(json.dumps({"files": [asset]}), encoding="utf-8")
            with patch.object(manage, "ROOT", root), patch.object(manage, "STATE", state), patch.object(manage, "CONFIG", {"archives": []}):
                self.assertEqual(len(manage.asset_errors()), 1)
                info = model.stat()
                record = {**asset, "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}
                (state / "verified.json").write_text(json.dumps({"model.bin": record}), encoding="utf-8")
                self.assertEqual(manage.asset_errors(), [])
                record["mtime_ns"] -= 1
                (state / "verified.json").write_text(json.dumps({"model.bin": record}), encoding="utf-8")
                self.assertEqual(len(manage.asset_errors()), 1)

    def test_install_lock_blocks_service_start(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("installer_for_manage_test", Path(__file__).with_name("install.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            installer = module.Installer(directory)
            with patch.object(manage, "STATE", Path(directory) / "_state"), installer.exclusive():
                with self.assertRaises(RuntimeError):
                    with manage.file_lock("install.lock", "安装中"):
                        self.fail("服务不应取得安装锁")


if __name__ == "__main__":
    unittest.main()
