"""保护可导入画布和网页实际提交图之间的对应关系。"""

import copy
import importlib.util
from pathlib import Path
import unittest

from validate_workflows import (
    API_NAME, CANVAS_NAME, ROOT, canvas_to_api, instantiate_api,
    read_json, schema_snapshot, validate_all, validate_model_downloads, validate_models,
)


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = schema_snapshot()
        cls.canvas = read_json(ROOT / CANVAS_NAME)
        cls.template = read_json(ROOT / API_NAME)

    def test_published_examples_and_baseline(self):
        report = validate_all()
        self.assertEqual(report["checked_frame_counts"], [56, 124])
        self.assertEqual(report["canvas_nodes"], 17)
        self.assertEqual(report["model_download_links"], 6)

    def test_clipproj_has_both_model_downloads(self):
        node = next(item for item in self.canvas["nodes"] if item["type"] == "ClipProjLoader")
        self.assertEqual([(item["name"], item["directory"]) for item in node["properties"]["models"]], [
            ("qwen3vl_4b_int4_convrot.safetensors", "text_encoders"),
            ("mmh3-4b-ClipProj-celeb-mlp.safetensors", "clip_projections"),
        ])

    def test_model_download_metadata_rejects_missing_links(self):
        # 覆盖基础画布与两个示例，并逐个删除全部六个模型的链接。
        paths = [ROOT / CANVAS_NAME, *sorted((ROOT / "examples").glob("*/workflow.json"))]
        for path in paths:
            original = read_json(path)
            graph = canvas_to_api(original, self.snapshot)
            for node in original["nodes"]:
                for index in range(len(node["properties"].get("models", []))):
                    with self.subTest(path=path, node=node["id"], model=index):
                        broken = copy.deepcopy(original)
                        target = next(item for item in broken["nodes"] if item["id"] == node["id"])
                        del target["properties"]["models"][index]
                        with self.assertRaisesRegex(ValueError, "下载元数据与锁不一致"):
                            validate_model_downloads(broken, graph)

    def test_model_download_metadata_rejects_wrong_filename_directory_and_revision(self):
        graph = canvas_to_api(self.canvas, self.snapshot)
        changes = [
            ("name", "renamed-model.safetensors"),
            ("directory", "checkpoints"),
            ("url", "https://huggingface.co/AX1Y2JP/MiniMax-H3-W4A8-ConvRot/resolve/main/"
             "minimax_h3_fl2va_pruned_w4a8_mixed.safetensors"),
            ("url", "https://huggingface.co/AX1Y2JP/MiniMax-H3-W4A8-ConvRot/resolve/"
             "61a33b9224f9aa119e2332cb24a884051c9cf810/wrong-file.safetensors"),
        ]
        for key, value in changes:
            with self.subTest(field=key, value=value):
                broken = copy.deepcopy(self.canvas)
                node = next(item for item in broken["nodes"] if item["type"] == "UNETLoader")
                node["properties"]["models"][0][key] = value
                with self.assertRaisesRegex(ValueError, "下载元数据与锁不一致"):
                    validate_model_downloads(broken, graph)

    def test_model_metadata_on_unrelated_node_rejected(self):
        broken = copy.deepcopy(self.canvas)
        source = next(item for item in broken["nodes"] if item["type"] == "UNETLoader")
        target = next(item for item in broken["nodes"] if item["type"] == "LoadImage")
        target["properties"]["models"] = copy.deepcopy(source["properties"]["models"])
        with self.assertRaisesRegex(ValueError, "下载元数据与锁不一致"):
            validate_model_downloads(broken, canvas_to_api(broken, self.snapshot))

    def test_duplicate_link_ids_rejected(self):
        broken = copy.deepcopy(self.canvas)
        broken["links"][1][0] = broken["links"][0][0]
        with self.assertRaisesRegex(ValueError, "连接 ID 重复"):
            canvas_to_api(broken, self.snapshot)

    def test_missing_source_rejected(self):
        broken = copy.deepcopy(self.canvas)
        broken["links"][0][1] = 999
        with self.assertRaisesRegex(ValueError, "端点不存在"):
            canvas_to_api(broken, self.snapshot)

    def test_wrong_output_type_rejected(self):
        broken = copy.deepcopy(self.canvas)
        link = next(item for item in broken["links"] if item[1] == 6 and item[2] == 1)
        link[2] = 0
        with self.assertRaisesRegex(ValueError, "类型不匹配"):
            canvas_to_api(broken, self.snapshot)

    def test_missing_reverse_link_rejected(self):
        broken = copy.deepcopy(self.canvas)
        source_id, source_slot = broken["links"][0][1:3]
        node = next(item for item in broken["nodes"] if item["id"] == source_id)
        node["outputs"][source_slot]["links"].clear()
        with self.assertRaisesRegex(ValueError, "反向索引错误"):
            canvas_to_api(broken, self.snapshot)

    def test_widget_input_order_rejected(self):
        broken = copy.deepcopy(self.canvas)
        node = next(item for item in broken["nodes"] if item["id"] == 1)
        node["inputs"][0], node["inputs"][1] = node["inputs"][1], node["inputs"][0]
        with self.assertRaisesRegex(ValueError, "输入顺序"):
            canvas_to_api(broken, self.snapshot)

    def test_wrong_model_widget_value_rejected(self):
        broken = copy.deepcopy(self.canvas)
        node = next(item for item in broken["nodes"] if item["id"] == 1)
        node["widgets_values"][0], node["widgets_values"][1] = node["widgets_values"][1], node["widgets_values"][0]
        with self.assertRaisesRegex(ValueError, "模型没有锁定"):
            validate_models(canvas_to_api(broken, self.snapshot))

    def test_paused_node_rejected(self):
        broken = copy.deepcopy(self.canvas)
        broken["nodes"][0]["mode"] = 2
        with self.assertRaisesRegex(ValueError, "不可静音"):
            canvas_to_api(broken, self.snapshot)

    def test_example_graphs_equal_actual_app_submission_builder(self):
        # 使用网页真实构图函数独立比对，覆盖首帧模式与可选末帧模式。
        spec = importlib.util.spec_from_file_location("h3_workflow_test_app", ROOT / "app/server.py")
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        for path in sorted((ROOT / "examples").glob("*/config.json")):
            with self.subTest(example=path.parent.name):
                config = read_json(path)
                first = Path(config["first_frame"]).name
                last = Path(config["last_frame"]).name if config.get("last_frame") else None
                graph = app.build_workflow(self.template, config, first, last,
                                           config["filename_prefix"].removeprefix("H3_4070/"))
                self.assertEqual(graph, instantiate_api(self.template, config))
                self.assertEqual(graph, canvas_to_api(read_json(path.with_name("workflow.json")), self.snapshot))


if __name__ == "__main__":
    unittest.main()
