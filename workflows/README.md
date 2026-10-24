# ComfyUI 画布工作流

`h3lite_4070_i2v.json` 可以拖入 ComfyUI 浏览器画布，或用「工作流 → 打开」导入。`h3lite_4070_i2v_api.json` 是本项目网页向 ComfyUI 提交任务的 API 模板，不能替代画布文件。

先按项目入口安装并启动服务，再打开 `http://127.0.0.1:8194`。在「首帧」节点上传 `examples/assets/h3_schoolyard_first.png`，检查提示词、尺寸和帧数后手动加入队列。画布的默认参数与 API 模板一致：640×352、124 帧、24 fps、种子 0、`res_multistep`、`simple`、4 个调度步；4 个调度步不代表 4 次 Euler 模型求值。

第一次实机检查建议导入 `examples/01-smoke-i2v/workflow.json`，使用 56 帧短任务；首末帧示例在 `examples/02-first-last/workflow.json`。导入 JSON 不会自动上传图片，必须在 LoadImage 节点中上传相应 PNG。可选末帧使用方式：增加一个 LoadImage 节点，并将 IMAGE 输出连接到 MiniMaxH3ImageToVideo 的 `last_frame` 输入，同时按首末帧示例修改提示词。

## 模型下载链接

基础画布和两个示例画布均已在加载节点的 `properties.models` 中附上六个模型的文件名、Hugging Face 直链和目标目录。链接来自 [`locks/models.json`](../locks/models.json)，使用完整固定 revision；API 模板保留执行参数，不携带画布下载元数据。已通过安装器准备的模型无需再次下载。

支持该功能的 ComfyUI 前端会在模型缺失时提供下载入口，需要用户点击确认。[官方说明](https://docs.comfy.org/interface/features/template)中的两种行为不同：ComfyUI 桌面版保存到已注册的模型目录；本项目这种自行启动服务、在普通浏览器访问的方式会触发浏览器下载，仍需把文件放入对应 `models/` 子目录，再刷新或重启服务。

| 加载节点 | 文件位置（相对 ComfyUI 根目录） |
| --- | --- |
| UNETLoader | 主模型 → `models/diffusion_models/` |
| ClipProjLoader | Qwen 编码器 → `models/text_encoders/`；ClipProj 投影 → `models/clip_projections/` |
| LoraLoaderModelOnly | Turbo LoRA → `models/loras/` |
| VAELoader（两个节点） | 视频与音频 VAE → `models/vae/` |

下载元数据不会安装自定义节点或 Python 依赖。`ClipProjLoader` 仍需要本项目锁定的 ComfyUI-ClipProj；它安装并加载后才会注册 `clip_projections` 目录。锁定的前端 1.48.7 会扫描自定义节点中的模型下拉控件并读取这些链接，但其他版本可能表现不同；缺少节点时请先安装节点，不要把它当作模型缺失。桌面端导入和真实模型生成仍待实机验证。

`schema_snapshot.json` 记录本项目锁定源码中用到的输入顺序、输出类型和控件顺序。开发者可以在项目根目录执行：

```text
python scripts/validate_workflows.py
python -m unittest discover -s scripts -p "test_workflows.py" -v
python examples/build_examples.py --check
```

这些命令仅使用 Python 标准库，检查画布连接、参数、模型锁、与 API 模板的对应关系及示例文件一致性，不下载模型、不启动 ComfyUI、不加载 GPU。正式浏览器导入与 4070 生成效果仍在目标机验收范围内。
