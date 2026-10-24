from pathlib import Path
from contextlib import ExitStack, contextmanager, redirect_stdout
import hashlib
import io
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


class InstallationCheckTests(unittest.TestCase):
    @contextmanager
    def environment(self, status=None, active=False, dependency="3.14.1", gpu_memory=8188):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            state = root / "_state"
            state.mkdir()
            (root / "locks").mkdir()
            (root / "locks/requirements.txt").write_text("aiohttp==3.14.1\n", encoding="utf-8")
            (root / "locks/models.json").write_text('{"files": []}', encoding="utf-8")
            if status is not None:
                manage.save_json(state / "install_status.json", status)
            stack.enter_context(patch.object(manage, "ROOT", root))
            stack.enter_context(patch.object(manage, "STATE", state))
            stack.enter_context(patch.object(manage.sys, "platform", "win32"))
            stack.enter_context(patch.object(manage.sys, "version_info", (3, 11, 9)))
            stack.enter_context(patch.object(manage.sys, "getwindowsversion", return_value=Mock(build=22631), create=True))
            stack.enter_context(patch.object(manage.subprocess, "CREATE_NO_WINDOW", 0, create=True))
            stack.enter_context(patch.object(manage, "memory_info", return_value={
                "total_physical": 32 * 1024 ** 3, "available_physical": 16 * 1024 ** 3,
                "available_pagefile": 24 * 1024 ** 3}))
            stack.enter_context(patch.object(manage.subprocess, "run", return_value=Mock(
                stdout=f"NVIDIA GeForce RTX 4070 Laptop GPU, {gpu_memory}, 591.86\n")))
            stack.enter_context(patch.object(manage, "lock_is_held", return_value=active))
            version = stack.enter_context(patch.object(manage.importlib.metadata, "version"))
            if dependency is None:
                version.side_effect = manage.importlib.metadata.PackageNotFoundError("aiohttp")
            else:
                version.return_value = dependency
            assets = stack.enter_context(patch.object(manage, "asset_errors", return_value=[]))
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            yield state, version, assets, output

    def test_active_download_defers_mutating_assets_but_still_checks_hardware(self):
        status = {"state": "downloading", "stage": "dependencies", "stage_label": "安装固定依赖",
                  "current_file": "torch.whl", "current_percent": 31.5}
        with self.environment(status, active=True, dependency=None) as (_, version, assets, output):
            result = manage.check_environment()
            self.assertFalse(result["environment_ready"])
            self.assertTrue(result["install_pending"])
            self.assertTrue(result["checks_deferred"])
            self.assertTrue(result["hardware_ready"])
            version.assert_not_called()
            assets.assert_not_called()
            self.assertIn("31.5%", output.getvalue())
            self.assertNotIn("缺项：依赖", output.getvalue())
        with self.environment(status, active=True, gpu_memory=4096) as (_, _, _, output):
            result = manage.check_environment()
            self.assertFalse(result["hardware_ready"])
            self.assertIn("8GB 显存", output.getvalue())

    def test_stale_downloading_is_interrupted_and_missing_details_are_saved(self):
        with self.environment({"state": "downloading"}, dependency=None) as (state, _, _, output):
            result = manage.check_environment()
            self.assertEqual(result["installation"]["state"], "interrupted")
            self.assertFalse(result["environment_ready"])
            self.assertIn("当前 缺失", result["errors"][0])
            self.assertEqual(json.loads((state / "environment_check.json").read_text(encoding="utf-8")), result)
            self.assertNotIn("缺项：依赖", output.getvalue())

    def test_terminal_install_states_have_distinct_guidance(self):
        cases = ((None, "not_started"), ({"state": "paused"}, "paused"),
                 ({"state": "error", "error": "下载超时", "log": "logs/install.log"}, "error"),
                 ({"state": "prepared"}, "incomplete"), ({"state": "models_ready"}, "incomplete"))
        for status, expected in cases:
            with self.subTest(expected=expected), self.environment(status) as (_, _, _, output):
                result = manage.check_environment()
                self.assertEqual(result["installation"]["state"], expected)
                self.assertFalse(result["environment_ready"])
                if expected == "error":
                    self.assertIn("下载超时", output.getvalue())
                    self.assertIn("logs/install.log", output.getvalue())

    def test_internal_final_check_ignores_parent_lock_but_never_skips_validation(self):
        with self.environment({"state": "working", "stage": "final_check"}, active=True) as (_, version, assets, _):
            result = manage.check_environment(force_full=True)
            self.assertTrue(result["environment_ready"])
            self.assertFalse(result["install_pending"])
            version.assert_called_once_with("aiohttp")
            assets.assert_called_once()
        with self.environment({"state": "working"}, active=True, dependency=None):
            result = manage.check_environment(force_full=True)
            self.assertFalse(result["environment_ready"])
            self.assertFalse(result["checks_deferred"])

    def test_dependency_damage_is_visible_even_when_install_is_paused(self):
        with self.environment({"state": "paused"}, dependency="1.0") as (_, _, _, output):
            result = manage.check_environment()
            self.assertTrue(result["dependency_damage"])
            self.assertIn("缺项：依赖 aiohttp", output.getvalue())
        with self.environment({"state": "paused"}, dependency=None) as (state, _, _, output):
            (state / "deps_ready.json").write_text("{}", encoding="utf-8")
            result = manage.check_environment()
            self.assertTrue(result["dependency_damage"])
            self.assertIn("缺项：依赖 aiohttp", output.getvalue())

    def test_ready_record_does_not_hide_missing_dependency_or_changed_model(self):
        with self.environment({"state": "ready"}, dependency=None) as (_, _, _, output):
            result = manage.check_environment()
            self.assertFalse(result["install_pending"])
            self.assertFalse(result["environment_ready"])
            self.assertIn("缺项：依赖 aiohttp", output.getvalue())
        with self.environment({"state": "paused"}) as (state, _, assets, output):
            asset = {"destination": "model.bin", "size": 4, "sha256": "a" * 64}
            (state.parent / "locks/models.json").write_text(json.dumps({"files": [asset]}), encoding="utf-8")
            manage.save_json(state / "verified.json", {"model.bin": asset})
            assets.return_value = ["模型文件已变化：model.bin"]
            result = manage.check_environment()
            self.assertTrue(result["model_damage"])
            self.assertIn("缺项：模型文件已变化", output.getvalue())

    def test_operating_system_lock_distinguishes_live_and_stale_installer(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(manage, "STATE", Path(directory)):
            manage.save_json(manage.STATE / "install_status.json", {"state": "downloading"})
            for name in ("install.lock", "bootstrap.lock"):
                with self.subTest(lock=name), manage.file_lock(name, "测试锁"):
                    self.assertEqual(manage.installation_status()["state"], "running")
                self.assertEqual(manage.installation_status()["state"], "interrupted")

    def test_check_exit_codes_keep_failed_installation_and_damage_as_errors(self):
        for state, damage, expected in (("running", False, 2), ("paused", False, 2),
                                        ("interrupted", False, 2), ("error", False, 1), ("paused", True, 1)):
            result = {"environment_ready": False, "install_pending": True, "hardware_ready": True,
                      "dependency_damage": damage, "model_damage": False, "installation": {"state": state}}
            with self.subTest(state=state, damage=damage), tempfile.TemporaryDirectory() as directory:
                with patch.object(manage, "STATE", Path(directory)), patch.object(manage.sys, "argv", ["manage.py", "check"]), patch.object(manage, "check_environment", return_value=result):
                    self.assertEqual(manage.main(), expected)

    def test_start_uses_full_check_under_its_owned_install_lock(self):
        with patch.object(manage, "stored_services", return_value={}), patch.object(manage, "check_environment", return_value={"environment_ready": False}) as check:
            with self.assertRaises(RuntimeError):
                manage.start_services()
            check.assert_called_once_with(force_full=True)


if __name__ == "__main__":
    unittest.main()
