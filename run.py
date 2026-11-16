"""Run everything with one command:  python run.py

Starts the model server, opens a Cloudflare tunnel, and prints the link to
open your website with. Press Ctrl+C to stop both.
"""
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

PORT = 8000
# After deploying to GitHub Pages, put your site address here, e.g.
# "https://your-username.github.io/potholedetector/"
PAGES_URL = ""

TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def wait_for_server(timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def watch_tunnel(proc, announced):
    """Read cloudflared output, announce the public URL once, keep draining."""
    for line in proc.stdout:
        m = TUNNEL_RE.search(line)
        if m and not announced.is_set():
            announced.set()
            url = m.group(0)
            site = f"{PAGES_URL.rstrip('/')}/?api={url}" if PAGES_URL else f"<your site address>/?api={url}"
            print("\n" + "=" * 64)
            print(f"  Model server : http://localhost:{PORT}")
            print(f"  Public tunnel: {url}")
            print(f"  Open the site: {site}")
            print("=" * 64 + "\n", flush=True)


def main():
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "server:app",
                               "--host", "127.0.0.1", "--port", str(PORT)])
    procs = [server]
    try:
        if not wait_for_server():
            print("The server did not start. Check the messages above.")
            return

        cloudflared = shutil.which("cloudflared")
        if cloudflared:
            tunnel = subprocess.Popen([cloudflared, "tunnel", "--url", f"http://127.0.0.1:{PORT}"],
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            procs.append(tunnel)
            threading.Thread(target=watch_tunnel, args=(tunnel, threading.Event()),
                             daemon=True).start()
        else:
            print("\ncloudflared is not installed, so only this computer can use the site.")
            print(f"  Model server: http://localhost:{PORT}  (the site uses it by default)\n")

        while server.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        print("\nStopped.")


if __name__ == "__main__":
    main()