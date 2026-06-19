import os
import uuid
import json
import requests
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Depends, Form
from fastapi.responses import Response
from groq import Groq
from dotenv import load_dotenv
from sqlalchemy import Column, DateTime, Float, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv()

# ---------------- DB ----------------
DATABASE_URL = "sqlite:///./hisab.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

# ---------------- MODELS ----------------
class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(String, primary_key=True)
    description = Column(String)
    person_name = Column(String)
    transaction_type = Column(String)  # given / received
    amount = Column(Float)
    category = Column(String, default="uncategorized")
    created_at = Column(DateTime, default=datetime.utcnow)


class PendingTransaction(Base):
    __tablename__ = "pending"

    phone_number = Column(String, primary_key=True)
    data = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class CreditLimit(Base):
    __tablename__ = "credit_limit"

    phone_number = Column(String, primary_key=True)
    person_name = Column(String, primary_key=True)
    limit = Column(Float, default=0)

Base.metadata.create_all(bind=engine)

app = FastAPI()

# ---------------- DB ----------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------------- AUDIO ----------------
def transcribe_audio(url: str):
    try:
        auth = (os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
        r = requests.get(url, auth=auth, timeout=30)
        r.raise_for_status()

        file = f"{uuid.uuid4()}.ogg"
        with open(file, "wb") as f:
            f.write(r.content)

        client = Groq(api_key=os.getenv("GROQ_API_KEY"))

        with open(file, "rb") as f:
            res = client.audio.transcriptions.create(
                model="whisper-large-v3",
                file=(file, f.read())
            )

        os.remove(file)
        return res.text

    except Exception as e:
        print("audio error", e)
        return None

# ---------------- AI ----------------
def extract_details(text: str):
    try:
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))

        res = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "Return ONLY JSON."},
                {"role": "user", "content": f"""
Extract:
- description
- amount
- person_name
- transaction_type (given/received)
- category

Text:
{text}

Rules:
- दिया/को/उधार => given
- लिया/से => received

Return JSON only.
"""}
            ],
            temperature=0
        )

        content = res.choices[0].message.content
        start = content.find("{")
        end = content.rfind("}")
        return json.loads(content[start:end+1])

    except:
        return {}

# ---------------- HELP ----------------
def last_tx(db):
    return db.query(Transaction).order_by(Transaction.created_at.desc()).first()

# ---------------- APP ----------------
@app.post("/whatsapp")
async def whatsapp(
    From: str = Form(...),
    Body: Optional[str] = Form(None),
    MediaUrl0: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    twiml = MessagingResponse()
    msg = (Body or "").lower()

    # ---------------- DAILY ----------------
    if Body and "आज का हिसाब" in Body:
        today = datetime.utcnow().date()

        txs = db.query(Transaction).filter(
            Transaction.created_at >= datetime.combine(today, datetime.min.time())
        ).all()

        total = sum(t.amount for t in txs)

        text = "📅 आज का हिसाब\n"
        text += "\n".join([f"{t.description} - ₹{t.amount}" for t in txs])
        text += f"\n\nकुल: ₹{total}"

        twiml.message(text)
        return Response(str(twiml), media_type="application/xml")

    # ---------------- MONTH ----------------
    if Body and "इस महीने का हिसाब" in Body:
        start = datetime.utcnow().replace(day=1)

        txs = db.query(Transaction).filter(
            Transaction.created_at >= start
        ).all()

        total = sum(t.amount for t in txs)

        text = "📆 इस महीने का हिसाब\n"
        text += "\n".join([f"{t.description} - ₹{t.amount}" for t in txs])
        text += f"\n\nकुल: ₹{total}"

        twiml.message(text)
        return Response(str(twiml), media_type="application/xml")

    # ---------------- LEDGER ----------------
    if Body and "का हिसाब" in Body:
        name = Body.replace("का हिसाब", "").strip()

        txs = db.query(Transaction).filter(
            Transaction.person_name == name
        ).all()

        given = sum(t.amount for t in txs if t.transaction_type == "given")
        received = sum(t.amount for t in txs if t.transaction_type == "received")

        bal = given - received

        text = f"""📒 {name} का हिसाब

दिया: ₹{given}
लिया: ₹{received}

{'बाकी लेना है' if bal>0 else 'बाकी देना है'}: ₹{abs(bal)}"""

        twiml.message(text)
        return Response(str(twiml), media_type="application/xml")

    # ---------------- SEARCH ----------------
    if Body and "ढूंढ" in Body:
        q = Body.replace("ढूंढ", "").strip()

        txs = db.query(Transaction).filter(
            Transaction.description.contains(q)
        ).all()

        text = "\n".join([f"{t.description} - ₹{t.amount}" for t in txs]) or "कुछ नहीं मिला"

        twiml.message(text)
        return Response(str(twiml), media_type="application/xml")

    # ---------------- DELETE LAST ----------------
    if Body and "delete last" in msg:
        tx = last_tx(db)
        if tx:
            db.delete(tx)
            db.commit()
            twiml.message("Deleted last entry")
        else:
            twiml.message("No data")

        return Response(str(twiml), media_type="application/xml")

    # ---------------- CONFIRM ----------------
    if msg in ["हाँ", "ha", "yes"]:
        pending = db.query(PendingTransaction).filter(
            PendingTransaction.phone_number == From
        ).first()

        if not pending:
            twiml.message("No pending")
            return Response(str(twiml), media_type="application/xml")

        d = json.loads(pending.data)

        tx = Transaction(
            id=str(uuid.uuid4()),
            description=d["description"],
            person_name=d["person_name"],
            transaction_type=d["transaction_type"],
            amount=float(d["amount"]),
            category=d.get("category", "uncategorized")
        )

        db.add(tx)
        db.delete(pending)
        db.commit()

        twiml.message("Saved ✅")
        return Response(str(twiml), media_type="application/xml")

    # ---------------- INPUT ----------------
    try:
        text = Body
        if MediaUrl0:
            text = transcribe_audio(MediaUrl0)

        if not text:
            return Response(str(twiml), media_type="application/xml")

        d = extract_details(text)

        if not d:
            twiml.message("Not understood")
            return Response(str(twiml), media_type="application/xml")

        pending = PendingTransaction(
            phone_number=From,
            data=json.dumps(d)
        )

        db.add(pending)
        db.commit()

        twiml.message(f"""Understood:
{d['description']}
₹{d['amount']}
Confirm?""")

    except Exception as e:
        print(e)
        twiml.message("Error")

    return Response(str(twiml), media_type="application/xml")


@app.get("/")
def health():
    return {"status": "ok"}
