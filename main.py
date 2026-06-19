import os
import uuid
import json
import requests
from datetime import datetime # Fix 1: Added missing import

from fastapi import FastAPI, Request, Depends, Form
from groq import Groq
from dotenv import load_dotenv
from sqlalchemy import Column, DateTime, Float, Integer, String, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv()

# --- Database Setup ---
DATABASE_URL = "sqlite:///./hisab.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
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

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- Helper Functions ---
def transcribe_audio(audio_url):
    # Fix 2: Use unique filename for concurrency
    temp_filename = f"{uuid.uuid4()}.ogg"
    try:
        auth = (os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
        response = requests.get(audio_url, auth=auth, allow_redirects=True)
        
        if response.status_code != 200:
            return None
            
        with open(temp_filename, "wb") as f:
            f.write(response.content)
            
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        with open(temp_filename, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-large-v3", 
                file=(temp_filename, audio_file, "audio/ogg")
            )
        return transcript.text
    except Exception as e:
        print(f"Transcription Error: {e}")
        return None
    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)

def extract_details(text):
    try:
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        prompt = f"Extract transaction JSON (description, amount, language) from: {text}. Return ONLY raw JSON."
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        # Robust parsing
        content = response.choices[0].message.content.strip()
        return json.loads(content)
    except Exception:
        return {}

# --- FastAPI Endpoints ---
@app.post("/whatsapp")
async def whatsapp_webhook(
    From: str = Form(...),
    Body: str = Form(None),
    MediaUrl0: str = Form(None),
    db: Session = Depends(get_db)
):
    twiml = MessagingResponse()
    text_to_process = Body

    if MediaUrl0:
        text_to_process = transcribe_audio(MediaUrl0)

    if not text_to_process:
        twiml.message("Sorry, I couldn't understand the audio or message.")
        return Response(content=str(twiml), media_type="application/xml")

    details = extract_details(text_to_process)
    
    if details and "amount" in details:
        new_tx = Transaction(
            id=str(uuid.uuid4()),
            description=details.get("description", "No description"),
            amount=float(details["amount"]),
            language=details.get("language", "en"),
            status="confirmed"
        )
        db.add(new_tx)
        db.commit()
        twiml.message(f"✅ Recorded: {new_tx.description} - ₹{new_tx.amount}")
    else:
        twiml.message("Could not extract transaction details. Please try again.")

    return Response(content=str(twiml), media_type="application/xml")
