import json
import shutil

import pytest

from clipperhub import director, procs, render
from clipperhub.config import Settings
from clipperhub.transcribe import Word


def test_director_keeps_causal_order_and_avoids_cutting_a_word():
    rows = [{"start": 2, "end": 6, "layout": "balanced"},
            {"start": 12, "end": 17, "layout": "reaction"}]
    edited = director.validate({"segments": rows}, 20, 6, 12, True, True, [Word(1.8, 2.2, "wait")])
    assert edited[0] == {"start": 1.8, "end": 6, "layout": "balanced"}
    assert edited[1]["layout"] == "reaction"
    with pytest.raises(director.EditorialError, match="overlap"):
        director.validate({"segments": rows[::-1]}, 20, 6, 12, True, True, [])
    with pytest.raises(director.EditorialError, match="one to six"):
        director.validate({"segments": []}, 20, 6, 12, True, True, [])
    assert director.validate({"segments": rows}, 20, 6, 12, False, True, [])[1]["layout"] == "gameplay"


def test_director_renders_saved_cuts_with_multiple_layouts(tmp_path, monkeypatch):
    if not shutil.which(procs.ffmpeg()) or not shutil.which(procs.ffprobe()):
        pytest.skip("FFmpeg/FFprobe not installed")
    monkeypatch.setattr(render, "W", 270)
    monkeypatch.setattr(render, "H", 1920)
    s = Settings(data_dir=tmp_path, fps=24, encoder="x264", whisper_model="off")
    source = tmp_path / "source.mp4"
    procs.run([procs.ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=24",
               "-f", "lavfi", "-i", "sine=f=430", "-t", "8", "-c:v", "libx264",
               "-c:a", "aac", str(source)], check=True)
    assert render.extract_frames(source, [1], width=64, timeout=12)[0].startswith(b"\xff\xd8")
    out = tmp_path / "edit.mp4"
    rows = [{"start": 0.5, "end": 2.5, "layout": "balanced"},
            {"start": 5, "end": 7, "layout": "reaction"}]
    render.render_edit(source, out, 0, rows, [Word(.6, 1, "Setup"), Word(5.2, 5.6, "Payoff")],
                       "The moment", facecam=(0, 0, .3, .3), style="ooc", settings=s)
    info = render.probe(out)
    assert info["audio"] and info["height"] == 1920
    assert info["duration"] == pytest.approx(4, abs=.2)
    (tmp_path / "edit-plan.json").write_text(json.dumps({
        "version": 1, "raw": source.name, "offset": 0, "segments": rows, "words": [],
        "facecam": [0, 0, .3, .3], "style": "ooc", "hook": "", "captions": False,
        "show_hook": False, "music": None, "fps": s.fps}), encoding="utf-8")
    remade = director.rerender(s, tmp_path, tmp_path / "remade.mp4")
    assert render.probe(remade)["duration"] == pytest.approx(info["duration"], abs=.05)
    with pytest.raises(director.EditorialError, match="already exists"):
        director.rerender(s, tmp_path, out)
