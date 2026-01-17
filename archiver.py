from __future__ import annotations
import argparse
import bz2
import os
import shutil
import subprocess
import sys
import tempfile
import tarfile
import threading
import time
from typing import Optional

CHUNK_SIZE = 1024 * 1024  # 1 MiB


def human_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024.0:
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} PiB"


class Spinner:
    def __init__(self, prefix: str = "", enabled: bool = True):
        self._enabled = enabled
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._prefix = prefix

    def start(self):
        if not self._enabled:
            return
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self):
        symbols = "|/-\\"
        idx = 0
        while not self._stop.is_set():
            print(f"\r{self._prefix} {symbols[idx % len(symbols)]}", end="", flush=True)
            idx += 1
            time.sleep(0.08)
        print("\r" + " " * (len(self._prefix) + 4) + "\r", end="", flush=True)

    def stop(self):
        if not self._enabled:
            return
        self._stop.set()
        if self._thread:
            self._thread.join()


def run_subprocess_with_spinner(args, spinner: Spinner) -> int:
    proc = subprocess.Popen(args)
    spinner.start()
    try:
        return_code = proc.wait()
    finally:
        spinner.stop()
    return return_code


def create_tar_of_dir(source_dir: str) -> str:
    tmp = tempfile.NamedTemporaryFile(prefix="archiver_", suffix=".tar", delete=False)
    tmp.close()
    with tarfile.open(tmp.name, "w") as tar:
        tar.add(source_dir, arcname=os.path.basename(source_dir))
    return tmp.name


def compress_bz2(src_path: str, dst_path: str, spinner: Spinner):
    total = os.path.getsize(src_path)
    written = 0
    compressor = bz2.BZ2Compressor()
    with open(src_path, "rb") as fin, open(dst_path, "wb") as fout:
        spinner.start()
        try:
            while True:
                chunk = fin.read(CHUNK_SIZE)
                if not chunk:
                    break
                data = compressor.compress(chunk)
                if data:
                    fout.write(data)
                written += len(chunk)
                print(f"\rCompressed {human_size(written)} / {human_size(total)}", end="", flush=True)
            tail = compressor.flush()
            if tail:
                fout.write(tail)
        finally:
            spinner.stop()
    print("\nCompression finished.")


def decompress_bz2(src_path: str, dst_path: str, spinner: Spinner):
    decompressor = bz2.BZ2Decompressor()
    tmp = tempfile.NamedTemporaryFile(prefix="archiver_out_", delete=False)
    tmp_name = tmp.name
    tmp.close()

    with open(src_path, "rb") as fin, open(tmp_name, "wb") as fout:
        spinner.start()
        try:
            while True:
                chunk = fin.read(CHUNK_SIZE)
                if not chunk:
                    break
                data = decompressor.decompress(chunk)
                if data:
                    fout.write(data)
        finally:
            spinner.stop()
    try:
        if tarfile.is_tarfile(tmp_name):
            print("Detected tar archive inside, extracting...")
            with tarfile.open(tmp_name) as tar:
                if os.path.isdir(dst_path):
                    tar.extractall(path=dst_path)
                else:
                    os.makedirs(dst_path, exist_ok=True)
                    tar.extractall(path=dst_path)
            os.unlink(tmp_name)
            print("Extraction complete.")
        else:
            # Move to dst_path (file)
            if os.path.isdir(dst_path):
                out_file = os.path.join(dst_path, os.path.basename(src_path).rsplit(".", 1)[0])
            else:
                out_file = dst_path
            shutil.move(tmp_name, out_file)
            print(f"Decompressed to {out_file}")
    except Exception:
        # On error, move temp to dst
        if os.path.exists(tmp_name):
            dest = dst_path if not os.path.isdir(dst_path) else os.path.join(dst_path, os.path.basename(src_path) + ".out")
            shutil.move(tmp_name, dest)
            print(f"Decompressed to {dest} (post-error move)")
        raise


def compress_zstd(src_path: str, dst_path: str, spinner: Spinner) -> None:
    zstd_cmd = shutil.which("zstd")
    if not zstd_cmd:
        raise RuntimeError("zstd command not found in PATH. Cannot compress to .zst without external zstd.")
    args = [zstd_cmd, "-o", dst_path, src_path]
    rc = run_subprocess_with_spinner(args, spinner)
    if rc != 0:
        raise RuntimeError(f"zstd failed with exit code {rc}")


def decompress_zstd(src_path: str, dst_path: str, spinner: Spinner) -> None:
    zstd_cmd = shutil.which("zstd")
    if not zstd_cmd:
        raise RuntimeError("zstd command not found in PATH. Cannot decompress .zst without external zstd.")
    tmp = tempfile.NamedTemporaryFile(prefix="archiver_out_", delete=False)
    tmp_name = tmp.name
    tmp.close()
    args = [zstd_cmd, "-d", src_path, "-o", tmp_name]
    rc = run_subprocess_with_spinner(args, spinner)
    if rc != 0:
        raise RuntimeError(f"zstd failed with exit code {rc}")
    if tarfile.is_tarfile(tmp_name):
        print("Detected tar archive inside, extracting...")
        with tarfile.open(tmp_name) as tar:
            if os.path.isdir(dst_path):
                tar.extractall(path=dst_path)
            else:
                os.makedirs(dst_path, exist_ok=True)
                tar.extractall(path=dst_path)
        os.unlink(tmp_name)
        print("Extraction complete.")
    else:
        dest = dst_path if not os.path.isdir(dst_path) else os.path.join(dst_path, os.path.basename(src_path).rsplit('.', 1)[0])
        shutil.move(tmp_name, dest)
        print(f"Decompressed to {dest}")


def parse_args():
    p = argparse.ArgumentParser(description="Archiver/unarchiver supporting .bz2 (builtin) and .zst (requires external zstd). Mode is inferred from filenames.")
    p.add_argument("source", help="Source file or directory (or archive to extract)")
    p.add_argument("target", help="Target file or directory. If target ends with .bz2/.zst -> compress; otherwise if source ends with .bz2/.zst -> decompress to target")
    p.add_argument("-b", "--benchmark", action="store_true", help="Print elapsed time for operation")
    p.add_argument("--spinner", action="store_true", help="Show a small spinner/progress indicator")
    p.add_argument("-f", "--force", action="store_true", help="Overwrite destination files without prompting")
    return p.parse_args()


def main():
    args = parse_args()
    src = args.source
    dst = args.target
    spinner = Spinner(prefix="Working", enabled=args.spinner)

    start = time.perf_counter()
    try:
        src_lower = src.lower()
        dst_lower = dst.lower()
        if dst_lower.endswith(".bz2") or dst_lower.endswith(".zst"):
            if not os.path.exists(src):
                print(f"Source not found: {src}")
                sys.exit(2)
            if os.path.exists(dst) and not args.force:
                print(f"Target exists: {dst}. Use --force to overwrite.")
                sys.exit(3)
            to_compress = src
            temp_tar = None
            try:
                if os.path.isdir(src):
                    print("Source is a directory; creating tar archive...")
                    temp_tar = create_tar_of_dir(src)
                    to_compress = temp_tar
                if dst_lower.endswith(".bz2"):
                    compress_bz2(to_compress, dst, spinner)
                elif dst_lower.endswith(".zst"):
                    compress_zstd(to_compress, dst, spinner)
                print("Done.")
            finally:
                if temp_tar and os.path.exists(temp_tar):
                    os.unlink(temp_tar)
        elif src_lower.endswith(".bz2") or src_lower.endswith(".zst"):
            # Decompress mode
            if not os.path.exists(src):
                print(f"Source not found: {src}")
                sys.exit(2)
            if os.path.isdir(dst) and not args.force:
                # ok, extract into existing dir
                pass
            elif os.path.exists(dst) and not args.force:
                print(f"Target exists: {dst}. Use --force to overwrite/extract anyway.")
                sys.exit(3)
            if src_lower.endswith(".bz2"):
                decompress_bz2(src, dst, spinner)
            else:
                decompress_zstd(src, dst, spinner)
        else:
            print("Cannot infer mode: either target must end with .bz2/.zst to compress, or source must end with .bz2/.zst to decompress.")
            sys.exit(4)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        if args.benchmark:
            elapsed = time.perf_counter() - start
            print(f"Elapsed: {elapsed:.3f} s")


if __name__ == "__main__":
    main()
