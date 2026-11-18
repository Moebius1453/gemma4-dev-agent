"""Kaggle Notebook 本地 CDP 控制器。

通过独立 Edge 进程的本地调试端口连接 Kaggle 编辑页及其跨域 JupyterLab iframe，
使用前端官方 notebook:run-cell 命令触发执行并轮询 cell.outputs 取回输出，
避免部署外部反向隧道，也避免直接调用底层 kernel.requestExecute 时在代理层无回传的问题。

注意：
1. py 和 sh 子命令共用末尾 [KSH_TERMINAL_CELL] 单元格，不可并发发起多条请求；
2. 本地等待超时不会自动中断云端内核，长耗时单元在外层工具调用时须显式放宽超时。
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

import websocket

CDP_HOST = os.environ.get("KAGGLE_CDP_HOST", "127.0.0.1")
CDP_PORT = int(os.environ.get("KAGGLE_CDP_PORT", "9333"))
NOTEBOOK_URL = os.environ.get(
    "KAGGLE_NOTEBOOK_URL",
    "https://www.kaggle.com/code/moebius1453/getting-started-gemma-4-developer-agent/edit",
)
EDITOR_ORIGIN = "https://kkb-production.jupyter-proxy.kaggle.net"
CELLS_DIR = Path(
    os.environ.get("KAGGLE_CELLS_DIR", str(Path(__file__).resolve().parent / "cells"))
)
SHELL_MARKER = "# [KSH_TERMINAL_CELL]"


def get_browser_ws():
    url = f"http://{CDP_HOST}:{CDP_PORT}/json/version"
    with urlopen(url, timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["webSocketDebuggerUrl"]


class CDPSession:
    def __init__(self):
        ws_url = get_browser_ws()
        self.sock = websocket.create_connection(
            ws_url, timeout=60, suppress_origin=True
        )
        self.seq = 0

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def call(self, method, params=None, session_id=None):
        self.seq += 1
        req_id = self.seq
        payload = {"id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        if session_id is not None:
            payload["sessionId"] = session_id
        self.sock.send(json.dumps(payload))
        while True:
            msg = json.loads(self.sock.recv())
            if msg.get("id") != req_id:
                continue
            if "error" in msg:
                err = msg["error"]
                raise RuntimeError(
                    f"CDP {method} failed: {err.get('code')} {err.get('message')}"
                )
            return msg.get("result", {})

    def find_targets(self):
        targets = self.call("Target.getTargets").get("targetInfos", [])
        page_target = None
        iframe_target = None
        all_pages = []
        for t in targets:
            u = t.get("url", "")
            parts = urlsplit(u)
            clean = f"{parts.scheme}://{parts.netloc}{parts.path}"
            if t.get("type") == "page":
                all_pages.append(
                    {
                        "title": t.get("title", ""),
                        "url": clean,
                        "targetId": t["targetId"],
                    }
                )
            if clean == NOTEBOOK_URL and t.get("type") == "page":
                page_target = t
            elif (
                f"{parts.scheme}://{parts.netloc}" == EDITOR_ORIGIN
                and parts.path.startswith("/static/assets/jupyterlab-v4/")
            ):
                iframe_target = t
        return page_target, iframe_target, all_pages

    def eval_in_target(self, target_id, expression, await_promise=True):
        sid = self.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )["sessionId"]
        res = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "timeout": 55000,
            },
            session_id=sid,
        )
        if "exceptionDetails" in res:
            det = res["exceptionDetails"]
            desc = det.get("exception", {}).get("description") or det.get(
                "text", "JS Error"
            )
            raise RuntimeError(desc)
        return res.get("result", {}).get("value")


def cmd_status(cdp):
    page_t, iframe_t, all_pages = cdp.find_targets()
    if not page_t:
        print(
            json.dumps(
                {
                    "notebook_open": False,
                    "message": "未找到目标 Notebook 编辑页，请检查是否仍在登录页",
                    "open_pages": all_pages,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    page_info = cdp.eval_in_target(
        page_t["targetId"],
        """(() => {
            const text = document.body.innerText || '';
            const sessionMatch = text.match(/Draft Session[\\s\\S]{0,60}/);
            return {
                title: document.title,
                url: location.href,
                sessionHint: sessionMatch ? sessionMatch[0].replace(/\\s+/g, ' ').trim() : null
            };
        })()""",
    )
    if not iframe_t:
        print(
            json.dumps(
                {
                    "notebook_open": True,
                    "iframe_ready": False,
                    "page": page_info,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    editor_info = cdp.eval_in_target(
        iframe_t["targetId"],
        """(() => {
            const w = window.jupyterapp?.shell?.currentWidget;
            const k = w?.context?.sessionContext?.session?.kernel;
            const cells = w?.content?.model?.cells;
            return {
                hasWidget: !!w,
                cellCount: cells ? cells.length : 0,
                dirty: w?.context?.model?.dirty ?? null,
                kernelStatus: k?.status ?? null,
                kernelConn: k?.connectionStatus ?? null
            };
        })()""",
    )
    print(
        json.dumps(
            {
                "notebook_open": True,
                "iframe_ready": True,
                "page": page_info,
                "editor": editor_info,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_pull(cdp):
    _, iframe_t, _ = cdp.find_targets()
    if not iframe_t:
        raise RuntimeError("未找到 Jupyter 编辑器 iframe")

    cells_data = cdp.eval_in_target(
        iframe_t["targetId"],
        """(() => {
            const w = window.jupyterapp.shell.currentWidget;
            const cells = w.content.model.cells;
            const out = [];
            for (let i = 0; i < cells.length; i++) {
                const c = cells.get(i);
                const outputs = [];
                if (c.outputs) {
                    for (let j = 0; j < c.outputs.length; j++) {
                        outputs.push(c.outputs.get(j).toJSON());
                    }
                }
                out.push({
                    index: i,
                    id: c.id,
                    type: c.type,
                    executionCount: c.executionCount ?? null,
                    source: c.sharedModel.getSource() || '',
                    outputs: outputs
                });
            }
            return out;
        })()""",
    )
    CELLS_DIR.mkdir(parents=True, exist_ok=True)
    (CELLS_DIR / "manifest.json").write_text(
        json.dumps(cells_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for item in cells_data:
        ext = "md" if item["type"] == "markdown" else "py"
        fname = CELLS_DIR / f"cell_{item['index']:02d}.{ext}"
        fname.write_text(item["source"], encoding="utf-8")
    print(f"已同步 {len(cells_data)} 个单元到 {CELLS_DIR}")
    for item in cells_data:
        first_line = (
            item["source"].strip().splitlines()[0]
            if item["source"].strip()
            else "<EMPTY>"
        )
        print(f"[{item['index']:02d}] ({item['type']}) {first_line[:85]}")


def format_outputs(outputs):
    parts = []
    for o in outputs:
        otype = o.get("output_type")
        if otype == "stream":
            txt = o.get("text", "")
            if isinstance(txt, list):
                txt = "".join(txt)
            parts.append(txt)
        elif otype in ("execute_result", "display_data"):
            data = o.get("data", {})
            txt = data.get("text/plain", "")
            if isinstance(txt, list):
                txt = "".join(txt)
            if txt:
                parts.append(txt + ("\n" if not txt.endswith("\n") else ""))
        elif otype == "error":
            tb = o.get("traceback", [])
            if tb:
                parts.append("\n".join(tb) + "\n")
            else:
                parts.append(f"{o.get('ename')}: {o.get('evalue')}\n")
    return "".join(parts)


def run_cell_and_stream(cdp, iframe_tid, cell_index, timeout_sec=600):
    cdp.eval_in_target(
        iframe_tid,
        f"""(async () => {{
            const w = window.jupyterapp.shell.currentWidget;
            w.content.activeCellIndex = {cell_index};
            w.content.deselectAll();
            const cell = w.content.model.cells.get({cell_index});
            cell.executionCount = null;
            if (cell.outputs) cell.outputs.clear();
            window.jupyterapp.commands.execute('notebook:run-cell');
            return true;
        }})()""",
    )
    start = time.time()
    printed_len = 0
    saw_busy_or_output = False
    while time.time() - start < timeout_sec:
        time.sleep(0.5)
        state = cdp.eval_in_target(
            iframe_tid,
            f"""(() => {{
                const w = window.jupyterapp.shell.currentWidget;
                const k = w?.context?.sessionContext?.session?.kernel;
                const c = w.content.model.cells.get({cell_index});
                const outs = [];
                if (c.outputs) {{
                    for (let i = 0; i < c.outputs.length; i++) {{
                        outs.push(c.outputs.get(i).toJSON());
                    }}
                }}
                const promptNode = w.content.widgets[{cell_index}]?.promptNode;
                const promptText = promptNode ? promptNode.innerText.trim() : '';
                return {{
                    kernelStatus: k?.status ?? null,
                    kernelConn: k?.connectionStatus ?? null,
                    executionCount: c.executionCount ?? null,
                    promptText: promptText,
                    outputs: outs
                }};
            }})()""",
        )
        full_text = format_outputs(state["outputs"])
        if len(full_text) > printed_len:
            sys.stdout.write(full_text[printed_len:])
            sys.stdout.flush()
            printed_len = len(full_text)
            saw_busy_or_output = True

        kstatus = state["kernelStatus"]
        prompt = state["promptText"]
        exec_count = state["executionCount"]
        if kstatus == "busy" or "*" in prompt or exec_count is not None:
            saw_busy_or_output = True

        if saw_busy_or_output and kstatus == "idle" and "*" not in prompt and exec_count is not None:
            time.sleep(0.2)
            final_state = cdp.eval_in_target(
                iframe_tid,
                f"""(() => {{
                    const w = window.jupyterapp.shell.currentWidget;
                    const c = w.content.model.cells.get({cell_index});
                    const outs = [];
                    if (c.outputs) {{
                        for (let i = 0; i < c.outputs.length; i++) {{
                            outs.push(c.outputs.get(i).toJSON());
                        }}
                    }}
                    return outs;
                }})()""",
            )
            final_text = format_outputs(final_state)
            if len(final_text) > printed_len:
                sys.stdout.write(final_text[printed_len:])
                sys.stdout.flush()
            return
    raise TimeoutError(f"单元 {cell_index} 执行等待超过 {timeout_sec} 秒")


def ensure_terminal_cell(cdp, iframe_tid, code_to_set):
    idx = cdp.eval_in_target(
        iframe_tid,
        f"""(() => {{
            const w = window.jupyterapp.shell.currentWidget;
            const cells = w.content.model.cells;
            const marker = {json.dumps(SHELL_MARKER)};
            let targetIdx = -1;
            for (let i = cells.length - 1; i >= 0; i--) {{
                const src = cells.get(i).sharedModel.getSource() || '';
                if (src.startsWith(marker)) {{
                    targetIdx = i;
                    break;
                }}
            }}
            if (targetIdx === -1) {{
                w.content.activeCellIndex = cells.length - 1;
                window.jupyterapp.commands.execute('notebook:insert-cell-below');
                targetIdx = cells.length - 1;
            }}
            const cell = cells.get(targetIdx);
            cell.sharedModel.setSource(marker + "\\n" + {json.dumps(code_to_set)});
            return targetIdx;
        }})()""",
    )
    return idx


def cmd_py(cdp, code, timeout_sec):
    _, iframe_t, _ = cdp.find_targets()
    if not iframe_t:
        raise RuntimeError("未找到 Jupyter 编辑器 iframe")
    idx = ensure_terminal_cell(cdp, iframe_t["targetId"], code)
    run_cell_and_stream(cdp, iframe_t["targetId"], idx, timeout_sec=timeout_sec)


def cmd_sh(cdp, shell_cmd, timeout_sec):
    py_wrapper = (
        "import subprocess, sys\n"
        f"_p = subprocess.run({json.dumps(shell_cmd)}, shell=True, text=True, capture_output=True)\n"
        "if _p.stdout: sys.stdout.write(_p.stdout)\n"
        "if _p.stderr: sys.stderr.write(_p.stderr)\n"
        "print(f'[EXIT_CODE={_p.returncode}]')\n"
    )
    cmd_py(cdp, py_wrapper, timeout_sec)


def cmd_push(cdp, index, file_path):
    _, iframe_t, _ = cdp.find_targets()
    if not iframe_t:
        raise RuntimeError("未找到 Jupyter 编辑器 iframe")
    src = (
        Path(file_path).read_text(encoding="utf-8")
        if file_path
        else (CELLS_DIR / f"cell_{index:02d}.py").read_text(encoding="utf-8")
    )
    cdp.eval_in_target(
        iframe_t["targetId"],
        f"""(() => {{
            const w = window.jupyterapp.shell.currentWidget;
            const c = w.content.model.cells.get({index});
            if (!c) throw new Error("单元索引不存在: {index}");
            c.sharedModel.setSource({json.dumps(src)});
            return true;
        }})()""",
    )
    print(f"已更新网页单元 [{index:02d}] ({len(src)} 字符)")


def cmd_set_cell(cdp, index, cell_type, file_path):
    _, iframe_t, _ = cdp.find_targets()
    if not iframe_t:
        raise RuntimeError("未找到 Jupyter 编辑器 iframe")
    src = Path(file_path).read_text(encoding="utf-8")
    res = cdp.eval_in_target(
        iframe_t["targetId"],
        f"""(() => {{
            const w = window.jupyterapp.shell.currentWidget;
            const cells = w.content.model.cells;
            while (cells.length <= {index}) {{
                w.content.activeCellIndex = cells.length - 1;
                window.jupyterapp.commands.execute('notebook:insert-cell-below');
            }}
            w.content.activeCellIndex = {index};
            w.content.deselectAll();
            const wantedType = {json.dumps(cell_type)};
            if (cells.get({index}).type !== wantedType) {{
                if (wantedType === 'markdown') {{
                    window.jupyterapp.commands.execute('notebook:change-cell-to-markdown');
                }} else {{
                    window.jupyterapp.commands.execute('notebook:change-cell-to-code');
                }}
            }}
            const c = cells.get({index});
            c.sharedModel.setSource({json.dumps(src)});
            if (wantedType === 'markdown') {{
                window.jupyterapp.commands.execute('notebook:run-cell');
            }}
            return {{ index: {index}, type: c.type, length: {len(src)} }};
        }})()""",
    )
    print(f"已设置单元 [{res['index']:02d}] ({res['type']}, {res['length']} 字符)")


def cmd_trim_from(cdp, start_index):
    _, iframe_t, _ = cdp.find_targets()
    if not iframe_t:
        raise RuntimeError("未找到 Jupyter 编辑器 iframe")
    res = cdp.eval_in_target(
        iframe_t["targetId"],
        f"""(() => {{
            const w = window.jupyterapp.shell.currentWidget;
            const cells = w.content.model.cells;
            let removed = 0;
            while (cells.length > {start_index}) {{
                w.content.activeCellIndex = cells.length - 1;
                w.content.deselectAll();
                window.jupyterapp.commands.execute('notebook:delete-cell');
                removed++;
            }}
            return {{ remaining: cells.length, removed: removed }};
        }})()""",
    )
    print(f"已清理多余尾部单元: 移除 {res['removed']} 个，当前共 {res['remaining']} 个单元")


def cmd_run_cell(cdp, index, timeout_sec):
    _, iframe_t, _ = cdp.find_targets()
    if not iframe_t:
        raise RuntimeError("未找到 Jupyter 编辑器 iframe")
    run_cell_and_stream(cdp, iframe_t["targetId"], index, timeout_sec=timeout_sec)


def main():
    parser = argparse.ArgumentParser(description="Kaggle Notebook 本地免弹窗控制器")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    sub.add_parser("pull")

    p_py = sub.add_parser("py")
    p_py.add_argument("code")
    p_py.add_argument("--timeout", type=int, default=600)

    p_sh = sub.add_parser("sh")
    p_sh.add_argument("command")
    p_sh.add_argument("--timeout", type=int, default=600)

    p_push = sub.add_parser("push")
    p_push.add_argument("index", type=int)
    p_push.add_argument("--file")

    p_set = sub.add_parser("set-cell")
    p_set.add_argument("index", type=int)
    p_set.add_argument("cell_type", choices=["code", "markdown"])
    p_set.add_argument("file")

    p_trim = sub.add_parser("trim-from")
    p_trim.add_argument("start_index", type=int)

    p_run = sub.add_parser("run-cell")
    p_run.add_argument("index", type=int)
    p_run.add_argument("--timeout", type=int, default=600)

    args = parser.parse_args()
    cdp = CDPSession()
    try:
        if args.cmd == "status":
            cmd_status(cdp)
        elif args.cmd == "pull":
            cmd_pull(cdp)
        elif args.cmd == "py":
            cmd_py(cdp, args.code, args.timeout)
        elif args.cmd == "sh":
            cmd_sh(cdp, args.command, args.timeout)
        elif args.cmd == "push":
            cmd_push(cdp, args.index, args.file)
        elif args.cmd == "set-cell":
            cmd_set_cell(cdp, args.index, args.cell_type, args.file)
        elif args.cmd == "trim-from":
            cmd_trim_from(cdp, args.start_index)
        elif args.cmd == "run-cell":
            cmd_run_cell(cdp, args.index, args.timeout)
    finally:
        cdp.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
