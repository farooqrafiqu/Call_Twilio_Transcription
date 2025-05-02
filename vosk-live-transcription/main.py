
import os
import io
import json
import time
import base64
import wave
import logging

from pathlib import Path
from queue import Queue

from flask import Flask, request, Response, jsonify
from flask_sock import Sock
from flask_socketio import SocketIO

from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Start, Stream, Dial
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from logging.handlers import RotatingFileHandler

# optional ASR
import vosk

# choose processing backend
USE_PYDUB = os.getenv("USE_PYDUB", "false").lower() == "true"
if USE_PYDUB:
    from pydub import AudioSegment
else:
    import numpy as np
    from scipy.signal import resample_poly

# ──────────────── configuration ────────────────
from dotenv import load_dotenv
load_dotenv()

AUDIO_ROOT = Path("captures")
AUDIO_ROOT.mkdir(exist_ok=True)

TWILIO_SID    = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM   = os.getenv("TWILIO_NUMBER")
TWIML_APP_SID = os.getenv("TWILIO_APP_SID")
PUBLIC_URL    = os.getenv("PUBLIC_URL")

# Twilio client
twilio = Client(TWILIO_SID, TWILIO_TOKEN)

# ──────────── logging: console + rotating file ──────────
logger = logging.getLogger("media")
logger.setLevel(logging.INFO)

fmt = logging.Formatter("%(asctime)s  %(message)s")
sh  = logging.StreamHandler()
sh.setFormatter(fmt)
logger.addHandler(sh)

fh = RotatingFileHandler("media.log", maxBytes=8_000_000, backupCount=4)
fh.setFormatter(fmt)
logger.addHandler(fh)

def log_event(evt: str, extra: dict | None = None):
    row = {"ts": time.time(), "evt": evt, **(extra or {})}
    logger.info(json.dumps(row))
    for h in logger.handlers:
        h.flush()

# ─────────── ASR / transcript events & SILENCE FLUSH ───────────
SAMPLE_RATE_IN        = 8000
SAMPLE_RATE_OUT       = 16000
SILENCE_FLUSH_SECS    = 3
THROTTLE_WINDOW_SECS  = 2

TRANSCRIPT_EVT = {
    "in":  {"partial": "transcript_in_partial",  "final": "Caller"},
    "out": {"partial": "transcript_out_partial", "final": "Receiver"},
}

MODEL = vosk.Model("model") if Path("model").is_dir() else None
rec_pool: dict[str, dict[str, vosk.KaldiRecognizer]] = {}
last_log_ts: dict[str, float] = {}

# ───────────── Flask + SocketIO + Sock setup ─────────────
app    = Flask(__name__)
sock   = Sock(app)
sockio = SocketIO(app, cors_allowed_origins="*")

def generate_token(identity: str) -> AccessToken:
    token = AccessToken(
        TWILIO_SID,
        os.environ['TWILIO_API_KEY'],
        os.environ['TWILIO_API_SECRET'],
        identity=identity
    )
    voice_grant = VoiceGrant(
        outgoing_application_sid=TWIML_APP_SID,
        incoming_allow=True
    )
    token.add_grant(voice_grant)
    return token

@app.route("/")
def hello():
    return "Twilio ↔ Flask ↔ WebSocket relay is running"

@app.route("/make-call", methods=["POST"])
def make_call():
    to = request.form.get("to")
    if not to:
        return {"error": "missing 'to'"}, 400
    voice_url = f"{PUBLIC_URL}/voice"
    call = twilio.calls.create(to=to, from_=TWILIO_FROM, url=voice_url)
    log_event("call_created", {"sid": call.sid, "to": to})
    return {"call_sid": call.sid}, 201

@app.route("/voice", methods=["POST"])
def voice():
    ws_url = PUBLIC_URL.replace("https", "wss") + "/stream"
    vr = VoiceResponse()
    vr.say("Hello, you are now connected. Your call is streaming.")
    start = Start()
    start.stream(url=ws_url, track="both_tracks")
    vr.append(start)
    dial = Dial(callerId=TWILIO_FROM, answerOnBridge=True)
    dial.number(request.form["To"])
    vr.append(dial)
    vr.pause(length=600)
    return Response(str(vr), mimetype="application/xml")

# ───────────── media conversion helpers ─────────────
if USE_PYDUB:
    def ulaw8k_to_pcm16k(ulaw_bytes: bytes) -> bytes:
        audio = AudioSegment.from_file(
            io.BytesIO(ulaw_bytes),
            format="mulaw",
            frame_rate=SAMPLE_RATE_IN,
            channels=1
        )
        audio = audio.set_frame_rate(SAMPLE_RATE_OUT).set_sample_width(2)
        return audio.raw_data
else:
    def ulaw2pcm16(ulaw_bytes: bytes) -> np.ndarray:
        ulaw = np.frombuffer(ulaw_bytes, dtype=np.uint8).astype(np.int16)
        ulaw = ~ulaw
        sign = ulaw & 0x80
        exp  = (ulaw >> 4) & 0x07
        mant = ulaw & 0x0F
        mag  = ((mant << 4) + 0x84) << exp
        pcm  = mag - 0x84
        return np.where(sign != 0, -pcm, pcm)

    def resample_to_16k(pcm8k: np.ndarray) -> np.ndarray:
        return resample_poly(pcm8k, up=2, down=1)

    def ulaw8k_to_pcm16k(ulaw_bytes: bytes) -> bytes:
        pcm8k = ulaw2pcm16(ulaw_bytes)
        pcm16 = resample_to_16k(pcm8k)
        return pcm16.astype(np.int16).tobytes()

    def save_wav(raw_ulaw: bytes, out_path: Path, rate_in: int = SAMPLE_RATE_IN):
        pcm = ulaw2pcm16(raw_ulaw)
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate_in)
            wf.writeframes(pcm.tobytes())

# ───────────── WebSocket media sink ─────────────
@sock.route("/stream")
def stream(ws):
    sid = None
    last_audio_ts = {"in": time.time(), "out": time.time()}

    while (msg := ws.receive()) is not None:
        data = json.loads(msg)
        evt  = data.get("event")

        if evt == "start":
            sid = data["streamSid"]
            log_event("ws_start", {"sid": sid})
            if MODEL:
                rec_pool[sid] = {
                    "in":  vosk.KaldiRecognizer(MODEL, SAMPLE_RATE_OUT),
                    "out": vosk.KaldiRecognizer(MODEL, SAMPLE_RATE_OUT),
                }

        elif evt == "media":
            ulaw = base64.b64decode(data["media"]["payload"])
            pcm16k = ulaw8k_to_pcm16k(ulaw)
            side = "in" if data["media"]["track"] == "inbound" else "out"
            now = time.time()

            # partial throttled
            if MODEL:
                rec = rec_pool[sid][side]
                part = json.loads(rec.PartialResult()).get("partial")
                if part:
                    key = f"partial_{side}_{part}"
                    if now - last_log_ts.get(key, 0) >= THROTTLE_WINDOW_SECS:
                        last_log_ts[key] = now
                        log_event("partial", {"sid": sid, "side": side, "txt": part})
                        sockio.emit(TRANSCRIPT_EVT[side]["partial"], {"sid": sid, "text": part})

            # final throttled
            if MODEL and rec.AcceptWaveform(pcm16k):
                txt = json.loads(rec.Result())["text"]
                if txt:
                    key = f"final_{side}_{txt}"
                    if now - last_log_ts.get(key, 0) >= THROTTLE_WINDOW_SECS:
                        last_log_ts[key] = now
                        log_event("final", {"sid": sid, "side": side, "txt": txt})
                        sockio.emit(TRANSCRIPT_EVT[side]["final"], {"sid": sid, "text": txt})

            # live audio
            sockio.emit(
                f"media_{side}",
                {"sid": sid, "pcm16k": base64.b64encode(pcm16k).decode()}
            )
            last_audio_ts[side] = now

        elif evt == "stop":
            if MODEL:
                for s in ("in", "out"):
                    txt = json.loads(rec_pool[sid][s].FinalResult())["text"]
                    if txt:
                        log_event("final", {"sid": sid, "side": s, "txt": txt})
            log_event("ws_stop", {"sid": sid})
            break

        # silence flush
        for s, ts in last_audio_ts.items():
            if time.time() - ts >= SILENCE_FLUSH_SECS:
                if MODEL:
                    txt = json.loads(rec_pool[sid][s].FinalResult())["text"]
                    if txt:
                        log_event("final", {"sid": sid, "side": s, "txt": txt})
                last_audio_ts[s] = float("inf")

    ws.close()

# ───────────── run app ─────────────
if __name__ == "__main__":
    import eventlet
    eventlet.monkey_patch()
    print(f"• public URL → {PUBLIC_URL}")
    sockio.run(app, host="0.0.0.0", port=6000, log_output=False, debug=False)
