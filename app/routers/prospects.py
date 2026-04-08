import csv
import io

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.prospect import Prospect
from app.schemas.prospect import ImportResult, ProspectCreate, ProspectOut

router = APIRouter(prefix="/prospects", tags=["prospects"])


@router.get("/", response_model=list[ProspectOut])
def list_prospects(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    return db.query(Prospect).offset(skip).limit(limit).all()


@router.get("/{prospect_id}", response_model=ProspectOut)
def get_prospect(prospect_id: str, db: Session = Depends(get_db)):
    prospect = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not prospect:
        raise HTTPException(status_code=404, detail="Prospect not found")
    return prospect


@router.post("/", response_model=ProspectOut, status_code=201)
def create_prospect(data: ProspectCreate, db: Session = Depends(get_db)):
    prospect = Prospect(**data.model_dump())
    db.add(prospect)
    try:
        db.commit()
        db.refresh(prospect)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already exists")
    return prospect


@router.post("/import/csv", response_model=ImportResult)
async def import_csv(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Import prospects from a CSV file.

    Expected columns (email is required, all others optional):
    email, first_name, last_name, company, title, linkedin_url,
    phone, asset_class_preference, geography, source
    """
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a .csv")

    max_size = 10 * 1024 * 1024  # 10MB
    content = await file.read(max_size + 1)
    if len(content) > max_size:
        raise HTTPException(status_code=400, detail="File too large — 10MB maximum")

    text = content.decode("utf-8-sig")  # handles BOM from Excel exports
    reader = csv.DictReader(io.StringIO(text))

    imported = 0
    skipped = 0
    errors = []

    for row_num, row in enumerate(reader, start=2):  # row 1 is header
        email = (row.get("email") or "").strip().lower()
        if not email:
            errors.append(f"Row {row_num}: missing email — skipped")
            skipped += 1
            continue

        asset_class = (row.get("asset_class_preference") or "").strip() or None
        if asset_class and asset_class not in ("PE", "RE", "both"):
            errors.append(
                f"Row {row_num}: invalid asset_class_preference '{asset_class}' — set to null"
            )
            asset_class = None

        prospect = Prospect(
            email=email,
            first_name=(row.get("first_name") or "").strip() or None,
            last_name=(row.get("last_name") or "").strip() or None,
            company=(row.get("company") or "").strip() or None,
            title=(row.get("title") or "").strip() or None,
            linkedin_url=(row.get("linkedin_url") or "").strip() or None,
            phone=(row.get("phone") or "").strip() or None,
            asset_class_preference=asset_class,
            geography=(row.get("geography") or "").strip() or None,
            source=(row.get("source") or "").strip() or "apollo",
        )
        db.add(prospect)
        try:
            # Use a savepoint so only this row rolls back on duplicate — not the whole batch
            with db.begin_nested():
                db.flush()
            imported += 1
        except IntegrityError:
            errors.append(f"Row {row_num}: {email} already exists — skipped")
            skipped += 1

    db.commit()
    return ImportResult(imported=imported, skipped=skipped, errors=errors)
