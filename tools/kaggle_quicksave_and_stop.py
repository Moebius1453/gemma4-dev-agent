"""Kaggle 编辑页零 GPU 消耗 Quick Save（含输出文件）与关停会话脚本。

Kaggle 编辑页 Save Version 弹窗默认选中 Save & Run All (Commit)，直接点击保存会触发云端重跑并消耗 GPU 额度。
本脚本显式切换为 Quick Save 并在 Advanced Settings 中开启保存输出，等版本号递增后再点击 Stop session 停止计费。
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ksh


JS_OPEN_MODAL = """(async () => {
    const btn = document.querySelector("button[aria-label='Save Version']");
    if (!btn) return {error: "Save Version button not found"};
    btn.click();
    await new Promise(r => setTimeout(r, 1200));
    return {opened: true};
})()"""

JS_SAVE = """(async () => {
    let comboboxes = Array.from(document.querySelectorAll("[role=combobox]"));
    let vtBox = comboboxes.find(
        el => (el.innerText || "").includes("Save & Run All") || (el.innerText || "").includes("Quick Save")
    );
    if (!vtBox) return {error: "Version Type selector not found"};

    if (!(vtBox.innerText || "").includes("Quick Save")) {
        vtBox.dispatchEvent(new MouseEvent("mousedown", {bubbles: true, cancelable: true}));
        await new Promise(r => setTimeout(r, 700));
        const options = Array.from(document.querySelectorAll("[role=option], li[data-value]"));
        const qsOpt = options.find(el => (el.innerText || "").includes("Quick Save"));
        if (!qsOpt) return {error: "Quick Save option not found"};
        qsOpt.click();
        await new Promise(r => setTimeout(r, 600));
    }

    const advBtn = Array.from(document.querySelectorAll("button, div[role=button], p, span")).find(
        el => (el.innerText || "").trim() === "Advanced Settings"
    );
    if (advBtn) {
        advBtn.click();
        await new Promise(r => setTimeout(r, 700));
    }

    comboboxes = Array.from(document.querySelectorAll("[role=combobox]"));
    const outBox = comboboxes.find(el => (el.innerText || "").toLowerCase().includes("save output"));
    if (outBox && (outBox.innerText || "").toLowerCase().includes("never")) {
        outBox.dispatchEvent(new MouseEvent("mousedown", {bubbles: true, cancelable: true}));
        await new Promise(r => setTimeout(r, 700));
        const opts = Array.from(document.querySelectorAll("[role=option], li[data-value]"));
        const saveOpt = opts.find(
            el => !(el.innerText || "").toLowerCase().includes("never")
               && (el.innerText || "").toLowerCase().includes("save output")
        );
        if (!saveOpt) return {error: "Save output option not found"};
        saveOpt.click();
        await new Promise(r => setTimeout(r, 600));
    }

    vtBox = Array.from(document.querySelectorAll("[role=combobox]")).find(
        el => (el.innerText || "").includes("Quick Save")
    );
    if (!vtBox) return {error: "Version Type is NOT Quick Save, aborting"};

    const saveBtns = Array.from(document.querySelectorAll("button")).filter(
        b => (b.innerText || "").trim() === "Save"
    );
    if (saveBtns.length === 0) return {error: "Modal Save button not found"};
    saveBtns[saveBtns.length - 1].click();
    await new Promise(r => setTimeout(r, 3000));
    return {
        vtText: vtBox.innerText,
        outText: outBox ? outBox.innerText : "",
        versionsBtn: document.querySelector("button[aria-label='Show Versions']")?.innerText || ""
    };
})()"""

JS_STOP = """(async () => {
    const stopBtn = document.querySelector("button[aria-label='Stop session']");
    if (!stopBtn) return {alreadyStopped: true};
    stopBtn.click();
    await new Promise(r => setTimeout(r, 1500));
    const confirmBtns = Array.from(document.querySelectorAll("button")).filter(b => {
        const t = (b.innerText || "").trim().toLowerCase();
        return t === "stop" || t === "stop session" || t === "confirm" || t === "ok";
    });
    const clickedConfirm = [];
    for (const cb of confirmBtns) {
        if (cb.getAttribute("aria-label") !== "Stop session") {
            clickedConfirm.push(cb.innerText.trim());
            cb.click();
        }
    }
    return {clickedStop: true, clickedConfirm};
})()"""

JS_POLL = """(() => {
    const txt = document.body.innerText;
    const lines = txt.split("\\n").filter(
        l => l.includes("Session") || l.includes("GPU") || l.includes("Starting") || l.includes("Stopping") || l.includes("off")
    );
    const hasStopBtn = !!document.querySelector("button[aria-label='Stop session']");
    const ver = document.querySelector("button[aria-label='Show Versions']")?.innerText || "";
    return {hasStopBtn, ver, lines: lines.slice(0, 10)};
})()"""


def main():
    cdp = ksh.CDPSession()
    try:
        page, _, _ = cdp.find_targets()
        tid = page["targetId"]
        before = cdp.eval_in_target(tid, JS_POLL)
        prev_ver = before["ver"].strip()

        cdp.eval_in_target(tid, JS_OPEN_MODAL)
        res1 = cdp.eval_in_target(tid, JS_SAVE)
        print("Quick Save result:", json.dumps(res1, ensure_ascii=False, indent=2))
        if isinstance(res1, dict) and res1.get("error"):
            return

        for _ in range(15):
            st = cdp.eval_in_target(tid, JS_POLL)
            if st["ver"].strip() not in ("", prev_ver):
                print("Quick Save Version ready:", st["ver"])
                break
            time.sleep(2)

        stop_res = cdp.eval_in_target(tid, JS_STOP)
        print("Stop session result:", json.dumps(stop_res, ensure_ascii=False))

        for i in range(15):
            time.sleep(2)
            st = cdp.eval_in_target(tid, JS_POLL)
            print(f"Poll {i + 1}:", json.dumps(st, ensure_ascii=False))
            if not st["hasStopBtn"]:
                break
    finally:
        cdp.close()


if __name__ == "__main__":
    main()
