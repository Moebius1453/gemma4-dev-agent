# Gemma 4 Developer Agent 工程记录与实验分析

更新日期：2026-11-18

## 1. 提交契约与算力约束

比赛采用声明式安全模型（`adk-submission`），参赛者不提交任意 Python 入口脚本，而是提交以 `agent.yaml` 为根的声明式配置包 `submission.zip`，包含模型别名、采样配置 `configs/sampling.yaml`、评测预算 `eval_config.yaml`、系统提示词 `prompts/`、子代理配置 `sub_agents/` 以及可选的技能与 LoRA 权重（展开总大小上限 3 GiB）。

重点：
1. 生成和校验 `submission.zip` 纯属文件检查与压缩，本地或 CPU 环境几秒即可完成，不消耗 GPU 额度。
2. Kaggle 每周交互式 GPU 额度按挂载 GPU 的 Draft Session 存活时长计费，即使容器内 vLLM 进程已退出，只要未点击 Stop session，额度仍会持续扣减。
3. Kaggle 编辑页的 Save Version 默认选中 Save & Run All (Commit)，在挂有 GPU 时点击会触发整本重跑；仅保存当前单元格与 `/kaggle/working` 输出文件时，必须手动切换为 Quick Save 并在 Advanced Settings 中开启保存输出。

## 2. 本地控制通道架构与使用边界

1. 独立浏览器进程：`tools/start_kaggle_browser.ps1` 复用本机 Edge 可执行文件，指定独立 `--user-data-dir` 与 `--remote-debugging-port=9333` 启动专属窗口。与日常主力 Edge 进程隔离，不会触发日常浏览器 `edge://inspect` 的逐次调试确认框，也不会抢占日常浏览窗口焦点。
2. 编辑器桥接：`tools/ksh.py` 通过 CDP 连接顶层 Kaggle 页面与跨域 JupyterLab iframe（`window.jupyterapp`），使用 `sharedModel.getSource/setSource` 读写单元格源码，调用前端官方 `jupyterapp.commands.execute('notebook:run-cell')` 触发运行并轮询 `cell.outputs` 流式取回输出。
3. 注意（已知局限）：
   - `py` 与 `sh` 子命令共用末尾 `# [KSH_TERMINAL_CELL]` 单元格，必须单通道串行调用，并发调用会覆盖源码并造成输出串台。
   - 本地 `--timeout` 或外层工具超时只结束本机轮询，不会自动终止云端正在运行的内核任务。

## 3. 已定位并修复的核心工程问题

### 3.1 修复评测预算覆盖冲突

早期单题调试单元在调用 `replace(sse_config.budget, tool_calls=20)` 之后残留了 `deepcopy(sse_config.budget)`，把刚修改的 20 次预算重新覆盖回 10 次。重构第 8 节与第 9 节后，统一在 `EvalConfig` 初始化与 `dataclasses.replace` 处单点设置预算并在末尾用 `assert` 校验，面板与运行摘要均确认 20 次和 25 次预算实际生效。

### 3.2 修复 `configs/sampling.yaml` 挤占输入窗口导致的上下文崩溃

1. 故障现象：在 `experiments/2026-11-18/bench_prompt_v2_20c_bvh967au` 中，`fastapi_15589`（17 次调用后）和 `requests_7315`（10 次调用后）均因以下错误中止：
   `This model's maximum context length is 32768 tokens. However, you requested 16384 output tokens and your prompt contains at least 16385 input tokens`
2. 根因：4 张 L4 上的 vLLM 服务设置 `max_model_len=32768`，而官方示例 `configs/sampling.yaml` 默认设置 `max_output_tokens: 16384`，导致单轮请求允许的最大输入长度被限制在 `32768 - 16384 = 16384` tokens。同时 ADK 的 `EventsCompactionConfig` 按新的顶层调用和调用后阈值检查触发，在单轮多步工具调用连续读入大文件时，输入会在触发压缩前突破 16384 上限。
3. 修复方案：将 `submission/configs/sampling.yaml` 中的 `max_output_tokens` 下调为 `4096`，`thinking_budget` 下调为 `2048`。
4. 实测效果：单次请求理论可用输入窗口扩大至约 28672 tokens。在随后的 `bench_v2_ctxfix_x13d4xq3`、`bench_v2_fixed_t7arcdjt`、`bench_v3_fixed_2lcir01c` 多轮测试中未再出现 `ContextWindowExceededError`。

### 3.3 本地 `SubprocessManager` 与正式 Docker 沙箱的两处环境差异

Kaggle Notebook 内置本地评测使用 `sandbox='subprocess'`（`SubprocessManager`），而比赛后台正式评分使用独立 Docker 容器环境，两者存在两处已核实的差异：

1. `src/` 布局仓库的 `PYTHONPATH` 缺失：
   - 现象：`requests_7315` 生成了正确的 `src/requests/adapters.py` 补丁，但本地阶段二验证仍然加载未修改逻辑并断言失败；用未打补丁的全新 `SubprocessManager` 验证官方标准补丁同样失败。
   - 根因：Kaggle 容器内虚拟环境缺少 `ensurepip`，`install_editable_package` 在 `SubprocessManager` 下直接跳过，而 `SubprocessManager.exec` 默认只设置 `PYTHONPATH=/workspace`。对 `psf/requests` 这类源码位于 `/workspace/src/requests` 的仓库，测试进程实际导入了宿主机 `/usr/local/lib/python3.12/dist-packages/requests` 中的旧包（注：`Textualize/rich` 仓库源码位于顶层 `rich/`，不属于 `src/` 布局）。
   - 处理：在 Notebook 本地评测单元中对存在 `/workspace/src` 的沙箱将 `/workspace/src` 前置到 `PYTHONPATH`，修复后 `requests_7315` 本地二阶段验证立即通过。该补丁仅用于修正 Kaggle 本地 `subprocess` 评测偏差，无需也不能放进声明式 `submission.zip`。
2. `pytest-httpbin` 缺失造成的本地 fixture 错误：
   - 现象：`requests_7433` 与 `requests_7502` 的二阶段验证对整份 `tests/test_requests.py` 运行 `pytest` 时，本地报 142 项通过、195 项 setup ERROR（`recursive dependency involving fixture 'httpbin'`）。
   - 边界说明：这是因为 Kaggle 本地环境与离线 wheel 包未包含 `pytest-httpbin` 插件，并非模型补丁引发的语法错误。但由于本地未能跑完整份 `test_requests.py`，这两道题在本地只能确认补丁代码质量，不能在未获线上评分前直接计入已通过指标。

### 3.4 工具路径与临时脚本污染边界

- `write_file` 工具受工作区路径约束不能写 `/tmp`，但 `run_command` 可以在 `/tmp/repro.py` 创建并运行临时复现脚本。
- `submit_patch` 内部执行 `git add -N .` 后提取 diff，留在 `/workspace` 下的未跟踪脚本会被打进补丁（例如 `requests_7315` 通过验证的那份原始补丁里就带上了 `reproduce_issue.py`，虽然未影响 `test_adapters.py` 通过，但在其他仓库测试中可能引发额外导入或检查问题）。因此 `submission/prompts/system.md` 已明确要求优先用 `python3 -c` 或 `/tmp/repro.py`，提交前不在 `/workspace` 留下临时文件。

## 4. 2026-11-18 五轮云端实验汇总

所有原始汇总文件与补丁保存在 `experiments/2026-11-18/`：

| 实验目录 | 模型与配置 | 任务结果摘要 | 工程结论 |
| --- | --- | --- | --- |
| `sse_prompt_v1_20c_20y88gjl` | 12B QAT，Prompt v1，20 次调用，5 分钟 | `fastapi_15588`：`resolved=false, exit_code=1`，974 字符补丁，20 次调用，220.37 秒 | 正确为 `event` 与 `id` 添加换行与回车校验，18 项通过、6 项因隐藏测试硬编码精确异常文案不匹配失败 |
| `bench_prompt_v2_20c_bvh967au` | 12B QAT，Prompt v2，默认 `sampling.yaml`（16384），20 次调用 | 0/3 通过；`fastapi_15589` 与 `requests_7315` 报 16385 输入超限错误，`requests_7502` 超时 | 暴露默认 `max_output_tokens: 16384` 腰斩可用输入上下文的根因 |
| `bench_v2_ctxfix_x13d4xq3` | 12B QAT，Prompt v2，修复 `sampling.yaml`（4096/2048），20 次调用 | 4 题均无上下文溢出；`requests_7433` 产出 584 字符补丁（修改 `PreparedRequest.prepare_body` 的 `Iterable` 判断，与目标修复一致）；`requests_7502` 该轮误改 `_encode_params` | 验证上下文窗口修复有效；同时暴露本地 `SubprocessManager` 未加载 `/workspace/src` |
| `bench_v2_fixed_t7arcdjt` | 12B QAT，Prompt v2，修复 `sampling.yaml` 加本地 `src/` 路径注入，20 次调用 | 1/4 端到端通过：`requests_7315` 取得 `resolved=true, exit_code=0`（19 次调用，105.42 秒，1633 字符补丁）；`requests_7427` 超时，`rich_4077` 改错位置，`rich_4076` 耗尽 20 次调用 | 取得首个完整端到端自动验证通过任务；同时表明 12B 模型在复杂跨类定位题（如 `rich_4077`、`rich_4076`）上一次命中率仍有瓶颈 |
| `bench_v3_fixed_2lcir01c` | 12B QAT，Prompt v3，修复 `sampling.yaml` 加本地 `src/` 路径注入，25 次调用（中途停机前已完成第 1 题） | `requests_7502` 产出 1153 字符补丁，准确修改 `src/requests/models.py` 中 `_encode_files` 与 `prepare_body` 的 `hasattr(..., "read")` 判断 | 25 次预算加 `__getattr__` 委托提示使 `requests_7502` 命中最核心的两处目标分支 |

## 5. 节省 GPU 额度的后续推进建议

1. 先用零 GPU 方式提交当前 `submission/` 打出的 12B 基线包（或直接使用 Kaggle Notebook 已固化的 Version 1 输出文件），取得真实线上得分作为对照基准。
2. 下一轮若启用交互式 GPU，优先对比 `gemma-4-31b-it-qat-w4a16-ct`（官方示例默认主模型，4 张 L4 共约 92 GB 显存可承载 W4A16 量化推理）：先在本机把 Notebook 单元和 `submission/` 改好，再启动云端会话只跑固定 4 题对照（重点看 12B 失败的 `rich_4077`、`rich_4076`、`fastapi_15589`），跑完拉回数据后立即执行 `tools/kaggle_quicksave_and_stop.py` 关停计费。
