import os
import sqlite3
import uuid
import json
from datetime import datetime, time, timedelta

import openai
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, Request
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
    create_engine,
    func,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

# Load environment variables
load_dotenv()

# --- Environment and API Key Setup ---
DATABASE_URL = "sqlite:///./hisab.db"
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Twilio Configuration
twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID")
twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN")
twilio_phone_number = os.getenv("TWILIO_PHONE_NUMBER")
twilio_client = Client(twilio_account_sid, twilio_auth_token)

# OpenAI Configuration
openai.api_key = os.getenv("OPENAI_API_KEY")
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- Database Models ---
class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(String, primary_key=True, index=True)
    description = Column(String, index=True)
    amount = Column(Float)
    language = Column(String)
    status = Column(String, default="pending")  # pending, confirmed, rejected
    created_at = Column(DateTime, default=datetime.utcnow)

class NightlySummary(Base):
    __tablename__ = "nightly_summaries"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(DateTime, unique=True)
    total_transactions = Column(Integer)
    total_amount = Column(Float)

Base.metadata.create_all(bind=engine)

# --- FastAPI Application ---
app = FastAPI()

# --- Dependency ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- Helper Functions ---
def send_whatsapp_message(to, message):
    try:
        msg = twilio_client.messages.create(
            from_=f"whatsapp:{twilio_phone_number}",
            body=message,
            to=f"whatsapp:{to}",
        )
        return msg.sid
    except Exception as e:
        print(f"Error sending WhatsApp message: {e}")
        return None

def transcribe_audio(audio_url):
    try:
        # Download audio using twilio credentials
        import requests
        response = requests.get(audio_url, auth=(twilio_account_sid, twilio_auth_token))
        ogg_path = "temp.ogg"
        with open(ogg_path, "wb") as f:
            f.write(response.content)
            
        # Send raw audio binary directly to OpenAI Whisper API
        with open(ogg_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1", 
                file=audio_file
            )
        os.remove(ogg_path)
        return transcript.text
    except Exception as e:
        print(f"Error transcribing audio: {e}")
        return None

def get_transaction_details_from_gpt(text):
    try:
        prompt = f"""
        You are a helpful assistant for a ledger bot.
        Extract the transaction details from the following text. The text could be in Hindi, English, Hinglish, or Punjabi.
        The output should be a JSON object with 'description' and 'amount'.
        Also, detect the language of the text and include it in the JSON as 'language'.
        Supported languages are: Hindi, English, Hinglish, Punjabi.
        If the text is not a valid transaction, return an empty JSON object.
        Text: "{text}"
        """
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100,
            temperature=0,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error getting transaction details from GPT: {e}")
        return "{}"

# --- FastAPI Endpoints ---
@app.post("/whatsapp")
async def whatsapp_webhook(
    request: Request,
    db=Depends(get_db),
    From: str = Form(...),
    Body: str = Form(None),
    MediaUrl0: str = Form(None),
):
    response = MessagingResponse()
    user_phone = From.split("whatsapp:")[-1]

    if MediaUrl0:
        transcribed_text = transcribe_audio(MediaUrl0)
        if transcribed_text:
            transaction_details_json = get_transaction_details_from_gpt(transcribed_text)
            try:
                # Strip out any markdown formatting if present
                clean_json = transaction_details_json.replace("```json", "").replace("```", "").strip()
                transaction_details = json.loads(clean_json)
                if transaction_details and "amount" in transaction_details:
                    new_transaction = Transaction(
                        id=str(uuid.uuid4()),
                        description=transaction_details.get("description", "Udhaar"),
                        amount=float(transaction_details["amount"]),
                        language=transaction_details.get("language", "Hinglish"),
                        status="pending",
                    )
                    db.add(new_transaction)
                    db.commit()
                    db.refresh(new_transaction)

                    confirmation_message = f"""🔍 Hisab AI Check:
मैने सुना: {new_transaction.description} को ₹{new_transaction.amount} का लेनदेन।

क्या यह सही है?
👍 हाँ के लिए '1' भेजें।
👎 गलत है तो '2' भेजें."""
                    
                    send_whatsapp_message(user_phone, confirmation_message)
                    return str(response)
                else:
                    response.message("Could not understand the transaction details from your voice message.")
            except Exception as e:
                print(f"JSON parsing error: {e}")
                response.message("Sorry, I could not process the details from your voice message.")
        else:
            response.message("Sorry, I could not transcribe your voice message.")
    elif Body:
        body_lower = Body.lower().strip()
        last_pending = (
            db.query(Transaction)
            .filter(Transaction.status == "pending")
            .order_by(Transaction.created_at.desc())
            .first()
        )

        if last_pending:
            if body_lower == "1":
                last_pending.status = "confirmed"
                db.commit()
                response.message("✅ Transaction successfully saved in your Hisab ledger!")
            elif body_lower == "2":
                last_pending.status = "rejected"
                db.commit()
                response.message("❌ Transaction cancelled.")
            else:
                response.message("Invalid input. Please reply '1' to confirm or '2' to reject.")
        else:
            response.message("No pending transaction found to confirm or reject.")
    else:
        response.message("Welcome to Hisab! Please send a voice message with your transaction details.")

    return str(response)
