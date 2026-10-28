"""Per-printer print queue: sends the next print only when the printer is idle and its bed is confirmed clear."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..db import Database
from .moonraker import PrinterBusy, PrinterError, PrinterMismatch
from .service import Printers, parse_printer_id

log = logging.getLogger("homeslice.queue")

DISPATCH_INTERVAL_S = 10.0
# Unlike a manual send, the queue never starts over an error state.
READY_STATES = {"standby", "complete", "cancelled"}
# Finished entries stay visible this long.
HISTORY_S = 24 * 3600


class QueueError(Exception):
    pass


def _parse_id(value: str) -> str:
    try:
        return uuid.UUID(value).hex
    except ValueError:
        raise QueueError("Not found") from None


class PrintQueue:
    def __init__(
        self,
        db: Database,
        printers: Printers,
        gcode_path: Callable[[str, str], Path | None],
        remote_name: Callable[[str, str], str],
        audit: Callable[[str | None, str, str], None],
    ) -> None:
        self._db = db
        self._printers = printers
        self._gcode_path = gcode_path
        self._remote_name = remote_name
        self._audit = audit
        self._lock = asyncio.Lock()
        # A send interrupted by a restart may or may not have started; never resend it blindly.
        self._db.execute(
            "UPDATE print_queue SET status='failed', error='Interrupted by a restart; check the printer before re-queueing.' "
            "WHERE status='sending'"
        )

    def list(self) -> list[dict[str, Any]]:
        rows = self._db.all(
            "SELECT q.*, p.name AS printer_name, j.title AS job_title FROM print_queue q "
            "JOIN printers p ON p.id = q.printer_id LEFT JOIN jobs j ON j.id = q.job_id "
            "WHERE q.status IN ('waiting', 'sending') OR q.created_at > ? "
            "ORDER BY q.printer_id, CASE WHEN q.status IN ('waiting', 'sending') THEN 0 ELSE 1 END, q.position, q.created_at DESC",
            (time.time() - HISTORY_S,),
        )
        return [dict(r) for r in rows]

    def waiting_count(self, printer_id: str) -> int:
        row = self._db.one(
            "SELECT COUNT(*) AS n FROM print_queue WHERE printer_id=? AND status='waiting'", (printer_id,)
        )
        return int(row["n"]) if row else 0

    def add(self, job_id: str, printer_id: str, file: str, owner: str | None) -> dict[str, Any]:
        pid = parse_printer_id(printer_id)
        if self._printers.get(pid) is None:
            raise QueueError("Unknown printer")
        if self._gcode_path(job_id, file) is None:
            raise QueueError("G-code file not found")
        last = self._db.one("SELECT MAX(position) AS p FROM print_queue WHERE printer_id=?", (pid,))
        item = {
            "id": uuid.uuid4().hex,
            "printer_id": pid,
            "job_id": job_id,
            "file": file,
            "position": (last["p"] or 0) + 1 if last else 1,
            "status": "waiting",
            "owner": owner,
            "created_at": time.time(),
        }
        self._db.execute(
            "INSERT INTO print_queue(id, printer_id, job_id, file, position, status, owner, created_at) "
            "VALUES(:id, :printer_id, :job_id, :file, :position, :status, :owner, :created_at)",
            item,
        )
        return item

    def cancel(self, item_id: str) -> bool:
        return (
            self._db.execute(
                "UPDATE print_queue SET status='cancelled' WHERE id=? AND status='waiting'", (_parse_id(item_id),)
            )
            > 0
        )

    def reorder(self, printer_id: str, ids: list[str]) -> None:
        """Sets the order of a printer's waiting items; ids not listed keep their place after the listed ones."""
        pid = parse_printer_id(printer_id)
        wanted = [_parse_id(i) for i in ids]
        with self._db.tx() as conn:
            waiting = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM print_queue WHERE printer_id=? AND status='waiting' ORDER BY position", (pid,)
                )
            ]
            if set(wanted) - set(waiting):
                raise QueueError("Queue changed; reload and try again")
            order = wanted + [i for i in waiting if i not in wanted]
            for pos, item_id in enumerate(order, start=1):
                conn.execute("UPDATE print_queue SET position=? WHERE id=?", (pos, item_id))

    async def run_forever(self) -> None:
        while True:
            try:
                await self.dispatch()
            except Exception:  # The loop must survive one bad round.
                log.exception("Queue dispatch failed")
            await asyncio.sleep(DISPATCH_INTERVAL_S)

    async def dispatch(self) -> list[str]:
        """One round: starts the first waiting print on each ready printer. Returns the item ids sent."""
        async with self._lock:
            live = await self._printers.live()  # Also updates the bed flags from state changes.
            sent: list[str] = []
            for p in live:
                if not p.get("connected") or p.get("state") not in READY_STATES or p.get("needs_attention"):
                    continue
                row = self._db.one(
                    "SELECT * FROM print_queue WHERE printer_id=? AND status='waiting' ORDER BY position LIMIT 1",
                    (p["id"],),
                )
                if row is not None and await self._send(dict(row), p["name"]):
                    sent.append(row["id"])
            return sent

    async def _send(self, item: dict[str, Any], printer_name: str) -> bool:
        path = self._gcode_path(item["job_id"], item["file"])
        if path is None:
            self._finish(item["id"], "failed", "The sliced file no longer exists.")
            return False
        # The printer's profile may have changed since this was queued.
        job = self._db.one("SELECT params FROM jobs WHERE id=?", (item["job_id"],))
        printer = self._printers.get(item["printer_id"])
        try:
            if printer is None:
                raise PrinterMismatch("Printer was removed.")
            self._printers.check_gcode_fits(printer, json.loads(job["params"]).get("printer") if job else None)
        except PrinterMismatch as e:
            self._finish(item["id"], "failed", str(e))
            return False
        self._db.execute("UPDATE print_queue SET status='sending' WHERE id=?", (item["id"],))
        try:
            remote = self._remote_name(item["job_id"], item["file"])
            await self._printers.run(item["printer_id"], lambda c: c.upload(path, remote, start=True))
        except PrinterBusy:
            self._db.execute("UPDATE print_queue SET status='waiting' WHERE id=?", (item["id"],))
            return False
        except PrinterError as e:
            self._finish(item["id"], "failed", str(e))
            self._audit(None, "queue_failed", f"{item['id']} -> {printer_name}: {e}")
            return False
        self._finish(item["id"], "sent", None)
        # The bed is in use from now on; the flag returns automatically when this print ends.
        self._printers.observe_state(item["printer_id"], "printing")
        job = self._db.one("SELECT title FROM jobs WHERE id=?", (item["job_id"],))
        title = (job["title"] if job else None) or item["file"]
        self._audit(item["owner"], "queue_started", f"{title} -> {printer_name}")
        return True

    def _finish(self, item_id: str, status: str, error: str | None) -> None:
        self._db.execute(
            "UPDATE print_queue SET status=?, error=?, sent_at=? WHERE id=?", (status, error, time.time(), item_id)
        )
