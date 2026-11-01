"""brick-plasma: owns brick9000's button lights and takes orders over HTTP."""

import logging
import os
import threading

from . import apa102, displays, patterns, renderer, signals, stats, web

log = logging.getLogger("brick_plasma")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
    log.info("brick-plasma up on %s:%d, %d displays, %d problems", host, port, len(catalog.named),
             len(catalog.errors))
    render.run(threading.Event())


if __name__ == "__main__":
    main()
