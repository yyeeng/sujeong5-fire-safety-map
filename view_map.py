from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import webbrowser


def main() -> None:
    parser = argparse.ArgumentParser(description="수정5동 후보 지도를 로컬 웹주소로 엽니다.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    map_path = root / "output" / "map.html"
    if not map_path.exists():
        raise SystemExit("output/map.html이 없습니다. 먼저 main.py를 실행하세요.")

    class ProjectHandler(SimpleHTTPRequestHandler):
        def __init__(self, *handler_args, **handler_kwargs):
            super().__init__(*handler_args, directory=str(root), **handler_kwargs)

    server = ThreadingHTTPServer((args.host, args.port), ProjectHandler)
    url = f"http://{args.host}:{args.port}/output/map.html"
    print(f"지도 주소: {url}")
    print("종료하려면 이 창에서 Ctrl+C를 누르세요.")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
