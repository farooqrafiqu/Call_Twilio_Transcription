import os, json, time, base64, audioop, wave, logging
from pathlib import Path
from queue import Queue
from threading import Thread
from dotenv import load_dotenv
from flask import Flask, has_request_context, request, Response, jsonify, current_app
from flask_sock import Sock
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Start, Stream, Dial
import vosk                                   # optional live ASR
from logging.handlers import RotatingFileHandler
from twilio.jwt.access_token import AccessToken
from flask_socketio import SocketIO, emit
from twilio.jwt.access_token.grants import VoiceGrant
# ──────────────── configuration ────────────────
load_dotenv()
AUDIO_ROOT = Path("captures"); AUDIO_ROOT.mkdir(exist_ok=True)
TWILIO_SID        = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN      = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM       = os.getenv("TWILIO_NUMBER")        # your verified/outbound number

public_url = os.getenv("PUBLIC_URL")
print(f"• public URL → {public_url}")


twilio = Client(TWILIO_SID, TWILIO_TOKEN)

# ──────────── logging: console + rotating file ──────────
logger = logging.getLogger("media")
logger.setLevel(logging.INFO)

fmt = logging.Formatter("%(asctime)s  %(message)s")
sh  = logging.StreamHandler()
sh.setFormatter(fmt)
logger.addHandler(sh)

fh = RotatingFileHandler("media.log", maxBytes=8_000_000, backupCount=4)  # ← use the class directly
fh.setFormatter(fmt)
logger.addHandler(fh)
def log_event(evt: str, extra: dict | None = None):
    row = {"ts": time.time(), "evt": evt, **(extra or {})}
    logger.info(json.dumps(row))

SAMPLE_RATE_IN  = 8000    # what Twilio sends
SAMPLE_RATE_OUT = 16000

# ─────────────── Flask + WebSocket ──────────────
app  = Flask(__name__)
sockio = SocketIO(app, cors_allowed_origins="*")
sock = Sock(app)

# at the top, after you create `sockio`
TRANSCRIPT_EVT = {
    "in":  {"partial": "transcript_in_partial",  "final": "Caller"},
    "out": {"partial": "transcript_out_partial", "final": "Receiver"},
}

# (optional) recognizer pools so you get live transcripts
MODEL = vosk.Model("model") if os.path.isdir("model") else None
rec_pool: dict[str, dict[str, vosk.KaldiRecognizer]] = {}
audio_q:  Queue[bytes | None] = Queue()

def generate_token(identity):
    token = AccessToken(
        os.environ['TWILIO_ACCOUNT_SID'],
        os.environ['TWILIO_API_KEY'],
        os.environ['TWILIO_API_SECRET'],
        identity=identity
    )
    
    voice_grant = VoiceGrant(
        outgoing_application_sid=os.environ['TWILIO_APP_SID'],
        incoming_allow=True
    )
    token.add_grant(voice_grant)
    return token

@app.route("/")
def hello():
    return "Twilio ↔ Flask ↔ Socket.IO relay is running"

# ─────────────── Twilio outbound entrypoint ─────
@app.route("/make-call", methods=["POST"])
def make_call():
    """POST form-urlencoded: to=<E.164>. Returns Call SID."""
    to = request.form.get("to")
    if not to:
        return {"error": "missing 'to'"}, 400

    voice_url = f"{public_url}/voice"
    call = twilio.calls.create(to=to, from_=TWILIO_FROM, url=voice_url)
    log_event("call_created", {"sid": call.sid, "to": to})
    return {"call_sid": call.sid}, 201

# ─────────────── TwiML generator ────────────────
@app.route("/voice", methods=["POST"])
def voice():
    """Tell Twilio to start BOTH tracks toward /stream and keep talking."""
    form_params  = request.form.to_dict(flat=True)          # {'username': 'alice', 'age': '30'}
    ws_url = public_url.replace("https", "wss") + "/stream"
    vr  = VoiceResponse()
    vr.say("Hello, you are now connected, your call is streaming.")
    st  = Start();  st.stream(url=ws_url, track="both_tracks")   # key line!
    vr.append(st)
    dial = Dial(callerId=TWILIO_FROM, answerOnBridge=True)    # :contentReference[oaicite:1]{index=1}
    dial.number(form_params['To'])    # child TwiML :contentReference[oaicite:2]{index=2}
    vr.append(dial)
    vr.pause(length=600)                              # keep call open 10 min
    return Response(str(vr), mimetype="application/xml")


# ─────────────── WebSocket media sink ────────────
@sock.route("/stream")
def stream(ws):
    sid = None
    last_audio_ts = {"in": time.time(), "out": time.time()}

    while (msg := ws.receive()) is not None:
        data = json.loads(msg)
        evt  = data.get("event")

        # ─── stream starts ───────────────────────────
        if evt == "start":
            sid = data["streamSid"]
            log_event("ws_start", {"sid": sid})
            rec_pool[sid] = {
                "in":  vosk.KaldiRecognizer(MODEL, 16000),
                "out": vosk.KaldiRecognizer(MODEL, 16000),
            }

        # ─── media frames ────────────────────────────
        elif evt == "media":
            track   = data["media"]["track"]              # "inbound" or "outbound"
            side    = "in" if track == "inbound" else "out"
            ulaw    = base64.b64decode(data["media"]["payload"])
            pcm16k  = ulaw8k_to_pcm16k(ulaw)

            # ───── forward raw audio to any Socket.IO clients ─────
            b64_pcm = base64.b64encode(pcm16k).decode()
            event   = "media_in" if side == "in" else "media_out"
            sockio.emit(event, {"sid": sid, "pcm16k": b64_pcm})

            # ───── keep track of last audio time ─────
            last_audio_ts[side] = time.time()

            # ───── perform ASR and log only FINAL results ─────
            rec = rec_pool[sid][side]
            if rec.AcceptWaveform(pcm16k):
                txt = json.loads(rec.Result())["text"]
                if txt:
                    log_event("final", {"sid": sid, "side": side, "txt": txt})
                    sockio.emit(
                        TRANSCRIPT_EVT[side]["final"],
                        {"sid": sid, "text": txt},
                    )


        # ─── normal stream end ───────────────────────
        elif evt == "stop":
            flush_leftovers(sid)                      # ➋ grab any trailing words
            log_event("ws_stop", {"sid": sid})
            break

        # ─── silence watcher ─────────────────────────
        # ➌ if no audio on a side for ≥30 s, flush it once
        for side, ts in last_audio_ts.items():
            if time.time() - ts >= 30:
                flush_leftovers(sid, side)
                last_audio_ts[side] = float('inf')    # ensure it runs only once

    ws.close()


def flush_leftovers(sid: str, side: str | None = None):
    """
    Force-log any text still sitting in the recognizer buffer.
    If *side* is None, flush both 'in' and 'out'.
    """
    if sid not in rec_pool:
        return
    sides = [side] if side else ["in", "out"]
    for s in sides:
        rec = rec_pool[sid][s]
        txt = json.loads(rec.FinalResult())["text"]
        if txt:
            log_event("final", {"sid": sid, "side": s, "txt": txt})

def save_wav(raw: bytes, out_path: Path, rate: int = 8000):
    pcm16 = audioop.ulaw2lin(raw, 2)
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(rate)
        wf.writeframes(pcm16)

def ulaw8k_to_pcm16k(ulaw_bytes: bytes) -> bytes:
    pcm8k = audioop.ulaw2lin(ulaw_bytes, 2)                   # 8 kHz, 16-bit
    pcm16k, _ = audioop.ratecv(pcm8k, 2, 1,
                               SAMPLE_RATE_IN,
                               SAMPLE_RATE_OUT,
                               None)
    return pcm16k

# ─────────────── app launch ──────────────────────
if __name__ == "__main__":
    # eventlet monkey-patching makes Flask-SocketIO non-blocking
    import eventlet
    import eventlet.wsgi
    eventlet.monkey_patch()

    # Start ngrok tunnel first (unchanged)
    print(f"• public URL → {public_url}")

    # One unified server for HTTP + SocketIO + Twilio WS
    sockio.run(app, host="0.0.0.0", port=6000, log_output=False, debug=False)