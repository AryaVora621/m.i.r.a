"""speaking: how a reply becomes sound, shared by the facade's say() and the streamed Hermes answer.

Helper module, not a plugin (no SPEC). Three parts:
- text rules: where the first spoken chunk ends (short, so the first audio is early), the sentence guard, and the
  splitter that turns Hermes's `assistant.delta` pieces into whole sentences while the answer is still arriving;
- caption groups: the reply cut into three-word groups with the time each one is due inside one audio chunk, so the
  wall's caption can be posted on the playback clock instead of an estimate on the page;
- Speaker: the pipeline (synthesis ahead of playback, the duck started with the first synthesis, the next chunk's
  player launched just before the current one ends).
"""

import queue
import re
import threading
import time
import wave
import io

from .common import log
from .speech_link import SpeechOffline
from .tts_remote import clean_for_speech, speech_chunks

LEGACY_CLASSES = ()

# A sentence ends at . ! ? (closing quotes allowed) followed by whitespace, so "3.5" and "5.30" never split.
_SENT_END = re.compile(r"[.!?][\"')\]]*(?=\s)")
_CLAUSE_END = re.compile(r"[,;:](?=\s)")
CLAUSE_MIN_CHARS = 24  # a clause shorter than this is not worth a chunk of its own


def _clause_cut(text, upto, first=False):
    """Index just after a clause break at or before `upto` that leaves a clause worth speaking, else -1: the last such
    break, or the first with `first` (a stream takes the earliest one it has: waiting for a better cut costs time)."""
    best = -1
    for m in _CLAUSE_END.finditer(text[:upto]):
        if m.end() >= CLAUSE_MIN_CHARS:
            best = m.end()
            if first:
                break
    return best


def cut_first(text, first_chars):
    """(head, rest) for the first spoken chunk: the first sentence, or its first clause when the sentence is longer
    than `first_chars`. (None, text) when the rule is off (first_chars 0) or the text is already short."""
    text = text.strip()
    if first_chars <= 0 or len(text) <= first_chars:
        return None, text
    m = _SENT_END.search(text)
    end = m.end() if m else len(text)
    if end <= first_chars:
        return text[:end].strip(), text[end:].strip()
    cut = _clause_cut(text, first_chars)
    if cut > 0:
        return text[:cut].strip(), text[cut:].strip()
    return text[:end].strip(), text[end:].strip()


def split_sentences(text):
    """The sentences of a finished text, in order."""
    out, pos = [], 0
    for m in _SENT_END.finditer(text):
        out.append(text[pos:m.end()].strip())
        pos = m.end()
    tail = text[pos:].strip()
    if tail:
        out.append(tail)
    return [s for s in out if s]


def limit_sentences(text, n):
    """The first `n` sentences (0 keeps all)."""
    if n <= 0:
        return text
    return " ".join(split_sentences(text)[:n])


def split_for_speech(text, limit=280, max_chunks=3, first_chars=0):
    """The spoken chunks of a whole reply: a short first chunk, then v1's sentence packing for the rest."""
    text = clean_for_speech(text)
    head, rest = cut_first(text, first_chars)
    if head is None or max_chunks <= 1:
        return speech_chunks(text, limit, max_chunks)
    return [head] + speech_chunks(rest, limit, max_chunks - 1)


class StreamSplitter:
    """Whole sentences out of Hermes's assistant.delta pieces. feed() returns the chunks that became complete;
    finish(final) the ones left once the answer is complete (`final` is assistant.completed's content, which wins when
    it extends what the deltas spelled). The first chunk is cut early (see cut_first) so speech can start sooner.
    `max_sentences` caps how many chunks are produced (0 is no cap), `max_chars` the total spoken length."""

    def __init__(self, first_chars=90, max_sentences=0, max_chars=840):
        self.first_chars, self.max_sentences, self.max_chars = first_chars, max_sentences, max_chars
        self.raw = ""
        self.pos = 0       # raw[:pos] has been handed out
        self.count = 0
        self.chars = 0

    @property
    def done(self):
        return (self.max_sentences > 0 and self.count >= self.max_sentences) or self.chars >= self.max_chars

    def _take(self, end, out):
        chunk = self.raw[self.pos:end].strip()
        self.pos = end
        if chunk:
            self.count += 1
            self.chars += len(chunk)
            out.append(chunk)

    def feed(self, delta):
        self.raw += delta
        out = []
        while not self.done:
            pending = self.raw[self.pos:]
            m = _SENT_END.search(pending)
            if m:
                end = m.end()
                if self.count == 0 and self.first_chars > 0 and end > self.first_chars:
                    cut = _clause_cut(pending, end, first=True)
                    if cut > 0:
                        end = cut
                self._take(self.pos + end, out)
            elif self.count == 0 and self.first_chars > 0 and len(pending) > self.first_chars:
                cut = _clause_cut(pending, len(pending), first=True)
                if cut <= 0:
                    break
                self._take(self.pos + cut, out)
            else:
                break
        return out

    def finish(self, final=None):
        out = []
        if final and final.startswith(self.raw[:self.pos]) and len(final) >= len(self.raw[:self.pos]):
            self.raw = final
        elif not self.raw.strip() and final:
            self.raw = final
        for s in split_sentences(self.raw[self.pos:]):
            if self.done:
                break
            self.count += 1
            self.chars += len(s)
            out.append(s)
        self.pos = len(self.raw)
        return out


# -- caption groups ------------------------------------------------------------------------------------------------

_FUNCTION = {"a", "an", "the", "to", "of", "in", "on", "at", "and", "or", "my", "your", "is", "it"}
_SENT_WORD = re.compile(r"[.!?…:;][\"')\]]*$")
_COMMA_WORD = re.compile(r",[\"')\]]*$")
SENTENCE_PAUSE_S, COMMA_PAUSE_S = 0.35, 0.15


def _bare(w):
    return re.sub(r"^[^a-z0-9']+|[^a-z0-9']+$", "", w.lower())


def caption_words(text, target=3, lo=2, hi=4):
    """The text as groups of two to four words (about three), breaking at sentence ends and commas and never leaving a
    function word dangling; the same idea as the page's captionChunks, kept here so the listener can time them."""
    words = [w for w in re.sub(r"[*_`#>~|]+", " ", text or "").split() if w]
    out, i = [], 0
    while i < len(words):
        left = len(words) - i
        if left <= hi and not any(_SENT_WORD.search(w) for w in words[i:i + left - 1]):
            if left >= lo or not out:
                out.append(" ".join(words[i:]))
                break
        n = min(target, left)
        for k in range(lo, min(hi, left) + 1):
            w = words[i + k - 1]
            if _SENT_WORD.search(w) or _COMMA_WORD.search(w):
                n = k
                break
        while n < min(hi, left) and _bare(words[i + n - 1]) in _FUNCTION and not (_SENT_WORD.search(words[i + n - 1]) or _COMMA_WORD.search(words[i + n - 1])):
            n += 1
        if left - n == 1 and n < hi and not _SENT_WORD.search(words[i + n - 1]):
            n += 1
        out.append(" ".join(words[i:i + n]))
        i += n
    return out


def caption_schedule(text, audio_s, min_group_s=0.5):
    """[(offset_s, group_text)]: when each caption group is due inside one audio chunk of `audio_s` seconds. The time is
    shared by characters (a sentence end and a comma weigh a little more, since the voice pauses there) and a group
    that would show for less than `min_group_s` rides with its neighbour."""
    groups = caption_words(text)
    if not groups:
        return []
    weights = []
    for g in groups:
        pause = SENTENCE_PAUSE_S if _SENT_WORD.search(g) else COMMA_PAUSE_S if _COMMA_WORD.search(g) else 0.0
        weights.append(len(g) + pause * 15)  # a pause counts as about fifteen characters of speech
    total = sum(weights) or 1.0
    out, at = [], 0.0
    for g, w in zip(groups, weights):
        out.append([at, g, audio_s * w / total])
        at += audio_s * w / total
    merged = []
    for off, g, dur in out:
        if merged and merged[-1][2] < min_group_s:
            merged[-1][1] += " " + g
            merged[-1][2] += dur
        else:
            merged.append([off, g, dur])
    if len(merged) > 1 and merged[-1][2] < min_group_s:
        last = merged.pop()
        merged[-1][1] += " " + last[1]
    return [(o, g) for o, g, _ in merged]


def wav_seconds(wav):
    try:
        with wave.open(io.BytesIO(wav), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError, ZeroDivisionError):
        return 0.0


# -- the pipeline --------------------------------------------------------------------------------------------------

class Speaker:
    """Speaks chunks as they are fed. `lis` is the facade (tts, playback, rep, src, _audio, _unduck, _guarded, _mark,
    _heard), `t` the turn. feed() is thread-safe and never blocks on speech; end() says nothing more is coming;
    wait() blocks until it has all been played or the turn was cancelled, and raises SpeechOffline when nothing at all
    could be spoken.

    Threads: synthesis (one chunk ahead of playback, so chunk N+1 is made while N plays), the duck (started with the
    first synthesis so it is done by the time the first audio is, not a serial step after it), and playback."""

    def __init__(self, lis, t, caption_sync=True, overlap_s=0.0, full_text=None):
        self.lis, self.t, self.caption_sync, self.overlap_s = lis, t, caption_sync, overlap_s
        self.full_text = full_text  # with caption_sync off: the caption is this, posted once when the voice starts
        self.inq, self.outq = queue.Queue(), queue.Queue()
        self.err = None
        self.started = False        # the first audio has begun
        self.fed = 0
        self.first_fed_at = None
        self.first_audio_at = None
        self.spoken = []            # the chunk texts that were played
        self._duck = None
        self._synth = None
        self._play = None
        self._lock = threading.Lock()

    # -- input
    def feed(self, text):
        text = clean_for_speech(text)
        if not text:
            return
        with self._lock:
            if self.first_fed_at is None:
                self.first_fed_at = time.monotonic()
                self._begin()
            self.fed += 1
        self.inq.put(text)

    def end(self):
        self.inq.put(None)

    def _begin(self):
        with self.t.lock:
            self.t.ducked = True
        self._duck = threading.Thread(target=self.lis._audio, args=("duck",), daemon=True, name="mira-duck")
        self._duck.start()
        self._synth = threading.Thread(target=self._synth_main, daemon=True, name="mira-synth")
        self._play = threading.Thread(target=self._play_main, daemon=True, name="mira-play")
        self._synth.start()
        self._play.start()

    # -- synthesis
    def _synth_main(self):
        i = 0
        try:
            while True:
                try:
                    ch = self.inq.get(timeout=0.1)
                except queue.Empty:
                    if self.t.cancel.is_set():
                        break
                    continue
                if ch is None or self.t.cancel.is_set():
                    break
                self.outq.put((ch, self.lis.tts.tts(ch, pin=i > 0)))
                i += 1
        except SpeechOffline as e:
            self.err = e
        except Exception as e:  # a failed synthesis ends the speech, never the listener
            log(f"synthesis failed: {type(e).__name__}")
            self.err = SpeechOffline(str(e))
        self.outq.put(None)

    # -- playback
    def _play_main(self):
        lis, t = self.lis, self.t
        threads = []
        try:
            while True:
                item = lis._next_wav(self.outq, t)
                if item is None or not t.live:
                    break
                ch, wav = item
                if not self.started:
                    if self._duck is not None:
                        self._duck.join(timeout=5)
                    if not t.live:
                        break
                    self.started = True
                    self.first_audio_at = time.monotonic()
                    t.speaking_since = lis.src.clock()
                    t.phase = "speaking"
                    lis.rep.update("speaking", lis._heard, "" if self.caption_sync else (self.full_text or ch), fast=True)
                    if self.first_fed_at is not None:
                        # A journal line, not a loop mark: marks are the turn's event trail and carry their own contract.
                        log(f"first_audio {int((self.first_audio_at - self.first_fed_at) * 1000)} ms after the text")
                elif not self.caption_sync and not self.full_text:
                    lis.rep.update("speaking", lis._heard, ch, fast=True)  # a streamed answer has no whole text yet
                if lis._guarded(ch):
                    t.guarded = True
                self.spoken.append(ch)
                th = threading.Thread(target=self._play_one, args=(ch, wav), daemon=True, name="mira-say")
                started = time.monotonic()
                th.start()
                threads.append(th)
                # Launch the next chunk's player a little before this one ends: a mira-audio say needs about 0.3 s to
                # reach the speaker, which otherwise is a pause between every two chunks.
                hold = max(0.0, wav_seconds(wav) - self.overlap_s)
                while th.is_alive() and time.monotonic() - started < hold and t.live:
                    th.join(0.02)
                if self.overlap_s <= 0:
                    th.join()
        finally:
            for th in threads:
                th.join(timeout=60)
            lis._unduck(t)

    def _play_one(self, ch, wav):
        import os
        import tempfile
        lis = self.lis
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav)
        try:
            lis._say_file(f.name, wav, ch if self.caption_sync else None, self.t)
        finally:
            try:
                os.unlink(f.name)
            except OSError:
                pass

    def wait(self):
        """Block until everything fed has been played (or the turn was cancelled)."""
        if self._play is None:
            return
        self._play.join()
        if self._synth is not None:
            self._synth.join(timeout=1)
        if not self.started and not self.t.cancel.is_set():
            raise self.err or SpeechOffline("no audio")
        if self.err is not None:
            log(f"speech ended early: {self.err}")
