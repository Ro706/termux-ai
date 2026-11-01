import os, platform, shutil, subprocess

def _command(args):
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL, timeout=3).strip()
    except Exception:
        return "Unavailable"

def system_snapshot():
    try:
        import psutil
        vm = psutil.virtual_memory()
        disk = shutil.disk_usage(os.path.expanduser("~"))
        mem = f"{vm.percent:.0f}% used ({vm.available // (1024**2)} MiB free)"
        disk_text = f"{disk.used // (1024**3)} / {disk.total // (1024**3)} GiB"
        cpu = f"{psutil.cpu_percent(interval=None):.0f}%"
    except Exception:
        mem, disk_text, cpu = "Unavailable", "Unavailable", "Unavailable"
    return {
        "Device": _command(["getprop", "ro.product.model"]),
        "Android": _command(["getprop", "ro.build.version.release"]),
        "Kernel": platform.release(),
        "Architecture": platform.machine(),
        "Python": platform.python_version(),
        "CPU usage": cpu,
        "Memory": mem,
        "Storage": disk_text,
        "Termux": "Yes" if "com.termux" in os.environ.get("PREFIX", "") else "Unknown"
    }
