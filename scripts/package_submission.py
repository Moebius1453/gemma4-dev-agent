"""离线校验并打包 Gemma 4 Developer Agent 比赛提交归档。

比赛后台不接受任意 Python 入口文件，只接受以 agent.yaml 为根的声明式目录。
本脚本在无 GPU 和无 Kaggle 容器环境下对 submission 目录做离线结构检查，
确保无符号链接、无越界 !include 路径、单根配置文件、允许的文件扩展名以及总大小不超过 3 GiB，
并可直接切换声明的官方量化模型别名后生成 submission.zip。
"""

import argparse
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

ALLOWED_MODELS = (
    "gemma-4-31b-it-qat-w4a16-ct",
    "gemma-4-12b-it-qat-w4a16-ct",
    "gemma-4-e4b-it-qat-w4a16-ct",
    "gemma-4-e2b-it-qat-w4a16-ct",
)

ROOT_CONFIG_NAMES = (
    "agent.yaml",
    "agent.yml",
    "root_agent.yaml",
    "root_agent.yml",
)

ALLOWED_EXTENSIONS = {
    ".yaml",
    ".yml",
    ".md",
    ".txt",
    ".json",
    ".safetensors",
}

MAX_SUBMISSION_SIZE_BYTES = 3 * 1024 * 1024 * 1024
INCLUDE_PATTERN = re.compile(r"!include\s+([^\s#]+)")


def validate_submission_tree(root_dir: Path) -> tuple[int, int]:
    if not root_dir.is_dir():
        raise FileNotFoundError(f"提交源目录不存在: {root_dir}")

    found_roots = [name for name in ROOT_CONFIG_NAMES if (root_dir / name).is_file()]
    if len(found_roots) != 1:
        raise ValueError(
            f"根目录必须且只能存在一个主配置文件 {ROOT_CONFIG_NAMES}，当前找到: {found_roots}"
        )

    file_count = 0
    total_bytes = 0
    root_resolved = root_dir.resolve()

    for dirpath, dirnames, filenames in os.walk(root_dir, followlinks=False):
        parent = Path(dirpath)
        for name in (*dirnames, *filenames):
            candidate = parent / name
            if candidate.is_symlink():
                raise ValueError(f"比赛禁止提交目录包含符号链接: {candidate.relative_to(root_dir)}")

        for fname in filenames:
            fpath = parent / fname
            rel = fpath.relative_to(root_dir)
            ext = fpath.suffix.lower()
            if ext not in ALLOWED_EXTENSIONS:
                raise ValueError(f"存在不允许的文件扩展名 {ext}: {rel}")

            size = fpath.stat().st_size
            file_count += 1
            total_bytes += size

            if ext in (".yaml", ".yml"):
                text = fpath.read_text(encoding="utf-8")
                for match in INCLUDE_PATTERN.finditer(text):
                    inc_raw = match.group(1).strip("'\"")
                    inc_path = (fpath.parent / inc_raw).resolve()
                    try:
                        inc_path.relative_to(root_resolved)
                    except ValueError as exc:
                        raise ValueError(f"!include 路径越出提交根目录: {inc_raw}") from exc
                    if not inc_path.is_file():
                        raise FileNotFoundError(f"!include 引用的文件不存在: {inc_raw}")

    if total_bytes > MAX_SUBMISSION_SIZE_BYTES:
        raise ValueError(f"展开总大小超过 3 GiB 上限: {total_bytes} bytes")

    return file_count, total_bytes


def rewrite_model_alias(staging_dir: Path, model_name: str) -> None:
    yaml_targets = [
        staging_dir / "agent.yaml",
        *sorted((staging_dir / "sub_agents").glob("*.yaml")),
    ]
    for ypath in yaml_targets:
        if ypath.is_file():
            text = ypath.read_text(encoding="utf-8")
            text = re.sub(
                r"^(\s*model:\s*).*$",
                rf"\g<1>{model_name}",
                text,
                flags=re.MULTILINE,
            )
            ypath.write_text(text, encoding="utf-8")


def build_archive(source_dir: Path, output_zip: Path, model_name: str | None) -> None:
    with tempfile.TemporaryDirectory(prefix="gemma4_pkg_") as tmp:
        staging = Path(tmp) / "submission"
        shutil.copytree(source_dir, staging, symlinks=True)
        if model_name is not None:
            rewrite_model_alias(staging, model_name)

        file_count, total_bytes = validate_submission_tree(staging)
        output_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for item in sorted(staging.rglob("*")):
                rel = item.relative_to(staging).as_posix()
                if item.is_dir():
                    zf.writestr(rel.rstrip("/") + "/", b"")
                else:
                    zf.write(item, arcname=rel)

    print(
        f"Created {output_zip} "
        f"(files={file_count}, unpacked={total_bytes} B, zip={output_zip.stat().st_size} B)"
    )


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="离线校验并打包 Gemma 4 比赛提交包")
    parser.add_argument(
        "--source",
        type=Path,
        default=repo_root / "submission",
        help="声明式 Agent 提交源目录",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "dist" / "submission.zip",
        help="输出的 zip 文件路径",
    )
    parser.add_argument(
        "--model",
        choices=ALLOWED_MODELS,
        default=None,
        help="可选覆盖 agent.yaml 与 sub_agents/*.yaml 中的模型名称",
    )
    args = parser.parse_args()
    build_archive(args.source.resolve(), args.output.resolve(), args.model)


if __name__ == "__main__":
    main()
