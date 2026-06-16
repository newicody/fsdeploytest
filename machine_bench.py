#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
machine_bench.py — inventaire + bench de l'appliance (a lancer une fois bootee).

Donne les proprietes reelles de la machine et mesure ce qui compte pour CETTE
activite : inference LLM (CPU/AVX, memoire), stockage (NVMe stripe fast_pool :
debit + IOPS), ZFS (ARC, compression), GPU (xe/i915). Produit un rapport texte
+ JSON (--json) a coller pour analyse d'axes d'amelioration.

NON destructif : ecrit uniquement dans un repertoire de travail temporaire
(--workdir, defaut /fast_pool/tmp/bench) qu'il nettoie. Stdlib uniquement.

Usage :
  python3 machine_bench.py
  python3 machine_bench.py --json /tmp/bench.json --workdir /fast_pool/tmp/bench
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import time


def sh(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


# --------------------------------------------------------------------------- #
# inventaire
# --------------------------------------------------------------------------- #
def inventory():
    inv = {}
    inv["kernel"] = platform.release()
    inv["arch"] = platform.machine()

    # CPU
    cpu = read("/proc/cpuinfo")
    model = next((l.split(":", 1)[1].strip() for l in cpu.splitlines()
                  if l.startswith("model name")), "?")
    flags = next((l.split(":", 1)[1].split() for l in cpu.splitlines()
                  if l.startswith("flags")), [])
    inv["cpu_model"] = model
    inv["cpu_threads"] = os.cpu_count()
    # jeux d'instructions pertinents pour l'inference
    inv["cpu_avx"] = [f for f in ("avx", "avx2", "avx512f", "avx512_vnni",
                                  "avx_vnni", "fma", "f16c", "sse4_2")
                      if f in flags]

    # memoire
    mem = read("/proc/meminfo")
    for key in ("MemTotal", "MemAvailable"):
        v = next((l.split()[1] for l in mem.splitlines()
                  if l.startswith(key)), "0")
        inv[key.lower()] = f"{int(v) // 1024} MiB"

    # GPU
    rc, out, _ = sh(["lspci", "-nnk"])
    gpu_lines = [l for l in out.splitlines()
                 if "VGA" in l or "Display" in l or "3D" in l]
    inv["gpu"] = gpu_lines[:2]
    rc, lsmod, _ = sh(["lsmod"])
    inv["gpu_driver"] = [d for d in ("xe", "i915")
                         if any(line.split()[0] == d
                                for line in lsmod.splitlines()[1:])]

    # ZFS pools
    rc, zpool, _ = sh(["zpool", "list", "-H", "-o", "name,size,health,frag,cap"])
    inv["zpools"] = [dict(zip(("name", "size", "health", "frag", "cap"),
                              l.split("\t"))) for l in zpool.splitlines() if l]
    # ARC max
    arc = read("/proc/spl/kstat/zfs/arcstats")
    csize = next((l.split()[2] for l in arc.splitlines()
                  if l.startswith("c_max")), "0")
    inv["zfs_arc_max"] = f"{int(csize) // (1024*1024)} MiB" if csize.isdigit() else "?"

    # disques
    rc, lsblk, _ = sh(["lsblk", "-dno", "NAME,SIZE,ROTA,MODEL"])
    inv["disks"] = [l.strip() for l in lsblk.splitlines() if l.strip()]

    # mode secours actif ?
    inv["rescue_mode"] = os.path.exists("/etc/rescue-mode")
    return inv


# --------------------------------------------------------------------------- #
# benchmarks
# --------------------------------------------------------------------------- #
def bench_cpu_compute():
    """Charge CPU representative de l'inference : produit matriciel dense
    repete. Utilise numpy s'il est la (BLAS/AVX), sinon pur Python (indicatif).
    Retourne GFLOP/s approx + backend."""
    res = {"backend": None}
    try:
        import numpy as np
        n = 1024
        a = np.random.rand(n, n).astype("float32")
        b = np.random.rand(n, n).astype("float32")
        # warmup
        np.dot(a, b)
        iters = 20
        t0 = time.time()
        for _ in range(iters):
            c = np.dot(a, b)
        dt = time.time() - t0
        flops = 2.0 * n**3 * iters
        res["backend"] = "numpy/BLAS"
        res["gflops"] = round(flops / dt / 1e9, 1)
        res["matmul_1024_ms"] = round(dt / iters * 1000, 2)
        # config BLAS si exposable
        try:
            res["numpy_config"] = np.__config__.show(mode="dicts").get(
                "Build Dependencies", {}).get("blas", {}).get("name", "?")
        except Exception:
            res["numpy_config"] = "?"
    except ImportError:
        # pur Python : juste un ordre de grandeur (tres lent)
        n = 128
        t0 = time.time()
        a = [[(i * j) % 7 for j in range(n)] for i in range(n)]
        s = sum(a[i][j] for i in range(n) for j in range(n))
        res["backend"] = "pur-python (numpy absent)"
        res["loop_128_ms"] = round((time.time() - t0) * 1000, 2)
    return res


def bench_mem_bandwidth():
    """Bande passante memoire approx via copie de gros buffers."""
    try:
        import numpy as np
        size = 256 * 1024 * 1024 // 8        # 256 Mo de float64
        a = np.ones(size)
        t0 = time.time()
        for _ in range(5):
            b = a.copy()
        dt = time.time() - t0
        gb = 256 * 5 / 1024.0
        return {"copy_gb_s": round(gb / dt, 1)}
    except ImportError:
        return {"copy_gb_s": None, "note": "numpy absent"}


def bench_disk(workdir):
    """Debit sequentiel ecriture/lecture + latence sur fast_pool (NVMe stripe).
    Non destructif : fichier temporaire de 1 Go dans workdir, supprime ensuite.
    On contourne le cache en lecture via posix_fadvise DONTNEED si possible."""
    os.makedirs(workdir, exist_ok=True)
    f = os.path.join(workdir, "bench.dat")
    size_mb = 1024
    block = 1024 * 1024
    buf = b"\xab" * block
    res = {}
    # ecriture sequentielle + fsync (mesure le vrai commit ZFS)
    try:
        t0 = time.time()
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        for _ in range(size_mb):
            os.write(fd, buf)
        os.fsync(fd)
        os.close(fd)
        dt = time.time() - t0
        res["write_seq_mb_s"] = round(size_mb / dt, 1)
    except OSError as e:
        res["write_error"] = str(e)
        return res
    # lecture sequentielle (tente d'invalider le cache)
    try:
        os.system(f"echo 3 > /proc/sys/vm/drop_caches 2>/dev/null")
        t0 = time.time()
        fd = os.open(f, os.O_RDONLY)
        while os.read(fd, block):
            pass
        os.close(fd)
        dt = time.time() - t0
        res["read_seq_mb_s"] = round(size_mb / dt, 1)
    except OSError as e:
        res["read_error"] = str(e)
    # latence petites ecritures (4k) + fsync : sensible pour metadata/DB
    try:
        small = b"\x00" * 4096
        n = 200
        fd = os.open(f, os.O_WRONLY | os.O_TRUNC)
        t0 = time.time()
        for _ in range(n):
            os.write(fd, small)
            os.fsync(fd)
        os.close(fd)
        res["fsync_4k_ms_avg"] = round((time.time() - t0) / n * 1000, 3)
    except OSError as e:
        res["fsync_error"] = str(e)
    finally:
        try:
            os.unlink(f)
        except OSError:
            pass
    return res


def bench_ollama(endpoint="http://127.0.0.1:11434/v1", model=None):
    """Si Ollama tourne : mesure tokens/s sur une generation courte. Sinon skip."""
    import urllib.request
    import urllib.error
    rc, out, _ = sh(["curl", "-s", f"{endpoint}/models"], timeout=5)
    if rc != 0 or not out:
        return {"available": False}
    try:
        models = json.loads(out).get("data", [])
        m = model or (models[0]["id"] if models else None)
    except (json.JSONDecodeError, KeyError, IndexError):
        return {"available": True, "note": "modeles illisibles"}
    if not m:
        return {"available": True, "note": "aucun modele charge"}
    body = json.dumps({"model": m, "stream": False, "max_tokens": 128,
                       "messages": [{"role": "user",
                                     "content": "Compte de 1 a 50."}]}).encode()
    req = urllib.request.Request(f"{endpoint}/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.load(r)
        dt = time.time() - t0
        txt = data["choices"][0]["message"]["content"]
        toks = max(1, len(txt.split()))
        return {"available": True, "model": m,
                "approx_tokens_s": round(toks / dt, 1), "latency_s": round(dt, 1)}
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError) as e:
        return {"available": True, "error": str(e)}


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="inventaire + bench de l'appliance")
    ap.add_argument("--json", default=None, help="ecrit le rapport JSON ici")
    ap.add_argument("--workdir", default="/fast_pool/tmp/bench",
                    help="repertoire de travail pour le bench disque")
    ap.add_argument("--skip-disk", action="store_true")
    ap.add_argument("--skip-llm", action="store_true")
    ap.add_argument("--endpoint", default="http://127.0.0.1:11434/v1")
    a = ap.parse_args()

    print(">> inventaire...", flush=True)
    report = {"timestamp": int(time.time()), "inventory": inventory()}

    print(">> bench CPU (matmul)...", flush=True)
    report["cpu_compute"] = bench_cpu_compute()
    print(">> bench memoire...", flush=True)
    report["mem_bandwidth"] = bench_mem_bandwidth()
    if not a.skip_disk:
        print(">> bench disque (fast_pool, ~1 Go temporaire)...", flush=True)
        report["disk"] = bench_disk(a.workdir)
    if not a.skip_llm:
        print(">> bench inference (si Ollama tourne)...", flush=True)
        report["llm"] = bench_ollama(a.endpoint)

    # rendu texte
    inv = report["inventory"]
    print("\n" + "=" * 60)
    print(f"MACHINE : {inv['cpu_model']}")
    print(f"  threads={inv['cpu_threads']}  mem={inv.get('memtotal')}  "
          f"arch={inv['arch']}  noyau={inv['kernel']}")
    print(f"  AVX/SIMD : {', '.join(inv['cpu_avx']) or 'aucun detecte'}")
    print(f"  GPU driver actif : {', '.join(inv['gpu_driver']) or 'aucun'}")
    print(f"  ARC max ZFS : {inv['zfs_arc_max']}")
    if inv.get("rescue_mode"):
        print("  !! MODE SECOURS ACTIF (fast_pool absent) — bench partiel")
    for p in inv["zpools"]:
        print(f"  pool {p['name']}: {p['size']} {p['health']} "
              f"frag={p['frag']} use={p['cap']}")
    cc = report["cpu_compute"]
    print(f"\nCPU compute : {cc.get('gflops','?')} GFLOP/s ({cc['backend']})")
    print(f"Memoire copie : {report['mem_bandwidth'].get('copy_gb_s','?')} Go/s")
    if "disk" in report:
        d = report["disk"]
        print(f"Disque fast_pool : ecriture {d.get('write_seq_mb_s','?')} Mo/s, "
              f"lecture {d.get('read_seq_mb_s','?')} Mo/s, "
              f"fsync 4k {d.get('fsync_4k_ms_avg','?')} ms")
    if "llm" in report and report["llm"].get("available"):
        l = report["llm"]
        print(f"Inference : ~{l.get('approx_tokens_s','?')} tok/s "
              f"(modele {l.get('model','?')})")
    print("=" * 60)

    if a.json:
        with open(a.json, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nrapport JSON : {a.json}  (colle-le pour analyse)")


if __name__ == "__main__":
    main()
