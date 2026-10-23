"""离线校验版本锁、CI 引用与源码编码，不安装运行时或下载模型。"""

from pathlib import Path, PurePosixPath
import json
import re
import sys
from urllib.parse import unquote, urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def validate_asset(asset, context, destination=False):
    require(isinstance(asset.get("size"), int) and asset["size"] > 0, f"{context}: size 无效")
    require(re.fullmatch(r"[a-f0-9]{64}", asset.get("sha256", "")) is not None, f"{context}: 缺少 SHA-256")
    if destination:
        value = asset["destination"]
        path = PurePosixPath(value)
        require(not path.is_absolute() and ".." not in path.parts and ":" not in value and "\\" not in value,
                f"{context}: 非法安装目标")


def validate_locks(root):
    runtime = read_json(root / "locks/runtime.json")
    models = read_json(root / "locks/models.json")
    packages = read_json(root / "locks/package-wheels.json")
    torch = read_json(root / "locks/torch-wheels.json")
    selection = read_json(root / "locks/dependency-selection.json")["packages"]
    require(re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-z0-9.]+)?", runtime["package_version"]) is not None, "发行版本格式错误")
    require(runtime["validation"] == "target_4070_not_tested", "当前发行包必须保留目标 GPU 待验证标记")
    require(runtime["python_version"].startswith("3.11."), "运行时需要 Python 3.11")
    requirements = {}
    for line in (root / "locks/requirements.txt").read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([a-zA-Z0-9_.-]+)==([^\s]+)", line)
        require(match is not None, "requirements 必须是精确版本：" + line)
        key = normalize(match[1])
        require(key not in requirements, "requirements 包重复：" + key)
        requirements[key] = match[2]
    require(requirements == {normalize(key): value for key, value in selection.items()}, "requirements 与依赖选择快照不一致")
    wheel_versions = {}
    for asset in packages["wheels"] + torch["wheels"]:
        filename = asset["file"]
        validate_asset(asset, filename)
        url = urlsplit(asset["url"])
        require(url.scheme == "https" and unquote(url.path).endswith("/" + filename), filename + ": URL 与文件名不一致")
        require(url.hostname in {"files.pythonhosted.org", "download.pytorch.org", "download-r2.pytorch.org"}, filename + ": 未知 wheel 来源")
        fields = filename.split("-")
        require(filename.endswith(".whl") and len(fields) >= 5, filename + ": wheel 文件名错误")
        name = normalize(fields[0])
        require(name not in wheel_versions, "wheel 重复：" + name)
        wheel_versions[name] = fields[1]
        if "package" in asset:
            require(normalize(asset["package"]) == name and asset["version"] == fields[1], filename + ": wheel 元数据不一致")
    require(wheel_versions.pop("pip", None) == runtime["pip_version"], "pip wheel 与运行时版本不一致")
    require(wheel_versions == requirements, "wheel 清单不覆盖所有固定依赖")
    require(all(requirements[key] == value for key, value in runtime["torch"].items()), "Torch 三件套锁定不一致")
    for key, commit in runtime["commits"].items():
        require(re.fullmatch(r"[a-f0-9]{40}", commit) is not None, key + ": 源码未锁定 commit")
        require(any(commit in item["url"] for item in runtime["archives"]), key + ": commit 未用于下载地址")
    destinations = set()
    for asset in runtime["archives"] + models["files"]:
        validate_asset(asset, asset["destination"], destination=True)
        require(asset["destination"] not in destinations, "安装目标重复：" + asset["destination"])
        destinations.add(asset["destination"])
    require(sum(item["size"] for item in models["files"]) == models["total_bytes"], "模型总大小不一致")
    for asset in models["files"]:
        require(re.fullmatch(r"[a-f0-9]{40}", asset["revision"]) is not None, asset["role"] + ": 模型未锁定 revision")
        require(asset["destination"].startswith("models/"), asset["role"] + ": 模型目录错误")
    return len(requirements), len(models["files"])


def validate_ci(root):
    pins = read_json(root / ".github/action-pins.json")["actions"]
    paths = list((root / ".github/workflows").glob("*.yml"))
    require(bool(paths), "缺少 GitHub Actions workflow")
    for path in paths:
        content = path.read_text(encoding="utf-8")
        document = yaml.safe_load(content)
        # PyYAML 使用 YAML 1.1，on 会被解析为 True；两者均只用于检查触发器存在。
        triggers = document.get("on", document.get(True, {}))
        require("pull_request_target" not in triggers, "CPU 测试不能使用 pull_request_target")
        require(document.get("permissions") == {"contents": "read"}, "CI 只应申请 contents: read")
        for job in document["jobs"].values():
            require(job.get("runs-on") == "windows-2025", "当前 CPU 测试需要 Windows runner")
            for step in job["steps"]:
                if "uses" not in step:
                    continue
                action, commit = step["uses"].rsplit("@", 1)
                require(action in pins and pins[action]["commit"] == commit, "Action 未使用已记录 commit：" + step["uses"])
                require(re.fullmatch(r"[a-f0-9]{40}", commit) is not None, "Action 必须锁定完整 commit")
    package = read_json(root / ".github/browser/package.json")
    lock = read_json(root / ".github/browser/package-lock.json")
    require(package["dependencies"] == lock["packages"][""]["dependencies"], "浏览器工具 lockfile 与 package.json 不一致")
    for name, entry in lock["packages"].items():
        if not name:
            continue
        require(entry.get("integrity", "").startswith("sha512-"), "npm 包缺少完整性校验：" + name)
        require(urlsplit(entry["resolved"]).hostname == "registry.npmjs.org", "npm 包来源错误：" + name)


def validate_text(root):
    suffixes = {".py", ".ps1", ".cmd", ".json", ".md", ".txt", ".yml", ".yaml", ".html", ".cjs", ".svg"}
    directories = {"app", "scripts", "locks", "licenses", "workflows", "examples", "docs", ".github"}
    count = 0
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if not path.is_file() or any(part.startswith("_") or part in {"node_modules", ".git"} for part in relative.parts):
            continue
        if path.suffix not in suffixes or (len(relative.parts) > 1 and relative.parts[0] not in directories):
            continue
        text = path.read_text(encoding="utf-8-sig")
        require("\x00" not in text, f"{relative}: 文本含空字符")
        require("\ufffd" not in text, f"{relative}: 文本含解码替换字符")
        count += 1
    return count


def main():
    packages, models = validate_locks(ROOT)
    validate_ci(ROOT)
    files = validate_text(ROOT)
    print(f"PASS: {packages} dependency pins, {models} model locks, action commits, npm integrity and {files} UTF-8 text files.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, UnicodeError, yaml.YAMLError) as error:
        print("FAIL: " + str(error), file=sys.stderr)
        sys.exit(1)
