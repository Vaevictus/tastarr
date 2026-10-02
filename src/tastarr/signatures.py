"""What a file is, judged by its name (stage 0) and by its first bytes (stage 1).

``tools/libscan.py`` runs ``sniff`` over an existing media library to check
that the video allowlist recognises every real file.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

# Files we expect to be a video container and therefore taste (stage 1).
VIDEO_EXTS = frozenset(
    {
        ".mkv",
        ".mk3d",
        ".webm",
        ".mp4",
        ".m4v",
        ".mov",
        ".3gp",
        ".avi",
        ".divx",
        ".ts",
        ".m2ts",
        ".mts",
        ".wmv",
        ".asf",
        ".mpg",
        ".mpeg",
        ".vob",
        ".flv",
        ".ogm",
        ".ogv",
        ".rm",
        ".rmvb",
    }
)

# Extensions that sink a whole torrent on sight (stage 0). A superset of
# Sonarr/Radarr's own "Executables" + "Potentially Dangerous" lists, so
# Tastarr never rejects something the *arr would have imported: it only
# rejects it earlier.
DANGEROUS_EXTS = frozenset(
    {
        # Windows executables / installers
        ".exe",
        ".scr",
        ".pif",
        ".msi",
        ".msp",
        ".msix",
        ".appx",
        ".cpl",
        ".dll",
        ".application",
        ".gadget",
        # scripts
        ".bat",
        ".cmd",
        ".ps1",
        ".psm1",
        ".vbs",
        ".vbe",
        ".js",
        ".jse",
        ".wsf",
        ".wsh",
        ".hta",
        ".sh",
        ".jar",
        ".reg",
        ".scf",
        # shortcuts
        ".lnk",
        # archive formats the *arrs flag as dangerous
        ".zipx",
        ".arj",
        ".lzh",
        # other platforms
        ".apk",
    }
)

# Bidirectional-control characters: used to disguise "evil<RLO>vkm.exe" as
# "evilexe.mkv". No legitimate release name needs them.
_BIDI_CONTROLS = frozenset("‎‏‪‫‬‭‮⁦⁧⁨⁩")


def extension(name: str) -> str:
    """Lower-cased final extension of a torrent file path ("" if none)."""
    return posixpath.splitext(posixpath.basename(name.rstrip()))[1].lower()


def name_problem(name: str) -> str | None:
    """Why a file name alone condemns its torrent, or None if it's fine."""
    if any(c in _BIDI_CONTROLS for c in name):
        return "bidi-control character in file name"
    ext = extension(name)
    if ext in DANGEROUS_EXTS:
        return f"dangerous extension {ext}"
    return None


def is_video_name(name: str) -> bool:
    return extension(name) in VIDEO_EXTS


@dataclass(frozen=True)
class Sniff:
    """Verdict on the first bytes of a file.

    kind:  "video"     a known video container            -> pass
           "dangerous" an executable/archive/script magic -> reject
           "unknown"   neither                            -> reject (allowlist)
           "empty"     zero-filled / nothing yet          -> not ready, retry
    """

    kind: str
    label: str


_ASF_GUID = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")
_ISOBMFF_BOXES = frozenset(
    {
        b"ftyp",
        b"moov",
        b"mdat",
        b"free",
        b"skip",
        b"wide",
        b"pnot",
        b"uuid",
        b"styp",
        b"sidx",
        b"moof",
    }
)

# (prefix, label) for formats that must never be inside a video file name.
_DANGEROUS_MAGIC = (
    (b"MZ", "pe-executable"),
    (b"\x7fELF", "elf-executable"),
    (b"\xfe\xed\xfa\xce", "mach-o"),
    (b"\xfe\xed\xfa\xcf", "mach-o"),
    (b"\xce\xfa\xed\xfe", "mach-o"),
    (b"\xcf\xfa\xed\xfe", "mach-o"),
    (b"\xca\xfe\xba\xbe", "mach-o-fat/java-class"),
    (b"L\x00\x00\x00\x01\x14\x02\x00", "windows-shortcut"),
    (bytes.fromhex("d0cf11e0a1b11ae1"), "ole-compound (msi/doc)"),
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),
    (b"Rar!\x1a\x07", "rar"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"MSCF", "cab"),
    (b"\x1f\x8b", "gzip"),
    (b"BZh", "bzip2"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"#!", "shebang-script"),
    (b"%PDF", "pdf"),
    (b"{\\rtf", "rtf"),
)


def _ts_aligned(head: bytes, stride: int, lead: int) -> bool:
    """MPEG transport stream: sync byte 0x47 every `stride` bytes (4 packets)."""
    for start in range(min(stride, len(head))):
        pos = start + lead
        if pos + 3 * stride >= len(head):
            return False
        if all(head[pos + i * stride] == 0x47 for i in range(4)):
            return True
    return False


def _video_label(head: bytes) -> str | None:
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "matroska"
    if len(head) >= 8 and head[4:8] in _ISOBMFF_BOXES:
        return "isobmff (mp4/mov)"
    if head.startswith(b"RIFF") and head[8:12] in (b"AVI ", b"AVIX", b"AMV "):
        return "avi"
    if head.startswith(_ASF_GUID):
        return "asf (wmv)"
    if head.startswith(b"\x00\x00\x01\xba"):
        return "mpeg-ps"
    if head.startswith(b"\x00\x00\x01\xb3"):
        return "mpeg-es"
    if head.startswith(b"FLV"):
        return "flv"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b".RMF"):
        return "realmedia"
    if _ts_aligned(head, 188, 0):
        return "mpeg-ts"
    if _ts_aligned(head, 192, 4):
        return "m2ts"
    if _ts_aligned(head, 204, 0):
        return "mpeg-ts (204)"
    return None


def _looks_like_text(head: bytes) -> bool:
    sample = head[:4096]
    printable = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13))
    return printable >= 0.95 * len(sample)


def sniff(head: bytes) -> Sniff:
    """Classify the first bytes of a file that claims to be video."""
    if not head or head.count(0) == len(head):
        return Sniff("empty", "zero-filled" if head else "no data")
    video = _video_label(head)
    if video:
        return Sniff("video", video)
    for magic, label in _DANGEROUS_MAGIC:
        if head.startswith(magic):
            return Sniff("dangerous", label)
    if _looks_like_text(head):
        return Sniff("unknown", "text")
    return Sniff("unknown", "unrecognised: " + head[:16].hex())
