"""Запустить отдельные процессы стека; Ctrl+C завершает только эти процессы."""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def free_port(port):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--crw-bin", required=True, type=Path)
    parser.add_argument("--searxng", required=True, type=Path)
    parser.add_argument("--searx-python", required=True, type=Path)
    parser.add_argument("--lightpanda-bin", type=Path)
    parser.add_argument("--node-bin", default="node")
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--crw-port", type=int, default=3080)
    parser.add_argument("--searx-port", type=int, default=8890)
    parser.add_argument("--chrome-port", type=int, default=9223)
    parser.add_argument("--gateway-port", type=int, default=8891)
    parser.add_argument("--state-dir", type=Path, default=ROOT.parent / ".cache/stack")
    parser.add_argument("--remote-search", choices=["none", "tavily", "brave"], default="none")
    args = parser.parse_args()
    state = args.state_dir.resolve()
    state.mkdir(parents=True, exist_ok=True)
    ready = state / "browser-ready.json"
    ready.unlink(missing_ok=True)
    for port in (
        [args.crw_port, args.searx_port, args.chrome_port]
        + ([9222] if args.lightpanda_bin else [])
        + ([args.gateway_port] if args.remote_search != "none" else [])
    ):
        free_port(port)
    processes, logs = [], []

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)

    def start(name, argv, cwd, env):
        log = (state / (name + ".log")).open("ab")
        logs.append(log)
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env={**os.environ, **env},
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        processes.append(process)
        return process

    try:
        start(
            "search",
            [
                str(args.searx_python.absolute()),
                "-m",
                "gunicorn",
                "--no-control-socket",
                "--bind",
                f"127.0.0.1:{args.searx_port}",
                "--workers",
                "1",
                "--threads",
                "3",
                "--timeout",
                "35",
                "searx.webapp:app",
            ],
            args.searxng.resolve(),
            {"SEARXNG_SETTINGS_PATH": str(ROOT / "searxng.yml")},
        )
        if args.lightpanda_bin:
            start(
                "lightpanda",
                [
                    str(args.lightpanda_bin.resolve()),
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "9222",
                    "--log-level",
                    "warn",
                    "--block-private-networks",
                ],
                state,
                {},
            )
        browser = start(
            "browser",
            [args.node_bin, "browser.cjs"],
            ROOT,
            {"BROWSER_READY_FILE": str(ready), "CHROME_PORT": str(args.chrome_port)},
        )
        deadline = time.monotonic() + 30
        while not ready.exists():
            if browser.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Браузер не запустился; журнал: " + str(state / "browser.log"))
            time.sleep(0.2)
        config = (ROOT / "crw.toml").read_text()
        config = config.replace("port = 3080", f"port = {args.crw_port}")
        config = config.replace("127.0.0.1:8890", f"127.0.0.1:{args.searx_port}")
        config += (
            "\n[renderer.chrome]\nws_url = "
            + json.dumps(json.loads(ready.read_text())["endpoint"])
            + "\n"
        )
        if args.remote_search != "none":
            start(
                "gateway",
                [sys.executable, str(ROOT / "search_gateway.py")],
                ROOT,
                {
                    "REMOTE_SEARCH": args.remote_search,
                    "SEARCH_GATEWAY_PORT": str(args.gateway_port),
                    "SEARX_URL": f"http://127.0.0.1:{args.searx_port}",
                },
            )
            config = config.replace(
                f"127.0.0.1:{args.searx_port}", f"127.0.0.1:{args.gateway_port}"
            )
        (state / "crw.toml").write_text(config)
        start(
            "crw",
            [str(args.crw_bin.resolve())],
            state,
            {
                "CRW_CONFIG": str(state / "crw.toml"),
                "RUST_LOG": "warn",
                **({"SSL_CERT_FILE": str(args.ca_file.resolve())} if args.ca_file else {}),
            },
        )
        (state / "pids.json").write_text(json.dumps([p.pid for p in processes]))
        print(f"CRW: http://127.0.0.1:{args.crw_port}; журналы: " + str(state), flush=True)
        while all(p.poll() is None for p in processes):
            time.sleep(1)
        raise RuntimeError("Один из процессов остановился; проверьте журнал")
    except KeyboardInterrupt:
        pass
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
