"""Run the site:  python3 -m collab_studio  [--host 127.0.0.1] [--port 8470]"""
import argparse
import logging

import uvicorn

from .web import create_app


def main():
    ap = argparse.ArgumentParser(description="Collab Studio web app")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8470)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info", access_log=False)  # our own log redacts private links


if __name__ == "__main__":
    main()
