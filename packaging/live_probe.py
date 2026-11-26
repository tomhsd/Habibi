"""
Live voice probe: does Gemini Live hear us? Speaks a test sentence with the
Windows voice, sends it to Gemini Live exactly like Habibi's mic does
(16 kHz int16 PCM via send_realtime_input), and prints what comes back.

    .venv312\\Scripts\\python.exe packaging\\live_probe.py           # TTS sentence
    .venv312\\Scripts\\python.exe packaging\\live_probe.py --mic     # 5 s of your mic

Prints only short lines; never prints the API key.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import numpy as np                       # noqa: E402
from google import genai                 # noqa: E402
from google.genai import types           # noqa: E402

MODEL = "models/gemini-3.1-flash-live-preview"
RATE = 16000
CHUNK = 1024


def tts_pcm(text: str) -> bytes:
    import win32com.client
    path = Path(tempfile.gettempdir()) / "habibi_probe.wav"
    v = win32com.client.Dispatch("SAPI.SpVoice")
    fs = win32com.client.Dispatch("SAPI.SpFileStream")
    fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
    fmt.Type = 18                         # SAFT16kHz16BitMono
    fs.Format = fmt
    fs.Open(str(path), 3)
    v.AudioOutputStream = fs
    v.Speak(text)
    fs.Close()
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


def mic_pcm(seconds: float = 5.0) -> bytes:
    import sounddevice as sd
    print(f"Speak now for {seconds:.0f} s (e.g. 'Habibi, what time is it?')…", flush=True)
    rec = sd.rec(int(seconds * RATE), samplerate=RATE, channels=1, dtype="int16")
    sd.wait()
    return rec.tobytes()


async def probe(pcm: bytes) -> None:
    from memory.config_manager import get_gemini_key
    key = get_gemini_key()
    if not key:
        print("No Gemini key in config.")
        return
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    print(f"audio: {len(a) / RATE:.1f}s rms={np.sqrt(np.mean(a * a)):.0f} peak={np.abs(a).max():.0f}")
    import google.genai as g
    print(f"google-genai {g.__version__}, python {sys.version.split()[0]}")
    client = genai.Client(api_key=key, http_options={"api_version": "v1beta"})
    cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription={},
        output_audio_transcription={},
        system_instruction="You are Habibi, a voice assistant. Answer in one short sentence.",
    )
    heard, said, audio_bytes = [], [], 0
    async with client.aio.live.connect(model=MODEL, config=cfg) as s:
        print("connected")
        silence = bytes(CHUNK * 2)
        data = pcm + silence * int(1.5 * RATE / CHUNK)
        for i in range(0, len(data), CHUNK * 2):
            await s.send_realtime_input(audio=types.Blob(data=data[i:i + CHUNK * 2],
                                                         mime_type="audio/pcm;rate=16000"))
            await asyncio.sleep(CHUNK / RATE)
        t0 = time.monotonic()

        async def recv():
            nonlocal audio_bytes
            async for r in s.receive():
                sc = r.server_content
                if r.data:
                    audio_bytes += len(r.data)
                if sc is None:
                    continue
                if sc.input_transcription and sc.input_transcription.text:
                    heard.append(sc.input_transcription.text)
                if sc.output_transcription and sc.output_transcription.text:
                    said.append(sc.output_transcription.text)
                if sc.turn_complete:
                    return
        try:
            await asyncio.wait_for(recv(), timeout=15)
        except asyncio.TimeoutError:
            print("no turn_complete within 15 s")
        print(f"reply after {time.monotonic() - t0:.1f}s")
    print("HEARD :", "".join(heard).strip() or "(nothing)")
    print("SAID  :", "".join(said).strip() or "(nothing)")
    print(f"reply audio bytes: {audio_bytes}")


if __name__ == "__main__":
    pcm = mic_pcm() if "--mic" in sys.argv else tts_pcm("Habibi, what time is it?")
    try:
        asyncio.run(probe(pcm))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {str(e)[:300]}")
