# 参与开发与验证

本项目当前是面向 Windows 11、RTX 4070 Laptop 8GB 显存与 32GB 系统内存的预览部署包。提交修改时，请区分 CPU 逻辑通过、目标电脑可以推理、媒体可播放、视觉结果合格这几种证据。

## 修改前

阅读 [README.md](README.md)、[VALIDATION.md](VALIDATION.md)、[原理与使用维护](docs/原理与使用维护.md) 和 `locks`。普通界面变更不应顺手改模型或升级 torch。

文本默认 UTF-8；读取和修改现有文件时保留其编码、换行及无关内容。代码注释使用中文。不要提交个人绝对路径、输入图、输出视频、任务历史、日志、凭据、已安装运行时或模型文件。

## 不运行 GPU 的测试

以下命令在 **Windows 的完整源码根目录**执行，需要 Python 3.11 和系统 curl。测试只使用临时目录、本地 HTTP 或模拟 Comfy；不会下载模型或开始推理。

```powershell
py -3.11 scripts\run_cpu_tests.py
py -3.11 scripts\validate_workflows.py
py -3.11 examples\build_examples.py --check
py -3.11 scripts\check_project.py
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check_windows.ps1
```

CPU 单元测试使用 `psutil`，项目锁文件与 CI 配置检查另需 PyYAML。新建开发环境后先运行 `py -3.11 -m pip install -r .github/requirements-ci.txt`，不需要安装数 GB 的推理依赖。

若使用本包已经安装的独立 Python，可将 `py -3.11` 替换为 `.\runtime\python.exe`。面向普通用户的精简 ZIP 不包含 `.github` 和单元测试源码；开发验证和上传 GitHub 应使用完整项目目录。浏览器测试另需 Node 与锁定的 Playwright 环境，入口为 `app/test_browser.cjs`；不能把模拟界面的媒体预览当成真实 H3 成片解码结果。

GitHub 测试会检查源码编码、依赖与模型清单的一致性、画布和 API 工作流、示例可再生成性、安装与任务逻辑以及发行包。CPU 检查通过后构建供拷贝部署的 ZIP，浏览器回归作为独立任务执行。GitHub runner 不验证真实 4070 推理，远程 CI 状态也必须以仓库实际运行结果为准。

不要为了运行 CPU 测试启动真实 Comfy、安装大模型或占用用户的 GPU。测试产物放临时目录，不修改其他部署目录。

## 更改锁定版本

1. 说明需要更改的具体问题，并核对上游正式来源。源码固定到提交号；模型固定仓库修订号；wheel 固定完整版本、Python/Windows 标签及下载来源。
2. 在新目录准备候选，更新相关锁文件的版本、文件名、字节数和 SHA-256。哈希应来自可信来源或实际完整下载校验，不能把相近文件名、同尺寸当成同一文件。
3. 同时检查 `runtime.json`、`requirements.txt`、`torch-wheels.json`、`package-wheels.json`、`models.json` 与工作流是否一致。确保依赖闭包能从本地锁定 wheel 解析，避免隐式拉取更新版本。
4. 核对许可证、版权与 NOTICE，补充第三方来源记录。模型许可与节点代码许可分别处理。
5. 运行适用的 CPU 测试与源码准备检查。需要真实推理的变化，按[目标机验收](docs/目标机验收.md)做记录；没有目标电脑时明确标为未验证。
6. 发布前核对清单、包文件哈希和验证范围，并保留可回退版本。升级不能删除用户的输入、成片和任务记录。

采样器、调度器、LoRA、精度格式或编码器改动应附带相同输入和明确参数的对照结果。只报告实际测到的耗时与资源数据；若没有视觉审阅，不写“质量无损”。

## 提交内容

说明问题、触发条件、修改后的行为和验证结果。附最小复现步骤、适用环境、残留限制；无需默认附带原始角色图或原视频。尚未完成的目标机测试应写清楚，不能用一次模拟成功替代。

安全问题的报告方式见 [SECURITY.md](SECURITY.md)。普通错误可使用[仓库问题模板](https://github.com/fiammettall/H3-4070-OneClick/issues/new/choose)；维护者不承诺固定响应时限。
