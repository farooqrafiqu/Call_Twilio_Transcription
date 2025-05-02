Twilio-Flask-SocketIO Relay
This project implements a real-time audio streaming and transcription relay using Twilio, Flask, and Socket.IO. It enables outbound calls, streams audio bidirectionally via WebSocket, performs live automatic speech recognition (ASR) with Vosk (optional), and forwards audio and transcripts to connected clients.
Features

Twilio Integration: Initiates outbound calls and handles inbound/outbound audio streams.
WebSocket Streaming: Relays audio frames to Socket.IO clients in real-time.
Live Transcription: Optional ASR using Vosk for both call sides (caller/receiver).
Logging: Console and rotating file logs for events and transcripts.
Audio Processing: Converts Twilio's μ-law 8kHz audio to PCM 16kHz for processing.
ngrok Support: Exposes the Flask server to the public internet for Twilio callbacks.

Prerequisites

Python 3.8+
Twilio account with:
Account SID (TWILIO_ACCOUNT_SID)
Auth Token (TWILIO_AUTH_TOKEN)
Verified phone number (TWILIO_NUMBER)
App SID (TWILIO_APP_SID)
API Key and Secret (TWILIO_API_KEY, TWILIO_API_SECRET)


ngrok account and public URL (PUBLIC_URL)
Optional: Vosk model (model directory) for live transcription
Dependencies listed in requirements.txt

Installation

Clone the repository:git clone <repository-url>
cd <repository-directory>


Install dependencies:pip install -r requirements.txt


Set up environment variables in a .env file:TWILIO_ACCOUNT_SID=your_account_sid
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_NUMBER=+1234567890
TWILIO_APP_SID=your_app_sid
TWILIO_API_KEY=your_api_key
TWILIO_API_SECRET=your_api_secret
PUBLIC_URL=https://your-ngrok-url


(Optional) Download and extract a Vosk model to the model directory for live transcription.

Usage

Start the Flask server:python app.py

The server runs on 0.0.0.0:6000 and exposes HTTP, WebSocket, and Socket.IO endpoints.
Initiate an outbound call by sending a POST request to /make-call:curl -X POST -d "to=+1234567890" http://localhost:6000/make-call

Replace +1234567890 with the destination phone number in E.164 format.
Audio streams are saved to the captures directory, and logs are written to media.log.

Endpoints

GET /: Health check endpoint returning a status message.
POST /make-call: Initiates an outbound call. Expects to (E.164 phone number) in form data. Returns Call SID.
POST /voice: Generates TwiML for Twilio to stream audio to /stream and maintain the call.
WS /stream: WebSocket endpoint for Twilio media streams. Processes audio and performs ASR.

Socket.IO Events

media_in: Emits inbound audio (caller) as base64-encoded PCM 16kHz.
media_out: Emits outbound audio (receiver) as base64-encoded PCM 16kHz.
Caller: Emits final transcriptions for the caller.
Receiver: Emits final transcriptions for the receiver.

Audio Processing

Twilio sends μ-law 8kHz audio, which is converted to PCM 16kHz for processing.
Audio is saved as WAV files in the captures directory.
Vosk (if enabled) transcribes audio in real-time, logging final results.

Logging

Events and transcripts are logged to media.log (8MB max, 4 backups).
Log format: JSON with timestamp (ts), event type (evt), and additional data (e.g., Call SID, transcript text).

Notes

Ensure PUBLIC_URL is set to a valid ngrok tunnel URL accessible by Twilio.
The server uses eventlet for non-blocking WebSocket handling.
Silence detection flushes transcription buffers after 30 seconds of inactivity.
The call remains open for 10 minutes (<Pause length="600"/> in TwiML).

License
MIT License. See LICENSE for details.
