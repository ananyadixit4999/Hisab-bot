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
        print(f"Transcription Error: {e}")
        return None

    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)


# ---------------- Extract Transaction Details ----------------
def extract_details(text: str):
    try:
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))

        prompt = f"""
Extract transaction information from the text below.

Return ONLY valid JSON in this format:

{{
    "description": "string",
    "amount": number,
    "language": "string"
}}

Text:
{text}
"""

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "user", "content": prompt}
            ],
            temperature=0
        )

        content = response.choices[0].message.content.strip()

        # Remove markdown code fences if present
        if content.startswith("```"):
            content = (
                content.replace("```json", "")
                .replace("```", "")
                .strip()
            )

        return json.loads(content)

    except Exception as e:
        print(f"Extraction Error: {e}")
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

    try:
        text_to_process = Body

        # If audio message received
        if MediaUrl0:
            text_to_process = transcribe_audio(MediaUrl0)

        if not text_to_process:
            twiml.message(
                "Sorry, I couldn't understand the audio or message."
            )
            return Response(
                content=str(twiml),
                media_type="application/xml"
            )

        details = extract_details(text_to_process)

        if not details or "amount" not in details:
            twiml.message(
                "Could not extract transaction details. Please try again."
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
            twiml.message("Invalid amount detected.")
            return Response(
                content=str(twiml),
                media_type="application/xml"
            )

        new_tx = Transaction(
            id=str(uuid.uuid4()),
            description=details.get(
                "description",
                "No description"
            ),
            amount=amount,
            language=details.get("language", "en"),
            status="confirmed"
        )

        db.add(new_tx)
        db.commit()

        twiml.message(
            f"✅ Recorded: {new_tx.description} - ₹{new_tx.amount}"
        )

    except Exception as e:
        db.rollback()
        print(f"Webhook Error: {e}")

        twiml.message(
            "An error occurred while processing your transaction."
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
