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

# ---------------- Database Setup ----------------
DATABASE_URL = "sqlite:///./hisab.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(String, primary_key=True, index=True)
    description = Column(String, index=True)
    amount = Column(Float)
    language = Column(String)
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)


class PendingTransaction(Base):
    __tablename__ = "pending_transactions"

    phone_number = Column(String, primary_key=True)
    data = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)

app = FastAPI()


# ---------------- Database Dependency ----------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------- Audio Transcription ----------------
def transcribe_audio(audio_url: str):
    temp_filename = f"{uuid.uuid4()}.ogg"

    try:
        auth = (
            os.getenv("TWILIO_ACCOUNT_SID"),
            os.getenv("TWILIO_AUTH_TOKEN")
        )

        response = requests.get(
            audio_url,
            auth=auth,
            allow_redirects=True,
            timeout=30
        )

        response.raise_for_status()

        with open(temp_filename, "wb") as f:
            f.write(response.content)

        client = Groq(api_key=os.getenv("GROQ_API_KEY"))

        with open(temp_filename, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-large-v3",
                file=(temp_filename, audio_file.read())
            )

        return transcript.text

    except Exception as e:
        print("Transcription Error:", e)
        return None

    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)


# ---------------- Extract Transaction Details ----------------
def extract_details(text: str):
    try:
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You extract expense transactions. "
                        "Return ONLY valid JSON."
                    )
                },
                {
                    "role": "user",
                    "content": f"""
Extract transaction information from:

{text}

Return ONLY JSON:

{{
  "description": "string",
  "amount": number,
  "language": "string"
}}
"""
                }
            ],
            temperature=0
        )

        content = response.choices[0].message.content

        print("RAW GROQ RESPONSE:", repr(content))

        if not content:
            return {}

        content = content.strip()

        # Remove markdown formatting if model adds it
        content = content.replace("```json", "")
        content = content.replace("```", "")
        content = content.strip()

        # Extract JSON object if extra text is present
        start = content.find("{")
        end = content.rfind("}")

        if start == -1 or end == -1:
            return {}

        content = content[start:end + 1]

        return json.loads(content)

    except Exception as e:
        print("Extraction Error:", e)
        return {}


# ---------------- WhatsApp Webhook ----------------
@app.post("/whatsapp")
async def whatsapp_webhook(
    From: str = Form(...),
    Body: Optional[str] = Form(None),
    MediaUrl0: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    twiml = MessagingResponse()

    msg = (Body or "").strip().lower()

    # YES
    if msg in ["हाँ", "ha", "haan", "yes"]:

            pending = db.query(PendingTransaction).filter(
            PendingTransaction.phone_number == From
        ).first()

        if pending:
            db.delete(pending)
            db.commit()

        pending = PendingTransaction(
            phone_number=From,
            data=json.dumps(details)
        )

        db.add(pending)
        db.commit()

        twiml.message(
            f"""मैंने यह समझा:

{details.get('description', '')}

राशि: ₹{amount}

क्या यह सही है?

उत्तर दें:

हाँ

या

नहीं"""
        )

        pending = db.query(PendingTransaction).filter(
            PendingTransaction.phone_number == From
        ).first()

        if pending:
            db.delete(pending)
            db.commit()

        twiml.message(
            "ठीक है। कृपया दोबारा लिखें या वॉइस मैसेज भेजें।"
        )

        return Response(
            content=str(twiml),
            media_type="application/xml"
        )

    try:
        text_to_process = Body

        # Audio message
        if MediaUrl0:
            text_to_process = transcribe_audio(MediaUrl0)

        if not text_to_process:
            twiml.message(
                "माफ़ कीजिए, मैं संदेश समझ नहीं पाया।"
            )
            return Response(
                content=str(twiml),
                media_type="application/xml"
            )

        details = extract_details(text_to_process)

        if not details or "amount" not in details:
            twiml.message(
                "लेन-देन की जानकारी नहीं मिली। कृपया फिर से प्रयास करें।"
            )
            return Response(
                content=str(twiml),
                media_type="application/xml"
            )

        try:
            amount = float(
                str(details["amount"])
                .replace("₹", "")
                .replace(",", "")
                .strip()
            )
        except ValueError:
            twiml.message("राशि सही नहीं मिली। कृपया फिर से प्रयास करें।")
            return Response(
                content=str(twiml),
                media_type="application/xml"
            )

       pending = db.query(PendingTransaction).filter(
    PendingTransaction.phone_number == From
).first()

if pending:
    db.delete(pending)
    db.commit()

pending = PendingTransaction(
    phone_number=From,
    data=json.dumps(details)
)

db.add(pending)
db.commit()

twiml.message(
    f"""मैंने यह समझा:

{details.get('description', '')}

राशि: ₹{amount}

क्या यह सही है?

उत्तर दें:

हाँ

या

नहीं"""
)

    except Exception as e:
        db.rollback()
        print("Webhook Error:", e)

        twiml.message(
            "लेन-देन दर्ज करते समय त्रुटि हुई।"
        )

    return Response(
        content=str(twiml),
        media_type="application/xml"
    )


# ---------------- Health Check ----------------
@app.get("/")
def health_check():
    return {
        "status": "running",
        "service": "Hisab WhatsApp Bot"
    }
