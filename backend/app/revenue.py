"""Ledger with explicit provenance.

Provenance classes:
- verified_platform: fetched from an official monetary API (none connected yet; see docs/ANALYTICS_AND_REVENUE.md)
- imported: rows from an uploaded CSV statement, linked to the file's SHA-256 (duplicate files are rejected)
- manual: entered by the operator (sponsorships, affiliate payouts, invoices)
- estimated: cost estimates only; never revenue and never counted as revenue
Platform earnings that cannot be fetched are reported as "unavailable", never as 0.
"""
import csv
import hashlib
import io
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import audit
from .auth import require_auth
from .db import session
from .models import LedgerEntry, RevenueImport

router = APIRouter(prefix="/api/v1/revenue")

REVENUE_CATEGORIES = ("platform_earnings", "affiliate", "sponsorship", "other")
COST_CATEGORIES = ("processing_cost", "cloud_cost", "other")
PLATFORMS = ("youtube", "tiktok", "instagram")
CSV_COLUMNS = ["date", "kind", "category", "amount", "currency", "platform", "reference"]
MAX_IMPORT_BYTES = 1_000_000
MAX_IMPORT_ROWS = 5000
UNAVAILABLE_REASON = {
    "youtube": "YouTube earnings need the yt-analytics-monetary.readonly scope on a monetized channel; not connected",
    "tiktok": "TikTok exposes no creator earnings API; import a statement CSV instead",
    "instagram": "Instagram exposes no creator earnings API; import a statement CSV instead",
}


class ManualEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["revenue", "expense"]
    category: str = Field(max_length=64)
    cents: int = Field(gt=0, le=10_000_000_000)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    provenance: Literal["manual", "estimated"] = "manual"
    platform: Literal["youtube", "tiktok", "instagram"] | None = None
    period_start: date | None = None
    period_end: date | None = None
    publication_id: int | None = Field(default=None, gt=0, le=2147483647)
    source_id: int | None = Field(default=None, gt=0, le=2147483647)
    reference: str = Field(min_length=3, max_length=500)

    def problems(self) -> str | None:
        allowed = REVENUE_CATEGORIES if self.kind == "revenue" else COST_CATEGORIES
        if self.category not in allowed:
            return f"category must be one of {', '.join(allowed)}"
        if self.provenance == "estimated" and self.kind == "revenue":
            return "estimated values may only be recorded as costs, never as revenue"
        if self.period_start and self.period_end and self.period_end < self.period_start:
            return "period_end must not precede period_start"
        return None


def day(value: date | None) -> datetime | None:
    return datetime.combine(value, time.min, tzinfo=timezone.utc) if value else None


def entry_read(entry: LedgerEntry) -> dict:
    return {"id": entry.id, "kind": entry.kind, "category": entry.category, "cents": entry.cents, "currency": entry.currency,
            "provenance": entry.provenance, "verified": entry.provenance == "verified_platform", "platform": entry.platform,
            "period_start": entry.period_start, "period_end": entry.period_end, "publication_id": entry.publication_id,
            "source_id": entry.source_id, "reference": entry.reference, "note": entry.note, "created_at": entry.created_at,
            "import_id": entry.import_id}


def summarize(entries: list[LedgerEntry]) -> dict:
    usd = [entry for entry in entries if entry.currency == "USD"]
    revenue = {kind: sum(e.cents for e in usd if e.kind == "revenue" and e.provenance == kind)
               for kind in ("verified_platform", "imported", "manual")}
    # Defensive: an estimated revenue row (impossible via the API) is excluded and surfaced, never summed.
    excluded = sum(e.cents for e in usd if e.kind == "revenue" and e.provenance not in revenue)
    revenue["total"] = sum(revenue.values())
    costs = {"actual": sum(e.cents for e in usd if e.kind == "expense" and e.provenance != "estimated"),
             "estimated": sum(e.cents for e in usd if e.kind == "expense" and e.provenance == "estimated")}
    costs["total"] = costs["actual"] + costs["estimated"]
    platforms = {}
    for platform in PLATFORMS:
        rows = [e for e in usd if e.kind == "revenue" and e.category == "platform_earnings" and e.platform == platform
                and e.provenance in {"verified_platform", "imported", "manual"}]
        platforms[platform] = ({"status": "available", "cents": sum(e.cents for e in rows),
                                "provenance": sorted({e.provenance for e in rows})} if rows
                               else {"status": "unavailable", "cents": None, "reason": UNAVAILABLE_REASON[platform]})
    other = sorted({e.currency for e in entries if e.currency != "USD"})
    return {"currency": "USD", "revenue_cents": revenue, "cost_cents": costs,
            "net_cents": revenue["total"] - costs["total"],
            "net_label": "verified + imported + manual revenue minus actual and estimated costs",
            "excluded_revenue_cents": excluded, "platform_earnings": platforms,
            "other_currencies_not_converted": other, "entries": len(entries)}


@router.get("/summary")
def revenue_summary(db: Session = Depends(session), _: str = Depends(require_auth)) -> dict:
    return summarize(list(db.scalars(select(LedgerEntry))))


@router.post("/entries", status_code=201)
def create_entry(payload: ManualEntry, db: Session = Depends(session), actor: str = Depends(require_auth)) -> dict:
    if problem := payload.problems():
        raise HTTPException(422, problem)
    entry = LedgerEntry(kind=payload.kind, category=payload.category, cents=payload.cents, currency=payload.currency,
                        provenance=payload.provenance, verified=False, platform=payload.platform,
                        period_start=day(payload.period_start), period_end=day(payload.period_end),
                        publication_id=payload.publication_id, source_id=payload.source_id, reference=payload.reference,
                        note=f"{payload.provenance} entry by operator")
    db.add(entry)
    db.flush()
    audit(db, actor, "ledger.create", "ledger_entry", entry.id, provenance=payload.provenance, kind=payload.kind)
    db.commit()
    return entry_read(entry)


def parse_amount(text: str) -> int:
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise ValueError("amount must be a decimal number") from None
    if value <= 0 or value.as_tuple().exponent < -2 or value > Decimal("100000000"):
        raise ValueError("amount must be positive with at most 2 decimals")
    return int(value * 100)


def parse_rows(raw: bytes) -> list[dict]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(422, "CSV must be UTF-8") from None
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != CSV_COLUMNS:
        raise HTTPException(422, f"CSV header must be exactly: {','.join(CSV_COLUMNS)}")
    rows, errors = [], []
    for number, row in enumerate(reader, start=2):
        if number - 1 > MAX_IMPORT_ROWS:
            raise HTTPException(413, f"CSV may contain at most {MAX_IMPORT_ROWS} rows")
        try:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("wrong number of columns")
            platform = row["platform"].strip() or None
            if platform and platform not in PLATFORMS:
                raise ValueError("unknown platform")
            entry = ManualEntry(kind=row["kind"].strip(), category=row["category"].strip(), cents=parse_amount(row["amount"]),
                                currency=row["currency"].strip().upper(), platform=platform,
                                period_start=date.fromisoformat(row["date"].strip()),
                                period_end=date.fromisoformat(row["date"].strip()), reference=row["reference"].strip() or "imported row")
            if problem := entry.problems():
                raise ValueError(problem)
            rows.append(entry)
        except ValidationError as exc:
            errors.append(f"row {number}: " + "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())[:200])
        except (ValueError, TypeError) as exc:
            errors.append(f"row {number}: {str(exc).splitlines()[0][:160]}")
    if errors:
        raise HTTPException(422, "; ".join(errors[:20]))
    if not rows:
        raise HTTPException(422, "CSV contains no rows")
    return rows


@router.post("/import", status_code=201)
async def import_csv(file: UploadFile = File(...), db: Session = Depends(session), actor: str = Depends(require_auth)) -> dict:
    raw = await file.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        raise HTTPException(413, "CSV import is limited to 1 MB")
    digest = hashlib.sha256(raw).hexdigest()
    if db.scalar(select(RevenueImport.id).where(RevenueImport.sha256 == digest)):
        raise HTTPException(409, "this file was already imported")
    rows = parse_rows(raw)
    record = RevenueImport(sha256=digest, filename=(file.filename or "statement.csv")[:255], rows=len(rows), actor=actor)
    db.add(record)
    db.flush()
    for row in rows:
        db.add(LedgerEntry(kind=row.kind, category=row.category, cents=row.cents, currency=row.currency, provenance="imported",
                           verified=False, platform=row.platform, period_start=day(row.period_start), period_end=day(row.period_end),
                           reference=row.reference, import_id=record.id, note=f"imported from {record.filename} ({digest[:12]})"))
    audit(db, actor, "ledger.import", "revenue_import", record.id, rows=len(rows), sha256=digest)
    db.commit()
    return {"import_id": record.id, "rows": len(rows), "sha256": digest}


def safe_cell(value) -> str:
    """Neutralize spreadsheet formula injection on export."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.get("/export.csv")
def export_csv(db: Session = Depends(session), _: str = Depends(require_auth)) -> Response:
    out = io.StringIO()
    writer = csv.writer(out)
    columns = ["id", "created_at", "kind", "category", "amount", "currency", "provenance", "platform", "reference"]
    writer.writerow(columns)
    for entry in db.scalars(select(LedgerEntry).order_by(LedgerEntry.id)):
        writer.writerow([safe_cell(item) for item in (entry.id, entry.created_at.isoformat() if entry.created_at else "", entry.kind,
                         entry.category, f"{entry.cents / 100:.2f}", entry.currency, entry.provenance, entry.platform, entry.reference)])
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="soe-ledger.csv"', "Cache-Control": "no-store"})
