from pathlib import Path
import hashlib
import json
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import build_release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "源码 中文路径"
        self.root.mkdir()
        self.write("locks/runtime.json", json.dumps({"package_version": "0.1.2-preview"}))
        self.write("README.md", "中文安装说明\n")
        self.write("scripts/install.py", "print('安装')\n")
        self.write("examples/first-frame.svg", "<svg/>\n")
        self.write("workflows/import.json", "{}\n")

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return path

    def archive(self, name="tampered.zip", extra=None, missing=False, duplicate=False, manifest_edit=None):
        path = self.base / name
        content = b"original"
        manifest = {"README.md": {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}}
        if manifest_edit is not None:
            manifest_edit(manifest)
        with zipfile.ZipFile(path, "w") as archive:
            if not missing:
                archive.writestr(build_release.RELEASE_ROOT + "/README.md", content)
            if duplicate:
                archive.writestr(build_release.RELEASE_ROOT + "/readme.md", content)
            if extra is not None:
                archive.writestr(extra, b"unlisted")
            archive.writestr(build_release.RELEASE_ROOT + "/MANIFEST.json", json.dumps(manifest))
        return path

    def test_reproducible_public_examples_and_exact_membership(self):
        for name in ["runtime/python.exe", "models/private.safetensors", "input/private.png",
                     "app/node_modules/big.js", "app/_test_artifacts/screenshot.png",
                     "scripts/test_install.py", ".github/workflows/check.yml", "app/.env",
                     "examples/checkpoints/model.safetensors", "scripts/download.log"]:
            self.write(name, "不得进入用户包")
        first = build_release.build_release(self.root, self.base / "first")
        second = build_release.build_release(self.root, self.base / "second")
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(build_release.verify_release(first["zip"], require_sidecar=True), first)
        with zipfile.ZipFile(first["zip"]) as archive:
            expected = {"README.md", "locks/runtime.json", "scripts/install.py",
                        "examples/first-frame.svg", "workflows/import.json", "MANIFEST.json"}
            self.assertEqual({name.split("/", 1)[1] for name in archive.namelist()}, expected)
            manifest = json.loads(archive.read(build_release.RELEASE_ROOT + "/MANIFEST.json"))
            self.assertEqual(set(manifest), expected - {"MANIFEST.json"})

    def test_unlisted_member_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "不完全一致"):
            build_release.verify_release(self.archive(extra=build_release.RELEASE_ROOT + "/app/extra.py"))

    def test_missing_member_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "不完全一致"):
            build_release.verify_release(self.archive(missing=True))

    def test_case_insensitive_duplicate_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "大小写冲突"):
            build_release.verify_release(self.archive(duplicate=True))

    def test_bad_manifest_hash_and_size_are_rejected(self):
        for update in [{"sha256": "0" * 64}, {"bytes": 999}, {"bytes": True}]:
            with self.subTest(update=update):
                path = self.archive(manifest_edit=lambda manifest: manifest["README.md"].update(update))
                with self.assertRaises(ValueError):
                    build_release.verify_release(path)

    def test_malicious_or_unexpected_archive_paths_are_rejected(self):
        for name in ["../outside.txt", "/outside.txt", "OtherRoot/README.md",
                     build_release.RELEASE_ROOT + "/../outside.txt",
                     build_release.RELEASE_ROOT + "/app\\escape.py",
                     build_release.RELEASE_ROOT + "/app/CON.txt",
                     build_release.RELEASE_ROOT + "/app/file.txt:stream",
                     build_release.RELEASE_ROOT + "/app/trailing. "]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    build_release.verify_release(self.archive(extra=name))

    def test_manifest_cannot_authorize_private_artifacts(self):
        path = self.base / "private.zip"
        content = b"private"
        name = "models/private.safetensors"
        manifest = {name: {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}}
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(build_release.RELEASE_ROOT + "/" + name, content)
            archive.writestr(build_release.RELEASE_ROOT + "/MANIFEST.json", json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "不允许分发"):
            build_release.verify_release(path)

    def test_zip_symlink_is_rejected(self):
        path = self.base / "symlink.zip"
        with zipfile.ZipFile(path, "w") as archive:
            entry = zipfile.ZipInfo(build_release.RELEASE_ROOT + "/README.md")
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, "../../private")
            archive.writestr(build_release.RELEASE_ROOT + "/MANIFEST.json", "{}")
        with self.assertRaisesRegex(ValueError, "链接"):
            build_release.verify_release(path)

    def test_sidecar_is_required_and_validated(self):
        path = self.archive()
        with self.assertRaisesRegex(ValueError, "缺少"):
            build_release.verify_release(path, require_sidecar=True)
        path.with_suffix(".zip.sha256").write_text("wrong\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            build_release.verify_release(path)

    def test_duplicate_manifest_key_is_rejected(self):
        path = self.base / "duplicate-manifest.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(build_release.RELEASE_ROOT + "/MANIFEST.json", '{"README.md": {}, "README.md": {}}')
        with self.assertRaisesRegex(ValueError, "重复键"):
            build_release.verify_release(path)

    def test_invalid_version_cannot_escape_output_directory(self):
        self.write("locks/runtime.json", json.dumps({"package_version": "../escape"}))
        with self.assertRaisesRegex(ValueError, "版本号"):
            build_release.build_release(self.root, self.base / "output")

    def test_failed_build_preserves_previous_release(self):
        first = build_release.build_release(self.root, self.base / "output")
        with patch.object(build_release, "MAX_FILE_BYTES", 1):
            with self.assertRaises(ValueError):
                build_release.build_release(self.root, self.base / "output")
        self.assertEqual(build_release.verify_release(first["zip"], require_sidecar=True), first)
        self.assertFalse(Path(first["zip"]).with_suffix(".zip.tmp").exists())


if __name__ == "__main__":
    unittest.main()
