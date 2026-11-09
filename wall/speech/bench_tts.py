"""Time Kokoro on the mini: real-time factor for a 2-sentence reply. Usage: bench_tts.py MODEL_DIR VOICE [THREADS]"""
import os, sys, time
os.environ["MIRA_TTS_DIR"] = sys.argv[1]
os.environ["MIRA_TTS_VOICE"] = sys.argv[2]
if len(sys.argv) > 3:
    os.environ["MIRA_TTS_THREADS"] = sys.argv[3]
sys.path.insert(0, os.path.dirname(__file__))
import server
t=time.perf_counter(); tts = server.Tts(server.TTS_DIR, server.TTS_VOICE); print("load", round(time.perf_counter()-t,1), "s")
text = "You have two events today. A physics review at three, and the printer finishes at five."
for i in range(3):
    t=time.perf_counter(); s, r = tts.speak(text, None); el=time.perf_counter()-t
    print(f"run{i} {el:.2f}s audio {len(s)/r:.2f}s rtf {el/(len(s)/r):.2f}")
