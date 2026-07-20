"""Pure BLE frame parsing and weighing-session assembly for the OKOK scale.

No Home Assistant imports here on purpose: this module is unit-testable in
total isolation (see tests/test_scale_parser.py) and is driven by the async
BLE callback wiring in __init__.py / coordinator.py.

Protocol recap (see build brief for the full reverse-engineering notes):
  * The scale advertises manufacturer_data entries keyed by a rolling
    "company id" of the form 0xNNC0, where the low byte 0xC0 is a constant
    Chipsea marker and the high byte NN is a rolling packet counter.
  * Each value is a fixed 13-byte payload: weight (BE, 10 g units),
    impedance (BE), a constant version marker, a flags byte (bit0 = locked/
    stable), and the scale's own 6-byte MAC (used to validate the frame
    actually belongs to *our* scale, since nearby devices could in theory
    reuse the same manufacturer id scheme).
  * Home Assistant redelivers the *accumulated* manufacturer_data dict on
    every advertisement callback, so frames must be deduplicated by the
    tuple (mfr_id, payload) rather than assumed to be new.
  * A "weighing session" is the burst of frames produced by one stand-on.
    A gap of more than SESSION_GAP_SECONDS between advertisements closes
    the session; its result is the highest-priority frame, where "locked"
    beats "unlocked" and, within the same lock state, the higher packet
    counter wins.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Mapping

from .const import (
    CHIPSEA_MARKER_BYTE,
    PAYLOAD_LENGTH,
    SESSION_GAP_SECONDS,
    STABLE_FLAG_BIT,
    STABLE_SESSION_GAP_SECONDS,
)
from .models import ScaleFrame


def mac_str_to_bytes(mac: str) -> bytes:
    """Convert 'F0:2C:59:F1:F0:28' (or '-'-separated / bare hex) to bytes."""
    cleaned = mac.replace(":", "").replace("-", "").strip()
    if len(cleaned) != 12:
        raise ValueError(f"Invalid MAC address: {mac!r}")
    return bytes.fromhex(cleaned)


def parse_frame(mfr_id: int, payload: bytes, mac_bytes: bytes) -> ScaleFrame | None:
    """Validate and decode one manufacturer_data entry.

    Returns None for anything that isn't a genuine, in-progress measurement
    frame from *this* scale: wrong marker byte, wrong length, MAC mismatch,
    or a weight-0 "wake up" frame.
    """
    if (mfr_id & 0xFF) != CHIPSEA_MARKER_BYTE:
        return None
    if len(payload) != PAYLOAD_LENGTH:
        return None
    if payload[7:13] != mac_bytes:
        return None

    weight_raw = int.from_bytes(payload[0:2], "big")
    if weight_raw == 0:
        return None  # wake-up frame, nothing measured yet

    impedance = int.from_bytes(payload[2:4], "big")
    flags = payload[6]
    stable = bool(flags & STABLE_FLAG_BIT)
    counter = (mfr_id >> 8) & 0xFF

    return ScaleFrame(
        counter=counter,
        weight_kg=round(weight_raw / 100, 2),
        impedance=impedance,
        stable=stable,
        raw_mfr_id=mfr_id,
        raw_payload=bytes(payload),
    )


@dataclass
class Session:
    """One weighing session in progress or completed."""

    id: str
    started_at: float
    last_update_at: float
    frames: list[ScaleFrame] = field(default_factory=list)
    _seen: set[tuple[int, bytes]] = field(default_factory=set, repr=False)

    def has_seen(self, mfr_id: int, payload: bytes) -> bool:
        return (mfr_id, payload) in self._seen

    def add(self, mfr_id: int, payload: bytes, frame: ScaleFrame, now: float) -> None:
        self._seen.add((mfr_id, payload))
        self.frames.append(frame)
        self.last_update_at = now

    @property
    def has_stable_frame(self) -> bool:
        """Whether any frame seen so far has the locked/stable flag set."""
        return any(f.stable for f in self.frames)

    @property
    def final_frame(self) -> ScaleFrame:
        """The frame that should be treated as the session's result.

        Locked (stable) frames outrank unlocked ones; ties (or the absence
        of any locked frame) are broken by the highest packet counter.
        """
        if not self.frames:
            raise ValueError("Session has no frames")
        return max(self.frames, key=lambda f: f.priority)


class SessionAssembler:
    """Stateful, pure assembly of BLE frames into weighing sessions.

    Feed it accumulated manufacturer_data snapshots via `ingest()` as they
    arrive from Home Assistant's bluetooth callback. Because the scale goes
    silent between weigh-ins (no more advertisements at all), a session
    that finished cleanly is only detected on the *next* weigh-in's first
    frame unless the caller also polls `check_timeout()` on a periodic
    timer to close out a session in near-real-time.

    The gap that counts as "finished" shrinks once the session has seen a
    locked (stable) frame: waiting the full, generous SESSION_GAP_SECONDS
    after the reading has already locked serves no purpose beyond making
    everything downstream (the last-measurement sensor, CSV logging, the
    "add person" dialog) feel unresponsive for up to a minute. See
    STABLE_SESSION_GAP_SECONDS.
    """

    def __init__(
        self,
        mac: str,
        gap_seconds: float = SESSION_GAP_SECONDS,
        stable_gap_seconds: float = STABLE_SESSION_GAP_SECONDS,
    ) -> None:
        self._mac_bytes = mac_str_to_bytes(mac)
        self._gap_seconds = gap_seconds
        self._stable_gap_seconds = stable_gap_seconds
        self.current: Session | None = None

    def _effective_gap(self, session: Session) -> float:
        return self._stable_gap_seconds if session.has_stable_frame else self._gap_seconds

    def ingest(self, manufacturer_data: Mapping[int, bytes], now: float) -> Session | None:
        """Process one accumulated manufacturer_data snapshot.

        Returns a completed Session if a gap in this batch closed a
        previous in-flight session (a new one is started transparently).
        Returns None if the batch only added to (or didn't affect) the
        current in-flight session.
        """
        completed: Session | None = None

        for mfr_id in sorted(manufacturer_data):
            payload = bytes(manufacturer_data[mfr_id])
            frame = parse_frame(mfr_id, payload, self._mac_bytes)
            if frame is None:
                continue
            if self.current is not None and self.current.has_seen(mfr_id, payload):
                continue  # already accounted for, doesn't affect timing

            if self.current is not None and (now - self.current.last_update_at) > self._effective_gap(self.current):
                completed = self.current
                self.current = None

            if self.current is None:
                self.current = Session(id=uuid.uuid4().hex[:12], started_at=now, last_update_at=now)

            self.current.add(mfr_id, payload, frame, now)

        return completed

    def check_timeout(self, now: float) -> Session | None:
        """Force-close the current session if it has gone quiet.

        Call this from a periodic timer; it's how a session that ends
        because the person simply stepped off (no further advertisements
        ever arrive) gets finalized instead of waiting indefinitely for a
        "next" frame that will only show up at the next weigh-in.
        """
        if self.current is not None and (now - self.current.last_update_at) > self._effective_gap(self.current):
            completed = self.current
            self.current = None
            return completed
        return None
