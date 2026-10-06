"""
Read the playing time of audio files from their headers (stdlib only).

Only a few KB of each file are read, so scanning an album is quick even on a
slow or network drive. Supported: MP3, FLAC, M4A/MP4/AAC/ALAC, WAV, AIFF,
Ogg Vorbis/Opus, WMA, Monkey's Audio (APE) and WavPack.
"""

import os
import struct
from pathlib import Path

AUDIO_EXTS = {
    ".mp3", ".flac", ".m4a", ".m4b", ".mp4", ".aac", ".alac", ".wav", ".aif", ".aiff", ".aifc",
    ".ogg", ".oga", ".opus", ".wma", ".ape", ".wv",
}


def _skip_id3v2(f) -> int:
    """Return the offset just past a leading ID3v2 tag (0 if there is none)."""
    f.seek(0)
    head = f.read(10)
    if len(head) < 10 or head[:3] != b"ID3":
        return 0
    size = (head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14 | (head[8] & 0x7F) << 7 | (head[9] & 0x7F)
    return 10 + size + (10 if head[5] & 0x10 else 0)  # footer flag


# --- MP3 ---

_MP3_BITRATES = {  # (MPEG-1?, layer) -> kbps by index
    (True, 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (True, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (True, 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (False, 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (False, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_MP3_BITRATES[(False, 3)] = _MP3_BITRATES[(False, 2)]
_MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mp3_length(f, size: int) -> float:
    start = _skip_id3v2(f)
    f.seek(start)
    buf = f.read(65536)
    for i in range(len(buf) - 4):
        if buf[i] != 0xFF or buf[i + 1] & 0xE0 != 0xE0:
            continue
        version = buf[i + 1] >> 3 & 3      # 3 = MPEG-1, 2 = MPEG-2, 0 = MPEG-2.5
        layer = 4 - (buf[i + 1] >> 1 & 3)  # 1..3
        bitrate_idx, rate_idx = buf[i + 2] >> 4, buf[i + 2] >> 2 & 3
        if version == 1 or layer == 4 or bitrate_idx in (0, 15) or rate_idx == 3:
            continue
        mpeg1 = version == 3
        rate = _MP3_RATES[version][rate_idx]
        bitrate = _MP3_BITRATES[(mpeg1, layer)][bitrate_idx] * 1000
        mono = buf[i + 3] >> 6 == 3
        samples = 384 if layer == 1 else 1152 if layer == 2 or mpeg1 else 576

        # VBR files carry a frame count in a Xing/Info or VBRI header in the first frame.
        frame = buf[i:i + 200]
        side = (17 if mono else 32) if mpeg1 else (9 if mono else 17)
        xing = frame[4 + side:4 + side + 12]
        if xing[:4] in (b"Xing", b"Info") and struct.unpack(">I", xing[4:8])[0] & 1:
            return struct.unpack(">I", xing[8:12])[0] * samples / rate
        if frame[36:40] == b"VBRI":
            return struct.unpack(">I", frame[50:54])[0] * samples / rate

        # Otherwise assume constant bitrate.
        audio = size - (start + i)
        f.seek(-128, os.SEEK_END)
        if f.read(3) == b"TAG":
            audio -= 128
        return audio * 8 / bitrate
    raise ValueError("no MPEG audio frame found")


# --- FLAC ---

def _flac_length(f, size: int) -> float:
    f.seek(_skip_id3v2(f))
    head = f.read(42)
    if head[:4] != b"fLaC" or head[4] & 0x7F != 0:  # STREAMINFO must come first
        raise ValueError("not a FLAC file")
    info = int.from_bytes(head[18:26], "big")
    rate = info >> 44
    total = info & 0xFFFFFFFFF
    return total / rate


# --- MP4 / M4A ---

def _mp4_length(f, size: int) -> float:
    def atoms(start: int, end: int):
        pos = start
        while pos + 8 <= end:
            f.seek(pos)
            atom_size, kind = struct.unpack(">I4s", f.read(8))
            header = 8
            if atom_size == 1:
                atom_size = struct.unpack(">Q", f.read(8))[0]
                header = 16
            elif atom_size == 0:
                atom_size = end - pos
            if atom_size < header:
                return
            yield kind, pos + header, pos + atom_size
            pos += atom_size

    for kind, start, end in atoms(0, size):
        if kind != b"moov":
            continue
        for sub, sub_start, _ in atoms(start, end):
            if sub == b"mvhd":
                f.seek(sub_start)
                data = f.read(32)
                if data[0] == 1:
                    scale, duration = struct.unpack(">IQ", data[20:32])
                else:
                    scale, duration = struct.unpack(">II", data[12:20])
                return duration / scale
    raise ValueError("no mvhd atom found")


# --- WAV / AIFF ---

def _wav_length(f, size: int) -> float:
    f.seek(0)
    if f.read(12)[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    byte_rate = None
    while True:
        head = f.read(8)
        if len(head) < 8:
            raise ValueError("no data chunk found")
        kind, chunk_size = struct.unpack("<4sI", head)
        if kind == b"fmt ":
            byte_rate = struct.unpack("<I", f.read(16)[8:12])[0]
            chunk_size -= 16
        elif kind == b"data":
            if not byte_rate:
                raise ValueError("data chunk before fmt chunk")
            return min(chunk_size, size - f.tell()) / byte_rate
        f.seek(chunk_size + (chunk_size & 1), os.SEEK_CUR)


def _aiff_length(f, size: int) -> float:
    f.seek(0)
    if f.read(12)[8:12] not in (b"AIFF", b"AIFC"):
        raise ValueError("not an AIFF file")
    while True:
        head = f.read(8)
        if len(head) < 8:
            raise ValueError("no COMM chunk found")
        kind, chunk_size = struct.unpack(">4sI", head)
        if kind == b"COMM":
            data = f.read(18)
            frames = struct.unpack(">I", data[2:6])[0]
            exponent, mantissa = struct.unpack(">HQ", data[8:18])  # 80-bit extended float
            rate = mantissa * 2.0 ** ((exponent & 0x7FFF) - 16383 - 63)
            return frames / rate
        f.seek(chunk_size + (chunk_size & 1), os.SEEK_CUR)


# --- Ogg (Vorbis, Opus) ---

def _ogg_length(f, size: int) -> float:
    f.seek(0)
    page = f.read(512)
    if page[:4] != b"OggS":
        raise ValueError("not an Ogg file")
    packet = page[27 + page[26]:]
    if packet[:7] == b"\x01vorbis":
        rate, pre_skip = struct.unpack("<I", packet[12:16])[0], 0
    elif packet[:8] == b"OpusHead":
        rate, pre_skip = 48000, struct.unpack("<H", packet[10:12])[0]  # Opus granules are 48 kHz
    else:
        raise ValueError("unsupported Ogg codec")
    f.seek(max(0, size - 65536))
    tail = f.read()
    last = tail.rfind(b"OggS")
    if last < 0 or last + 14 > len(tail):
        raise ValueError("no final Ogg page found")
    granule = struct.unpack("<q", tail[last + 6:last + 14])[0]
    return (granule - pre_skip) / rate


# --- WMA (ASF) ---

_ASF_HEADER = bytes.fromhex("3026B2758E66CF11A6D900AA0062CE6C")
_ASF_FILE_PROPERTIES = bytes.fromhex("A1DCAB8C47A9CF118EE400C00C205365")


def _wma_length(f, size: int) -> float:
    f.seek(0)
    head = f.read(30)
    if head[:16] != _ASF_HEADER:
        raise ValueError("not an ASF file")
    for _ in range(struct.unpack("<I", head[24:28])[0]):
        obj = f.read(24)
        if len(obj) < 24:
            break
        obj_size = struct.unpack("<Q", obj[16:24])[0]
        if obj[:16] == _ASF_FILE_PROPERTIES:
            data = f.read(64)
            play, _send, preroll = struct.unpack("<QQQ", data[40:64])
            return play / 1e7 - preroll / 1000
        if obj_size < 24:
            break
        f.seek(obj_size - 24, os.SEEK_CUR)
    raise ValueError("no file properties object found")


# --- Monkey's Audio (APE) ---

def _ape_length(f, size: int) -> float:
    f.seek(_skip_id3v2(f))
    head = f.read(8)
    if head[:4] != b"MAC ":
        raise ValueError("not an APE file")
    version = struct.unpack("<H", head[4:6])[0]
    if version >= 3980:  # descriptor, then header
        descriptor_size = struct.unpack("<I", f.read(4))[0]
        f.seek(descriptor_size - 12, os.SEEK_CUR)
        blocks, final_blocks, frames, _bits, _channels, rate = struct.unpack("<4xIIIHHI", f.read(24))
    else:
        compression, _flags, _channels, rate = struct.unpack("<HHHI", head[6:8] + f.read(8))
        _wav_header, _terminating, frames, final_blocks = struct.unpack("<IIII", f.read(16))
        blocks = 73728 * 4 if version >= 3950 else 73728 if version >= 3900 or compression >= 4000 else 9216
    return ((frames - 1) * blocks + final_blocks) / rate if frames else 0.0


# --- WavPack ---

_WV_RATES = (6000, 8000, 9600, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 64000, 88200, 96000, 192000)


def _wv_length(f, size: int) -> float:
    f.seek(0)
    head = f.read(32)
    if head[:4] != b"wvpk":
        raise ValueError("not a WavPack file")
    total = struct.unpack("<I", head[12:16])[0]
    if total == 0xFFFFFFFF:
        raise ValueError("WavPack length not stored")
    total += head[11] << 32  # high byte of the sample count (WavPack 5)
    rate_index = struct.unpack("<I", head[24:28])[0] >> 23 & 0xF
    if rate_index >= len(_WV_RATES):
        raise ValueError("non-standard WavPack sample rate")
    return total / _WV_RATES[rate_index]


_READERS = {
    ".mp3": _mp3_length, ".flac": _flac_length,
    ".m4a": _mp4_length, ".m4b": _mp4_length, ".mp4": _mp4_length, ".aac": _mp4_length,
    ".alac": _mp4_length, ".wav": _wav_length, ".aif": _aiff_length, ".aiff": _aiff_length,
    ".aifc": _aiff_length, ".ogg": _ogg_length, ".oga": _ogg_length, ".opus": _ogg_length,
    ".wma": _wma_length, ".ape": _ape_length, ".wv": _wv_length,
}


def track_length(path: Path) -> float:
    """Playing time of one audio file in seconds. Raises OSError/ValueError if unreadable."""
    reader = _READERS.get(path.suffix.lower())
    if reader is None:
        raise ValueError(f"unsupported file type: {path.suffix}")
    with path.open("rb") as f:
        try:
            seconds = reader(f, os.fstat(f.fileno()).st_size)
        except (struct.error, IndexError, KeyError, ZeroDivisionError) as e:
            raise ValueError(f"bad header in {path.name}") from e
    if not 0 <= seconds < 86400:
        raise ValueError(f"implausible length in {path.name}")
    return seconds


def album_length(album_dir: Path) -> tuple[float, int, int]:
    """Total playing time of the audio files in a folder (and its subfolders, e.g. CD1/CD2).

    Returns (seconds, tracks read, tracks that couldn't be read).
    """
    total, tracks, failed = 0.0, 0, 0
    for dirpath, dirs, files in os.walk(album_dir):
        dirs[:] = [d for d in dirs if d != "__MACOSX"]  # macOS zip metadata, not music
        for name in files:
            path = Path(dirpath) / name
            if path.suffix.lower() not in AUDIO_EXTS or name.startswith("._"):
                continue
            try:
                total += track_length(path)
                tracks += 1
            except (OSError, ValueError):
                failed += 1
    return total, tracks, failed


def format_length(seconds: float) -> str:
    """Seconds -> "h:mm:ss", or "mm:ss" under an hour, rounded to the nearest second."""
    minutes, secs = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
