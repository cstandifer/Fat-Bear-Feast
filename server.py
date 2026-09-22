#!/usr/bin/env python3
"""Fat Bear Feast — a tiny multiplayer game server for your local Wi-Fi.

No installs needed. Run:   python3 server.py
Then open the printed address on every phone/laptop on the same network.
"""
import asyncio
import base64
import hashlib
import json
import math
import os
import random
import socket
import struct
import time

PORT = int(os.environ.get("PORT", 8000))
HERE = os.path.dirname(os.path.abspath(__file__))
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Board geometry (world units; the board is 1000 x 1000)
C = 500.0            # centre
R = 240.0            # arena (bowl) radius
BALL = 19.0          # marble radius
MOUTH_REST = R + 12  # distance of a resting mouth from the centre
MOUTH_LIP = 44.0     # how far the open upper jaw reaches ahead of the snout
REACH = MOUTH_REST - MOUTH_LIP + 10  # lunge just far enough for the jaws to reach past the centre
CATCH_W = 33.0       # a marble's centre must be this close to the mouth's middle to be swallowed
CATCH_DEPTH = 35.0   # how deep the open mouth is behind the snout
HEAD_BACK = 35.0     # the solid head is a circle centred this far behind the snout...
HEAD_R = 54.0        # ...with this radius; marbles that miss the mouth bounce off it
HEAD_KICK = 480.0    # top speed a lunging head can knock a marble with
HEAD_BOUNCE = 0.5
DIRS = [(0, 1), (-1, 0), (0, -1), (1, 0)]  # seat 0 bottom, 1 left, 2 top, 3 right

EXT_OUT, EXT_HOLD, EXT_IN = 0.08, 0.05, 0.11

# Marble feel: hard little balls rolling in a shallow plastic bowl
BOWL = 205.0          # pull back toward the low middle, felt at the rim (units/s^2); gentler nearer the centre
ROLL = 14.0           # rolling friction (units/s^2)
LAUNCH = (260.0, 425.0)  # marbles are released with a flick outward and roll back down
WALL_BOUNCE = 0.55    # plastic rim: share of head-on speed kept
RIM_GRIP = 0.96       # a little tangential friction on each rim hit
BALL_BOUNCE = 0.9     # marble-on-marble clacks
STALE_SECS = 6.0      # if nobody eats for this long, the table gets a bump to free stuck marbles
SUBSTEPS = 4
CHOMP_T = EXT_OUT + EXT_HOLD + EXT_IN
MARBLE_CHOICES = (12, 20, 30, 40)
# Computer bears play like people: they spot a salmon lined up with their mouth, take a moment
# to react, and only partly allow for how far it swims in that moment -- so fast fish get away.
#   rest:  pause between chomps (s)
#   eager: chance per tick of noticing a salmon in their lane
#   react: reaction time range (s) between spotting and chomping
#   guess: how much of the fish's movement during that reaction they allow for (1 = perfectly)
CPU_LEVELS = {
    "easy":   {"rest": 1.0,  "eager": 0.03, "react": (0.30, 0.50), "guess": 0.2},
    "normal": {"rest": 0.55, "eager": 0.05, "react": (0.20, 0.35), "guess": 0.5},
    "hard":   {"rest": 0.35, "eager": 0.08, "react": (0.14, 0.24), "guess": 0.75},
}
# How long each computer bear takes to react to "GO!" (seconds, picked at random in this range),
# roughly a person's reaction time plus the moment their tap takes to reach this server
CPU_START = {"easy": (0.6, 0.9), "normal": (0.4, 0.65), "hard": (0.3, 0.45)}
SILENT_SECS = 8  # a page we haven't heard from in this long is treated as gone (pages check in every 2s)
END_PAUSE = 1.0  # seconds the board stays up after the last marble is eaten
OVER_SECS = 6  # how long the results show before everyone returns to the waiting room


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------- websocket bits

def frame(data: bytes, op: int = 1) -> bytes:
    n = len(data)
    head = bytes([0x80 | op])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + data


async def read_frame(reader):
    b1, b2 = await reader.readexactly(2)
    op = b1 & 0x0F
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", await reader.readexactly(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", await reader.readexactly(8))[0]
    if n > 65536:
        raise ConnectionError("frame too large")
    mask = await reader.readexactly(4) if b2 & 0x80 else b"\0\0\0\0"
    payload = bytearray(await reader.readexactly(n))
    for i in range(n):
        payload[i] ^= mask[i % 4]
    return op, bytes(payload)


class Client:
    def __init__(self, writer):
        self.writer = writer
        self.token = None
        self.name = ""
        self.visible = True               # is the page on screen (not a locked phone or background tab)?
        self.last_seen = time.monotonic() # last time we heard anything from this page

    def send(self, obj):
        w = self.writer
        if w.is_closing() or w.transport.get_write_buffer_size() > 512 * 1024:
            return  # drop frames for a stalled client rather than lag everyone
        w.write(frame(json.dumps(obj, separators=(",", ":")).encode()))


# ---------------------------------------------------------------- the game

class Marble:
    __slots__ = ("x", "y", "vx", "vy", "alive")

    def __init__(self, x, y):
        self.x, self.y, self.vx, self.vy, self.alive = x, y, 0.0, 0.0, True


class Game:
    def __init__(self, url, code=""):
        self.url = url            # the share link for this table
        self.code = code
        self.empty_since = None   # when the last person left (empty tables are tidied away)
        self.clients = []
        self.seats = [None] * 4   # None | {"kind": "human", "token", "name", "online"} | {"kind": "cpu"}
        self.phase = "lobby"      # lobby | countdown | playing | over
        self.cpu = True
        self.level = "normal"
        self.n_marbles = 20
        self.marbles = []
        self.chomp_at = [-99.0] * 4
        self.scores = [0] * 4
        self.winners = []
        self.last_result = []     # [[name, score, colour index], ...] from the previous round
        self.over_at = 0.0
        self.countdown_end = 0.0
        self.eats = []
        self.last_eat = 0.0
        self.bumped = False
        self.cpu_ready = [0.0] * 4
        self.cpu_plan = [None] * 4
        self.finish_at = 0.0

    # ---- helpers
    def leader_token(self):
        """The first person in the waiting room (by arrival) who has a bear decides when to start.
        Someone whose page is hidden (phone locked, switched apps) is skipped, so nobody waits on them."""
        seated = [c for c in self.clients if c.token and self.seat_of(c.token) >= 0]
        for c in seated:
            if self.token_visible(c.token):
                return c.token
        return seated[0].token if seated else None

    def token_visible(self, token):
        return any(c.token == token and c.visible for c in self.clients)

    def seat_waiting(self):
        """Give a bear to everyone who arrived while a round was on."""
        for c in self.clients:
            if c.token and self.seat_of(c.token) < 0 and None in self.seats:
                self.seats[self.seats.index(None)] = {"kind": "human", "token": c.token, "name": c.name, "online": True}

    def seat_of(self, token):
        for i, s in enumerate(self.seats):
            if s and s["kind"] == "human" and s["token"] == token:
                return i
        return -1

    def ext(self, i, now):
        t = now - self.chomp_at[i]
        if t < 0 or t > CHOMP_T:
            return 0.0
        if t < EXT_OUT:
            return t / EXT_OUT
        if t < EXT_OUT + EXT_HOLD:
            return 1.0
        return max(0.0, 1 - (t - EXT_OUT - EXT_HOLD) / EXT_IN)

    def tip(self, i, e):
        dx, dy = DIRS[i]
        d = MOUTH_REST - e * REACH
        return C + dx * d, C + dy * d

    def cpu_controlled(self, i):
        s = self.seats[i]
        return s is not None and (s["kind"] == "cpu" or not s["online"])

    # ---- broadcasting
    def lobby_msg(self, c):
        leader = self.leader_token()
        leader_name = next((x.name for x in self.clients if x.token == leader), "")
        return {
            "t": "lobby",
            "phase": self.phase,
            "seats": [
                None if s is None else
                {"kind": s["kind"], "name": s.get("name", "CPU"), "online": s.get("online", True),
                 "away": s["kind"] == "human" and not self.token_visible(s["token"])}
                for s in self.seats
            ],
            "you": self.seat_of(c.token),
            "leader": c.token is not None and c.token == leader,
            "leaderName": leader_name,
            "cpu": self.cpu,
            "lastResult": self.last_result,
            "backIn": round(max(0.0, self.over_at + OVER_SECS - time.monotonic())) if self.phase == "over" else 0,
            "marbles": self.n_marbles,
            "level": self.level,
            "scores": self.scores,
            "winners": self.winners,
            "url": self.url,
            "room": self.code,
        }

    def broadcast_lobby(self):
        for c in self.clients:
            c.send(self.lobby_msg(c))

    def broadcast_state(self, now):
        m = []
        for b in self.marbles:
            if b.alive:
                m += [round(b.x), round(b.y)]
            else:
                m += [-1, -1]
        msg = {
            "t": "s",
            "m": m,
            "e": [round(self.ext(i, now) * 100) for i in range(4)],
            "sc": self.scores,
            "cd": round(max(0.0, self.countdown_end - now), 2) if self.phase == "countdown" else 0,
            "eat": self.eats,
            "bump": self.bumped,
        }
        self.bumped = False
        self.eats = []
        for c in self.clients:
            c.send(msg)

    # ---- phases
    def start(self, now):
        if not any(s and s["kind"] == "human" for s in self.seats):
            return
        for i in range(4):
            if self.seats[i] is None and self.cpu:
                self.seats[i] = {"kind": "cpu"}
            elif self.seats[i] and self.seats[i]["kind"] == "cpu" and not self.cpu:
                self.seats[i] = None
        self.scores = [0] * 4
        self.winners = []
        self.chomp_at = [-99.0] * 4
        self.marbles = []
        n = self.n_marbles
        tries = 0
        while len(self.marbles) < n:  # scatter them in the middle without overlaps
            tries += 1
            rmax = min(R - BALL - 20, 100 + tries * 0.05)
            a, r = random.random() * math.tau, rmax * math.sqrt(random.random())
            x, y = C + math.cos(a) * r, C + math.sin(a) * r
            if all(math.hypot(x - m.x, y - m.y) > 2 * BALL + 3 for m in self.marbles):
                self.marbles.append(Marble(x, y))
        self.phase = "countdown"
        self.countdown_end = now + 3.0
        self.broadcast_lobby()

    def go(self):
        self.phase = "playing"
        now = time.monotonic()
        self.last_eat = now
        self.cpu_ready = [now + random.uniform(*CPU_START[self.level]) for _ in range(4)]
        self.cpu_plan = [None] * 4
        self.finish_at = 0.0
        for b in self.marbles:
            a = random.random() * math.tau
            sp = random.uniform(*LAUNCH)
            b.vx, b.vy = math.cos(a) * sp, math.sin(a) * sp
        self.broadcast_lobby()

    def finish(self):
        self.phase = "over"
        best = max(self.scores[i] for i in range(4) if self.seats[i]) if any(self.seats) else 0
        self.winners = [i for i in range(4) if self.seats[i] and self.scores[i] == best]
        self.last_result = sorted(
            ([s.get("name", "CPU"), self.scores[i], i] for i, s in enumerate(self.seats) if s),
            key=lambda r: -r[1],
        )
        self.over_at = time.monotonic()
        self.broadcast_lobby()

    def to_lobby(self):
        """Back to the waiting room: open every seat that isn't a connected person."""
        self.phase = "lobby"
        self.marbles = []
        for i, s in enumerate(self.seats):
            if s and (s["kind"] == "cpu" or not s["online"]):
                self.seats[i] = None
        self.seat_waiting()
        self.broadcast_lobby()

    # ---- simulation
    def physics(self, dt):
        live = [b for b in self.marbles if b.alive]
        for b in live:
            dx, dy = b.x - C, b.y - C
            r = math.hypot(dx, dy) or 1e-6
            ux, uy = dx / r, dy / r
            # the bowl: the further up the side, the harder it rolls back to the middle
            g = -BOWL * r / R
            ax, ay = ux * g, uy * g
            b.vx += ax * dt
            b.vy += ay * dt
            sp = math.hypot(b.vx, b.vy)
            if sp > 0:
                k = max(0.0, sp - ROLL * dt) / sp
                b.vx *= k
                b.vy *= k
            b.x += b.vx * dt
            b.y += b.vy * dt
            # the rim: angle in equals angle out, minus what the plastic soaks up
            dx, dy = b.x - C, b.y - C
            d = math.hypot(dx, dy) or 1e-6
            if d > R - BALL:
                nx, ny = dx / d, dy / d
                b.x, b.y = C + nx * (R - BALL), C + ny * (R - BALL)
                vn = b.vx * nx + b.vy * ny
                if vn > 0:
                    tx, ty = -ny, nx
                    vt = b.vx * tx + b.vy * ty
                    if vn > 40:  # a real impact (not just resting against the rim while rolling along it)
                        vt = vt * RIM_GRIP + random.gauss(0, 0.03) * vn
                    vn = -vn * WALL_BOUNCE
                    b.vx, b.vy = vn * nx + vt * tx, vn * ny + vt * ty
        # marble-on-marble: equal masses trade the head-on part of their speed
        for i in range(len(live)):
            a = live[i]
            for j in range(i + 1, len(live)):
                b = live[j]
                dx, dy = b.x - a.x, b.y - a.y
                d2 = dx * dx + dy * dy
                if 0 < d2 < (2 * BALL) ** 2:
                    d = math.sqrt(d2)
                    nx, ny = dx / d, dy / d
                    push = (2 * BALL - d) / 2
                    a.x -= nx * push; a.y -= ny * push
                    b.x += nx * push; b.y += ny * push
                    rel = (a.vx - b.vx) * nx + (a.vy - b.vy) * ny
                    if rel > 0:
                        k = rel * (1 + BALL_BOUNCE) / 2
                        a.vx -= k * nx; a.vy -= k * ny
                        b.vx += k * nx; b.vy += k * ny

    def bump(self, now):
        """Someone knocks the table: every marble left gets jostled."""
        self.last_eat = now
        self.bumped = True
        for b in self.marbles:
            if b.alive:
                a = random.random() * math.tau
                sp = random.uniform(150, 260)
                b.vx += math.cos(a) * sp
                b.vy += math.sin(a) * sp

    def heads(self, now):
        """Bear heads: the open mouth swallows marbles it lines up with; the rest of the head is solid."""
        for i in range(4):
            if not self.seats[i]:
                continue
            t = now - self.chomp_at[i]
            e = self.ext(i, now)
            jaws_open = 0 <= t < EXT_OUT + EXT_HOLD * 0.5
            if 0 <= t < EXT_OUT:
                head_v = -min(REACH / EXT_OUT, HEAD_KICK)   # lunging in
            elif EXT_OUT + EXT_HOLD <= t < CHOMP_T:
                head_v = min(REACH / EXT_IN, HEAD_KICK)     # pulling back
            else:
                head_v = 0.0
            dx, dy = DIRS[i]
            tip = MOUTH_REST - e * REACH
            hx, hy = C + dx * (tip + HEAD_BACK), C + dy * (tip + HEAD_BACK)
            hvx, hvy = dx * head_v, dy * head_v
            for b in self.marbles:
                if not b.alive:
                    continue
                along = (b.x - C) * dx + (b.y - C) * dy
                across = abs((b.x - C) * dy - (b.y - C) * dx)
                if jaws_open and across < CATCH_W and tip - MOUTH_LIP < along < tip + CATCH_DEPTH:
                    b.alive = False
                    self.scores[i] += 1
                    self.eats.append([i, round(b.x), round(b.y)])
                    self.last_eat = now
                    continue
                ox, oy = b.x - hx, b.y - hy
                dist = math.hypot(ox, oy)
                if 0 < dist < HEAD_R + BALL:
                    nx, ny = ox / dist, oy / dist
                    b.x, b.y = hx + nx * (HEAD_R + BALL), hy + ny * (HEAD_R + BALL)
                    vn = (b.vx - hvx) * nx + (b.vy - hvy) * ny
                    if vn < 0:
                        b.vx -= (1 + HEAD_BOUNCE) * vn * nx
                        b.vy -= (1 + HEAD_BOUNCE) * vn * ny

    def cpu_think(self, now):
        lv = CPU_LEVELS[self.level]
        for i in range(4):
            if not self.cpu_controlled(i) or now < self.cpu_ready[i]:
                continue
            if self.cpu_plan[i] is not None:          # decided already; chomp once the reaction time is up
                if now >= self.cpu_plan[i]:
                    self.chomp_at[i] = now
                    self.cpu_plan[i] = None
                continue
            if now - self.chomp_at[i] < CHOMP_T + lv["rest"]:
                continue
            dx, dy = DIRS[i]
            react = random.uniform(*lv["react"])
            for b in self.marbles:
                if not b.alive or random.random() >= lv["eager"]:
                    continue
                # where it'll be when the jaws arrive, as this bear judges it
                along_now = (b.x - C) * dx + (b.y - C) * dy
                lead = EXT_OUT * max(0.2, min(1.0, (MOUTH_REST - along_now) / REACH)) + react * lv["guess"]
                px, py = b.x + b.vx * lead, b.y + b.vy * lead
                along = (px - C) * dx + (py - C) * dy
                across = abs((px - C) * dy - (py - C) * dx)
                if MOUTH_REST - REACH - MOUTH_LIP < along < MOUTH_REST and across < CATCH_W:
                    self.cpu_plan[i] = now + react
                    break
            else:
                if random.random() < 0.002:           # the odd impatient chomp at nothing
                    self.chomp_at[i] = now

    def humans(self):
        return sum(1 for s in self.seats if s and s["kind"] == "human")

    def tick(self, now, dt):
        if self.phase == "countdown" and now >= self.countdown_end:
            self.go()
        for c in list(self.clients):
            if now - c.last_seen > SILENT_SECS:
                c.writer.transport.abort()  # its read loop then ends and frees the seat
        if self.phase == "over" and now - self.over_at >= OVER_SECS:
            self.to_lobby()
        if self.phase == "playing":
            self.cpu_think(now)
            if now - self.last_eat > STALE_SECS:
                self.bump(now)
            for _ in range(SUBSTEPS):
                self.physics(dt / SUBSTEPS)
                self.heads(now)
            if not any(b.alive for b in self.marbles):
                # let the last chomp play out on screen before showing the results
                if not self.finish_at:
                    self.finish_at = now + END_PAUSE
                elif now >= self.finish_at:
                    self.broadcast_state(now)
                    self.finish()
        if self.phase in ("countdown", "playing"):
            self.broadcast_state(now)  # 60 updates a second keeps fast marbles smooth

    # ---- messages from players
    def on_message(self, c, msg):
        t = msg.get("t")
        now = time.monotonic()
        c.last_seen = now
        if t == "vis":
            leader_before = self.leader_token()
            was = c.visible
            c.visible = bool(msg.get("visible"))
            if was != c.visible or leader_before != self.leader_token():
                self.broadcast_lobby()
            return
        is_leader = c.token is not None and c.token == self.leader_token()
        if t == "join":
            c.token = str(msg.get("token", ""))[:40] or None
            c.name = (str(msg.get("name", "")).strip() or "Bear")[:16]
            i = self.seat_of(c.token)
            if i >= 0:
                self.seats[i]["online"] = True
                self.seats[i]["name"] = c.name
            elif self.phase == "lobby" and None in self.seats:
                self.seats[self.seats.index(None)] = {"kind": "human", "token": c.token, "name": c.name, "online": True}
            # arriving mid-round: watch, and get a bear when everyone returns to the waiting room
            self.broadcast_lobby()
        elif t == "name":
            c.name = (str(msg.get("name", "")).strip() or "Bear")[:16]
            i = self.seat_of(c.token)
            if i >= 0:
                self.seats[i]["name"] = c.name
            self.broadcast_lobby()
        elif t == "seat" and self.phase == "lobby":
            j = msg.get("seat")
            if isinstance(j, int) and 0 <= j < 4 and self.seats[j] is None:
                i = self.seat_of(c.token)
                if i >= 0:
                    self.seats[i] = None
                self.seats[j] = {"kind": "human", "token": c.token, "name": c.name, "online": True}
                self.broadcast_lobby()
        elif t == "stand" and self.phase == "lobby":
            i = self.seat_of(c.token)
            if i >= 0:
                self.seats[i] = None
                self.broadcast_lobby()
        elif t == "chomp" and self.phase == "playing":
            i = self.seat_of(c.token)
            if i >= 0 and now - self.chomp_at[i] >= CHOMP_T:
                self.chomp_at[i] = now
        elif t == "start" and is_leader and self.phase == "lobby":
            self.start(now)
        elif t == "lobby" and self.phase == "over":
            self.to_lobby()
        elif t == "cpu" and is_leader and self.phase == "lobby":
            self.cpu = bool(msg.get("on"))
            self.broadcast_lobby()
        elif t == "level" and is_leader and self.phase == "lobby":
            if msg.get("level") in CPU_LEVELS:
                self.level = msg["level"]
                self.broadcast_lobby()
        elif t == "marbles" and is_leader and self.phase == "lobby":
            if msg.get("n") in MARBLE_CHOICES:
                self.n_marbles = msg["n"]
                self.broadcast_lobby()

    def remove(self, c):
        if c in self.clients:
            self.clients.remove(c)
        if c.token and not any(x.token == c.token for x in self.clients):
            i = self.seat_of(c.token)
            if i >= 0:
                if self.phase == "lobby":
                    self.seats[i] = None
                else:
                    self.seats[i]["online"] = False  # a CPU takes over until they come back
        if self.phase != "lobby" and not any(s and s["kind"] == "human" and s["online"] for s in self.seats):
            self.to_lobby()
        self.broadcast_lobby()


# ---------------------------------------------------------------- http + upgrade

# ---------------------------------------------------------------- tables (rooms)

ROOM_LETTERS = "ABCDEFGHJKMNPQRSTUVWXYZ"   # no I/L/O: easy to read out loud
EMPTY_ROOM_SECS = 120
rooms = {}
BASE_URL = ""


def make_room(code=None):
    while not code or code in rooms:
        code = "".join(random.choice(ROOM_LETTERS) for _ in range(4))
    rooms[code] = Game(f"{BASE_URL}/?t={code}", code)
    return rooms[code]


def pick_room(asked):
    """A table code in the link picks that table. Otherwise: a waiting room with an open seat,
    then a round in progress with room for one more person, and failing that a brand-new table."""
    asked = "".join(ch for ch in asked.upper() if ch.isalnum())[:8]
    if asked == "NEW":
        return make_room()
    if asked:
        return rooms.get(asked) or make_room(asked)
    for g in rooms.values():
        if g.phase == "lobby" and None in g.seats:
            return g
    for g in rooms.values():
        if g.phase != "lobby" and g.humans() + sum(1 for c in g.clients if c.token and g.seat_of(c.token) < 0) < 4:
            return g
    return make_room()


async def ticker():
    last = time.monotonic()
    while True:
        await asyncio.sleep(1 / 60)
        now = time.monotonic()
        dt = min(now - last, 0.05)
        last = now
        for code, g in list(rooms.items()):
            g.tick(now, dt)
            if g.clients:
                g.empty_since = None
            elif g.empty_since is None:
                g.empty_since = now
            elif now - g.empty_since > EMPTY_ROOM_SECS:
                del rooms[code]


async def handle(reader, writer):
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    except Exception:
        writer.close()
        return
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    path = parts[1] if len(parts) > 1 else "/"
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    if headers.get("upgrade", "").lower() == "websocket":
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        writer.write(
            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
        )
        query = dict(kv.split("=", 1) for kv in path.partition("?")[2].split("&") if "=" in kv)
        game = pick_room(query.get("t", ""))
        c = Client(writer)
        game.clients.append(c)
        c.send({"t": "hello", "room": game.code})
        try:
            while True:
                op, payload = await read_frame(reader)
                if op == 8:
                    break
                if op == 9:
                    writer.write(frame(payload, 10))
                    continue
                if op != 1:
                    continue
                try:
                    msg = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    game.on_message(c, msg)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            game.remove(c)
            writer.close()
        return

    if path.split("?")[0] in ("/", "/index.html"):
        with open(os.path.join(HERE, "index.html"), "rb") as f:
            body = f.read()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nCache-Control: no-store\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
        )
    else:
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    try:
        await writer.drain()
    finally:
        writer.close()


async def main():
    global BASE_URL
    url = BASE_URL = f"http://{lan_ip()}:{PORT}"
    server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    print("\n  🐻  Fat Bear Feast is running!\n")
    print(f"  On this computer:      http://localhost:{PORT}")
    print(f"  Phones on your Wi-Fi:  {url}\n")
    print("  Press Ctrl+C to stop.\n")
    asyncio.create_task(ticker())
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
