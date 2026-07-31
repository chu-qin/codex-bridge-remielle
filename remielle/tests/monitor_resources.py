"""Quick resource monitor for pythonw.exe — no dependencies."""
import subprocess, sys, time, json

def snapshot():
    try:
        out = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-Command",
             "Get-Process pythonw -ErrorAction SilentlyContinue | "
             "Select-Object Id,WorkingSet64,HandleCount,Threads | "
             "ConvertTo-Json"],
            timeout=10,
        )
        data = json.loads(out)
        # ConvertTo-Json can return a single object or an array
        if isinstance(data, dict):
            items = [data]
        else:
            items = data
        return items
    except Exception as e:
        return [{"error": str(e)}]

def fmt(item):
    mb = item.get("WorkingSet64", 0) / (1024 * 1024)
    hc = item.get("HandleCount", "?")
    tc = item.get("Threads", "?")
    return f"PID={item.get('Id','?')}  Mem={mb:.1f}MB  Handles={hc}  Threads={tc}"

if __name__ == "__main__":
    label = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    items = snapshot()
    print(f"[{label}]")
    for item in items:
        print(f"  {fmt(item)}")
    if not items:
        print("  (no pythonw process found)")
