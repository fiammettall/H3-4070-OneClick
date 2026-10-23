"""离线检查画布/API 工作流、模型锁和公开示例，不导入 torch。"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
from pathlib import Path
import tarfile


ROOT = Path(__file__).resolve().parent.parent
API_NAME = "workflows/h3lite_4070_i2v_api.json"
CANVAS_NAME = "workflows/h3lite_4070_i2v.json"
SCHEMA_NAME = "workflows/schema_snapshot.json"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def schema_snapshot(root=ROOT):
    snapshot = read_json(root / SCHEMA_NAME)
    runtime = read_json(root / "locks/runtime.json")
    for name, commit in snapshot["commits"].items():
        require(runtime["commits"][name] == commit, f"节点快照与运行时版本不一致：{name}")
    return snapshot


def validate_source_archives(snapshot, cache_directory, root=ROOT):
    """可选：从已下载的固定源码包核对快照；只读 AST，不执行节点代码。"""
    runtime = read_json(root / "locks/runtime.json")
    archives = {"comfyui": "comfyui.tar.gz", "clipproj": "clipproj.tar.gz"}
    source_trees = []
    for package, filename in archives.items():
        expected = next(item for item in runtime["archives"] if item["file"] == filename)
        path = Path(cache_directory) / filename
        require(hashlib.sha256(path.read_bytes()).hexdigest() == expected["sha256"], f"源码包哈希错误：{filename}")
        with tarfile.open(path) as archive:
            for name, evidence in snapshot["source_files"].items():
                if not name.startswith(package + "/"):
                    continue
                stream = archive.extractfile(evidence["member"])
                require(stream is not None, f"源码成员丢失：{name}")
                content = stream.read()
                require(hashlib.sha256(content).hexdigest() == evidence["sha256"], f"节点源码哈希错误：{name}")
                source_trees.append(ast.parse(content.decode("utf-8")))
    classes = {node.name: node for tree in source_trees for node in tree.body if isinstance(node, ast.ClassDef)}
    for node_type, schema in snapshot["nodes"].items():
        definition = classes[node_type]
        function = next(node for node in definition.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name in {"define_schema", "INPUT_TYPES"})
        returned = next(node.value for node in ast.walk(function) if isinstance(node, ast.Return))
        if function.name == "define_schema":
            slots = next(keyword.value for keyword in returned.keywords if keyword.arg == "inputs")
            names = [slot.args[0].value for slot in slots.elts]
        else:
            names = []
            for key, value in zip(returned.keys, returned.values):
                if isinstance(key, ast.Constant) and key.value in {"required", "optional"}:
                    names.extend(item.value for item in value.keys)
        require(names == [slot["name"] for slot in schema["inputs"]], f"源码输入顺序与快照不一致：{node_type}")
    return len(snapshot["nodes"])


def canvas_to_api(canvas, snapshot):
    """按固定源码控件顺序还原 API；同时检查连接表的两个方向。"""
    require(canvas.get("version") == 0.4, "画布格式必须为 LiteGraph 0.4")
    nodes = canvas.get("nodes", [])
    require(bool(nodes), "画布没有节点")
    by_id = {node["id"]: node for node in nodes}
    require(len(by_id) == len(nodes), "节点 ID 重复")
    require(all(type(key) is int and key > 0 for key in by_id), "画布节点 ID 必须为正整数")
    links = canvas.get("links", [])
    require(all(len(link) == 6 and type(link[0]) is int for link in links), "连接表结构错误")
    link_by_id = {link[0]: link for link in links}
    require(len(link_by_id) == len(links), "连接 ID 重复")
    require(canvas["last_node_id"] >= max(by_id), "last_node_id 错误")
    require(canvas["last_link_id"] >= max(link_by_id, default=0), "last_link_id 错误")
    graph = {}
    identities = {}
    for node in nodes:
        node_type = node["type"]
        require(node_type in snapshot["nodes"], f"未知节点类型：{node_type}")
        require(node.get("mode", 0) == 0, f"节点不可静音或旁路：{node['id']}")
        require(node.get("properties", {}).get("Node name for S&R") == node_type,
                f"节点类型属性错误：{node['id']}")
        identity = node["properties"]["api_node_id"]
        require(identity not in graph, "API 节点 ID 重复")
        identities[node["id"]] = identity
        graph[identity] = {"class_type": node_type, "inputs": {}}
    observed_outputs = {}
    observed_inputs = set()
    for link_id, source_id, source_slot, target_id, target_slot, link_type in links:
        require(source_id in by_id and target_id in by_id, f"连接 {link_id} 的端点不存在")
        source, target = by_id[source_id], by_id[target_id]
        source_schema = snapshot["nodes"][source["type"]]
        target_schema = snapshot["nodes"][target["type"]]
        require(len(target.get("inputs", [])) == len(target_schema["inputs"]), f"输入数量错误：{target_id}")
        require(type(source_slot) is int and 0 <= source_slot < len(source_schema["outputs"]),
                f"连接 {link_id} 的输出槽位错误")
        require(type(target_slot) is int and 0 <= target_slot < len(target_schema["inputs"]),
                f"连接 {link_id} 的输入槽位错误")
        slot = target_schema["inputs"][target_slot]
        require(not slot.get("widget"), f"连接 {link_id} 意外替换了固定控件")
        require(source_schema["outputs"][source_slot] == slot["type"] == link_type,
                f"连接 {link_id} 的类型不匹配")
        require((target_id, target_slot) not in observed_inputs, f"输入槽位连接重复：{target_id}/{target_slot}")
        observed_inputs.add((target_id, target_slot))
        require(target["inputs"][target_slot]["link"] == link_id, f"连接 {link_id} 未出现在输入槽位")
        observed_outputs.setdefault((source_id, source_slot), []).append(link_id)
        graph[identities[target_id]]["inputs"][slot["name"]] = [identities[source_id], source_slot]
    for node in nodes:
        schema = snapshot["nodes"][node["type"]]
        require(len(node["inputs"]) == len(schema["inputs"]), f"输入数量错误：{node['id']}")
        require(len(node["outputs"]) == len(schema["outputs"]), f"输出数量错误：{node['id']}")
        widget_specs = [slot for slot in schema["inputs"] if slot.get("widget")]
        widgets = node.get("widgets_values", [])
        require(len(widgets) == len(widget_specs) + len(schema.get("extra_widgets", [])),
                f"控件数量错误：{node['id']}")
        for index, slot in enumerate(schema["inputs"]):
            actual = node["inputs"][index]
            require(actual["name"] == slot["name"] and actual["type"] == slot["type"],
                    f"输入顺序或类型错误：{node['id']}/{slot['name']}")
            if actual.get("link") is not None:
                require((node["id"], index) in observed_inputs, f"输入引用不存在的连接：{node['id']}/{index}")
            elif not slot.get("widget"):
                require(slot.get("optional", False), f"必要输入未连接：{node['id']}/{slot['name']}")
        for index, slot in enumerate(widget_specs):
            graph[identities[node["id"]]]["inputs"][slot["name"]] = widgets[index]
        require(widgets[len(widget_specs):] == schema.get("extra_widgets", []), f"辅助控件错误：{node['id']}")
        for index, output_type in enumerate(schema["outputs"]):
            output = node["outputs"][index]
            require(output["type"] == output_type, f"输出类型错误：{node['id']}/{index}")
            require(sorted(output.get("links") or []) == sorted(observed_outputs.get((node["id"], index), [])),
                    f"输出连接反向索引错误：{node['id']}/{index}")
    # 检查有向环，避免连接表正确但执行依赖相互等待。
    visiting, visited = set(), set()

    def visit(identity):
        require(identity not in visiting, f"工作流存在依赖环：{identity}")
        if identity in visited:
            return
        visiting.add(identity)
        for value in graph[identity]["inputs"].values():
            if isinstance(value, list):
                visit(value[0])
        visiting.remove(identity)
        visited.add(identity)

    for identity in graph:
        visit(identity)
    return graph


def validate_models(graph, root=ROOT):
    locked = {Path(item["destination"]).name for item in read_json(root / "locks/models.json")["files"]}
    used = set()
    for node in graph.values():
        for name, value in node["inputs"].items():
            if name in {"unet_name", "clip_name", "projection", "lora_name", "vae_name"}:
                require(value in locked, f"工作流模型没有锁定：{value}")
                used.add(value)
    require(used == locked, "工作流与模型清单不完整对应")


def instantiate_api(template, config):
    graph = copy.deepcopy(template)
    graph["6"]["inputs"].update({name: config[name] for name in ("prompt", "width", "height", "length")})
    graph["20"]["inputs"]["image"] = Path(config["first_frame"]).name
    graph["10"]["inputs"]["noise_seed"] = config["seed"]
    graph["15"]["inputs"]["filename_prefix"] = config["filename_prefix"]
    if config.get("last_frame"):
        graph["21"] = {"class_type": "LoadImage", "inputs": {"image": Path(config["last_frame"]).name}}
        graph["6"]["inputs"]["last_frame"] = ["21", 0]
    return graph


def validate_all(root=ROOT):
    snapshot = schema_snapshot(root)
    template = read_json(root / API_NAME)
    base = canvas_to_api(read_json(root / CANVAS_NAME), snapshot)
    # 只有不可执行的占位提示词和图片名被公开示例替换，其他参数必须等同 API 模板。
    base["6"]["inputs"]["prompt"] = "__H3_PROMPT__"
    base["20"]["inputs"]["image"] = "__H3_FIRST_FRAME__"
    require(base == template, "基础画布与 API 模板不一致")
    validate_models(base, root)
    cases = sorted((root / "examples").glob("*/config.json"))
    require(len(cases) >= 2, "缺少公开示例")
    lengths = set()
    for path in cases:
        config = read_json(path)
        lengths.add(config["length"])
        expected = instantiate_api(template, config)
        require(read_json(path.with_name("workflow.api.json")) == expected, f"示例 API 与配置不一致：{path.parent.name}")
        require(canvas_to_api(read_json(path.with_name("workflow.json")), snapshot) == expected,
                f"示例画布与配置不一致：{path.parent.name}")
        require(path.with_name("prompt.txt").read_text(encoding="utf-8") == config["prompt"] + "\n",
                f"示例提示词不一致：{path.parent.name}")
        require(config["width"] % 32 == 0 and config["height"] % 32 == 0, "尺寸必须为 32 的倍数")
        require((config["length"] - 5) % 17 == 0, "帧数必须符合 17k+5 网格")
        require(config["fps"] == 24, "示例帧率必须为 24")
        require(config["sampler"] == expected["8"]["inputs"]["sampler_name"]
                and config["scheduler"] == expected["9"]["inputs"]["scheduler"]
                and config["scheduler_steps"] == expected["9"]["inputs"]["steps"], "示例采样参数与图不一致")
        for name in ("first_frame", "last_frame"):
            if config.get(name):
                asset = (path.parent / config[name]).resolve()
                require(asset.is_relative_to((root / "examples").resolve()) and asset.is_file(), "示例资源丢失或越界")
                content = asset.read_bytes()
                require(content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) > 100, "示例图片不是 PNG")
                require(int.from_bytes(content[16:20], "big") == config["width"]
                        and int.from_bytes(content[20:24], "big") == config["height"], "示例图片尺寸不符")
        validate_models(expected, root)
    require({56, 124}.issubset(lengths), "示例应覆盖 56 帧和 124 帧")
    return {"canvas_nodes": len(base), "examples": len(cases), "checked_frame_counts": sorted(lengths),
            "validation": "static_only_no_gpu"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--source-archives", type=Path, help="可选的本地固定源码包缓存目录；不会联网下载")
    args = parser.parse_args()
    report = validate_all(args.root)
    if args.source_archives:
        report["source_node_schemas"] = validate_source_archives(schema_snapshot(args.root), args.source_archives, args.root)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
