"""命令行入口：python -m timesync [--port 8000] [--host 127.0.0.1] [--scenario default]"""

import argparse

from .server import serve
from .app import SCENARIOS


def main() -> None:
    parser = argparse.ArgumentParser(description="漂移时钟校时与可追溯时间轴演示系统")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--scenario", default="default", choices=sorted(SCENARIOS))
    args = parser.parse_args()
    serve(host=args.host, port=args.port, scenario=args.scenario)


if __name__ == "__main__":
    main()
