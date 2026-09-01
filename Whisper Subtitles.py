#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Whisper Subtitles - Whisper-powered subtitles inside DaVinci Resolve.

Workspace > Scripts > Utility > Whisper Subtitles

How it works:
  1. reads the clips on the chosen audio track (the voice-over one) and rebuilds
     them with ffmpeg into a WAV aligned to the timeline, so Whisper hears only
     the voice, without music, and the timings map 1:1 onto the timeline
  2. runs whisper_srt.py, which breaks the lines respecting the character limit
     and the grammar
  3. imports the SRT and drops it on the Subtitle 1 track

The SRT is only a transport format: it is written to a temporary folder, imported,
then deleted along with the media pool clip. The text stays in the timeline
(verified). If you need a file, export it from Resolve.

Everything is logged to ~/whisper_subtitles.log.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

# --------------------------------------------------------------------------------------
# Where whisper_srt.py is installed
# --------------------------------------------------------------------------------------

HOME = os.path.expanduser("~")
MARKER = os.path.join(HOME, ".whisper-subtitles-resolve")   # written by install.sh


def install_home():
    """Where the repo lives: whisper_srt.py plus its virtualenv.

    Resolve does not reliably define __file__ for scripts launched from its menu,
    so the marker file written by install.sh is the primary source of truth.
    """
    env = os.environ.get("WHISPER_SUBTITLES_HOME")
    if env and os.path.isdir(env):
        return env
    try:
        with open(MARKER, encoding="utf-8") as fh:
            path = fh.read().strip()
        if path and os.path.isdir(path):
            return path
    except OSError:
        pass
    try:
        # installed as a symlink: follow it back to the repo
        here = os.path.dirname(os.path.realpath(__file__))
        if os.path.exists(os.path.join(here, "whisper_srt.py")):
            return here
    except NameError:
        pass
    return ""   # nothing found: run_whisper raises with the install instructions


BASE = install_home()
SCRIPT = os.path.join(BASE, "whisper_srt.py")
# the virtualenv built by install.sh, or whatever python3 is on PATH
PYTHON = os.path.join(BASE, ".venv", "bin", "python")
if not os.path.exists(PYTHON):
    from shutil import which as _which
    PYTHON = _which("python3") or PYTHON

WINDOW_ID = "WhisperSubtitles"
LOGFILE = os.path.join(HOME, "whisper_subtitles.log")

LANGUAGES = [("Auto detect", "auto"), ("Italian", "it"), ("English", "en"),
             ("Spanish", "es"), ("French", "fr"), ("German", "de"), ("Portuguese", "pt")]
MODELS = [("large-v3 (best)", "large-v3"), ("turbo (fast)", "turbo"),
          ("medium", "medium"), ("small", "small"), ("base", "base"), ("tiny", "tiny")]


def log_file(msg):
    """File and console only.

    The raw output of whisper_srt.py lands here. Guarded against Resolve's ASCII
    locale, where printing an accented character raises.
    """
    text = str(msg)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass
    for candidate in (text, text.encode("ascii", "replace").decode("ascii")):
        try:
            print("[whisper-sub]", candidate)
            return
        except Exception:
            continue


def utf8_env():
    """Resolve launches scripts with an ASCII locale: without forcing UTF-8, reading
    a child process's output blows up on the first accented character."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("LC_ALL", "en_US.UTF-8")
    env.setdefault("LANG", "en_US.UTF-8")
    return env


def find_ffmpeg():
    for p in ("/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if os.path.exists(p):
            return p
    from shutil import which
    return which("ffmpeg")


# --------------------------------------------------------------------------------------
# Hooking into Resolve (works from the Scripts menu and from a terminal)
# --------------------------------------------------------------------------------------

def get_resolve():
    try:
        return resolve  # injected by Resolve when launched from the menu
    except NameError:
        pass
    api = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
    os.environ.setdefault("RESOLVE_SCRIPT_API", api)
    os.environ.setdefault(
        "RESOLVE_SCRIPT_LIB",
        "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so")
    sys.path.append(os.path.join(api, "Modules"))
    import DaVinciResolveScript as dvr
    return dvr.scriptapp("Resolve")


def get_ui():
    """UIManager and dispatcher, from the Scripts menu or from a terminal."""
    try:
        fu = fusion       # global injected by Resolve
    except NameError:
        fu = None
    try:
        disp_mod = bmd    # same
    except NameError:
        disp_mod = None
    if fu is None or disp_mod is None:
        # DaVinciResolveScript replaces itself with the fusionscript module, which
        # exposes both scriptapp() and UIDispatcher.
        import DaVinciResolveScript as fusionscript
        if fu is None:
            fu = fusionscript.scriptapp("Fusion")
        if disp_mod is None:
            disp_mod = fusionscript
    if fu is None:
        raise RuntimeError("Could not reach Fusion to build the interface.")
    return fu.UIManager, disp_mod.UIDispatcher(fu.UIManager)


# --------------------------------------------------------------------------------------
# Rebuilding the audio of one track
# --------------------------------------------------------------------------------------

def read_track(timeline, project, track_index):
    """Clips on the track: source, in-point, duration, position in the timeline."""
    fps = float(project.GetSetting("timelineFrameRate"))
    origin = timeline.GetStartFrame()
    segs, sources, skipped = [], [], 0
    for item in (timeline.GetItemListInTrack("audio", track_index) or []):
        mpi = item.GetMediaPoolItem()
        if not mpi:  # transitions and cross-fades carry no media
            continue
        path = mpi.GetClipProperty("File Path")
        if not path or not os.path.exists(path):
            skipped += 1
            continue
        if path not in sources:
            sources.append(path)
        segs.append({"src": sources.index(path),
                     "in": item.GetLeftOffset() / fps,
                     "dur": item.GetDuration() / fps,
                     "at": (item.GetStart() - origin) / fps})
    return segs, sources, skipped


def build_wav(segs, sources, out_path, log):
    """A 16 kHz mono WAV mirroring the timeline, so the timings map 1:1."""
    ff = find_ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg not found. Install it with: brew install ffmpeg")
    cmd = [ff, "-y", "-loglevel", "error"]
    for p in sources:
        cmd += ["-i", p]
    filt, labels = [], []
    for k, s in enumerate(segs):
        filt.append("[{}:a]atrim=start={:.6f}:end={:.6f},asetpts=PTS-STARTPTS,"
                    "adelay={}:all=1[s{}]".format(s["src"], s["in"], s["in"] + s["dur"],
                                                  int(round(s["at"] * 1000)), k))
        labels.append("[s{}]".format(k))
    filt.append("".join(labels) +
                "amix=inputs={}:normalize=0:dropout_transition=0[out]".format(len(segs)))
    cmd += ["-filter_complex", ";".join(filt), "-map", "[out]", "-ac", "1", "-ar", "16000", out_path]
    log("Rebuilding audio from {} clips ({} source file{})...".format(
        len(segs), len(sources), "" if len(sources) == 1 else "s"))
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=utf8_env())
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + r.stderr[-800:])
    return out_path


# --------------------------------------------------------------------------------------
# Whisper
# --------------------------------------------------------------------------------------

def run_whisper(wav, srt, opts, log):
    if not os.path.exists(PYTHON) or not os.path.exists(SCRIPT):
        raise RuntimeError(
            "Could not find whisper_srt.py and its virtualenv.\n"
            "Run install.sh from the repository, or point WHISPER_SUBTITLES_HOME at it.\n"
            "Looked in: {}".format(BASE or "(unknown)"))
    cmd = [PYTHON, SCRIPT, wav, "-o", srt,
           "-m", opts["model"], "-l", opts["language"],
           "-c", str(opts["chars"]), "--max-lines", str(opts["lines"]),
           "--min-gap", "{:.4f}".format(opts["gap_sec"])]
    if opts["fill_gaps"]:
        cmd.append("--fill-gaps")
    if opts["stretch"]:
        cmd.append("--stretch-short")
    if opts["upper"]:
        cmd.append("--uppercase")
    if opts["nopunct"]:
        cmd.append("--no-punct")
    log("Transcribing with {} - this may take a few minutes...".format(opts["model"]))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            env=utf8_env())
    previous = ""
    for line in proc.stdout:
        line = line.strip()
        if not line or line == previous:
            continue
        previous = line
        progress = re.search(r"transcribing\D*(\d+(?:[.,]\d+)?)\s*%", line)
        if progress:
            # on screen only: dozens of lines, they would flood the log
            log("Transcribing with {}... {:.0f}%".format(
                opts["model"], float(progress.group(1).replace(",", "."))), False)
        else:
            log_file(line)
    proc.wait()
    if proc.returncode != 0 or not os.path.exists(srt):
        raise RuntimeError("whisper_srt.py failed - see " + LOGFILE)
    return srt


# --------------------------------------------------------------------------------------
# Dropping the subtitles into the timeline
# --------------------------------------------------------------------------------------

def insert_srt(project, timeline, srt, log, replace):
    """Insert the subtitles, then clean up after ourselves.

    AppendToTimeline ALWAYS writes to Subtitle 1 and appends to whatever is already
    there, so without an explicit replace we stop with an error rather than stack
    copies on top of the user's work.
    """
    existing = timeline.GetItemListInTrack("subtitle", 1) or []
    if existing:
        if not replace:
            raise RuntimeError(
                "Subtitle 1 already has {} subtitles. Tick \"Replace existing "
                "subtitles\", or clear the track first.".format(len(existing)))
        log("Replacing {} existing subtitles...".format(len(existing)))
        timeline.DeleteClips(existing)

    mp = project.GetMediaPool()
    if timeline.GetTrackCount("subtitle") == 0:
        timeline.AddTrack("subtitle")
    items = mp.ImportMedia([srt])
    if not items:
        raise RuntimeError("Resolve refused to import the SRT.")
    # NOTE: passing trackIndex or recordFrame would append the subtitles at the very
    # end of the timeline. With mediaPoolItem alone they line up with its start.
    ok = mp.AppendToTimeline([{"mediaPoolItem": items[0]}])
    n = len(timeline.GetItemListInTrack("subtitle", 1) or [])
    try:
        mp.DeleteClips(items)   # the text lives in the timeline now, the clip is dead weight
    except Exception:
        pass
    if not ok or n == 0:
        raise RuntimeError("Could not insert the subtitles into the timeline.")
    log("Done - {} subtitles inserted on Subtitle 1.".format(n))


# --------------------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------------------

def list_audio_tracks(timeline):
    """Labels for the audio track picker, plus how many clips each track holds."""
    tracks = []
    for i in range(1, timeline.GetTrackCount("audio") + 1):
        n = len([x for x in (timeline.GetItemListInTrack("audio", i) or [])
                 if x.GetMediaPoolItem()])
        # the clip count stays out of the label, but is kept: it preselects the
        # first track that actually has audio on it
        tracks.append(("A{}  {}".format(i, timeline.GetTrackName("audio", i)), i, n))
    return tracks


def first_track_with_clips(rows):
    for k, (_, _, n) in enumerate(rows):
        if n:
            return k
    return 0


def main():
    # Resolve starts scripts with an ASCII locale: without this, printing an accented
    # character or an em dash raises UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    resolve_app = get_resolve()
    if resolve_app is None:
        print("Could not connect to Resolve.")
        return
    project = resolve_app.GetProjectManager().GetCurrentProject()
    current = project.GetCurrentTimeline() if project else None
    if not current:
        print("No timeline open.")
        return

    ui, disp = get_ui()

    # One window at a time. FindWindow only reports windows whose script is still
    # running, so this never trips over a leftover from an instance already closed.
    already_open = ui.FindWindow(WINDOW_ID)
    if already_open:
        try:
            already_open.Show()
            already_open.Raise()
        except Exception:
            pass
        print("Whisper Subtitles is already open.")
        return

    fps = float(project.GetSetting("timelineFrameRate"))

    timelines = []
    for i in range(1, (project.GetTimelineCount() or 0) + 1):
        tl = project.GetTimelineByIndex(i)
        if tl:
            timelines.append((tl.GetName(), i))
    current_name = current.GetName()
    default_timeline = next((k for k, (name, _) in enumerate(timelines)
                             if name == current_name), 0)

    tracks = list_audio_tracks(current)

    # Fixed, consistent widths: Fusion widgets do not shrink below the width of their
    # own content, and the layout adds spacing of its own. Without the slack, the last
    # widget on each row spills past the window edge and gets clipped.
    WIN_W, WIN_H = 780, 660
    MARGIN = 24
    LBL = 170                                    # label column
    ROW = 26                                     # row height
    SLACK = 70
    FIELD = WIN_W - 2 * MARGIN - LBL - SLACK

    def row(text, control):
        return ui.HGroup({"Weight": 0}, [
            ui.Label({"Text": text, "Weight": 0, "MinimumSize": [LBL, ROW],
                      "Alignment": {"AlignRight": True, "AlignVCenter": True}}),
            control])

    def combo(wid):
        return ui.ComboBox({"ID": wid, "Weight": 0,
                            "MinimumSize": [FIELD, ROW], "MaximumSize": [FIELD, ROW]})

    def number(wid, value, lo, hi, suffix):
        return ui.HGroup({"Weight": 0}, [
            ui.SpinBox({"ID": wid, "Value": value, "Minimum": lo, "Maximum": hi,
                        "Weight": 0, "MinimumSize": [90, ROW], "MaximumSize": [90, ROW]}),
            ui.Label({"Text": "  " + suffix, "Weight": 0,
                      "MinimumSize": [FIELD - 90, ROW]})])

    def check(wid, text, checked=False):
        return ui.HGroup({"Weight": 0}, [
            ui.Label({"Text": "", "Weight": 0, "MinimumSize": [LBL, ROW]}),
            ui.CheckBox({"ID": wid, "Text": text, "Checked": checked, "Weight": 0,
                         "MinimumSize": [FIELD, ROW]})])

    win = disp.AddWindow({"ID": WINDOW_ID, "WindowTitle": "Create Subtitles with Whisper",
                          "Geometry": [200, 100, WIN_W, WIN_H]}, [
        ui.VGroup({"Spacing": 6, "Margin": MARGIN}, [
            row("Timeline", combo("Timeline")),
            row("Audio Track", combo("Track")),
            row("Language", combo("Lang")),
            row("Model", combo("Model")),
            row("Maximum", number("Chars", 18, 4, 60, "characters per line")),
            row("Lines", combo("Lines")),
            row("Gap Between Subtitles", number("Gap", 0, 0, 25, "frames")),

            ui.VGap(10),
            check("Fill", "No gaps between subtitles", True),
            check("Stretch", "Extend subtitles that are too short", True),
            check("Upper", "UPPERCASE"),
            check("NoPunct", "No punctuation"),
            check("Replace", "Replace existing subtitles"),

            ui.VGap(10),
            ui.Label({"ID": "Status", "Text": "Ready.", "Weight": 0,
                      "MinimumSize": [LBL + FIELD, 44], "WordWrap": True,
                      "Alignment": {"AlignLeft": True, "AlignTop": True}}),

            ui.VGap(10),
            ui.HGroup({"Weight": 0, "Spacing": 10}, [
                ui.Label({"Text": "", "Weight": 0,
                          "MinimumSize": [LBL + FIELD - 260, 32]}),
                ui.Button({"ID": "Cancel", "Text": "Cancel", "Weight": 0,
                           "MinimumSize": [120, 30], "MaximumSize": [120, 30]}),
                ui.Button({"ID": "Create", "Text": "Create", "Default": True, "Weight": 0,
                           "MinimumSize": [120, 30], "MaximumSize": [120, 30]})]),
        ])])

    it = win.GetItems()
    for name, _ in timelines:
        it["Timeline"].AddItem(name)
    it["Timeline"].CurrentIndex = default_timeline
    for label, _, _ in tracks:
        it["Track"].AddItem(label)
    it["Track"].CurrentIndex = first_track_with_clips(tracks)
    for label, _ in LANGUAGES:
        it["Lang"].AddItem(label)
    it["Lang"].CurrentIndex = 0  # Auto detect: Whisper works out the language itself
    for label, _ in MODELS:
        it["Model"].AddItem(label)
    it["Lines"].AddItem("Single")
    it["Lines"].AddItem("Double")

    state = {"tracks": tracks}

    def selected_timeline():
        return project.GetTimelineByIndex(timelines[it["Timeline"].CurrentIndex][1])

    def log(msg, to_file=True):
        """A message for the interface, and by default for the log as well.

        Guarded against Resolve's ASCII locale and against exceptions: Fusion
        swallows errors raised inside event handlers, so a failing log would make
        the message vanish without a trace.
        """
        text = str(msg)
        if to_file:
            log_file(text)
        for candidate in (text, text.encode("ascii", "replace").decode("ascii")):
            try:
                it["Status"].Text = candidate
                break
            except Exception:
                continue

    def on_timeline(ev):
        """Picking another timeline repopulates the audio tracks: they differ."""
        try:
            rows = list_audio_tracks(selected_timeline())
            state["tracks"] = rows
            it["Track"].Clear()
            for label, _, _ in rows:
                it["Track"].AddItem(label)
            it["Track"].CurrentIndex = first_track_with_clips(rows)
            log("Ready.", False)   # UI only: on every timeline change it would be log noise
        except Exception as e:
            log("ERROR: {}".format(e))

    def work(opts):
        timeline = selected_timeline()
        # Subtitles always land on the current timeline, so make the chosen one current.
        open_now = project.GetCurrentTimeline()
        if not open_now or timeline.GetName() != open_now.GetName():
            log("Switching to timeline \"{}\"...".format(timeline.GetName()))
            project.SetCurrentTimeline(timeline)
            timeline = project.GetCurrentTimeline()

        # Check right away: no point making the user wait a minute of transcription
        # only to find out the track was occupied.
        taken = len(timeline.GetItemListInTrack("subtitle", 1) or [])
        if taken and not opts["replace"]:
            raise RuntimeError(
                "Subtitle 1 already has {} subtitles. Tick \"Replace existing "
                "subtitles\", or clear the track first.".format(taken))

        segs, sources, skipped = read_track(timeline, project, opts["track"])
        if not segs:
            raise RuntimeError("No audio clips with media on the selected track.")
        if skipped:
            log("Warning: skipped {} clip(s), source file not found".format(skipped))
        # A fresh folder each run: ImportMedia refuses a path it has already imported.
        tmp = tempfile.mkdtemp(prefix="whisper_sub_")
        try:
            wav = os.path.join(tmp, "audio.wav")
            srt = os.path.join(tmp, "subtitles.srt")
            build_wav(segs, sources, wav, log)
            run_whisper(wav, srt, opts, log)
            insert_srt(project, timeline, srt, log, opts["replace"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def read_options():
        return {
            "track": state["tracks"][it["Track"].CurrentIndex][1],
            "language": LANGUAGES[it["Lang"].CurrentIndex][1],
            "model": MODELS[it["Model"].CurrentIndex][1],
            "chars": int(it["Chars"].Value),
            "lines": 1 if it["Lines"].CurrentIndex == 0 else 2,
            "gap_sec": int(it["Gap"].Value) / fps,
            "fill_gaps": it["Fill"].Checked,
            "stretch": it["Stretch"].Checked,
            "upper": it["Upper"].Checked,
            "nopunct": it["NoPunct"].Checked,
            "replace": it["Replace"].Checked,
        }

    def on_create(ev):
        import traceback
        log_file("--- Create pressed ---")
        try:
            opts = read_options()
            log_file("options: " + repr(opts))
        except Exception:
            log("ERROR reading the options - see " + LOGFILE)
            try:
                with open(LOGFILE, "a", encoding="utf-8") as fh:
                    fh.write(traceback.format_exc())
            except Exception:
                pass
            return
        try:
            it["Create"].Text = "Working..."
        except Exception:
            pass
        try:
            work(opts)
        except Exception as e:
            log("ERROR: {}".format(e))
            try:
                with open(LOGFILE, "a", encoding="utf-8") as fh:
                    fh.write(traceback.format_exc())
            except Exception:
                pass
        try:
            it["Create"].Text = "Create"
        except Exception:
            pass

    win.On[WINDOW_ID].Close = lambda ev: disp.ExitLoop()
    win.On.Cancel.Clicked = lambda ev: disp.ExitLoop()
    win.On.Create.Clicked = on_create
    win.On.Timeline.CurrentIndexChanged = on_timeline

    win.Show()
    # Fixed-size window. Inside the AddWindow dict these two land on the content and
    # the frame stays draggable: they must be set as attributes, and only after Show().
    try:
        win.MinimumSize = [WIN_W, WIN_H]
        win.MaximumSize = [WIN_W, WIN_H]
    except Exception:
        pass
    disp.RunLoop()
    win.Hide()


main()
