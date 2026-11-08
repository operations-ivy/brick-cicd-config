import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from brick_plasma import apa102, displays, patterns, renderer, signals, stats, web

JENKINS = Path(__file__).resolve().parents[2] / "jenkins"
DARK = (b"dark",)


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def decode(writes, data_pin=14, clock_pin=15) -> bytes:
    """What an LED would read: the data line at each rising clock edge."""
    data, clock = 1 << data_pin, 1 << clock_pin
    level, bits = 0, []
    for register, mask in writes:
        if mask == data:
            level = 1 if register == apa102.GPSET0 else 0
        elif mask == clock and register == apa102.GPSET0:
            bits.append(level)
    return bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))


class Registers:
    def __init__(self):
        self.writes = []

    def __setitem__(self, register, mask):
        self.writes.append((register, mask))


class Apa102Test(unittest.TestCase):
    def test_a_frame_is_framed_and_ordered_as_the_leds_want(self):
        wire = apa102.encode([(1, 2, 3)] * 40)
        self.assertEqual(len(wire), 4 + 40 * 4 + 5)
        self.assertEqual(wire[:4], bytes(4))
        self.assertEqual(wire[4:8], bytes([0xFF, 3, 2, 1]))  # marker and full brightness, then b, g, r
        self.assertEqual(wire[-5:], bytes(5))

    def test_the_clock_carries_every_bit_of_the_frame(self):
        regs = Registers()
        wire = apa102.encode([(200, 100, 50)] * 40)
        apa102.Chain(regs).write(wire)
        self.assertEqual(decode(regs.writes), wire)

    def test_only_the_two_pins_are_ever_touched(self):
        regs = Registers()
        apa102.Chain(regs).write(apa102.encode([(255, 0, 170)] * 40))
        self.assertEqual({mask for _, mask in regs.writes}, {0, 1 << 14, 1 << 15})  # 0 changes nothing
        self.assertEqual({r for r, _ in regs.writes}, {apa102.GPSET0, apa102.GPCLR0})

    def test_every_edge_is_held(self):
        regs = Registers()
        apa102.Chain(regs).write(bytes(1))
        edges = [i for i, (_, mask) in enumerate(regs.writes) if mask]
        self.assertTrue(all(b - a == apa102.HOLD + 1 for a, b in zip(edges[:3], edges[1:3])))
        self.assertEqual(len(regs.writes), 8 * (3 + 2 * apa102.HOLD))

    def test_the_clock_ends_low(self):
        regs = Registers()
        apa102.Chain(regs).write(bytes(1))
        self.assertEqual(regs.writes[-1], (apa102.GPCLR0, 1 << 15))


class PatternTest(unittest.TestCase):
    def test_every_colour_effect_works_in_any_colour(self):
        for name in ("solid", "pulse", "flash", "chase", "stream", "fill", "scanner", "rush", "converge"):
            frames = patterns.make(name, color=(10, 20, 30), seconds=1.0)
            self.assertTrue(all(len(f) == patterns.WIDTH for f in frames), name)

    def test_an_effect_refuses_settings_it_doesnt_have(self):
        for effect, params in (("nope", {}), ("rainbow", {"seconds": 1, "color": (1, 2, 3)}),
                               ("chase", {"color": (1, 2, 3), "seconds": 1, "wobble": 2})):
            with self.assertRaises(ValueError):
                patterns.make(effect, **params)

    def test_colours(self):
        self.assertEqual(patterns.parse_colour("#ff8000"), (255, 128, 0))
        self.assertEqual(patterns.parse_colour("ff8000"), (255, 128, 0))
        self.assertEqual(patterns.parse_colour([1, 2, 3]), (1, 2, 3))
        for bad in ("red", [1, 2], [1, 2, 256], [1, 2, True], None):
            with self.assertRaises(ValueError):
                patterns.parse_colour(bad)

    def test_only_slots_darkens_the_rest(self):
        [frame] = patterns.only_slots(patterns.solid((9, 9, 9)), [1])
        self.assertEqual(frame[4:8], [(9, 9, 9)] * 4)
        self.assertEqual({frame[i] for i in range(40) if not 4 <= i < 8}, {patterns.OFF})
        for bad in ([10], [-1], [True], 3):
            with self.assertRaises(ValueError):
                patterns.only_slots([], bad)

    def test_nightly_is_a_seamless_eight_second_loop(self):
        frames = patterns.nightly(8.0)
        self.assertEqual(len(frames), 8 * patterns.FPS)
        self.assertEqual(frames, patterns.nightly(8.0))  # seeded: the same on every start
        for i in range(patterns.WIDTH):  # every LED changes
            self.assertGreater(len({f[i] for f in frames}), 10, i)
        self.assertIn(255, {v for f in frames for c in f for v in c})

    def test_rainbow_and_flash_colours(self):
        rainbow = patterns.rainbow(0.75)
        self.assertEqual(len(rainbow), int(0.75 * patterns.FPS))
        hues = {px for frame in rainbow for px in frame}
        self.assertTrue(any(r > 200 for r, _, _ in hues) and any(g > 200 for _, g, _ in hues)
                        and any(b > 200 for _, _, b in hues))
        flashes = {px for frame in patterns.flash(patterns.GREEN, 0.5) for px in frame}
        self.assertEqual(flashes, {patterns.GREEN, patterns.OFF})

    def test_boil_bubbles_every_slot_on_its_own_beat(self):
        frames = patterns.boil(2.0)
        self.assertEqual(len(frames), 2 * patterns.FPS)
        firsts = [patterns.LEDS_PER_SLOT * s for s in range(patterns.SLOTS)]
        for i in firsts:
            column = [frame[i] for frame in frames]
            self.assertGreater(max(max(px) for px in column), 240)  # swells to full
            self.assertLess(min(max(px) for px in column), 20)  # and pops near dark
        self.assertGreater(len({frames[60][i] for i in firsts}), 5)  # many colours at once
        pops = [min(range(len(frames)), key=lambda f: max(frames[f][i])) for i in firsts]
        self.assertGreater(len(set(pops)), 3)  # not in step
        for i in firsts:  # rolling: every slot pops at least three times a second
            column = [max(frame[i]) for frame in frames]
            self.assertGreaterEqual(sum(1 for f in range(len(column)) if column[f] < column[f - 1]), 6)
        self.assertEqual(patterns.boil(2.0), frames)  # seeded

    def test_buttons_light_only_the_slots_given(self):
        frames = patterns.buttons({1: patterns.RED}, 1)
        self.assertEqual(frames[0][4 * 5], patterns.OFF)
        self.assertTrue(frames[0][4 * 1][0] > 0)

    def test_a_busy_button_pulses_fully(self):
        frames = patterns.buttons({3: patterns.GREEN}, 0, {3})
        greens = {frame[4 * 3] for frame in frames}
        self.assertIn(patterns.GREEN, greens)
        self.assertLess(min(g for _, g, _ in greens), 50)


class ShippedDisplaysTest(unittest.TestCase):
    """The real directory: a bad drop-in fails here, so it never deploys."""

    @classmethod
    def setUpClass(cls):
        cls.named, cls.errors = displays.load()

    def test_every_file_loads_clean(self):
        self.assertEqual(self.errors, [])
        self.assertGreaterEqual(len({d.file for d in self.named.values()}), 3)

    def test_the_party_show(self):
        show = self.named["party"]
        self.assertEqual(show.file, "board.yaml")
        self.assertGreaterEqual(show.priority, self.named["off"].priority)  # over quiet hours too
        # As long as brick-status's show (party.SECONDS).
        root = Path(__file__).resolve().parents[2]
        seconds = float(re.search(r"^SECONDS = ([\d.]+)", (root / "brick-status" / "brick_status" / "party.py")
                                  .read_text(), re.M).group(1))
        self.assertEqual(show.ttl, seconds)
        frames = patterns.make("party", seconds=seconds)
        self.assertEqual(len(frames), int(seconds * patterns.FPS))
        self.assertTrue(all(len(f) == patterns.WIDTH for f in frames))
        self.assertEqual(frames[-1], [(255, 255, 255)] * patterns.WIDTH)  # the white finish
        self.assertGreater(len({f[0] for f in frames}), 50)  # it really is crazy

    def test_every_jenkins_kind_has_its_display(self):
        text = (JENKINS / "lib" / "plasma.groovy").read_text()
        kinds = re.findall(r"'(\w+)'", re.search(r"def kinds = \[(.*?)\]", text).group(1))
        self.assertGreater(len(kinds), 5)
        for kind in kinds:
            self.assertEqual(self.named[f"stage-{kind}"].file, "jenkins.yaml")

    def test_the_board_and_deploys_only_ask_for_displays_that_exist(self):
        root = Path(__file__).resolve().parents[2]
        lights = (root / "brick-status" / "brick_status" / "lights.py").read_text()
        asked = set(re.findall(r'return "([a-z]+)"', lights)) | set(re.findall(r'self\.show\("([a-z]+)"\)', lights))
        script = (root / "brick9000" / "build-status.sh").read_text()
        asked |= set(re.findall(r"plasma_signal ([a-z-]+) \d+", script))
        main = (root / "brick-status" / "brick_status" / "__main__.py").read_text()
        answers = set(re.findall(r'answer\("([a-z-]+)"', main)) | set(re.findall(r'"(check-[a-z]+)"', main))
        self.assertGreaterEqual(len(answers), 3, answers)
        jobs = "".join(f.read_text() for f in (JENKINS / "jobs").glob("*.groovy"))
        shown = set(re.findall(r"plasmaResult\('([a-z-]+)'", jobs))
        self.assertGreaterEqual(len(shown), 2, shown)
        asked |= answers | shown
        self.assertGreater(len(asked), 10, asked)  # the regexes still find them
        self.assertLessEqual(asked, set(self.named))

    def test_the_order_of_priorities(self):
        priority = {name: d.priority for name, d in self.named.items()}
        active = int(re.search(r"PRIORITY_ACTIVE = (\d+)", (JENKINS.parent / "brick-status" / "brick_status"
                                                            / "lights.py").read_text())[1])
        self.assertLess(priority["alert"], priority["stage-test"])
        self.assertLess(priority["stage-test"], priority["nightly"])
        self.assertLess(priority["nightly"], active)
        self.assertLess(active, priority["deploying"])
        self.assertLess(priority["deploying"], priority["off"])


class DropInTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def write(self, name, text):
        (self.directory / name).write_text(text)

    def test_a_new_source_needs_only_a_file(self):
        self.write("garage.yaml", """
defaults:
  priority: 40
  ttl: 120

displays:
  door-open:
    effect: chase
    color: "#ff0000"
    seconds: 1.0
    tail: 2

  door-shut:
    effect: solid
    color: [0, 255, 0]
    priority: 45
    slots: [3, 4]
""")
        named, errors = displays.load(self.directory)
        self.assertEqual(errors, [])
        door = named["door-open"]
        self.assertEqual((door.priority, door.ttl, door.file, len(door.frames)), (40, 120.0, "garage.yaml", 60))
        self.assertEqual(named["door-shut"].priority, 45)
        wire = named["door-shut"].frames[0]
        self.assertEqual(wire[4:8], bytes([0xFF, 0, 0, 0]))  # slot 0 dark
        self.assertEqual(wire[4 + 12 * 4:4 + 12 * 4 + 4], bytes([0xFF, 0, 255, 0]))  # slot 3 lit

    def test_a_display_without_defaults_gets_the_apis(self):
        self.write("a.yaml", "displays:\n  x:\n    effect: rainbow\n    seconds: 1\n")
        named, _ = displays.load(self.directory)
        self.assertEqual((named["x"].priority, named["x"].ttl), (signals.DEFAULT_PRIORITY, signals.DEFAULT_TTL))

    def test_a_file_that_wont_parse_costs_only_itself(self):
        self.write("a-broken.yaml", "displays: [not: closed")
        self.write("b-good.yaml", "displays:\n  good:\n    effect: boil\n")
        named, errors = displays.load(self.directory)
        self.assertEqual(list(named), ["good"])
        self.assertEqual(len(errors), 1)
        self.assertIn("a-broken.yaml", errors[0])

    def test_a_file_of_the_wrong_shape_costs_only_itself(self):
        for name, text in (("a-empty.yaml", ""), ("b-list.yaml", "- one\n- two\n"),
                           ("c-text.yaml", "just words\n"), ("d-displays.yaml", "displays: [x]\n"),
                           ("e-binary.yaml", "\ufffe")):
            self.write(name, text)
        (self.directory / "f-not-utf8.yaml").write_bytes(b"displays: \xff\xfe\x00")
        self.write("g-good.yaml", "displays:\n  good:\n    effect: boil\n")
        named, errors = displays.load(self.directory)
        self.assertEqual(list(named), ["good"])
        self.assertEqual(len(errors), 6, errors)

    def test_a_bad_display_costs_only_itself(self):
        self.write("a.yaml", """
displays:
  good:
    effect: boil

  no-effect:
    color: "#ff0000"

  bad-effect:
    effect: sparkle

  bad-colour:
    effect: solid
    color: mauve

  bad-setting:
    effect: chase
    color: "#ff0000"
    wobble: 3

  colour-for-rainbow:
    effect: rainbow
    color: "#ff0000"

  bad-priority:
    effect: boil
    priority: 101

  bad-ttl:
    effect: boil
    ttl: 99999

  too-long:
    effect: boil
    seconds: 3600

  bad-slots:
    effect: boil
    slots: [10]

  bad-number:
    effect: pulse
    color: "#ff0000"
    low: dim
""")
        named, errors = displays.load(self.directory)
        self.assertEqual(list(named), ["good"])
        self.assertEqual(len(errors), 10, errors)

    def test_an_unquoted_colour_is_reported(self):
        # Unquoted, # starts a comment: YAML sees no value at all.
        self.write("a.yaml", "displays:\n  x:\n    effect: solid\n    color: #ff0000\n")
        named, errors = displays.load(self.directory)
        self.assertEqual(named, {})
        self.assertIn("quote", errors[0])

    def test_an_unquoted_off_is_reported(self):
        # Unquoted, YAML reads off, on, yes and no as true or false.
        self.write("a.yaml", 'displays:\n  off:\n    effect: boil\n  "on":\n    effect: boil\n')
        named, errors = displays.load(self.directory)
        self.assertEqual(list(named), ["on"])
        self.assertIn("quotes", errors[0])

    def test_a_name_twice_in_one_file_costs_that_file(self):
        self.write("a.yaml", "displays:\n  x:\n    effect: boil\n  x:\n    effect: rainbow\n")
        named, errors = displays.load(self.directory)
        self.assertEqual(named, {})
        self.assertIn("x appears 2 times", errors[0])

    def test_a_file_cant_build_objects(self):
        self.write("a.yaml", "displays: !!python/object/apply:os.getcwd []\n")
        named, errors = displays.load(self.directory)
        self.assertEqual(named, {})
        self.assertIn("a.yaml", errors[0])

    def test_the_first_file_keeps_a_name_two_want(self):
        self.write("a.yaml", "displays:\n  same:\n    effect: boil\n")
        self.write("b.yaml", "displays:\n  same:\n    effect: rainbow\n")
        named, errors = displays.load(self.directory)
        self.assertEqual(named["same"].file, "a.yaml")
        self.assertIn("already defined in a.yaml", errors[0])

    def test_a_typo_in_defaults_is_reported(self):
        self.write("a.yaml", "defaults:\n  priorty: 90\ndisplays:\n  x:\n    effect: boil\n")
        named, errors = displays.load(self.directory)
        self.assertIn("priorty", errors[0])
        self.assertEqual(named["x"].priority, signals.DEFAULT_PRIORITY)

    def test_an_empty_directory_is_no_displays(self):
        self.assertEqual(displays.load(self.directory), ({}, []))


class SignalsTest(unittest.TestCase):
    def setUp(self):
        self.changes = []
        self.board = signals.Signals(on_change=lambda: self.changes.append(1))
        self.a, self.b = (b"a",), (b"b",)

    def label(self, now=0.0):
        winner = self.board.winner(now)
        return winner.source if winner else None

    def test_the_highest_priority_wins_however_old(self):
        self.board.put("high", "x", self.a, 80, 10, 0)
        self.board.put("low", "x", self.b, 10, 10, 0)
        self.assertEqual(self.label(), "high")

    def test_among_equals_the_newest_wins(self):
        self.board.put("one", "x", self.a, 50, 10, 0)
        self.board.put("two", "x", self.b, 50, 10, 0)
        self.assertEqual(self.label(), "two")

    def test_a_heartbeat_extends_but_doesnt_jump_the_queue(self):
        self.board.put("one", "x", self.a, 50, 10, 0)
        self.board.put("two", "x", self.b, 50, 10, 0)
        before = len(self.changes)
        self.board.put("one", "x", self.a, 50, 10, 5)
        self.assertEqual(self.label(5), "two")
        self.assertEqual(len(self.changes), before)
        self.assertEqual(self.label(14), "one")  # two ran out at 10; one lives to 15

    def test_a_new_order_from_a_sender_is_news(self):
        self.board.put("one", "x", self.a, 50, 10, 0)
        self.board.put("two", "x", self.b, 50, 10, 0)
        self.board.put("one", "x", self.b, 50, 10, 0)
        self.assertEqual(self.label(), "one")

    def test_a_sender_that_dies_runs_out(self):
        self.board.put("gone", "x", self.a, 90, 5, 0)
        self.assertEqual(self.label(4.9), "gone")
        self.assertIsNone(self.label(5.0))

    def test_delete_uncovers_what_was_underneath(self):
        self.board.put("low", "x", self.a, 10, 10, 0)
        self.board.put("high", "x", self.b, 90, 10, 0)
        self.assertTrue(self.board.remove("high"))
        self.assertEqual(self.label(), "low")
        self.assertFalse(self.board.remove("high"))

    def test_senders_are_limited_but_expired_ones_dont_count(self):
        for i in range(signals.MAX_SIGNALS):
            self.board.put(f"s{i}", "x", self.a, 50, 10, 0)
        with self.assertRaises(signals.Full):
            self.board.put("more", "x", self.a, 50, 10, 0)
        self.board.put("s0", "x", self.b, 50, 10, 0)  # one already held still can
        self.board.put("more", "x", self.a, 50, 10, 11)


class RecordingChain:
    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)


class ShownTest(unittest.TestCase):
    def setUp(self):
        self.board = signals.Signals()
        self.a, self.b = ("a",), ("b",)

    def test_only_signals_that_say_what_they_are(self):
        self.board.put("lights", "x", self.a, 50, 10, 0)
        self.board.put("job", "x", self.a, 50, 10, 0, {"title": "t"})
        self.assertEqual([s.source for s in self.board.shown(0)], ["job"])

    def test_a_new_stage_keeps_its_place(self):
        self.board.put("one", "x", self.a, 50, 10, 0, {"title": "1"})
        self.board.put("two", "x", self.a, 50, 10, 0, {"title": "2"})
        self.board.put("one", "y", self.b, 50, 10, 1, {"title": "1", "status": "next"})
        self.assertEqual([s.source for s in self.board.shown(1)], ["one", "two"])
        self.assertEqual(self.board.shown(1)[0].show["status"], "next")

    def test_a_heartbeat_can_change_what_it_says(self):
        self.board.put("job", "x", self.a, 50, 10, 0, {"title": "t", "status": "one"})
        self.board.put("job", "x", self.a, 50, 10, 1, {"title": "t", "status": "two"})
        self.assertEqual(self.board.shown(1)[0].show["status"], "two")

    def test_it_expires_with_the_signal(self):
        self.board.put("job", "x", self.a, 50, 10, 0, {"title": "t"})
        self.assertEqual(self.board.shown(11), [])


class RendererTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.chain = RecordingChain()
        self.stats = stats.Stats()
        self.board = signals.Signals()
        self.render = renderer.Renderer(self.board, self.chain, DARK, self.stats, clock=self.clock)
        self.loop = tuple(bytes([i]) for i in range(120))

    def put(self, source, frames, priority=50, ttl=10):
        self.board.put(source, "x", frames, priority, ttl, self.clock.now)

    def test_dark_with_nothing_to_show(self):
        self.assertEqual(self.render.step(), renderer.STILL_SECONDS)
        self.assertEqual(self.chain.frames, [b"dark"])

    def test_a_new_signal_starts_from_its_first_frame(self):
        self.render.step()
        self.put("a", self.loop)
        self.clock.now += 0.1
        self.assertEqual(self.render.step(), renderer.FRAME_SECONDS)
        self.assertEqual(self.chain.frames[-1], self.loop[0])

    def test_frames_follow_the_clock_and_loop(self):
        self.put("a", self.loop)
        self.render.step()
        self.clock.now += 0.5
        self.render.step()
        self.assertEqual(self.chain.frames[-1], self.loop[30])
        self.clock.now += 2.0
        self.render.step()
        self.assertEqual(self.chain.frames[-1], self.loop[30])  # 2 s is one whole loop

    def test_a_heartbeat_doesnt_restart_the_animation(self):
        self.put("a", self.loop)
        self.render.step()
        self.clock.now += 0.5
        self.put("a", self.loop)
        self.render.step()
        self.assertEqual(self.chain.frames[-1], self.loop[30])

    def test_a_frame_is_not_written_twice_within_its_time(self):
        self.put("a", self.loop)
        self.render.step()
        self.render.step()
        self.assertEqual(len(self.chain.frames), 1)

    def test_a_still_pattern_is_refreshed_only_now_and_then(self):
        self.put("a", (b"still",))
        self.render.step()
        self.clock.now += 0.01
        self.render.step()
        self.assertEqual(len(self.chain.frames), 1)
        self.clock.now += renderer.STILL_SECONDS
        self.render.step()
        self.assertEqual(len(self.chain.frames), 2)

    def test_when_a_signal_runs_out_the_next_one_shows(self):
        self.put("under", (b"under",), priority=10, ttl=100)
        self.put("over", (b"over",), priority=90, ttl=1)
        self.render.step()
        self.assertEqual(self.chain.frames[-1], b"over")
        self.clock.now += 1
        self.render.step()
        self.assertEqual(self.chain.frames[-1], b"under")

    def test_a_new_signal_cuts_the_wait_short(self):
        board = signals.Signals(on_change=lambda: render.wake.set())
        render = renderer.Renderer(board, self.chain, DARK, self.stats)
        stop = threading.Event()
        thread = threading.Thread(target=render.run, args=(stop,), daemon=True)
        thread.start()
        written = threading.Event()
        self.chain.write = lambda frame: written.set() if frame == b"now" else None
        board.put("a", "x", (b"now",), 50, 10, render.clock())
        # Far less than STILL_SECONDS, which it would wait without the wake.
        self.assertTrue(written.wait(renderer.STILL_SECONDS / 5))
        stop.set()
        render.wake.set()
        thread.join(2)


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = web.Catalog(*displays.load())

    def setUp(self):
        self.clock = Clock()
        self.stats = stats.Stats()
        self.board = signals.Signals()
        self.server = web.make_server("127.0.0.1", 0, self.catalog, self.board, self.stats, clock=self.clock)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}{path}", data, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as r:
                text = r.read()
                return r.status, json.loads(text) if r.headers.get_content_type() == "application/json" else text
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def showing(self):
        return self.call("GET", "/")[1]["showing"]

    def test_put_shows_a_named_pattern(self):
        status, reply = self.call("PUT", "/signals/job-1", {"pattern": "stage-test", "priority": 60, "ttl": 90})
        self.assertEqual(status, 200)
        self.assertEqual(reply, {"source": "job-1", "pattern": "stage-test", "priority": 60, "ttl": 90.0})
        self.assertEqual(self.showing(), {"source": "job-1", "pattern": "stage-test", "priority": 60,
                                          "expires_in": 90.0,
                                          "look": {"effect": "scanner", "color": "#00ff00", "seconds": 1.6}})
        winner = self.board.winner(self.clock.now)
        self.assertIs(winner.frames, self.catalog.named["stage-test"].frames)

    def test_the_defaults(self):
        self.call("PUT", "/signals/a", {"color": "#ffffff"})
        shown = self.showing()
        self.assertEqual((shown["priority"], shown["expires_in"]), (signals.DEFAULT_PRIORITY, signals.DEFAULT_TTL))

    def test_a_display_brings_its_own_priority_and_a_sender_can_overrule_it(self):
        self.call("PUT", "/signals/a", {"pattern": "deploying"})
        self.assertEqual(self.showing()["priority"], 80)
        self.call("PUT", "/signals/a", {"pattern": "deploying", "priority": 20, "ttl": 5})
        self.assertEqual((self.showing()["priority"], self.showing()["expires_in"]), (20, 5.0))

    def test_a_colourless_effect_is_refused_a_colour(self):
        self.assertEqual(self.call("PUT", "/signals/a", {"color": "#ffffff", "effect": "rainbow"})[0], 400)

    def test_any_colour_in_any_effect(self):
        self.assertEqual(self.call("PUT", "/signals/a", {"color": "#ff8000"})[0], 200)
        first = self.board.winner(self.clock.now).frames
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0][4:8], bytes([0xFF, 0, 0x80, 0xFF]))
        self.assertEqual(self.call("PUT", "/signals/b", {"color": [0, 0, 255], "effect": "chase", "seconds": 1})[0], 200)
        self.assertEqual(len(self.board.winner(self.clock.now).frames), patterns.FPS)

    def test_the_same_custom_request_is_a_repeat_not_news(self):
        self.call("PUT", "/signals/a", {"color": [1, 2, 3], "effect": "pulse"})
        first = self.board.winner(self.clock.now).frames
        self.call("PUT", "/signals/a", {"color": [1, 2, 3], "effect": "pulse"})
        self.assertIs(self.board.winner(self.clock.now).frames, first)

    def test_a_custom_signal_says_how_it_looks(self):
        self.call("PUT", "/signals/a", {"color": [255, 128, 0], "effect": "pulse", "seconds": 1})
        self.assertEqual(self.showing()["look"], {"effect": "pulse", "color": "#ff8000", "seconds": 1.0})

    def test_a_colourless_display_looks_like_its_effect(self):
        self.call("PUT", "/signals/a", {"pattern": "stage-build"})
        self.assertEqual(self.showing()["look"], {"effect": "rainbow", "seconds": 0.75})

    def test_the_boards_buttons_have_no_look(self):  # they're brick9000's own, slot by slot
        self.call("PUT", "/signals/board", {"buttons": {"colors": {"1": [255, 0, 0]}}})
        self.assertIsNone(self.showing()["look"])

    def test_buttons(self):
        body = {"buttons": {"colors": {"1": [255, 0, 0], "3": "#00ff00"}, "selected": 1, "busy": [3]}}
        self.assertEqual(self.call("PUT", "/signals/board", body)[0], 200)
        winner = self.board.winner(self.clock.now)
        self.assertEqual((winner.label, len(winner.frames)), ("buttons", 2 * patterns.FPS))

    def test_delete_and_deleting_again(self):
        self.call("PUT", "/signals/a", {"pattern": "calm"})
        self.assertEqual(self.call("DELETE", "/signals/a")[0], 204)
        self.assertIsNone(self.showing())
        self.assertEqual(self.call("DELETE", "/signals/a")[0], 204)

    def test_refused_without_touching_the_lights(self):
        for body in ({}, {"pattern": "nope"}, {"pattern": 7}, {"pattern": "calm", "color": [1, 2, 3]},
                     {"color": "red"}, {"color": [1, 2]}, {"color": [1, 2, 256]}, {"color": [1, 2, True]},
                     {"color": [1, 2, 3], "effect": "nope"}, {"color": [1, 2, 3], "seconds": "long"},
                     {"pattern": "calm", "ttl": "x"}, {"pattern": "calm", "priority": None},
                     {"buttons": "x"}, {"buttons": {"colors": {"99": [1, 2, 3]}}},
                     {"buttons": {"colors": {}, "busy": 3}}, {"buttons": {"colors": {"x": [1, 2, 3]}}}):
            self.assertEqual(self.call("PUT", "/signals/a", body)[0], 400, body)
        self.assertEqual(self.call("PUT", "/signals/a", raw=b"not json")[0], 400)
        self.assertEqual(self.call("PUT", "/signals/a", raw=b"[1]")[0], 400)
        self.assertIsNone(self.showing())

    def test_a_bad_source_or_path_is_a_404(self):
        self.assertEqual(self.call("PUT", "/signals/a b".replace(" ", "%20"), {"pattern": "calm"})[0], 404)
        self.assertEqual(self.call("PUT", "/signals/" + "x" * 65, {"pattern": "calm"})[0], 404)
        self.assertEqual(self.call("PUT", "/elsewhere", {"pattern": "calm"})[0], 404)
        self.assertEqual(self.call("GET", "/elsewhere")[0], 404)

    def test_an_oversized_body_is_refused(self):
        self.assertEqual(self.call("PUT", "/signals/a", raw=b" " * (web.MAX_BODY + 1))[0], 400)

    def test_time_limits_are_capped(self):
        self.call("PUT", "/signals/a", {"pattern": "calm", "ttl": 10 ** 9})
        self.assertEqual(self.showing()["expires_in"], signals.MAX_TTL)

    def test_too_many_senders(self):
        for i in range(signals.MAX_SIGNALS):
            self.board.put(f"s{i}", "x", (b"",), 50, 10, self.clock.now)
        self.assertEqual(self.call("PUT", "/signals/more", {"pattern": "calm"})[0], 429)

    def test_what_can_be_asked_for_is_listed(self):
        status, reply = self.call("GET", "/patterns")
        self.assertEqual(status, 200)
        self.assertEqual(reply["patterns"]["stage-push"], {"priority": 50, "ttl": 30.0, "file": "jenkins.yaml"})
        self.assertIn("chase", reply["effects"])
        self.assertEqual(reply["errors"], [])

    def test_metrics_and_health(self):
        self.assertEqual(self.call("GET", "/healthz")[0], 200)
        self.call("PUT", "/signals/a", {"pattern": "calm"})
        self.stats.showing = "calm"
        text = self.call("GET", "/metrics")[1].decode()
        self.assertIn("brick_plasma_signals 1", text)
        self.assertIn('brick_plasma_showing{pattern="calm"} 1', text)
        self.assertIn('brick_plasma_requests_total{code="200"}', text)

    def test_activity_lists_what_senders_say(self):
        self.call("PUT", "/signals/job", {"pattern": "calm", "show": {
            "title": "overwatch:0.8.6", "status": "build", "details": {"job": "build-image"}}})
        self.call("PUT", "/signals/lights", {"pattern": "calm"})
        self.assertEqual(self.call("GET", "/activity")[1], {"activity": [
            {"source": "job", "title": "overwatch:0.8.6", "status": "build", "details": {"job": "build-image"}}]})

    def test_show_keeps_escape_codes_out_of_the_terminal(self):
        self.call("PUT", "/signals/job", {"pattern": "calm", "show": {
            "title": "a\x1b[2Jb\n", "status": "\x07ok", "details": {"x\x1b": "y\r"}}})
        [shown] = self.call("GET", "/activity")[1]["activity"]
        self.assertEqual((shown["title"], shown["status"], shown["details"]), ("a[2Jb", "ok", {"x": "y"}))

    def test_show_limits_its_size(self):
        self.call("PUT", "/signals/job", {"pattern": "calm", "show": {"title": "x" * 500}})
        self.assertEqual(len(self.call("GET", "/activity")[1]["activity"][0]["title"]), web.MAX_TEXT)

    def test_bad_show_is_refused(self):
        for show in ("text", {}, {"title": ""}, {"title": 1}, {"title": "t", "details": []},
                     {"title": "t", "details": {str(i): "v" for i in range(web.MAX_DETAILS + 1)}}):
            self.assertEqual(self.call("PUT", "/signals/job", {"pattern": "calm", "show": show})[0], 400, show)
        self.assertEqual(self.showing(), None)


if __name__ == "__main__":
    unittest.main()
