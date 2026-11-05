"""The named displays, from one drop-in file per source in displays/*.yaml.

A broken file or display is skipped and reported, never fatal: the lights
matter less than the service staying up, and every other source keeps working.
The tests load the real directory, so one can't deploy (brick9000/deploy).
"""

import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import apa102, patterns, signals

log = logging.getLogger(__name__)
DIRECTORY = Path(__file__).resolve().parent.parent / "displays"
MAX_SECONDS = 60.0  # a loop is held in memory, frame by frame
KEYS = {"priority", "ttl", "slots"}  # the rest of a display goes to its effect


class Loader(yaml.SafeLoader):
    """Refuses a repeated key, where YAML would quietly keep the last one."""

    def construct_mapping(self, node, deep=False):
        keys = Counter(key.value for key, _ in node.value if isinstance(key, yaml.ScalarNode))
        for key, count in keys.items():
            if count > 1:
                raise yaml.constructor.ConstructorError(None, None, f"{key} appears {count} times", node.start_mark)
        return super().construct_mapping(node, deep)


@dataclass(frozen=True)
class Display:
    frames: tuple
    priority: int
    ttl: float
    file: str
    look: dict | None = None  # effect, color, seconds: for other lights to copy (GET /, "look")


def build(spec: dict, defaults: dict, file: str) -> Display:
    """One display from its mapping. ValueError says what's wrong with it."""
    settings = {**defaults, **spec}
    priority, ttl = settings.get("priority", signals.DEFAULT_PRIORITY), settings.get("ttl", signals.DEFAULT_TTL)
    if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 100:
        raise ValueError("priority must be a whole number, 0 to 100")
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 0 < ttl <= signals.MAX_TTL:
        raise ValueError(f"ttl must be seconds, up to {signals.MAX_TTL:.0f}")
    params = {k: v for k, v in spec.items() if k not in KEYS}
    effect = params.pop("effect", None)
    if not isinstance(effect, str):
        raise ValueError("no effect")
    seconds = params.get("seconds", 2.0)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= MAX_SECONDS:
        raise ValueError(f"seconds must be above 0, up to {MAX_SECONDS:.0f}")
    params.setdefault("seconds", seconds)
    if "color" in params:
        if params["color"] is None:
            raise ValueError('color is empty: quote it, "#rrggbb", since # starts a comment')
        params["color"] = patterns.parse_colour(params["color"])
    frames = patterns.make(effect, **params)
    if "slots" in spec:
        frames = patterns.only_slots(frames, spec["slots"])
    return Display(tuple(apa102.encode(f) for f in frames), priority, float(ttl), file,
                   look(effect, params.get("color"), params["seconds"]))


def look(effect: str, colour, seconds: float) -> dict:
    """What a display looks like, in words another set of lights can play: the Presto's
    seven LEDs copy the buttons with it. colour is an (r, g, b) or None."""
    out = {"effect": effect, "seconds": float(seconds)}
    if colour is not None:
        out["color"] = "#%02x%02x%02x" % tuple(colour)
    return out


def load(directory: Path = DIRECTORY) -> tuple[dict[str, Display], list[str]]:
    """Every display that's good, and a line for everything that isn't."""
    found, errors = {}, []

    def problem(text: str) -> None:
        log.error("displays: %s", text)
        errors.append(text)

    for path in sorted(Path(directory).glob("*.yaml")):
        try:
            with open(path, "rb") as f:
                config = yaml.load(f, Loader)  # a SafeLoader: data, never objects
        except (OSError, ValueError, yaml.YAMLError) as e:
            problem(f"{path.name}: {e}")
            continue
        if not isinstance(config, dict):
            problem(f"{path.name}: needs displays:, and optionally defaults:")
            continue
        defaults, spec_of = config.get("defaults", {}), config.get("displays", {})
        if not isinstance(defaults, dict) or not isinstance(spec_of, dict):
            problem(f"{path.name}: defaults and displays are mappings")
            continue
        for extra in sorted(set(defaults) - {"priority", "ttl"}):
            problem(f"{path.name}: defaults can't set {extra}")
        defaults = {k: v for k, v in defaults.items() if k in ("priority", "ttl")}
        for name, spec in spec_of.items():
            if not isinstance(name, str):  # off, on, yes, no and numbers aren't strings in YAML
                problem(f'{path.name}: {name!r} is not a name: put it in "quotes"')
            elif name in found:
                problem(f"{path.name}: {name} is already defined in {found[name].file}")
            elif not isinstance(spec, dict):
                problem(f"{path.name}: {name} is not a mapping")
            else:
                try:
                    found[name] = build(spec, defaults, path.name)
                except Exception as e:  # whatever an effect makes of odd numbers
                    problem(f"{path.name}: {name}: {e}")
    return found, errors
