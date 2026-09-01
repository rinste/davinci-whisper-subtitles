#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
whisper_srt.py - audio/video to .srt, with short lines and sensible line breaks.

Built for vertical video (TikTok / Reels / Shorts), where lines must be very short
(18 characters is a good value). It uses Whisper's per-word timestamps and decides
where to break based on:

  - punctuation: sentences are not split in half when it can be avoided
  - real pauses in the audio
  - grammar: a line never ends on a word that governs the next one - articles,
    prepositions, conjunctions, auxiliaries, negations ("the", "of", "not", "have")
  - balance: the two lines of a subtitle end up roughly the same length

Examples:
    python3 whisper_srt.py audio.m4a -c 18
    python3 whisper_srt.py video.mp4 -c 20 --max-lines 1 --model large-v3 -l it
    python3 whisper_srt.py audio.wav --words-json words.json   # save the words
    python3 whisper_srt.py --from-json words.json -c 24        # re-split, no re-transcribe
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Optional

# --------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Cue:
    words: list
    lines: list
    start: float = 0.0
    end: float = 0.0

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class SplitCfg:
    """Everything that governs how the text is split."""

    max_chars: int = 18  # maximum characters per line
    max_lines: int = 2  # maximum lines per subtitle
    max_duration: float = 6.0  # longest a subtitle may stay on screen (s)
    min_duration: float = 0.9  # shortest, when there is room for it (s)
    max_gap: float = 0.6  # a pause this long always closes a subtitle (s)
    min_gap: float = 0.04  # minimum gap between two subtitles (s)
    pad_start: float = 0.0  # lead-in (s)
    pad_end: float = 0.10  # lead-out (s)
    fill_gaps: bool = False  # hold each subtitle until the next one, no holes
    max_fill: float = 2.0  # ...but do not fill pauses longer than this (s)
    stretch_short: bool = False  # lengthen flashes by borrowing from the next one
    max_shift: float = 0.35  # ...but never delay the next one by more than this (s)
    sentence_split: bool = True  # close after . ! ?
    sentence_min_fill: float = 0.45  # ...but only if the subtitle is at least this full
    sentence_min_gap: float = 0.25  # ...or if the pause after it is at least this long
    cue_min_fill: float = 0.30  # minimum fill when backing up to a better break point
    max_time: float = 0.0  # nothing may run past this many seconds; 0 = no limit
    lang: str = "it"
    joiner: str = " "  # "" for Chinese/Japanese

    @property
    def capacity(self) -> int:
        return self.max_chars * self.max_lines


@dataclass
class Weights:
    """Bonuses and penalties, in "characters squared", for choosing a break point."""

    strong: float  # bonus: the break falls after . ! ?
    clause: float  # bonus: the break falls after , ; : ...
    sticky: float  # penalty: line would end on an article/preposition/auxiliary
    before_conj: float  # bonus: the new line starts with a conjunction
    orphan: float  # penalty: a line holding one tiny word on its own


LINE_W = Weights(strong=70, clause=35, sticky=150, before_conj=20, orphan=45)
CUE_W = Weights(strong=90, clause=45, sticky=180, before_conj=25, orphan=60)
CUE_WASTE = 260.0  # weight of the characters "wasted" when backing up, relative to capacity
OVERFLOW_W = 6.0  # weight of characters past the limit (words longer than the limit)

# --------------------------------------------------------------------------------------
# Language knowledge
# --------------------------------------------------------------------------------------

# Words a line should never END on: they govern the word that follows.
NO_BREAK_AFTER = {
    "it": """il lo la i gli le un uno una di a da in con su per tra fra del dello della dei degli
        delle al allo alla ai agli alle dal dallo dalla dai dagli dalle nel nello nella nei negli
        nelle sul sullo sulla sui sugli sulle col coi e ed o od ma se che perché mentre quando come
        né sia però anche non mi ti si ci vi ne è ho hai ha abbiamo avete hanno sono sei siamo siete
        ero eri era eravamo eravate erano avevo avevi aveva avevamo avevate avevano sarà sarò sarai
        saremo sarete saranno più meno molto tanto troppo ogni questo questa questi queste quel
        quello quella quei quegli quelle mio mia miei mie tuo tua tuoi tue suo sua suoi sue nostro
        nostra nostri nostre vostro vostra vostri vostre loro cui quale quali chi dove qualche
        nessun nessuno alcuni certi tutto tutta tutti tutte molti molte poco pochi due tre
        io tu lui lei noi voi""",
    "en": """a an the of to in on at for with from by and or but if that which who whose is are was
        were be been being am my your his her its our their this these those as into than then so
        not no do does did has have had will would can could should shall may might must very more
        most just about over under after before while during between each every both some any
        i we they he she""",
    "es": """el la los las un una unos unas de a en con por para y e o u que si no me te se nos os lo
        le les del al su sus mi tu es son era eran ha han he has muy más pero como cuando donde este
        esta estos estas ese esa aquel todo toda todos todas dos tres""",
    "fr": """le la les un une des de du au aux à en dans sur pour par avec et ou mais que qui ne pas
        se me te nous vous son sa ses mon ma mes ton ta tes leur leurs est sont était étaient ai as
        ont avons avez très plus moins comme quand où ce cette ces tout toute tous toutes deux""",
    "de": """der die das den dem des ein eine einen einem einer eines und oder aber wenn dass weil
        ich du er sie es wir ihr mir mich dir dich sich ist sind war waren habe hast hat haben wird
        werden für mit von zu aus bei nach über unter auf in an im am zum zur sehr mehr noch nur
        auch nicht kein keine diese dieser dieses alle alles zwei drei""",
    "pt": """o a os as um uma uns umas de do da dos das em no na nos nas por para com e ou mas que
        se não me te lhe seu sua meu minha é são era eram tem têm muito mais como quando onde este
        esta esse essa aquele todo toda todos todas dois três""",
}

# Words it feels natural to START a new line with (conjunctions, connectives).
BREAK_BEFORE = {
    "it": """e ed ma o oppure perché che quando mentre se però quindi dunque anche con per in su tra
         fra da di a come dove poi allora invece cioè""",
    "en": """and but or so because that when while if with for in on to of then however although
         since after before""",
    "es": "y e pero o porque que cuando mientras si con para en de como aunque entonces",
    "fr": "et mais ou parce que quand pendant si avec pour dans de comme donc alors",
    "de": "und aber oder weil dass wenn während mit für in von wie also dann",
    "pt": "e mas ou porque que quando enquanto se com para em de como então",
}

_STRIP = "\"'“”„«»‘’()[]{}<>.,;:!?…–—-"
_QUOTES = "\"'”’»)]}"


def _wordset(mapping: dict, lang: str) -> frozenset:
    keys = [lang] if lang in mapping else ["it", "en"]
    out = set()
    for k in keys:
        out.update(mapping[k].split())
    return frozenset(out)


def norm(tok: str) -> str:
    return tok.strip(_STRIP).lower()


def ends_sentence(tok: str) -> bool:
    """A real sentence end: . ! ?  (an ellipsis counts as a weak pause instead)."""
    t = tok.rstrip(_QUOTES)
    if t.endswith("...") or t.endswith("…"):
        return False
    return bool(t) and t[-1] in ".!?"


def ends_clause(tok: str) -> bool:
    """A weak pause: comma, semicolon, colon, ellipsis."""
    t = tok.rstrip(_QUOTES)
    if not t:
        return False
    return t[-1] in ",;:" or t.endswith("...") or t.endswith("…")


def is_sticky(tok: str, sticky: frozenset) -> bool:
    """True if the word must not end a line (article, preposition, elision...)."""
    t = tok.rstrip(_QUOTES)
    if t.endswith("'") or t.endswith("’"):  # elisions: l', un', dell', quell'...
        return True
    return norm(tok) in sticky


def break_penalty(prev: str, nxt: Optional[str], w: Weights, sticky: frozenset,
                  before: frozenset) -> float:
    """How bad it is to break between `prev` and `nxt`. Lower is better."""
    p = 0.0
    if ends_sentence(prev):
        p -= w.strong
    elif ends_clause(prev):
        p -= w.clause
    elif is_sticky(prev, sticky):
        p += w.sticky
    if nxt is not None and norm(nxt) in before:
        p -= w.before_conj
    return p


# --------------------------------------------------------------------------------------
# Laying out one subtitle (balanced lines, good break points)
# --------------------------------------------------------------------------------------


def wrap_cue(words: list, cfg: SplitCfg, sticky: frozenset, before: frozenset,
             allow_overflow: bool = False) -> Optional[list]:
    """Split the words into <= max_lines lines of <= max_chars characters.

    Returns the list of lines (each a list of words), or None if they do not fit.
    Picks the lowest-cost layout: balanced lines plus breaks at sensible points.
    """
    toks = [w.text for w in words]
    n = len(toks)
    if n == 0:
        return []

    sep = len(cfg.joiner)
    prefix = [0] * (n + 1)
    for i, t in enumerate(toks):
        prefix[i + 1] = prefix[i] + len(t)

    def line_len(i: int, j: int) -> int:
        return prefix[j] - prefix[i] + (j - i - 1) * sep

    INF = float("inf")

    @lru_cache(maxsize=None)
    def solve(i: int, lines_left: int):
        if i == n:
            return 0.0, ()
        if lines_left == 0:
            return INF, ()
        best = (INF, ())
        for j in range(i + 1, n + 1):
            L = line_len(i, j)
            over = L > cfg.max_chars
            if over:
                if j == i + 1 and allow_overflow:
                    cost = (L - cfg.max_chars) ** 2 * OVERFLOW_W
                else:
                    break
            else:
                cost = (cfg.max_chars - L) ** 2
            if j - i == 1 and len(toks[i]) <= 3 and n > 1:
                cost += LINE_W.orphan  # a line holding one tiny word
            if j < n:
                cost += break_penalty(toks[j - 1], toks[j], LINE_W, sticky, before)
            sub_cost, sub_breaks = solve(j, lines_left - 1)
            if sub_cost == INF:
                if over:
                    break
                continue
            total = cost + sub_cost
            if total < best[0]:
                best = (total, (j,) + sub_breaks)
            if over:
                break
        return best

    cost, breaks = solve(0, cfg.max_lines)
    solve.cache_clear()
    if cost == INF:
        return None

    lines, prev = [], 0
    for b in breaks:
        lines.append(words[prev:b])
        prev = b
    if prev < n:
        lines.append(words[prev:n])
    return lines


# --------------------------------------------------------------------------------------
# Grouping words into subtitles
# --------------------------------------------------------------------------------------


def _chars(words: list, cfg: SplitCfg) -> int:
    return sum(len(w.text) for w in words) + max(0, len(words) - 1) * len(cfg.joiner)


def choose_cue_break(cur: list, nxt: Optional[Word], cfg: SplitCfg, sticky: frozenset,
                     before: frozenset) -> int:
    """Once a subtitle is full, choose where to actually close it.

    It can back up a few words to land on a comma, or to avoid leaving "the"/"of"/
    "not" dangling at the end. Returns how many words to keep.
    """
    total = _chars(cur, cfg)
    floor = cfg.cue_min_fill * cfg.capacity
    k_min = len(cur)
    for k in range(1, len(cur) + 1):
        if _chars(cur[:k], cfg) >= floor:
            k_min = k
            break

    best_k, best_score = len(cur), None
    for k in range(k_min, len(cur) + 1):
        following = cur[k].text if k < len(cur) else (nxt.text if nxt else None)
        score = break_penalty(cur[k - 1].text, following, CUE_W, sticky, before)
        waste = (total - _chars(cur[:k], cfg)) / max(1, cfg.capacity)
        score += CUE_WASTE * waste ** 2
        if best_score is None or score < best_score:
            best_k, best_score = k, score
    return best_k


def build_cues(words: list, cfg: SplitCfg) -> list:
    sticky = _wordset(NO_BREAK_AFTER, cfg.lang)
    before = _wordset(BREAK_BEFORE, cfg.lang)

    def fits(ws: list) -> bool:
        return wrap_cue(ws, cfg, sticky, before) is not None

    cues: list = []
    cur: list = []

    for w in words:
        if not cur:
            cur = [w]
            continue

        gap = max(0.0, w.start - cur[-1].end)
        forced = gap > cfg.max_gap or (w.end - cur[0].start) > cfg.max_duration
        if cfg.sentence_split and ends_sentence(cur[-1].text):
            fill = _chars(cur, cfg) / cfg.capacity
            if fill >= cfg.sentence_min_fill or gap >= cfg.sentence_min_gap:
                forced = True

        if forced:
            cues.append(cur)
            cur = [w]
            continue

        if fits(cur + [w]):
            cur.append(w)
            continue

        # It no longer fits: look for the best point to close on.
        k = choose_cue_break(cur, w, cfg, sticky, before)
        cues.append(cur[:k])
        rest = cur[k:]
        if rest and fits(rest + [w]):
            cur = rest + [w]
        else:
            if rest:
                cues.append(rest)
            cur = [w]

    if cur:
        cues.append(cur)

    out = []
    for ws in cues:
        lines = wrap_cue(ws, cfg, sticky, before) or wrap_cue(
            ws, cfg, sticky, before, allow_overflow=True
        )
        if lines is None:  # should not happen, but never drop text
            lines = [ws]
        out.append(Cue(words=ws, lines=[cfg.joiner.join(x.text for x in ln) for ln in lines]))
    return out


# --------------------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------------------


def _stretch_short(cues: list, cfg: SplitCfg) -> None:
    """Lengthen subtitles that are too short to read - the "flashes".

    It moves the boundary with the next subtitle, but never makes that one shorter
    than the one being lengthened, and never delays it by more than max_shift. So
    sync does not drift, and the problem is not just pushed one step further along.
    """
    for i in range(len(cues) - 1):
        cur, nxt = cues[i], cues[i + 1]
        dur_cur = cur.end - cur.start
        dur_nxt = nxt.end - nxt.start
        if dur_cur >= cfg.min_duration or dur_nxt <= dur_cur:
            continue
        give = min(cfg.max_shift, cfg.min_duration - dur_cur, (dur_nxt - dur_cur) / 2)
        if give > 0.02:
            cur.end += give
            nxt.start += give

    # The last subtitle has nothing after it to borrow from. Normally it just extends
    # forward, but under a ceiling (a marked in/out range) it cannot, and a word right
    # on the boundary ends up flashing. So let it borrow backwards instead, under the
    # same rule: never leave the donor shorter than the one being helped.
    if len(cues) >= 2:
        prev, last = cues[-2], cues[-1]
        dur_last = last.end - last.start
        dur_prev = prev.end - prev.start
        if dur_last < cfg.min_duration and dur_prev > dur_last:
            give = min(cfg.max_shift, cfg.min_duration - dur_last, (dur_prev - dur_last) / 2)
            if give > 0.02:
                last.start -= give
                prev.end -= give


def _fill_gaps(cues: list, cfg: SplitCfg) -> None:
    """No holes: each subtitle stays on screen until the next one arrives.

    Real pauses (longer than --max-fill: music breaks, silence) are left alone -
    there the screen clears, which is what you want.
    """
    for i in range(len(cues) - 1):
        cur, nxt = cues[i], cues[i + 1]
        if cur.end < nxt.start <= cur.end + cfg.max_fill:
            cur.end = nxt.start


def finalize_timings(cues: list, cfg: SplitCfg) -> None:
    raw_starts = [c.words[0].start for c in cues]
    for c in cues:
        c.start = c.words[0].start - cfg.pad_start
        c.end = c.words[-1].end + cfg.pad_end

    for i, c in enumerate(cues):
        lower = 0.0 if i == 0 else cues[i - 1].end + cfg.min_gap
        c.start = max(c.start, lower, 0.0)

        if i + 1 < len(cues):
            upper = raw_starts[i + 1] - cfg.min_gap
        else:
            # The last subtitle has nothing after it, so the minimum-duration rule
            # below would stretch it past the end of the audio. With a marked range
            # that means spilling outside it, possibly onto existing subtitles.
            upper = cfg.max_time or None
        if upper is not None:
            c.end = min(c.end, upper)

        if c.end - c.start < cfg.min_duration:
            target = c.start + cfg.min_duration
            c.end = target if upper is None else min(target, upper)
        if c.end - c.start < cfg.min_duration:  # try starting earlier instead
            c.start = max(lower, c.end - cfg.min_duration, 0.0)
        if c.end <= c.start:
            c.end = c.start + 0.2

    if cfg.stretch_short:
        _stretch_short(cues, cfg)
    if cfg.fill_gaps:
        _fill_gaps(cues, cfg)


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[,.;:!?…]+(?=\s|$)")


def render_text(cue: Cue, uppercase: bool, no_punct: bool) -> str:
    lines = cue.lines
    if no_punct:
        lines = [re.sub(r"\s{2,}", " ", _PUNCT_RE.sub("", ln)).strip() for ln in lines]
    if uppercase:
        lines = [ln.upper() for ln in lines]
    return "\n".join(ln for ln in lines if ln)


def fmt_ts(t: float, ms_sep: str = ",") -> str:
    ms = max(0, int(round(t * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{ms_sep}{ms:03d}"


def to_srt(cues: list, uppercase=False, no_punct=False) -> str:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{fmt_ts(c.start)} --> {fmt_ts(c.end)}\n{render_text(c, uppercase, no_punct)}\n")
    return "\n".join(out)


def to_vtt(cues: list, uppercase=False, no_punct=False) -> str:
    out = ["WEBVTT\n"]
    for c in cues:
        out.append(
            f"{fmt_ts(c.start, '.')} --> {fmt_ts(c.end, '.')}\n{render_text(c, uppercase, no_punct)}\n"
        )
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# Transcription
# --------------------------------------------------------------------------------------


def _split_segment(text: str, start: float, end: float) -> list:
    """No per-word timestamps available, so estimate them.

    Better than plain character-proportional timing: each word lasts in proportion
    to its length (plus a fixed base, or short words would last almost nothing),
    and a small pause is set aside after punctuation.
    """
    toks = text.split()
    if not toks:
        return []
    span = max(0.05, end - start)

    pauses = []
    for i, t in enumerate(toks):
        if i == len(toks) - 1:
            pauses.append(0.0)
        elif ends_sentence(t):
            pauses.append(0.28)
        elif ends_clause(t):
            pauses.append(0.13)
        else:
            pauses.append(0.0)
    tot = sum(pauses)
    if tot > span * 0.30:  # do not overdo it on short segments
        pauses = [p * span * 0.30 / tot for p in pauses]
        tot = span * 0.30

    weights = [len(t) + 1.5 for t in toks]
    total_w = sum(weights)
    speech = span - tot

    words, t = [], start
    for tok, wgt, pause in zip(toks, weights, pauses):
        d = speech * wgt / total_w
        words.append(Word(tok, t, t + d))
        t += d + pause
    words[-1].end = min(words[-1].end, end)
    return words


def guess_language(words: list) -> str:
    """Guess the language by counting known function words - the split rules need it."""
    toks = [t for t in (norm(w.text) for w in words[:500]) if t]
    best, best_hits = "it", -1
    for lang, blob in NO_BREAK_AFTER.items():
        vocab = set(blob.split())
        hits = sum(1 for t in toks if t in vocab)
        if hits > best_hits:
            best, best_hits = lang, hits
    return best


def transcribe_wavespeed(path: str, args) -> tuple:
    """Transcribe through the wavespeed CLI (Whisper large-v3 in the cloud).

    WARNING: the endpoint only returns per-segment timestamps, not per-word ones,
    so the individual word timings are estimated (see _split_segment).
    """
    import shutil
    import subprocess
    import tempfile

    if shutil.which("wavespeed") is None:
        sys.exit("The 'wavespeed' CLI was not found. Install it, or use --backend local.")

    audio_url = path
    if not re.match(r"^https?://", path):
        print(f"[wavespeed] uploading {os.path.basename(path)} to the WaveSpeed CDN...", file=sys.stderr)
        up = subprocess.run(["wavespeed", "upload", path, "--json"],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        if up.returncode != 0:
            sys.exit(f"upload failed:\n{up.stderr.strip()}")
        audio_url = json.loads(up.stdout)["url"]

    payload = {
        "audio": audio_url,
        "language": args.language or "auto",
        "task": "translate" if args.translate else "transcribe",
        "enable_timestamps": True,
    }
    if args.initial_prompt:
        payload["prompt"] = args.initial_prompt

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(payload, fh)
        tmp = fh.name
    try:
        print("[wavespeed] transcribing with wavespeed-ai/openai-whisper...", file=sys.stderr)
        run = subprocess.run(
            ["wavespeed", "run", "wavespeed-ai/openai-whisper", "--input-file", tmp, "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    finally:
        os.unlink(tmp)
    if run.returncode != 0:
        sys.exit(f"wavespeed run failed:\n{run.stderr.strip()}")

    out = json.loads(run.stdout).get("raw", {}).get("outputs", [{}])[0]
    details = out.get("text_details")
    if not details:
        text = (out.get("text") or "").strip()
        sys.exit("WaveSpeed returned no timestamps."
                 + (f" Text received:\n{text[:200]}" if text else ""))

    words = []
    for seg in details:
        words.extend(_split_segment(seg["text"], float(seg["start"]), float(seg["end"])))
    print(f"[wavespeed] {len(details)} segments -> {len(words)} words "
          "(per-word timings ESTIMATED: the endpoint does not provide them)", file=sys.stderr)
    lang = args.language if args.language not in (None, "auto") else guess_language(words)
    return words, lang


def transcribe(path: str, args) -> tuple:
    lang = None if args.language in (None, "auto") else args.language
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return _transcribe_openai(path, args, lang)

    compute = args.compute_type or ("float16" if args.device == "cuda" else "int8")
    print(f"[whisper] model '{args.model}' (device={args.device}, compute={compute})",
          file=sys.stderr)
    model = WhisperModel(args.model, device=args.device, compute_type=compute)
    segments, info = model.transcribe(
        path,
        language=lang,
        task="translate" if args.translate else "transcribe",
        word_timestamps=True,
        vad_filter=not args.no_vad,
        beam_size=args.beam_size,
        initial_prompt=args.initial_prompt,
        condition_on_previous_text=False,
    )
    dur = getattr(info, "duration", 0) or 0
    print(f"[whisper] language: {info.language} ({info.language_probability:.0%}) - "
          f"duration: {dur:.0f}s", file=sys.stderr)

    words = []
    for seg in segments:
        if seg.words:
            for w in seg.words:
                t = w.word.strip()
                if t:
                    words.append(Word(t, float(w.start), float(w.end)))
        else:
            words.extend(_split_segment(seg.text, seg.start, seg.end))
        if dur:
            print(f"\r[whisper] transcribing... {min(100, seg.end / dur * 100):5.1f}%",
                  end="", file=sys.stderr)
    print("\r[whisper] transcription complete.       ", file=sys.stderr)
    return words, info.language


def _transcribe_openai(path: str, args, lang):
    try:
        import whisper  # openai-whisper
    except ImportError:
        sys.exit(
            "Whisper is not installed. Recommended (fast on CPU):\n"
            "    pip3 install faster-whisper\n"
            "or:\n"
            "    pip3 install openai-whisper"
        )
    print(f"[whisper] openai-whisper, model '{args.model}'", file=sys.stderr)
    model = whisper.load_model(args.model)
    res = model.transcribe(
        path,
        language=lang,
        task="translate" if args.translate else "transcribe",
        word_timestamps=True,
        initial_prompt=args.initial_prompt,
        condition_on_previous_text=False,
        verbose=False,
    )
    words = []
    for seg in res.get("segments", []):
        if seg.get("words"):
            for w in seg["words"]:
                t = str(w.get("word", "")).strip()
                if t:
                    words.append(Word(t, float(w["start"]), float(w["end"])))
        else:
            words.extend(_split_segment(seg["text"], seg["start"], seg["end"]))
    return words, res.get("language", "it")


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Turn audio/video into an .srt with Whisper: short lines, sensible breaks.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("audio", nargs="?", help="audio or video file (any format ffmpeg can read)")
    p.add_argument("-o", "--output", help="output .srt file")
    p.add_argument("-c", "--max-chars", type=int, default=18,
                   help="maximum characters per line (18 works well for vertical video)")
    p.add_argument("--max-lines", type=int, default=2, help="maximum lines per subtitle")
    p.add_argument("--format", choices=["srt", "vtt", "both"], default="srt")

    g = p.add_argument_group("whisper")
    g.add_argument("--backend", choices=["local", "wavespeed"], default="local",
                   help="local = Whisper on this machine; wavespeed = cloud (large-v3, estimated timings)")
    g.add_argument("-m", "--model", default="medium",
                   help="tiny|base|small|medium|large-v3|turbo (or a local path)")
    g.add_argument("-l", "--language", default="auto", help="language code (it, en, ...) or 'auto'")
    g.add_argument("--translate", action="store_true", help="translate to English instead of transcribing")
    g.add_argument("--device", default="auto", help="auto|cpu|cuda")
    g.add_argument("--compute-type", default=None, help="int8|int8_float16|float16|float32")
    g.add_argument("--beam-size", type=int, default=5)
    g.add_argument("--initial-prompt", default=None,
                   help="glossary/context: proper nouns, technical terms, style")
    g.add_argument("--no-vad", action="store_true", help="disable the voice activity filter (VAD)")

    t = p.add_argument_group("timing")
    t.add_argument("--max-duration", type=float, default=6.0, help="longest a subtitle may stay on screen (s)")
    t.add_argument("--min-duration", type=float, default=0.9, help="shortest a subtitle should stay on screen (s)")
    t.add_argument("--max-gap", type=float, default=0.6,
                   help="a pause longer than this always closes the subtitle (s)")
    t.add_argument("--max-time", type=float, default=0.0,
                   help="clamp every timestamp to this many seconds; 0 = no limit")
    t.add_argument("--offset", type=float, default=0.0,
                   help="add this many seconds to every timestamp, for audio that\n"
                        "starts partway into a longer timeline")
    t.add_argument("--min-gap", type=float, default=0.04,
                   help="minimum gap between two subtitles (s); 0 = back to back")
    t.add_argument("--pad-start", type=float, default=0.0, help="lead-in (s)")
    t.add_argument("--pad-end", type=float, default=0.10, help="lead-out (s)")
    t.add_argument("--fill-gaps", action="store_true",
                   help="no holes: each subtitle lasts until the next one starts")
    t.add_argument("--max-fill", type=float, default=2.0,
                   help="with --fill-gaps, a pause longer than this is left as a hole (s)")
    t.add_argument("--stretch-short", action="store_true",
                   help="lengthen subtitles shorter than --min-duration by borrowing\nfrom the next one (max 0.35 s), when it has room to give")
    t.add_argument("--no-sentence-split", action="store_true",
                   help="do not force a break after . ! ?")

    s = p.add_argument_group("stile")
    s.add_argument("--uppercase", action="store_true", help="ALL UPPERCASE")
    s.add_argument("--no-punct", action="store_true", help="strip punctuation from the output")

    c = p.add_argument_group("cache")
    c.add_argument("--words-json", help="save the per-word timestamps to this JSON file")
    c.add_argument("--from-json", help="reuse a saved JSON instead of transcribing again")

    args = p.parse_args(argv)
    if not args.audio and not args.from_json:
        p.error("an audio file is required (or --from-json)")
    if args.max_chars < 4:
        p.error("--max-chars is too small")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.from_json:
        with open(args.from_json, encoding="utf-8") as fh:
            data = json.load(fh)
        words = [Word(w["w"], float(w["s"]), float(w["e"])) for w in data["words"]]
        lang = data.get("language", "it")
        source = args.from_json
    else:
        if not re.match(r"^https?://", args.audio) and not os.path.exists(args.audio):
            sys.exit(f"File not found: {args.audio}")
        if args.backend == "wavespeed":
            words, lang = transcribe_wavespeed(args.audio, args)
        else:
            words, lang = transcribe(args.audio, args)
        source = args.audio

    if not words:
        sys.exit("No speech recognised in the file.")

    if args.words_json:
        with open(args.words_json, "w", encoding="utf-8") as fh:
            json.dump(
                {"language": lang,
                 "words": [{"w": w.text, "s": round(w.start, 3), "e": round(w.end, 3)}
                           for w in words]},
                fh, ensure_ascii=False, indent=1,
            )
        print(f"[out] words -> {args.words_json}", file=sys.stderr)

    cfg = SplitCfg(
        max_chars=args.max_chars,
        max_lines=args.max_lines,
        max_duration=args.max_duration,
        min_duration=args.min_duration,
        max_gap=args.max_gap,
        min_gap=args.min_gap,
        pad_start=args.pad_start,
        pad_end=args.pad_end,
        sentence_split=not args.no_sentence_split,
        # --max-time is given in output coordinates; the cues are built before the
        # offset is applied, so bring the ceiling back into audio coordinates.
        max_time=max(0.0, args.max_time - args.offset) if args.max_time else 0.0,
        stretch_short=args.stretch_short,
        fill_gaps=args.fill_gaps,
        max_fill=args.max_fill,
        lang=(lang if lang and lang != "auto" else guess_language(words)).lower()[:2],
    )
    if cfg.lang in {"zh", "ja", "th", "yu"}:
        cfg = replace(cfg, joiner="")

    cues = build_cues(words, cfg)
    finalize_timings(cues, cfg)

    if args.offset:
        # The audio handed to us starts somewhere other than zero on the caller's
        # timeline (a marked in/out range, for instance), so shift every timestamp.
        for c in cues:
            c.start += args.offset
            c.end += args.offset

    if args.max_time > 0:      # belt and braces after the shift
        for c in cues:
            c.end = min(c.end, args.max_time)

    base = args.output
    if not base:
        if re.match(r"^https?://", source):
            base = os.path.basename(source.split("?")[0]) or "output"
            base = os.path.splitext(base)[0] + ".srt"
        else:
            base = os.path.splitext(source)[0] + ".srt"
    stem = os.path.splitext(base)[0]

    targets = []
    if args.format in ("srt", "both"):
        targets.append((stem + ".srt", to_srt(cues, args.uppercase, args.no_punct)))
    if args.format in ("vtt", "both"):
        targets.append((stem + ".vtt", to_vtt(cues, args.uppercase, args.no_punct)))
    for path, content in targets:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"[out] {path}", file=sys.stderr)

    # Quality summary
    longest = max((len(ln) for c in cues for ln in c.lines), default=0)
    over = sum(1 for c in cues for ln in c.lines if len(ln) > cfg.max_chars)
    cps = max(((len(c.text) - c.text.count("\n")) / max(0.001, c.end - c.start)) for c in cues)
    shortest = min(c.end - c.start for c in cues)
    print(
        f"[info] {len(cues)} subtitles - longest line {longest} chars "
        f"(limit {cfg.max_chars}, {over} over) - shortest {shortest:.2f}s - "
        f"peak {cps:.0f} chars/s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
