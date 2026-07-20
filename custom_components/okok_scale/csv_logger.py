"""Per-person CSV history: one file per person, one row per captured frame.

The interesting logic (row read/write/delete, session reassignment) lives
in plain, synchronous, file-path-based functions at module level so it can
be unit tested directly (see tests/test_session_engine.py) without any
Home Assistant runtime. `CsvLogger` is a thin async wrapper around those
functions for use from the coordinator, dispatching every call through
`hass.async_add_executor_job` so the event loop never blocks on file I/O.
"""

from __future__ import annotations

import csv
import functools
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .body_composition import calc_body_fat_pct, calc_relative_body_fat_pct
from .const import CSV_FIELDNAMES, DEFAULT_BODY_FAT_FORMULA

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def ensure_parent_dir(csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)


def append_row(csv_path: Path, row: dict[str, Any]) -> None:
    """Append one row, writing the header first if the file is new/empty."""
    ensure_parent_dir(csv_path)
    is_new = not csv_path.exists() or csv_path.stat().st_size == 0
    with csv_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES)
        if is_new:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in CSV_FIELDNAMES})


def read_rows(csv_path: Path) -> list[dict[str, str]]:
    if not csv_path.exists():
        return []
    with csv_path.open("r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def read_last_weight_kg(csv_path: Path) -> float | None:
    """Used on startup / after a reassignment to seed a person's ref_weight_kg."""
    rows = read_rows(csv_path)
    if not rows:
        return None
    try:
        return float(rows[-1]["weight_kg"])
    except (KeyError, ValueError):
        return None


def read_last_row(csv_path: Path) -> dict[str, str] | None:
    """The full last row (all computed fields), or None if the file is empty/missing."""
    rows = read_rows(csv_path)
    return rows[-1] if rows else None


def delete_session_rows(csv_path: Path, session_id: str) -> list[dict[str, str]]:
    """Remove every row belonging to `session_id`, rewriting the file.

    Returns the removed rows (empty list if the file didn't have any).
    """
    rows = read_rows(csv_path)
    removed = [row for row in rows if row.get("session_id") == session_id]
    if not removed:
        return []
    keep = [row for row in rows if row.get("session_id") != session_id]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for row in keep:
            writer.writerow({key: row.get(key, "") for key in CSV_FIELDNAMES})
    return removed


def reassign_session(
    from_path: Path,
    to_path: Path,
    session_id: str,
    *,
    target_height_cm: float,
    target_age_years: float,
    target_sex: str,
    target_formula: str = DEFAULT_BODY_FAT_FORMULA,
    target_baseline_body_fat_pct: float | None = None,
) -> list[dict[str, Any]]:
    """Move every row of `session_id` from one person's CSV to another's.

    body_fat_pct and body_fat_relative_pct are recomputed against the
    *target* person's height/age/sex/baseline before the rows are written,
    since those were originally derived from whoever the session was
    (wrongly) assigned to. Returns the new rows as written to `to_path`
    (used by the caller to recompute ref_weight_kg/ref_impedance).
    """
    removed = delete_session_rows(from_path, session_id)
    moved: list[dict[str, Any]] = []
    for row in removed:
        weight_kg = float(row["weight_kg"])
        impedance = int(float(row["impedance"])) if row.get("impedance") else 0
        body_fat_pct = calc_body_fat_pct(
            weight_kg, target_height_cm, target_age_years, target_sex, impedance, target_formula
        )
        body_fat_relative_pct = calc_relative_body_fat_pct(body_fat_pct, target_baseline_body_fat_pct)
        new_row: dict[str, Any] = {
            "time": row["time"],
            "session_id": row["session_id"],
            "weight_kg": weight_kg,
            "impedance": impedance,
            "body_fat_pct": body_fat_pct,
            "body_fat_relative_pct": body_fat_relative_pct,
        }
        append_row(to_path, new_row)
        moved.append(new_row)
    return moved


class CsvLogger:
    """Async, executor-backed façade over the sync functions above."""

    def __init__(self, hass: HomeAssistant, base_dir: Path) -> None:
        self._hass = hass
        self._base_dir = base_dir

    def path_for(self, person_id: str) -> Path:
        return self._base_dir / f"{person_id}.csv"

    async def async_append_row(self, person_id: str, row: dict[str, Any]) -> None:
        await self._hass.async_add_executor_job(append_row, self.path_for(person_id), row)

    async def async_read_last_weight_kg(self, person_id: str) -> float | None:
        return await self._hass.async_add_executor_job(read_last_weight_kg, self.path_for(person_id))

    async def async_read_last_row(self, person_id: str) -> dict[str, str] | None:
        return await self._hass.async_add_executor_job(read_last_row, self.path_for(person_id))

    async def async_reassign_session(
        self,
        from_person_id: str,
        to_person_id: str,
        session_id: str,
        **target_kwargs: Any,
    ) -> list[dict[str, Any]]:
        func = functools.partial(
            reassign_session,
            self.path_for(from_person_id),
            self.path_for(to_person_id),
            session_id,
            **target_kwargs,
        )
        return await self._hass.async_add_executor_job(func)
