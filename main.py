
import os
import sqlite3
import uuid
from datetime import datetime, time

import openai
import speech_recognition as sr
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, Request
from pydub import AudioSegment
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
        message = twilio_client.messages.create(
            from_=f"whatsapp:{twilio_phone_number}",
            body=message,
            to=f"whatsapp:{to}",
        )
        return message.sid
    except Exception as e:
        print(f"Error sending WhatsApp message: {e}")
        return None


def transcribe_audio(audio_url):
    try:
        # Download and convert audio to WAV
        audio_content = twilio_client.http_client.request("GET", audio_url)
        audio = AudioSegment.from_file(
            audio_content, format="ogg"
        )  # Twilio sends ogg
        wav_path = "temp.wav"
        audio.export(wav_path, format="wav")

        # Transcribe audio
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)
        text = recognizer.recognize_google(audio_data)
        os.remove(wav_path)
        return text
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
        response = openai.Completion.create(
            model="gpt-4o-mini",
            prompt=prompt,
            max_tokens=100,
            temperature=0,
        )
        details = response.choices[0].text.strip()
        return details
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
            transaction_details_json = get_transaction_details_from_gpt(
                transcribed_text
            )
            try:
                import json

                transaction_details = json.loads(transaction_details_json)
                if transaction_details:
                    # Create a new pending transaction
                    new_transaction = Transaction(
                        id=str(uuid.uuid4()),
                        description=transaction_details["description"],
                        amount=transaction_details["amount"],
                        language=transaction_details["language"],
                        status="pending",
                    )
                    db.add(new_transaction)
                    db.commit()
                    db.refresh(new_transaction)

                    # Send confirmation message
                    confirmation_message = f"""
                    Please confirm the following transaction:
                    Description: {new_transaction.description}
                    Amount: {new_transaction.amount}

                    Reply '1' to confirm, '2' to reject.
                    (Transaction ID: {new_transaction.id})
                    """
                    send_whatsapp_message(user_phone, confirmation_message)
                    response.message("Processing your request...")
                else:
                    response.message(
                        "Could not understand the transaction from your voice message."
                    )
            except (json.JSONDecodeError, KeyError):
                response.message(
                    "Sorry, I could not process the details from your voice message."
                )
        else:
            response.message(
                "Sorry, I could not transcribe your voice message."
            )
    elif Body:
        # Handle confirmation/rejection
        body_lower = Body.lower().strip()
        # Find the last pending transaction for this user to confirm/reject.
        # Note: A more robust solution might involve caching the transaction ID
        # per user or using a more sophisticated state management.
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
                response.message("Transaction confirmed!")
            elif body_lower == "2":
                last_pending.status = "rejected"
                db.commit()
                response.message("Transaction rejected.")
            else:
                response.message("Invalid input. Please reply '1' to confirm or '2' to reject.")
        else:
            response.message("No pending transaction found to confirm/reject.")

    else:
        response.message(
            "Welcome to Hisab! Please send a voice message with your transaction details."
        )

    return str(response)


# --- Nightly Summary Logic ---
def run_nightly_summary(db):
    yesterday = datetime.utcnow().date() - timedelta(days=1)
    start_of_day = datetime.combine(yesterday, time.min)
    end_of_day = datetime.combine(yesterday, time.max)

    confirmed_transactions = db.query(Transaction).filter(
        Transaction.status == "confirmed",
        Transaction.created_at >= start_of_day,
        Transaction.created_at < end_of_day,
    )

    total_transactions = confirmed_transactions.count()
    total_amount = confirmed_transactions.with_entities(
        func.sum(Transaction.amount)
    ).scalar()

    if total_transactions > 0:
        summary = NightlySummary(
            date=start_of_day,
            total_transactions=total_transactions,
            total_amount=total_amount or 0,
        )
        db.add(summary)
        db.commit()
        print(f"Nightly summary for {yesterday} created.")


# You would run this function with a scheduler like APScheduler or a cron job.
# For simplicity, here's a manual trigger endpoint (remove in production).
@app.post("/trigger-nightly-summary")
async def trigger_nightly_summary_endpoint(db=Depends(get_db)):
    run_nightly_summary(db)
    return {"message": "Nightly summary job triggered."}


if __name__ == "__main__":
    import uvicorn
    from datetime import timedelta
    import threading

    # In a real production app, use a more robust scheduler.
    def schedule_nightly_job():
        import time as time_module

        while True:
            now = datetime.utcnow()
            # Schedule to run at around 1 AM UTC
            run_time = now.replace(
                hour=1, minute=0, second=0, microsecond=0
            )
            if now > run_time:
                run_time += timedelta(days=1)
            sleep_seconds = (run_time - now).total_seconds()
            time_module.sleep(sleep_seconds)
            with SessionLocal() as db:
                run_nightly_summary(db)

    scheduler_thread = threading.Thread(target=schedule_nightly_job, daemon=True)
    scheduler_thread.start()

    uvicorn.run(app, host="0.0.0.0", port=8000)

