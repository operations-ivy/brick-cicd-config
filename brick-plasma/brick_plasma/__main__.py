"""brick-plasma: owns brick9000's button lights and takes orders over HTTP."""

import os
import threading

import structlog

from . import apa102, displays, logs, patterns, renderer, signals, stats, web

log = structlog.get_logger("brick_plasma")


def main() -> None:
    logs.configure("brick-plasma")
    host = os.environ.get("BRICK_PLASMA_HOST", "0.0.0.0")
    port = int(os.environ.get("BRICK_PLASMA_PORT", "8766"))
    counters = stats.Stats()
    chain = apa102.open_chain()
    catalog = web.Catalog(*displays.load(os.environ.get("BRICK_PLASMA_DISPLAYS", displays.DIRECTORY)))
    dark = (apa102.encode(patterns.solid(patterns.OFF)[0]),)
    render = None
    board = signals.Signals(on_change=lambda: render.wake.set())
    render = renderer.Renderer(board, chain, dark, counters)
    server = web.make_server(host, port, catalog, board, counters)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("brick-plasma up", host=host, port=port, displays=len(catalog.named),
             problems=len(catalog.errors))
    render.run(threading.Event())


if __name__ == "__main__":
    main()
