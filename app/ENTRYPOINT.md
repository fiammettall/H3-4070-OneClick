# 启动接口

界面入口：`python app/server.py --root <包目录> --host 127.0.0.1 --port 8195 --comfy-port 8194`。

仅使用 Python 标准库。默认 `root` 是 `app` 的上一级。服务只能绑定 `127.0.0.1`，Comfy 地址固定为该地址，端口可改。

启动器应将 Comfy 的 `--input-directory` 指向 `<包目录>/input`、`--output-directory` 指向 `<包目录>/output`，监听 `127.0.0.1:8194`。界面会通过 Comfy 的上传接口再次登记图片，因此不会依赖手工编辑工作流路径。用户图片和任务记录分别存放 `input`、`user/jobs`。

工作流模板：`workflows/h3lite_4070_i2v_api.json`。这是提交给 Comfy `/prompt` 的 API 格式，包含占位文件名；请通过界面填入首图和提示词。模板保留 H3 Lite Set A compat 的 dynamic ClipProj、4 steps、res_multistep/simple 和原生音频路径，支持可选末图。此路线等待用户的 RTX 4070 Laptop 8GB + 32GB Windows 首测，不能标作已经实机验证。

浏览器主页：`http://127.0.0.1:8195/`。`GET /api/health` 可供启动器等待界面就绪。服务启动本身不会启动 Comfy、检查 CUDA、导入 torch 或生成视频。

## 任务接口与恢复

- `GET /api/jobs`：最近 30 项任务与已登记成片。刷新页面只查询状态，不会重新提交。
- `POST /api/generate`：参数外附 32 位十六进制 `request_id`。同编号、同参数返回原任务；同编号、不同参数返回 409。浏览器在响应丢失时保留原请求编号，避免重复提交 GPU 工作。
- `POST /api/cancel`：正文 `{"job_id":"任务编号"}`。未提交的任务直接终止；队列中的任务只删除该任务的 `prompt_id`。运行任务仅在 Comfy 当前只运行该精确编号时发出定向 `/interrupt`。检测到其他任务同时运行时保留取消请求并提示核对，不清空队列、不全局中断。
- `POST /api/retry`：正文包含原 `job_id` 和新 `request_id`。复用原图片、提示词、尺寸和种子创建新任务；原提交结果仍不确定时拒绝重试。若需修改参数，使用页面的“复用输入并修改”。
- `GET /api/job-inputs?job=任务编号`：恢复该任务已上传图片的记录；`GET /api/input?image=图片编号` 只读取已登记输入，不能传入任意文件路径。

所有写操作要求主页生成的 `X-Local-Token`，仅接受本机 Host。关闭界面不会取消 Comfy 已接受的工作。重新启动界面时，先按保存的 Comfy UUID 查询原队列与历史；持续找不到任务或服务持续失联时给出失败说明，不永远停留在“正在生成”。Comfy 返回提交错误会直接展示节点错误；提交超时按原 UUID 核对，绝不自动重发。

`user/jobs/<任务编号>.json` 保存输入 SHA-256、实际提交的 `workflow` 和规范 JSON 的 `workflow_sha256`、模板摘要、`locks/*.json` 各文件 SHA-256、种子、状态时间与总耗时。工作流摘要按键排序、无空白的 UTF-8 JSON 计算；组件锁摘要按文件原始字节计算。总耗时包含队列等待，不能当作纯采样速度。锁摘要用于比对版本，不代表实际安装文件已再次校验。成片通过 Comfy `SaveVideo` 的 `images` UI 输出登记（同时兼容 `videos`、`gifs`）；视频解码和画质仍需实机确认。

维护自测：在 `app` 目录运行 `python -m unittest -v test_server`，仅启动临时模拟 HTTP 服务，不加载 torch、CUDA 或模型。可选页面回归为 `node test_browser.cjs <Playwright模块目录> <Python路径> [Chromium路径]`，固定 headless、禁 GPU；这验证页面交互，不能替代 4070 视频推理验收。
