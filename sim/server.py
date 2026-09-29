#!/usr/bin/env python3
"""
The Shattered Expanse — phase 0 authoritative sim server.

- Fixed 10 Hz tick, arcade Newtonian-drift physics.
- WebSocket + JSON protocol (see proto/protocol.md).
- Spawns one ship per authenticated connection.

Phase-0 simplifications (all deliberate, all documented):
  * auth accepts any non-empty token (account DB + hashed tokens come later)
  * full entity list broadcast every tick (delta compression later)
  * single star system, no persistence (snapshot/restore is phase 1)
"""

import argparse
import asyncio
import json
import math
import time

from websockets.asyncio.server import serve

TICK_HZ = 10
DT = 1.0 / TICK_HZ


# ---------------------------------------------------------------- vec3 ----

def vadd(a, b):
    return [a[0] + b[0], a[1] + b[1], a[2] + b[2]]


def vsub(a, b):
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def vmul(a, s):
    return [a[0] * s, a[1] * s, a[2] * s]


def vlen(a):
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def heading_dir(yaw, pitch):
    """Unit vector for a yaw/pitch heading. yaw=0 -> +x, pitch>0 -> +z."""
    cp = math.cos(pitch)
    return [cp * math.cos(yaw), cp * math.sin(yaw), math.sin(pitch)]


# ---------------------------------------------------------------- ship ---

class Ship:
    _next_id = 1

    def __init__(self, name):
        self.id = Ship._next_id
        Ship._next_id += 1
        self.name = name
        self.pos = [0.0, 0.0, 0.0]
        self.vel = [0.0, 0.0, 0.0]
        self.yaw = 0.0
        self.pitch = 0.0
        self.throttle = 0.0
        # Phase 0: single ship class. Classes/modules arrive in phase 4.
        self.max_speed = 500.0   # km/s
        self.accel = 120.0       # km/s^2
        self.turn_rate = 1.2     # rad/s
        self.target_yaw = 0.0
        self.target_pitch = 0.0

    def step(self, dt):
        # Turn toward the commanded heading, rate-limited.
        for attr, target_attr in (("yaw", "target_yaw"),
                                  ("pitch", "target_pitch")):
            cur = getattr(self, attr)
            tgt = getattr(self, target_attr)
            diff = (tgt - cur + math.pi) % (2 * math.pi) - math.pi
            max_turn = self.turn_rate * dt
            cur += max(-max_turn, min(max_turn, diff))
            setattr(self, attr, cur)
        # Velocity chases the throttle target, acceleration-limited.
        want = vmul(heading_dir(self.yaw, self.pitch),
                    self.max_speed * self.throttle)
        dv = vsub(want, self.vel)
        d = vlen(dv)
        max_dv = self.accel * dt
        if d > max_dv:
            dv = vmul(dv, max_dv / d)
        self.vel = vadd(self.vel, dv)
        self.pos = vadd(self.pos, vmul(self.vel, dt))

    def to_dict(self):
        return {
            "id": self.id,
            "type": "ship",
            "name": self.name,
            "pos": [round(x, 2) for x in self.pos],
            "vel": [round(x, 2) for x in self.vel],
            "yaw": round(self.yaw, 3),
            "pitch": round(self.pitch, 3),
            "throttle": self.throttle,
        }


# ----------------------------------------------------------------- sim ---

class Sim:
    def __init__(self):
        self.ships = {}
        self.tick = 0
        self.clients = set()

    def spawn(self, name):
        ship = Ship(name)
        self.ships[ship.id] = ship
        return ship

    def step(self):
        # Deterministic order: sorted by entity id.
        for sid in sorted(self.ships):
            self.ships[sid].step(DT)
        self.tick += 1

    def snapshot(self):
        return {
            "t": "state",
            "tick": self.tick,
            "entities": [self.ships[sid].to_dict()
                         for sid in sorted(self.ships)],
        }


def apply_intent(ship, msg):
    """Validate an intent, apply it, return an ack or reject message."""
    iid = msg.get("id")
    action = msg.get("action")

    def ack():
        return {"t": "ack", "id": iid, "action": action}

    def reject(reason):
        return {"t": "reject", "id": iid, "action": action,
                "reason": reason}

    if action == "set_throttle":
        v = msg.get("value")
        if not isinstance(v, (int, float)) or not 0.0 <= v <= 1.0:
            return reject("value must be a number in [0, 1]")
        ship.throttle = float(v)
        return ack()

    if action == "set_heading":
        yaw, pitch = msg.get("yaw"), msg.get("pitch")
        if not all(isinstance(x, (int, float)) and math.isfinite(x)
                   for x in (yaw, pitch)):
            return reject("yaw and pitch must be finite numbers")
        if not -math.pi / 2 <= pitch <= math.pi / 2:
            return reject("pitch must be in [-pi/2, pi/2]")
        ship.target_yaw = float(yaw)
        ship.target_pitch = float(pitch)
        return ack()

    if action == "stop":
        ship.throttle = 0.0
        return ack()

    return reject(f"unknown action: {action!r}")


# ----------------------------------------------------------------- net ---

async def handle_client(ws, sim):
    # Phase 0 auth: any non-empty token is accepted and logged.
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
    except (asyncio.TimeoutError, json.JSONDecodeError, ConnectionError):
        return
    if msg.get("t") != "auth" or not msg.get("token"):
        try:
            await ws.send(json.dumps({"t": "error",
                                      "reason": "auth required"}))
        finally:
            await ws.close()
        return

    ship = sim.spawn(str(msg.get("name", "pilot"))[:32])
    print(f"[sim] auth ok: {ship.name!r} -> ship #{ship.id}", flush=True)
    sim.clients.add(ws)
    try:
        await ws.send(json.dumps({"t": "welcome", "you": ship.id,
                                  "tick": sim.tick}))
        await ws.send(json.dumps(sim.snapshot()))
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("t") == "intent":
                await ws.send(json.dumps(apply_intent(ship, msg)))
    except ConnectionError:
        pass
    finally:
        sim.clients.discard(ws)
        print(f"[sim] disconnect: ship #{ship.id}", flush=True)


async def ticker(sim):
    while True:
        t0 = time.monotonic()
        sim.step()
        payload = json.dumps(sim.snapshot())
        if sim.clients:
            results = await asyncio.gather(
                *(ws.send(payload) for ws in sim.clients),
                return_exceptions=True)
            for ws, r in zip(list(sim.clients), results):
                if isinstance(r, Exception):
                    sim.clients.discard(ws)
        elapsed = time.monotonic() - t0
        await asyncio.sleep(max(0.0, DT - elapsed))


async def main():
    ap = argparse.ArgumentParser(description="Shattered Expanse sim server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    sim = Sim()
    async with serve(lambda ws: handle_client(ws, sim),
                     args.host, args.port):
        print(f"[sim] listening on {args.host}:{args.port} "
              f"@ {TICK_HZ} Hz", flush=True)
        await ticker(sim)


if __name__ == "__main__":
    asyncio.run(main())
