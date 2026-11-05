"""共享换声入口：微信机器人和 QQ 插件都调这一个脚本。

用法：
    python sing_convert.py --input vocals.wav --output-dir 输出目录 [--pitch 0]
                           [--ref ref.wav] [--steps 30]

成功时把生成的 wav 路径打印到 stdout（最后一行以 OUT 开头），退出码 0。
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
SEED = Path(os.environ.get("SEED_VC_DIR", BASE / "tools" / "ai" / "seed-vc"))
PY = SEED / ".venv" / "Scripts" / "python.exe"
DEFAULT_REF = Path(os.environ.get("SEED_VC_REF", BASE / "tools" / "data" / "refs" / "ref.wav"))
CACHE = Path(os.environ.get("SEED_VC_CACHE", BASE / "tools" / "cache" / "indexhome"))


def tool_env() -> dict:
    env = dict(os.environ)
    env.update({
        "USERPROFILE": str(CACHE),
        "HOME": str(CACHE),
        "HF_HOME": str(CACHE / "hf"),
        "HF_ENDPOINT": "https://hf-mirror.com",
        "MPLCONFIGDIR": str(CACHE / "matplotlib"),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def convert(input_path: str, output_dir: str, ref: str, pitch: int, steps: int) -> str:
    src = Path(input_path)
    out = Path(output_dir)
    ref_p = Path(ref or DEFAULT_REF)
    if not src.is_file():
        raise SystemExit(f"人声文件不存在：{src}")
    if not PY.is_file():
        raise SystemExit(f"Seed-VC 环境不存在：{PY}")
    if not ref_p.is_file():
        raise SystemExit(f"参考音不存在：{ref_p}")
    out.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(PY), str(SEED / "inference.py"),
        "--source", str(src),
        "--target", str(ref_p),
        "--output", str(out),
        "--diffusion-steps", str(steps),
        "--f0-condition", "True",
        "--fp16", "True",
    ]
    if pitch:
        cmd += ["--semi-tone-shift", str(int(pitch))]

    print(f"RUN {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, cwd=str(SEED), env=tool_env(),
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        raise SystemExit(f"Seed-VC 退出码 {proc.returncode}：{tail}")

    produced = sorted(out.glob("vc_*.wav"))
    if not produced:
        raise SystemExit("Seed-VC 没生成 vc_*.wav")
    result = max(produced, key=lambda p: p.stat().st_mtime)
    if result.stat().st_size < 4096:
        raise SystemExit(f"生成的 wav 太小：{result}")
    print("OUT " + str(result), flush=True)
    print(json.dumps({"file": str(result)}, ensure_ascii=False), flush=True)
    return str(result)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--ref", default=str(DEFAULT_REF))
    ap.add_argument("--pitch", type=int, default=0)
    ap.add_argument("--steps", type=int, default=30)
    args = ap.parse_args()
    convert(args.input, args.output_dir, args.ref, args.pitch, args.steps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
