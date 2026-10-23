# 第三方来源与许可说明

本部署包组合了独立启动脚本、界面、工作流模板及第三方下载清单。**不能把整套环境、全部模型或全部工作流统一声明为 MIT。** 各组件保留其原许可；模型的使用、修改和再分发也受对应模型条款约束。

本次整理日期：2026-10-23。许可原文保存在 `licenses`；`licenses\SOURCES.json` 记录来源 URL 和本地副本 SHA-256。下表是归属索引，许可全文及上游随附声明具有完整内容。

MiniMax H3 分发声明另存于 `licenses/NOTICE`：

> MiniMax H3 is licensed under the MiniMax H3 Community License Agreement, Copyright © 2026 MiniMax. All Rights Reserved.

## 软件与工作流

| 组件 | 归属与固定来源 | 许可原文 |
| --- | --- | --- |
| CPython 3.11.9 / 官方 NuGet Python | Python Software Foundation 及许可文件中列出的历史贡献者；[CPython v3.11.9](https://github.com/python/cpython/tree/v3.11.9)，[NuGet 包](https://www.nuget.org/packages/python/3.11.9) | `licenses/Python-3.11.9-LICENSE.txt`，包含 PSF 及历史许可。 |
| ComfyUI | ComfyUI 贡献者；[commit 72865f4](https://github.com/Comfy-Org/ComfyUI/tree/72865f4f27eaf5396f8f36370e0a2be3a9a090ee) | GNU GPL v3，`licenses/ComfyUI-LICENSE.txt`。 |
| ComfyUI-ClipProj | nicolab28；[commit c01ba8f](https://github.com/nicolab28/ComfyUI-ClipProj/tree/c01ba8fb8f41b4f2094dbd0b185cdc238fb6134c) | MIT，`licenses/ClipProj-LICENSE.txt`。 |
| H3 Lite Set A 兼容工作流 | Rimagination；[commit 9bafafa](https://github.com/Rimagination/h3lite/tree/9bafafa6f1741fd6eb49d46b078e2dbb0feec3be) | MIT，`licenses/H3Lite-LICENSE.txt`。本包调整文件名、输入与本地提交方式，保留来源归属；属于适配版。 |
| MiniMax-H3-Turbo 项目 | ModelTC / LightX2V 项目贡献者；[项目仓库](https://github.com/ModelTC/Minimax-H3-Turbo) | Apache 2.0，`licenses/ModelTC-Turbo-LICENSE.txt`。项目代码许可不替代基础模型的 MiniMax H3 条款。 |
| PyTorch 2.11.0 | PyTorch 贡献者；[v2.11.0](https://github.com/pytorch/pytorch/tree/v2.11.0) | BSD 风格许可，`licenses/PyTorch-2.11.0-LICENSE.txt`；二进制分发另有其随附第三方声明。 |
| comfy-kitchen 0.2.31 | Comfy-Org 项目贡献者；[v0.2.31](https://github.com/Comfy-Org/comfy-kitchen/tree/v0.2.31) | Apache 2.0，`licenses/Comfy-Kitchen-0.2.31-LICENSE.txt`。 |
| comfy-aimdo 0.4.13 | rattus 及 Comfy-Org 项目贡献者；[v0.4.13](https://github.com/Comfy-Org/comfy-aimdo/tree/v0.4.13) | GNU GPL v3，`licenses/Comfy-Aimdo-0.4.13-LICENSE.txt`；保留原文中的替代许可说明。 |

ComfyUI 和节点源码由安装器按固定提交从原仓库下载。完整源码来源及压缩包校验值在 `locks/runtime.json`，下载后的源码保留在本包目录中。其他固定 Python 依赖见 `locks/requirements.txt` 与 `locks/package-wheels.json`，其许可证和版权声明随各上游分发包保留；本表不是全部传递依赖的重新授权文件。

## 模型与衍生文件

本压缩包本身不附带权重；安装器下载清单位于 `locks/models.json`。下载来源、修订号和哈希用于确定文件身份，不改变模型许可。

| 文件或模型来源 | 归属与许可关系 |
| --- | --- |
| MiniMax H3 基础模型、Comfy-Org 的视频/音频 VAE、AX1Y2JP W4A8 主模型 | 基础模型归属 MiniMax，量化与转换贡献分别归属对应发布者。[MiniMax H3 官方模型](https://huggingface.co/MiniMaxAI/MiniMax-H3)采用自定义 **MiniMax H3 Community License Agreement**；Comfy-Org 与 AX1Y2JP 模型卡均链接该条款。完整原文为 `licenses/MiniMax-H3-COMMUNITY-LICENSE.txt`，不能按 MIT 或 Apache 2.0 基础模型处理。 |
| `minimax_h3_fl2va_pruned_w4a8_mixed.safetensors` | 来自 [AX1Y2JP/MiniMax-H3-W4A8-ConvRot](https://huggingface.co/AX1Y2JP/MiniMax-H3-W4A8-ConvRot/tree/61a33b9224f9aa119e2332cb24a884051c9cf810)。本包保留该真实文件名，不将文件名相近或尺寸相同当成 H3 Lite 作者整合包哈希相同的证据。 |
| `qwen3vl_4b_int4_convrot.safetensors` | 量化文件由 [Merserk](https://huggingface.co/Merserk/qwen3vl-4b-int4-convrot/tree/2ea2566f0a61baeb0bbdad0a3037e43012481788)发布，模型卡标记 MIT，仓库未附独立 LICENSE 文本。其底层 [Qwen3-VL-4B-Instruct](https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct)由 Qwen 团队发布，模型卡为 Apache 2.0；保留该基础模型归属和许可，原文为 `licenses/Qwen3-VL-LICENSE.txt`。 |
| `mmh3-4b-ClipProj-celeb-mlp.safetensors` | 来自 [NicoLab28/ClipProj-MiniMax-H3](https://huggingface.co/NicoLab28/ClipProj-MiniMax-H3/tree/2ebdbcdc27a29a9607efdb221a9afcb9a0cdd808)，模型卡标记 MIT，仓库未附独立 LICENSE 文本；其声明的基础模型包括 MiniMax H3 与 Qwen3-VL，相关基础模型条款仍应保留。节点代码的 MIT 原文见 `licenses/ClipProj-LICENSE.txt`，不能据此推定全部模型衍生权利都由节点代码许可覆盖。 |
| FL2V Turbo LoRA | 选用 [Kijai/MiniMax-H3_comfy](https://huggingface.co/Kijai/MiniMax-H3_comfy/tree/098f8c48fccead9a93191c166ca31a130659d3bd)发布的 ComfyUI 格式文件，来源技术为 ModelTC / LightX2V MiniMax-H3-Turbo。保留 ModelTC 项目 Apache 2.0 归属及 MiniMax H3 基础模型条款；Kijai 仓库当前未提供独立模型许可文本，不能将此缺失解释为无使用限制。 |

## 素材与后续分发

`examples/assets` 的几何机器人场景、SVG 与生成脚本为本项目原创测试资源，适用项目根目录的 MIT 许可证。输入来源和 SHA-256 记录在 `examples/assets/manifest.json`；这些图片是测试输入，不能作为 H3 已生成样片的证据。

本包不提供动漫原片、原作角色立绘、音乐或用户上传素材的授权。素材与生成结果的使用权需按其各自来源判断。

若后续重新分发已下载的 Python 环境、源码或模型，应同时保留各组件随附的版权、许可证和 NOTICE，并按其条款提供必要的源代码或其他材料。本文件不以统一许可覆盖这些组件。
