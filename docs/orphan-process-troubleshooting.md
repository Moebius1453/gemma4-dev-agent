# 云端残留进程与控制端并发排查说明

更新日期：2026-11-18
适用范围：Gemma 4 Developer Agent 比赛（Kaggle 4x L4 容器与本机 CDP 控制端）

## 1. 两类常见异常的区分

在本地通过 `ksh.py` 驱动云端 Kaggle Notebook 时，容易遇到两类症状不同、位置不同的进程驻留或队列冲突：

1. 云端容器内的 vLLM GPU Worker 孤儿进程：严格意义上并非操作系统 `Z` 态僵尸进程，而是主进程退出后未释放 GPU 显存的存活子进程。
2. 本机控制端的 `ksh.py` 轮询超时与共享单元格串台：外层命令超时后旧轮询未停或云端队列未空，紧接着发新命令导致输出错位。

另外需注意计费规则：停止容器内的 `server_instance` 或杀掉 `VLLM::Worker_TP` 只释放显存，不会停止 Kaggle 的 GPU 额度扣减；结束计费必须点击页面 `Stop session` 使页面状态变为 `Draft Session off`。

## 2. 云端 vLLM Worker 孤儿进程（显存占满无法重启）

### 2.1 现象与根因

- 现象：`/health` 连接被拒绝，但重新执行 `server_instance.start()` 时报错空闲显存不足（例如空闲仅 1.5 GiB）。
- 根因：vLLM 在 `tensor_parallel_size=4` 下通过 `spawn` 拉起 1 个 `APIServer` 与 4 个 `VLLM::Worker_TP0..3` 子进程（每卡占用约 20.6 GiB）。当启动或推理单元被强制中断而未走完 `server_instance.stop()` 时，`APIServer` 退出，4 个 Worker 成为孤儿进程继续占满显存。

### 2.2 排查与按特征清理方法

先在云端查看 GPU 进程表：

```powershell
python tools/ksh.py sh "nvidia-smi" --timeout 30
```

不成立或禁止：不要把历史日志里的 PID 写死到清理命令中，容器或服务每次重启后 PID 都会改变。应按进程命令行特征 `VLLM::Worker_TP` 动态匹配清理：

```python
import subprocess
import psutil

if "server_instance" in globals() and server_instance is not None:
    try:
        server_instance.stop()
    except Exception:
        pass

workers = []
for proc in psutil.process_iter(["pid", "name", "cmdline"]):
    try:
        identity = (proc.info.get("name") or "") + " " + " ".join(proc.info.get("cmdline") or [])
        if "VLLM::Worker_TP" in identity:
            print(f"Terminating orphan vLLM worker PID={proc.pid}")
            proc.terminate()
            workers.append(proc)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        continue

_, remaining = psutil.wait_procs(workers, timeout=5)
for proc in remaining:
    try:
        proc.kill()
    except psutil.NoSuchProcess:
        pass
psutil.wait_procs(remaining, timeout=5)

print(subprocess.run(["nvidia-smi"], capture_output=True, text=True).stdout)
```

## 3. 本机 `ksh.py` 超时与共享终端单元冲突

### 3.1 现象与根因

- 现象：`ksh.py py` 返回上一次命令的输出，或者连续执行多个 `run-cell` 时出现超时排队。
- 根因：
  1. 外层 Shell 工具默认超时通常为 120 秒；若只给 `ksh.py` 传 `--timeout 600` 而未同步放大外层工具超时，本机进程会在 120 秒被截断或转入后台，而云端内核仍在继续执行原单元格。
  2. `ksh.py py` 与 `ksh.py sh` 共用同一个末尾 `# [KSH_TERMINAL_CELL]` 单元格；在内核仍为 `busy` 时发送新命令会改写该单元格源码并排入执行队列。

### 3.2 预防与恢复规矩

1. 重点：运行耗时超过 1 分钟的单元（启动 vLLM、单题或多题评测）时，必须在前台单条执行，并同时把 `ksh.py --timeout` 与外层 PowerShell 工具超时设为充足值（如 600000 毫秒），严禁放入后台运行。
2. 遇到超时后，先只读运行 `python tools/ksh.py status` 检查 `kernelStatus` 是否仍为 `busy`，并检查本机是否有未退出的 `ksh.py` 进程：

```powershell
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match "ksh\.py" } |
    Select-Object ProcessId, CreationDate, CommandLine
```

3. 注意：若需结束残留轮询，只按上面查到的 `ksh.py` 对应 `ProcessId` 定点停止，严禁无差别结束本机全部 `python.exe`，以免误杀本机其他常驻服务。
