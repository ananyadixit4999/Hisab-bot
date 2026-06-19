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

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(String, primary_key=True)
    description = Column(String)
    person_name = Column(String)
    transaction_type = Column(String)
    amount = Column(Float)
    language = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class PendingTransaction(Base):
    __tablename__ = "pending_transactions"

    phone_number = Column(String, primary_key=True)
    data = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)

app = FastAPI()

# ---------------- DB ----------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------- Transcription ----------------
def transcribe_audio(audio_url: str):
    try:
        auth = (os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))

        r = requests.get(audio_url, auth=auth, timeout=30)
        r.raise_for_status()

        file_name = f"{uuid.uuid4()}.ogg"
        with open(file_name, "wb") as f:
            f.write(r.content)

        client = Groq(api_key=os.getenv("GROQ_API_KEY"))

        with open(file_name, "rb") as f:
            res = client.audio.transcriptions.create(
                model="whisper-large-v3",
                file=(file_name, f.read())
            )

        os.remove(file_name)
        return res.text

    except Exception as e:
        print("Audio error:", e)
        return None


# ---------------- Extract ----------------
def extract_details(text: str):
    try:
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))

        res = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": "Return ONLY JSON."
                },
                {
                    "role": "user",
                    "content": f"""
Extract:

- description
- amount
- person_name
- transaction_type (given/received)
- language

Text:
{text}

Rules:
- को/दिए/उधार दिए => given
- से लिए/लौटाए => received

Return ONLY JSON.
"""
                }
            ],
            temperature=0
        )

        content = res.choices[0].message.content.strip()

        start = content.find("{")
        end = content.rfind("}")

        if start == -1 or end == -1:
            return {}

        return json.loads(content[start:end+1])

    except Exception as e:
        print("extract error:", e)
        return {}


# ---------------- WEBHOOK ----------------
@app.post("/whatsapp")
async def whatsapp_webhook(
    From: str = Form(...),
    Body: Optional[str] = Form(None),
    MediaUrl0: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    twiml = MessagingResponse()
    msg = (Body or "").strip().lower()

    # ===================== TODAY SUMMARY =====================
    if Body and "आज का हिसाब" in Body:
        today = datetime.utcnow().date()

        txs = db.query(Transaction).filter(
            Transaction.created_at >= datetime.combine(today, datetime.min.time())
        ).all()

        total = 0
        lines = []

        for t in txs:
            lines.append(f"{t.description} - ₹{t.amount}")
            total += t.amount

        text = "📅 आज का हिसाब\n\n"
        text += "\n".join(lines) if lines else "कोई लेन-देन नहीं"
        text += f"\n\nकुल: ₹{total}"

        twiml.message(text)
        return Response(content=str(twiml), media_type="application/xml")

    # ===================== LEDGER =====================
    if Body and "का हिसाब" in Body:
        name = Body.replace("का हिसाब", "").strip()

        txs = db.query(Transaction).filter(
            Transaction.person_name == name
        ).all()

        given = sum(t.amount for t in txs if t.transaction_type == "given")
        received = sum(t.amount for t in txs if t.transaction_type == "received")

        balance = given - received

        if balance > 0:
            text = f"""📒 {name} का हिसाब

दिया: ₹{given}
लिया: ₹{received}

बाकी लेना है: ₹{balance}"""
        else:
            text = f"""📒 {name} का हिसाब

दिया: ₹{given}
लिया: ₹{received}

बाकी देना है: ₹{abs(balance)}"""

        twiml.message(text)
        return Response(content=str(twiml), media_type="application/xml")

    # ===================== YES =====================
    if msg in ["हाँ", "ha", "haan", "yes"]:
        pending = db.query(PendingTransaction).filter(
            PendingTransaction.phone_number == From
        ).first()

        if not pending:
            twiml.message("कोई लंबित एंट्री नहीं मिली।")
            return Response(content=str(twiml), media_type="application/xml")

        details = json.loads(pending.data)

        tx = Transaction(
            id=str(uuid.uuid4()),
            description=details["description"],
            person_name=details["person_name"],
            transaction_type=details["transaction_type"],
            amount=float(details["amount"]),
            language=details.get("language", "")
        )

        db.add(tx)
        db.delete(pending)
        db.commit()

        twiml.message("✅ हिसाब दर्ज कर लिया गया।")
        return Response(content=str(twiml), media_type="application/xml")

    # ===================== NO =====================
    if msg in ["नहीं", "nahi", "nahin", "no"]:
        pending = db.query(PendingTransaction).filter(
            PendingTransaction.phone_number == From
        ).first()

        if pending:
            db.delete(pending)
            db.commit()

        twiml.message("ठीक है, फिर से भेजें।")
        return Response(content=str(twiml), media_type="application/xml")

    # ===================== INPUT =====================
    try:
        text = Body

        if MediaUrl0:
            text = transcribe_audio(MediaUrl0)

        if not text:
            twiml.message("समझ नहीं आया।")
            return Response(content=str(twiml), media_type="application/xml")

        details = extract_details(text)

        if not details:
            twiml.message("जानकारी नहीं मिली।")
            return Response(content=str(twiml), media_type="application/xml")

        required = ["amount", "person_name", "transaction_type"]

        if not all(k in details for k in required):
            twiml.message("अधूरी जानकारी।")
            return Response(content=str(twiml), media_type="application/xml")

        pending = PendingTransaction(
            phone_number=From,
            data=json.dumps(details)
        )

        db.add(pending)
        db.commit()

        amount = float(str(details["amount"]).replace("₹", ""))

        twiml.message(f"""मैंने यह समझा:

{details['description']}

राशि: ₹{amount}

हाँ / नहीं?""")

    except Exception as e:
        db.rollback()
        print("error:", e)
        twiml.message("त्रुटि हुई।")

    return Response(content=str(twiml), media_type="application/xml")


# ---------------- HEALTH ----------------
@app.get("/")
def health():
    return {"status": "running"}
