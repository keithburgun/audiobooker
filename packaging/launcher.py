"""Audiobooker.exe — small launcher that sets up the Python/CUDA runtime on
first run, then starts the real app.

Why a launcher: PyTorch with CUDA is ~3 GB, too big to ship sensibly inside an
installer. Instead the installer is small, and on first launch this downloads
a private Python 3.11, PyTorch for the user's GPU and the app's libraries into
%LOCALAPPDATA%\\Audiobooker\\runtime using `uv` (bundled). Later launches go
straight to the app. Run with --repair to rebuild the runtime.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

LAUNCHER_VERSION = "2"
PYTHON_VERSION = "3.11"
CHATTERBOX = "chatterbox-tts==0.1.7"
CUDA_CHECK = ("import torch, sys; x = torch.ones(8, device='cuda') * 2; torch.cuda.synchronize(); "
              "print('CUDA OK on', torch.cuda.get_device_name(0)); sys.exit(0 if float(x.sum()) == 16 else 4)")
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

if getattr(sys, "frozen", False):
    INSTALL_DIR = Path(sys.executable).resolve().parent
else:
    INSTALL_DIR = Path(__file__).resolve().parent.parent
APP_SRC = INSTALL_DIR / "app"
REQS = APP_SRC / "requirements.txt"
UV = INSTALL_DIR / ("uv.exe" if sys.platform == "win32" else "uv")
if not UV.exists():
    UV = Path(shutil.which("uv") or "uv")

DATA = Path(os.environ.get("AUDIOBOOKER_HOME") or
            (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Audiobooker"))
RUNTIME = DATA / "runtime"
VENV = RUNTIME / "venv"
STAMP = RUNTIME / "installed.json"
LOG = DATA / "logs" / "setup.log"
# Python shipped inside the installer. Using it avoids `uv python install`,
# whose folder junctions some Windows 11 security settings refuse to follow
# ("untrusted mount point", os error 448).
BUNDLED_PY = INSTALL_DIR / "python" / ("python.exe" if sys.platform == "win32" else "bin/python3")
if sys.platform == "win32":
    PY = VENV / "Scripts" / "python.exe"
    PYW = VENV / "Scripts" / "pythonw.exe"
else:
    PY = PYW = VENV / "bin" / "python"


def spec_hash() -> str:
    h = hashlib.sha256()
    h.update(REQS.read_bytes() if REQS.exists() else b"")
    base = str(BUNDLED_PY) if BUNDLED_PY.exists() else PYTHON_VERSION
    h.update(f"{LAUNCHER_VERSION}|{base}|{CHATTERBOX}".encode())
    return h.hexdigest()[:16]


def runtime_ok() -> bool:
    try:
        return PY.exists() and json.loads(STAMP.read_text())["spec"] == spec_hash()
    except Exception:
        return False


def torch_indexes_for(gpu: dict) -> list[str]:
    """Best-first list of PyTorch CUDA builds for this card and driver.

    cu128/cu130 builds need driver >= 570/580 and have dropped older cards
    (Maxwell/Pascal/Volta, compute capability < 7.5); cu126 still covers them."""
    try:
        drv = float(".".join(gpu.get("driver", "0").split(".")[:2]))
    except ValueError:
        drv = 0.0
    try:
        cc = float(gpu.get("cc") or 0)
    except ValueError:
        cc = 0.0
    if (cc and cc < 7.5) or (drv and drv < 570):
        return ["cu126", "cu128"]
    if drv >= 580:
        return ["cu128", "cu130", "cu126"]
    return ["cu128", "cu126"]


def detect_nvidia() -> dict:
    """{'name', 'driver', 'cc'} for the first NVIDIA GPU, or {} if none."""
    candidates = ["nvidia-smi"]
    if sys.platform == "win32":
        candidates.append(str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"))
    for exe in candidates:
        for query in ("name,driver_version,compute_cap", "name,driver_version"):
            try:
                out = subprocess.run([exe, f"--query-gpu={query}", "--format=csv,noheader"],
                                     capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW)
                if out.returncode == 0 and out.stdout.strip():
                    parts = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]
                    return {"name": parts[0], "driver": parts[1] if len(parts) > 1 else "",
                            "cc": parts[2] if len(parts) > 2 else ""}
            except Exception:
                continue
    return {}


class Setup:
    def __init__(self, repair: bool, headless: bool = False):
        self.repair = repair
        self.headless = headless
        self.q: queue.Queue = queue.Queue()
        self.cancelled = False
        self.proc: subprocess.Popen | None = None
        self.ok = False
        if headless:
            return

        self.root = tk.Tk()
        self.root.title("Audiobooker — first-time setup")
        self.root.geometry("640x420")
        self.root.minsize(520, 360)
        ico = INSTALL_DIR / "icon.ico"
        if ico.exists():
            try:
                self.root.iconbitmap(str(ico))
            except Exception:
                pass
        frm = ttk.Frame(self.root, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Setting up Audiobooker", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(frm, wraplength=600, justify="left", text=(
            "This happens once. Audiobooker is downloading its own copy of Python, PyTorch for your graphics "
            "card and the default voice model — about 3–4 GB in total, so it can take a while on a slow "
            "connection. Nothing is installed system-wide.")).pack(anchor="w", pady=(6, 12))
        self.step = tk.StringVar(value="Starting…")
        ttk.Label(frm, textvariable=self.step).pack(anchor="w")
        self.bar = ttk.Progressbar(frm, mode="determinate", maximum=7)
        self.bar.pack(fill="x", pady=(4, 10))
        self.text = tk.Text(frm, height=12, wrap="none", font=("Consolas", 8), state="disabled",
                            background="#f4f4f4", relief="flat")
        self.text.pack(fill="both", expand=True)
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(10, 0))
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.cancel)
        self.cancel_btn.pack(side="right")
        self.root.protocol("WM_DELETE_WINDOW", self.cancel)

    # -- ui plumbing -----------------------------------------------------------
    def log(self, s: str):
        if self.headless:
            print(s.rstrip(), flush=True)
        else:
            self.q.put(("log", s))

    def set_step(self, n: int, s: str):
        if self.headless:
            print(f"== [{n}/7] {s}", flush=True)
        else:
            self.q.put(("step", (n, s)))

    def pump(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.text.configure(state="normal")
                    self.text.insert("end", val.rstrip() + "\n")
                    lines = int(self.text.index("end-1c").split(".")[0])
                    if lines > 2000:
                        self.text.delete("1.0", f"{lines - 2000}.0")
                    self.text.see("end")
                    self.text.configure(state="disabled")
                elif kind == "step":
                    n, s = val
                    self.bar["value"] = n
                    self.step.set(s)
                elif kind == "done":
                    self.ok = True
                    self.root.destroy()
                    return
                elif kind == "error":
                    messagebox.showerror("Audiobooker setup failed", val + f"\n\nDetails were saved to:\n{LOG}\n\n"
                                         "Check your internet connection and start Audiobooker again; it will "
                                         "pick up where it left off.")
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def cancel(self):
        if messagebox.askyesno("Audiobooker", "Stop setup? You can resume it next time you open Audiobooker."):
            self.cancelled = True
            if self.proc:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.root.destroy()

    # -- work -------------------------------------------------------------------
    def run_cmd(self, args: list, env=None) -> int:
        if self.cancelled:
            raise SystemExit(1)
        self.log("> " + " ".join(str(a) for a in args))
        e = dict(os.environ)
        e.update({
            "UV_PYTHON_INSTALL_DIR": str(RUNTIME / "python"),
            "UV_CACHE_DIR": os.environ.get("AUDIOBOOKER_UV_CACHE", str(RUNTIME / "uv-cache")),
            "UV_LINK_MODE": "copy",
            "UV_HTTP_TIMEOUT": "600",
            "UV_NO_PROGRESS": "1",
            "PYTHONUTF8": "1",
            "AUDIOBOOKER_HOME": str(DATA),
        })
        if env:
            e.update(env)
        self.proc = subprocess.Popen([str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", env=e,
                                     cwd=str(APP_SRC), creationflags=NO_WINDOW)
        with open(LOG, "a", encoding="utf-8") as lf:
            lf.write("> " + " ".join(str(a) for a in args) + "\n")
            for line in self.proc.stdout:
                lf.write(line)
                self.log(line)
        rc = self.proc.wait()
        self.proc = None
        if self.cancelled:
            raise SystemExit(1)
        return rc

    def must(self, args, what: str, env=None):
        for attempt in range(3):
            if self.run_cmd(args, env) == 0:
                return
            self.log(f"(attempt {attempt + 1} failed, retrying…)")
            time.sleep(3)
        raise RuntimeError(f"Could not {what}.")

    def work(self):
        try:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            RUNTIME.mkdir(parents=True, exist_ok=True)
            if self.repair and VENV.exists():
                self.set_step(0, "Removing the old runtime…")
                shutil.rmtree(VENV, ignore_errors=True)
            STAMP.unlink(missing_ok=True)

            if BUNDLED_PY.exists():
                python_arg = str(BUNDLED_PY)
                self.log(f"Using bundled Python: {BUNDLED_PY}")
                # A venv made from a different base Python (older version of
                # this app, or a moved install) must be rebuilt.
                cfg = VENV / "pyvenv.cfg"
                if cfg.exists():
                    home = next((l.split("=", 1)[1].strip() for l in cfg.read_text("utf-8", "replace").splitlines()
                                 if l.strip().lower().startswith("home")), "")
                    if Path(home).resolve() != BUNDLED_PY.parent.resolve() and \
                            Path(home).resolve() != (BUNDLED_PY.parent / "bin").resolve():
                        self.log(f"Rebuilding environment (was based on {home})")
                        shutil.rmtree(VENV, ignore_errors=True)
                # Leftovers from an earlier failed `uv python install`.
                shutil.rmtree(RUNTIME / "python", ignore_errors=True)
            else:
                self.set_step(0, f"Downloading Python {PYTHON_VERSION}…")
                self.must([UV, "python", "install", PYTHON_VERSION], "download Python")
                python_arg = PYTHON_VERSION

            self.set_step(1, "Creating the runtime environment…")
            if not PY.exists():
                self.must([UV, "venv", VENV, "--python", python_arg, "--seed"], "create the environment")

            gpu = detect_nvidia()
            self.log(f"NVIDIA GPU: {gpu or 'none found'}")
            if gpu:
                have_torch = self.run_cmd([PY, "-c", "import torchaudio; " + CUDA_CHECK]) == 0
            else:
                have_torch = self.run_cmd([PY, "-c", "import torch, torchaudio"]) == 0
            forced = os.environ.get("AUDIOBOOKER_TORCH_INDEX")  # e.g. "cpu", "cu128" or "pypi"
            if not have_torch and forced:
                self.set_step(2, f"Downloading PyTorch ({forced})…")
                extra = [] if forced == "pypi" else ["--index-url", f"https://download.pytorch.org/whl/{forced}"]
                self.must([UV, "pip", "install", "--python", PY, "torch", "torchaudio", *extra], "download PyTorch")
            elif not have_torch:
                if gpu:
                    self.set_step(2, f"Downloading PyTorch with CUDA for your {gpu['name']} (≈2.5 GB)…")
                    installed_any = False
                    for idx in torch_indexes_for(gpu):
                        if self.run_cmd([UV, "pip", "install", "--python", PY, "torch", "torchaudio",
                                         "--reinstall-package", "torch", "--reinstall-package", "torchaudio",
                                         "--index-url", f"https://download.pytorch.org/whl/{idx}"]) != 0:
                            continue
                        installed_any = True
                        if self.run_cmd([PY, "-c", CUDA_CHECK]) == 0:
                            break
                        self.log(f"PyTorch {idx} installed but could not use the GPU; trying another build…")
                    else:
                        if not installed_any:
                            raise RuntimeError("Could not download PyTorch with CUDA support.")
                        self.log("WARNING: no PyTorch build could use this GPU; Audiobooker will run on the CPU. "
                                 "Updating your NVIDIA driver and choosing 'repair setup' may fix this.")
                else:
                    self.set_step(2, "No NVIDIA GPU found — downloading the CPU version of PyTorch…")
                    self.must([UV, "pip", "install", "--python", PY, "torch", "torchaudio",
                               "--index-url", "https://download.pytorch.org/whl/cpu"], "download PyTorch")

            self.set_step(3, "Downloading Audiobooker's libraries…")
            self.must([UV, "pip", "install", "--python", PY, "-r", REQS], "install libraries")
            self.must([UV, "pip", "install", "--python", PY, "--no-deps", CHATTERBOX], "install Chatterbox")

            self.set_step(4, "Checking the installation…")
            self.must([PY, "-c", "import torch, pymupdf, PySide6, kokoro, chatterbox.tts, rapidocr_onnxruntime, perth; "
                                 "perth.PerthImplicitWatermarker(); "
                                 "print('torch', torch.__version__, 'cuda', torch.cuda.is_available())"],
                      "verify the installation")

            self.set_step(5, "Downloading the default voice model (Kokoro, ≈340 MB)…")
            rc = self.run_cmd([PY, "-c",
                               "from audiobooker import paths; paths.configure_model_cache(); "
                               "from audiobooker.engines.kokoro_engine import REPO, FILES; "
                               "from audiobooker.downloads import ensure_repo; "
                               "ensure_repo(REPO, FILES, 'Kokoro', lambda f, m: print(m, flush=True))"],
                              env={"PYTHONPATH": str(APP_SRC)})
            if rc != 0:
                self.log("(voice model download failed; the app will retry when it is needed)")

            self.set_step(6, "Tidying up…")
            self.run_cmd([UV, "cache", "clean"])
            STAMP.write_text(json.dumps({"spec": spec_hash(), "gpu": gpu, "when": time.time()}))
            self.set_step(7, "Ready")
            self.q.put(("done", None))
        except SystemExit:
            pass
        except Exception as e:  # noqa: BLE001
            self.log(f"ERROR: {e}")
            self.q.put(("error", str(e)))

    def run(self) -> bool:
        if self.headless:
            self.work()
            kind, val = self.q.get_nowait()
            if kind == "error":
                print("SETUP FAILED:", val, flush=True)
            return kind == "done"
        threading.Thread(target=self.work, daemon=True).start()
        self.root.after(100, self.pump)
        self.root.mainloop()
        return self.ok


def launch_app(args: list[str]) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(APP_SRC)
    env["AUDIOBOOKER_HOME"] = str(DATA)
    env.pop("PYTHONHOME", None)
    env["PYTHONUTF8"] = "1"
    env["TQDM_DISABLE"] = "1"  # model progress bars would bloat the log
    err_log = DATA / "logs" / "app-stderr.log"
    err_log.parent.mkdir(parents=True, exist_ok=True)
    with open(err_log, "w", encoding="utf-8") as ef:
        p = subprocess.Popen([str(PYW), "-m", "audiobooker", *args], cwd=str(APP_SRC), env=env,
                             stdout=ef, stderr=ef, creationflags=NO_WINDOW)
    # If it dies straight away, say so instead of failing silently.
    for _ in range(40):
        time.sleep(0.25)
        rc = p.poll()
        if rc is not None:
            if rc != 0:
                tail = err_log.read_text("utf-8", "replace")[-1500:]
                r = tk.Tk()
                r.withdraw()
                if messagebox.askyesno("Audiobooker", f"Audiobooker could not start:\n\n{tail}\n\n"
                                       "Repair the installation now?"):
                    r.destroy()
                    if Setup(repair=True).run():
                        launch_app(args)
                else:
                    r.destroy()
            return


def main() -> int:
    args = [a for a in sys.argv[1:]]
    flags = {a for a in args if a in ("--repair", "--setup-only", "--headless")}
    args = [a for a in args if a not in flags]
    repair = "--repair" in flags
    if repair or not runtime_ok():
        if not Setup(repair, headless="--headless" in flags).run():
            return 1
    if "--setup-only" in flags:
        return 0
    launch_app(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
