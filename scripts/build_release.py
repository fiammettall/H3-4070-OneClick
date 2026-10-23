from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import os
import re
import stat
import zipfile


ROOT = Path(__file__).resolve().parent.parent
RELEASE_ROOT = "H3-4070-OneClick"
ALLOWED_DIRECTORIES = {"app", "scripts", "workflows", "locks", "licenses", "docs", "examples"}
TOP_FILES = {
    "README.md", "VALIDATION.md", "THIRD_PARTY_NOTICES.md", "LICENSE", "NOTICE", ".gitignore",
    "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md",
    "1_安装或继续下载.cmd", "2_启动.cmd", "3_检查环境.cmd", "4_停止服务.cmd",
    "5_暂停下载.cmd", "6_修复安装.cmd",
}
BLOCKED_PARTS = {"runtime", "models", "cache", ".cache", "user", "logs", "node_modules",
                 ".git", ".venv", "venv", "engine", "custom_nodes"}
BLOCKED_SUFFIXES = {".pyc", ".pyo", ".log", ".partial", ".zip", ".7z", ".whl", ".nupkg",
                    ".safetensors", ".ckpt", ".pt", ".pth", ".onnx", ".bin"}
MAX_FILES = 10000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024


def safe_name(name):
    """拒绝解压时会产生路径歧义的名称，兼容 Windows 目标机。"""
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError("发行包路径无效")
    parts = name.split("/")
    for part in parts:
        if not part or part in {".", ".."} or part.endswith((" ", ".")):
            raise ValueError("发行包路径包含空段、上级目录或尾随字符：" + name)
        if any(ord(character) < 32 or character in '<>:"|?*' for character in part):
            raise ValueError("发行包路径包含 Windows 非法字符：" + name)
        if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part, re.IGNORECASE):
            raise ValueError("发行包路径包含 Windows 保留设备名：" + name)
    return parts


def included_name(name):
    parts = safe_name(name)
    if len(parts) == 1:
        return name in TOP_FILES
    if parts[0] not in ALLOWED_DIRECTORIES:
        return False
    # GitHub CI 和开发测试保留在源码仓库；用户包仅包含运行、维护和示例所需文件。
    if any(part.startswith(("_", ".")) or part.casefold() in BLOCKED_PARTS for part in parts):
        return False
    if parts[0] != "examples" and any(part.casefold() in {"input", "output"} for part in parts):
        return False
    leaf = PurePosixPath(name)
    return not leaf.name.startswith("test_") and leaf.suffix.casefold() not in BLOCKED_SUFFIXES


def is_link(path):
    # Windows 的目录联接也属于重解析点，不能仅检查符号链接。
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def release_files(root=ROOT):
    root = Path(root)
    for entry in sorted(root.iterdir()):
        if entry.name in ALLOWED_DIRECTORIES:
            if is_link(entry):
                raise ValueError("发行包不接受链接目录：" + str(entry))
            if not entry.is_dir():
                raise ValueError("发行目录应为文件夹：" + str(entry))
            for current, directories, filenames in os.walk(entry, followlinks=False):
                current = Path(current)
                accepted = []
                for directory in sorted(directories):
                    child = current / directory
                    # 使用虚拟叶子判断目录是否属于运行时、缓存或其他排除范围。
                    candidate = child.relative_to(root).as_posix() + "/placeholder.txt"
                    if not included_name(candidate):
                        continue
                    if is_link(child):
                        raise ValueError("发行包不接受链接目录：" + str(child))
                    accepted.append(directory)
                directories[:] = accepted
                for filename in sorted(filenames):
                    path = current / filename
                    name = path.relative_to(root).as_posix()
                    if included_name(name):
                        if is_link(path) or not path.is_file():
                            raise ValueError("发行包只接受普通文件：" + str(path))
                        yield path, name
        elif entry.name in TOP_FILES:
            if is_link(entry) or not entry.is_file():
                raise ValueError("发行包只接受普通文件：" + str(entry))
            yield entry, entry.name


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("MANIFEST.json 包含重复键：" + key)
        result[key] = value
    return result


def verify_release(path, require_sidecar=False):
    path = Path(path)
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if sidecar.exists():
        expected = digest + "  " + path.name + "\n"
        if sidecar.read_text(encoding="utf-8") != expected:
            raise ValueError("发行包 SHA-256 校验文件不匹配")
    elif require_sidecar:
        raise ValueError("缺少发行包 SHA-256 校验文件")
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if not entries or len(entries) > MAX_FILES + 1:
            raise ValueError("发行包文件数量异常")
        names = []
        seen = set()
        total = 0
        for entry in entries:
            parts = safe_name(entry.filename)
            if len(parts) < 2 or parts[0] != RELEASE_ROOT:
                raise ValueError("发行包包含意外的顶层路径：" + entry.filename)
            folded = entry.filename.casefold()
            if folded in seen:
                raise ValueError("发行包包含重复或大小写冲突路径：" + entry.filename)
            seen.add(folded)
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if entry.is_dir() or mode not in {0, stat.S_IFREG} or entry.flag_bits & 1:
                raise ValueError("发行包包含目录、链接或加密文件：" + entry.filename)
            if entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ValueError("发行包压缩格式不受支持")
            total += entry.file_size
            if entry.file_size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError("发行包超过无模型发行包的大小上限")
            names.append("/".join(parts[1:]))
        manifest_name = RELEASE_ROOT + "/MANIFEST.json"
        if "MANIFEST.json" not in names:
            raise ValueError("发行包缺少 MANIFEST.json")
        manifest = json.loads(archive.read(manifest_name).decode("utf-8"), object_pairs_hook=unique_object)
        if not isinstance(manifest, dict) or set(names) != set(manifest) | {"MANIFEST.json"}:
            raise ValueError("发行包文件与 MANIFEST.json 清单不完全一致")
        for name, expected in manifest.items():
            if not included_name(name):
                raise ValueError("发行包包含不允许分发的文件：" + name)
            if not isinstance(expected, dict) or set(expected) != {"bytes", "sha256"}:
                raise ValueError("MANIFEST.json 文件记录格式无效：" + name)
            if (type(expected["bytes"]) is not int or expected["bytes"] < 0
                    or not isinstance(expected["sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", expected["sha256"]) is None):
                raise ValueError("MANIFEST.json 文件长度或哈希无效：" + name)
            content = archive.read(RELEASE_ROOT + "/" + name)
            if len(content) != expected["bytes"] or hashlib.sha256(content).hexdigest() != expected["sha256"]:
                raise ValueError("发行包文件长度或 SHA-256 不匹配：" + name)
        bad = archive.testzip()
        if bad is not None:
            raise ValueError("发行包 CRC 校验失败：" + bad)
    return {"zip": str(path), "bytes": path.stat().st_size,
            "files": len(manifest) + 1, "sha256": digest}


def build_release(root=ROOT, output_directory=None):
    root = Path(root)
    config = json.loads((root / "locks/runtime.json").read_text(encoding="utf-8"))
    version = config["package_version"]
    if not isinstance(version, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?", version) is None:
        raise ValueError("发行版本号无效")
    destination_directory = Path(output_directory) if output_directory is not None else root.parent
    destination_directory.mkdir(parents=True, exist_ok=True)
    destination = destination_directory / ("H3-4070-OneClick-v" + version + ".zip")
    temporary = destination.with_suffix(".zip.tmp")
    manifest = {}
    total = 0
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for source, name in sorted(release_files(root), key=lambda item: item[1]):
                size = source.stat().st_size
                if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES or len(manifest) >= MAX_FILES:
                    raise ValueError("发行包超过无模型发行包的大小上限")
                content = source.read_bytes()
                total += len(content)
                if len(content) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                    raise ValueError("构建期间文件大小发生变化，已停止打包")
                manifest[name] = {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                write_member(archive, name, content)
            write_member(archive, "MANIFEST.json",
                         (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
        result = verify_release(temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    sidecar = destination.with_suffix(".zip.sha256")
    sidecar_temporary = sidecar.with_suffix(".sha256.tmp")
    try:
        sidecar_temporary.write_text(result["sha256"] + "  " + destination.name + "\n", encoding="utf-8", newline="\n")
        sidecar_temporary.replace(sidecar)
    finally:
        sidecar_temporary.unlink(missing_ok=True)
    return verify_release(destination, require_sidecar=True)


def write_member(archive, name, content):
    # 固定时间和权限，避免文件系统时间戳导致相同源码生成不同的压缩包。
    entry = zipfile.ZipInfo(RELEASE_ROOT + "/" + name, date_time=(1980, 1, 1, 0, 0, 0))
    entry.create_system = 3
    entry.external_attr = (stat.S_IFREG | 0o644) << 16
    entry.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(entry, content, compresslevel=9)


def main():
    parser = argparse.ArgumentParser(description="构建或校验不含模型的 4070 用户发行包")
    parser.add_argument("--verify", type=Path, help="只校验指定 ZIP 及相邻的 .sha256 文件")
    parser.add_argument("--output-dir", type=Path, help="构建输出目录，默认是项目上级目录")
    arguments = parser.parse_args()
    if arguments.verify is not None and arguments.output_dir is not None:
        parser.error("--verify 与 --output-dir 不可同时使用")
    result = (verify_release(arguments.verify, require_sidecar=True) if arguments.verify is not None
              else build_release(output_directory=arguments.output_dir))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
