"""Exhaustive unit tests for web_panel.app._merge_audio_parts.

Seam: web_panel.app.subprocess.run (patched per-test).
A fake subprocess result exposes .returncode (int) and .stderr (bytes).
"""
import os

import pytest
from unittest.mock import MagicMock, call, patch


def _fake_result(returncode=0, stderr=b""):
    """Build a stand-in for subprocess.CompletedProcess."""
    r = MagicMock()
    r.returncode = returncode
    r.stderr = stderr
    return r


# 1. single-file -------------------------------------------------------------
def test_single_file_uses_copy_and_never_calls_subprocess(appmod, tmp_path):
    src = tmp_path / "only.mp3"
    payload = b"\x00\x01RAW-AUDIO-BYTES\xff"
    src.write_bytes(payload)
    out = tmp_path / "merged.mp3"

    with patch.object(appmod.subprocess, "run") as run:
        result = appmod._merge_audio_parts([str(src)], str(out))

    assert result == str(out)
    assert out.exists()
    assert out.read_bytes() == payload
    run.assert_not_called()


# 2. multi happy path --------------------------------------------------------
def test_multi_happy_path_returns_output_and_single_call(appmod, tmp_path):
    out = tmp_path / "out.mp3"
    paths = [str(tmp_path / "a.mp3"), str(tmp_path / "b.mp3")]

    with patch.object(appmod.subprocess, "run", return_value=_fake_result(0)) as run:
        result = appmod._merge_audio_parts(paths, str(out))

    assert result == str(out)
    assert run.call_count == 1

    argv = run.call_args.args[0]
    # copy-codec fast path arguments present
    assert "-c" in argv
    assert "copy" in argv
    # the -c flag is immediately followed by copy
    assert argv[argv.index("-c") + 1] == "copy"
    # the concat filelist path is passed as input
    assert (str(out) + ".filelist.txt") in argv


# 3. codec-mismatch fallback -------------------------------------------------
def test_codec_mismatch_falls_back_to_libmp3lame(appmod, tmp_path):
    out = tmp_path / "out.mp3"
    paths = [str(tmp_path / "a.mp3"), str(tmp_path / "b.mp3")]

    side = [_fake_result(1), _fake_result(0)]
    with patch.object(appmod.subprocess, "run", side_effect=side) as run:
        result = appmod._merge_audio_parts(paths, str(out))

    assert result == str(out)
    assert run.call_count == 2

    second_argv = run.call_args_list[1].args[0]
    assert "-c:a" in second_argv
    assert "libmp3lame" in second_argv
    assert second_argv[second_argv.index("-c:a") + 1] == "libmp3lame"


# 4. both fail ---------------------------------------------------------------
def test_both_attempts_fail_raises_runtimeerror_with_stderr(appmod, tmp_path):
    out = tmp_path / "out.mp3"
    paths = [str(tmp_path / "a.mp3"), str(tmp_path / "b.mp3")]

    side = [_fake_result(1), _fake_result(1, stderr=b"boom")]
    with patch.object(appmod.subprocess, "run", side_effect=side) as run:
        with pytest.raises(RuntimeError) as exc_info:
            appmod._merge_audio_parts(paths, str(out))

    assert run.call_count == 2
    assert "boom" in str(exc_info.value)


# 5. filelist format ---------------------------------------------------------
def test_filelist_contains_each_path_in_order(appmod, tmp_path):
    out = tmp_path / "out.mp3"
    paths = [
        str(tmp_path / "first.mp3"),
        str(tmp_path / "second.mp3"),
        str(tmp_path / "third.mp3"),
    ]
    filelist = str(out) + ".filelist.txt"

    captured = {}

    def _read_then_succeed(*args, **kwargs):
        with open(filelist) as f:
            captured["contents"] = f.read()
        return _fake_result(0)

    with patch.object(appmod.subprocess, "run", side_effect=_read_then_succeed):
        appmod._merge_audio_parts(paths, str(out))

    contents = captured["contents"]
    lines = contents.splitlines()
    assert lines == [f"file '{p}'" for p in paths]
    # ordering: each path appears, in order, exactly once
    assert contents == "".join(f"file '{p}'\n" for p in paths)


# 6a. filelist cleanup after success ----------------------------------------
def test_filelist_removed_after_successful_merge(appmod, tmp_path):
    out = tmp_path / "out.mp3"
    paths = [str(tmp_path / "a.mp3"), str(tmp_path / "b.mp3")]
    filelist = str(out) + ".filelist.txt"

    with patch.object(appmod.subprocess, "run", return_value=_fake_result(0)):
        appmod._merge_audio_parts(paths, str(out))

    assert not os.path.exists(filelist)


# 6b. filelist cleanup after failure ----------------------------------------
def test_filelist_removed_after_failed_merge(appmod, tmp_path):
    out = tmp_path / "out.mp3"
    paths = [str(tmp_path / "a.mp3"), str(tmp_path / "b.mp3")]
    filelist = str(out) + ".filelist.txt"

    side = [_fake_result(1), _fake_result(1, stderr=b"boom")]
    with patch.object(appmod.subprocess, "run", side_effect=side):
        with pytest.raises(RuntimeError):
            appmod._merge_audio_parts(paths, str(out))

    assert not os.path.exists(filelist)
