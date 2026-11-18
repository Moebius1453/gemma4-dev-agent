# Gemma 4 Developer Agent 参赛工程仓库

本仓库用于维护 Google Gemma 4 Developer Agent 比赛的声明式 Agent 配置、离线打包校验脚本、Kaggle Notebook 实验脚本及真实评测记录。

## 目录结构

- `submission/`：可直接打包提交的声明式 Agent 目录（含 `agent.yaml`、`eval_config.yaml`、`configs/sampling.yaml`、`prompts/`、`sub_agents/`）
- `scripts/package_submission.py`：本地零 GPU 离线结构校验与 `submission.zip` 打包脚本，支持按需切换官方模型别名
- `notebooks/getting-started-gemma-4-developer-agent.ipynb`：整理为 9 节 19 个单元的 Kaggle 实验 Notebook
- `tools/`：本地独立 Edge 调试实例启动脚本、`ksh.py` 单元格与命令控制器、零 GPU 消耗 Quick Save 与停机脚本
- `experiments/2026-11-18/`：从 Kaggle 云端拉回的 5 轮真实评测汇总 JSON 与模型生成的 `.patch` 补丁
- `docs/engineering-notes.md`：工程问题定位、修复方案、实验对照与已知边界说明
- `docs/orphan-process-troubleshooting.md`：云端 vLLM Worker 显存残留进程与本地控制端轮询冲突排查手册

## 本地离线校验与打包命令

打包比赛提交文件不需要启动云端 GPU。在本机直接运行：

```powershell
python scripts/package_submission.py
```

如需将声明模型切换为 31B 量化版生成候选提交包：

```powershell
python scripts/package_submission.py --model gemma-4-31b-it-qat-w4a16-ct --output dist/submission-31b.zip
```

## 当前已验证进展摘要

- 已完成：打通本地独立 Edge 实例（端口 9333）与 `tools/ksh.py` 控制通道，实现本地编辑与云端官方 Notebook 内核执行联动，无需部署外部反向隧道。
- 已完成：定位并修复官方模板 `configs/sampling.yaml` 在 `max_model_len=32768` 下因 `max_output_tokens: 16384` 将可用输入窗口锁死在 16384 tokens 的缺陷；改为 `max_output_tokens: 4096`、`thinking_budget: 2048` 后可用输入上限扩至约 28672 tokens，后续轮次未再出现 `ContextWindowExceededError`。
- 已完成：定位 Kaggle 本地 `SubprocessManager` 跳过 editable install 且 `PYTHONPATH` 缺少 `/workspace/src` 导致 `psf/requests` 导入宿主机旧包的本地评测问题；注入 `/workspace/src` 后，在 `gemma-4-12b-it-qat-w4a16-ct` 上取得首个端到端全自动验证通过任务 `requests_7315`（`resolved=true, exit_code=0, tool_calls=19, duration_seconds=105.42`）。
- 已完成：在 `requests_7433`、`requests_7502`、`fastapi_15588` 上产出针对目标位置的有效代码补丁（详见 `docs/engineering-notes.md` 中的验证边界分析）。
- 待处理：提交首版 `submission.zip` 获取线上排行榜基准分数，并在节省交互式 GPU 额度的前提下对比 `gemma-4-31b-it-qat-w4a16-ct` 表现。
