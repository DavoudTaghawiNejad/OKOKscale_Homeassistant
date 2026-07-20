from __future__ import annotations

import pytest

from custom_components.okok_scale.scale_parser import (
    SessionAssembler,
    mac_str_to_bytes,
    parse_frame,
)

MAC = "F0:2C:59:F1:F0:28"
MAC_BYTES = mac_str_to_bytes(MAC)

# Real captured frames from the build brief, keyed by rolling company id.
WAKEUP = (0x01C0, bytes.fromhex("000000000a0124f02c59f1f028"))
F_6210 = (0x07C0, bytes.fromhex("184200000a0124f02c59f1f028"))
F_6195 = (0x0EC0, bytes.fromhex("183300000a0124f02c59f1f028"))
F_6190 = (0x1BC0, bytes.fromhex("182E00000a0124f02c59f1f028"))
F_6190_LOCKED = (0x30C0, bytes.fromhex("182E17700a0125f02c59f1f028"))


def test_reference_hex_payloads_are_13_bytes() -> None:
    for _mfr_id, payload in (WAKEUP, F_6210, F_6195, F_6190, F_6190_LOCKED):
        assert len(payload) == 13


class TestParseFrame:
    def test_wakeup_frame_is_ignored(self) -> None:
        mfr_id, payload = WAKEUP
        assert parse_frame(mfr_id, payload, MAC_BYTES) is None

    def test_valid_unlocked_frame(self) -> None:
        mfr_id, payload = F_6210
        frame = parse_frame(mfr_id, payload, MAC_BYTES)
        assert frame is not None
        assert frame.weight_kg == pytest.approx(62.10)
        assert frame.impedance == 0
        assert frame.stable is False
        assert frame.counter == 0x07

    def test_valid_locked_frame(self) -> None:
        mfr_id, payload = F_6190_LOCKED
        frame = parse_frame(mfr_id, payload, MAC_BYTES)
        assert frame is not None
        assert frame.weight_kg == pytest.approx(61.90)
        assert frame.impedance == 6000
        assert frame.stable is True
        assert frame.counter == 0x30

    def test_wrong_marker_byte_rejected(self) -> None:
        mfr_id, payload = F_6210
        assert parse_frame(mfr_id ^ 0x01, payload, MAC_BYTES) is None

    def test_wrong_length_rejected(self) -> None:
        mfr_id, payload = F_6210
        assert parse_frame(mfr_id, payload[:-1], MAC_BYTES) is None

    def test_mac_mismatch_rejected(self) -> None:
        mfr_id, payload = F_6210
        other_mac = mac_str_to_bytes("AA:BB:CC:DD:EE:FF")
        assert parse_frame(mfr_id, payload, other_mac) is None


class TestSessionAssembler:
    def test_full_session_reference_decode(self) -> None:
        """The captured session must resolve to 61.90 kg / impedance 6000."""
        assembler = SessionAssembler(MAC)
        t = 1000.0

        # HA redelivers *accumulated* manufacturer_data on every callback.
        accumulated: dict[int, bytes] = {}

        accumulated[WAKEUP[0]] = WAKEUP[1]
        assert assembler.ingest(accumulated, t) is None
        assert assembler.current is None  # wake-up frame produces no session

        t += 1
        accumulated[F_6210[0]] = F_6210[1]
        assert assembler.ingest(dict(accumulated), t) is None
        assert assembler.current is not None
        assert len(assembler.current.frames) == 1

        t += 1
        accumulated[F_6195[0]] = F_6195[1]
        assert assembler.ingest(dict(accumulated), t) is None
        assert len(assembler.current.frames) == 2

        t += 1
        accumulated[F_6190[0]] = F_6190[1]
        assert assembler.ingest(dict(accumulated), t) is None
        assert len(assembler.current.frames) == 3

        t += 1
        accumulated[F_6190_LOCKED[0]] = F_6190_LOCKED[1]
        assert assembler.ingest(dict(accumulated), t) is None
        assert len(assembler.current.frames) == 4

        # Redelivery of the identical accumulated snapshot must not create
        # duplicate frame entries.
        t += 1
        assert assembler.ingest(dict(accumulated), t) is None
        assert len(assembler.current.frames) == 4

        session = assembler.current
        assert session is not None
        final = session.final_frame
        assert final.weight_kg == pytest.approx(61.90)
        assert final.impedance == 6000
        assert final.stable is True

    def test_gap_over_60s_starts_new_session_via_timeout(self) -> None:
        assembler = SessionAssembler(MAC)
        t = 0.0
        assembler.ingest({F_6210[0]: F_6210[1]}, t)
        first_session_id = assembler.current.id

        # Nothing arrives for over 60s -> a periodic check should close it.
        assert assembler.check_timeout(t + 30) is None
        completed = assembler.check_timeout(t + 61)
        assert completed is not None
        assert completed.id == first_session_id
        assert assembler.current is None

        # A brand new weigh-in afterwards starts a fresh session.
        t2 = t + 200
        assembler.ingest({F_6190[0]: F_6190[1]}, t2)
        assert assembler.current is not None
        assert assembler.current.id != first_session_id
        assert len(assembler.current.frames) == 1

    def test_gap_over_60s_detected_inline_during_ingest(self) -> None:
        assembler = SessionAssembler(MAC)
        assembler.ingest({F_6210[0]: F_6210[1]}, 0.0)
        first_session_id = assembler.current.id

        # A whole new accumulated snapshot (next weigh-in) arrives 90s later
        # without an intervening check_timeout() call.
        completed = assembler.ingest({F_6190_LOCKED[0]: F_6190_LOCKED[1]}, 90.0)

        assert completed is not None
        assert completed.id == first_session_id
        assert assembler.current is not None
        assert assembler.current.id != first_session_id
        assert len(assembler.current.frames) == 1
        assert assembler.current.final_frame.weight_kg == pytest.approx(61.90)
