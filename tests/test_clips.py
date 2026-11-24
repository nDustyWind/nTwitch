"""The rolling clip buffer, driven by a fake clock and a fake byte stream (no streamlink, no network)."""
import types

from ntwitch import clip_manager, tools
from ntwitch.clip_manager import TS_PACKET, ClipBuffer, ClipManager, SegmentWriter


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Feed:
    """Yields fixed-size chunks of a known byte pattern, advancing the clock each time."""

    def __init__(self, clock, chunks, step):
        self.clock, self.step = clock, step
        self.payload = bytes(i % 251 for i in range(chunks * TS_PACKET * 10 + 77))  # not packet aligned on purpose
        self.pos = 0
        self.chunk = TS_PACKET * 10 + 7

    def read1(self, n):
        if self.pos >= len(self.payload):
            return b""
        self.clock.now += self.step
        data = self.payload[self.pos:self.pos + self.chunk]
        self.pos += len(data)
        return data


def run_writer(tmp_path, chunks=60, step=5.0, keep=60.0, segment=20.0):
    clock = FakeClock()
    feed = Feed(clock, chunks, step)
    writer = SegmentWriter(feed, tmp_path, keep_seconds=keep, segment_seconds=segment, clock=clock)
    writer.run()  # synchronously
    return writer, feed, clock


def test_buffer_is_bounded_and_rolls(tmp_path):
    writer, feed, clock = run_writer(tmp_path)
    files = sorted(tmp_path.glob("seg_*.ts"))
    on_disk = sum(f.stat().st_size for f in files)
    assert on_disk < len(feed.payload) / 2, "old segments must be deleted"
    assert len(files) == len(writer.segments) >= 3
    assert writer.buffered_seconds() <= 60.0
    assert writer.ended


def test_concatenation_is_the_stream_tail_on_a_packet_boundary(tmp_path):
    writer, feed, _ = run_writer(tmp_path)
    out = tmp_path / "joined.ts"
    size = writer.concatenate(out)
    first_start = int(writer.segments[0].path.stem.split("_")[1])
    skip = (TS_PACKET - first_start % TS_PACKET) % TS_PACKET
    assert out.read_bytes() == feed.payload[first_start + skip:]
    assert size == len(out.read_bytes())
    assert (first_start + skip) % TS_PACKET == 0


def test_save_clip_without_ffmpeg_keeps_a_real_ts_file(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "find_ffmpeg", lambda: None)
    clips = ClipManager(clips_path=str(tmp_path / "clips"))
    clock = FakeClock()
    feed = Feed(clock, 200, 1.0)
    seg_dir = tmp_path / "segs"
    seg_dir.mkdir()
    writer = SegmentWriter(feed, seg_dir, keep_seconds=600, segment_seconds=20, clock=clock)
    writer.run()
    clips.active_buffer = ClipBuffer("xqc", types.SimpleNamespace(poll=lambda: 0), writer)

    ok, message, path = clips.save_clip("xqc")
    assert ok, message
    assert path.suffix == ".ts" and path.parent.name == "xqc" and path.stat().st_size > 100_000
    assert not list((tmp_path / "clips" / ".temp").glob("*.ts")), "temp file must not be left behind"


def test_save_clip_refuses_when_too_little_is_buffered(tmp_path, monkeypatch):
    clips = ClipManager(clips_path=str(tmp_path / "clips"))
    clock = FakeClock()
    writer = SegmentWriter(Feed(clock, 1, 1.0), tmp_path, keep_seconds=600, clock=clock)
    writer.run()
    clips.active_buffer = ClipBuffer("xqc", types.SimpleNamespace(poll=lambda: 0), writer)
    ok, message, path = clips.save_clip("xqc")
    assert not ok and path is None and "buffered" in message


def test_status_text(tmp_path):
    clips = ClipManager(buffer_minutes=10, clips_path=str(tmp_path / "clips"))
    assert clips.get_buffer_status("xqc") is None
    writer, _, _ = run_writer(tmp_path)
    clips.active_buffer = ClipBuffer("xqc", types.SimpleNamespace(poll=lambda: 0), writer)
    assert clips.get_buffer_status("xqc").endswith("/10")
