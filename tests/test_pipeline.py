# -*- coding: utf-8 -*-
"""流水线测试用例。

运行方式（在源码根目录）：
    python -m pytest tests/ -v
或  python tests/test_pipeline.py

覆盖验收点：
1. FFmpeg 探测可用
2. 合成样片（视频+音频）→ 抽音频 → 输出文件正确
3. SRT 写出/解析往返一致、UTF-8 中文不乱码、时间戳连续
4. 损坏文件只抛业务异常（FFmpegError），不崩溃
5. ASR 端到端（需模型已下载，否则自动跳过）
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.asr.base import Segment, TranscribeParams, create_backend  # noqa: E402
from core.ffmpeg_utils import FFmpegError, extract_audio, ffmpeg_path, probe_media  # noqa: E402
from core.model_manager import is_model_downloaded  # noqa: E402
from core.srt_utils import parse_srt, write_srt  # noqa: E402


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="subgen_test_")
        # 用 ffmpeg 合成 5 秒测试样片（440Hz 正弦音 + 彩条画面），全程本地生成
        cls.sample_mp4 = os.path.join(cls.tmp, "sample.mp4")
        cmd = [
            ffmpeg_path(), "-y", "-v", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
            "-f", "lavfi", "-i", "testsrc=duration=5:size=320x240:rate=10",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            cls.sample_mp4,
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           creationflags=creationflags)
        if r.returncode != 0:
            raise unittest.SkipTest("无法合成测试样片：" + r.stderr.decode("utf-8", "ignore")[:200])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_01_probe_media(self):
        """ffprobe 读取时长/音频流"""
        info = probe_media(self.sample_mp4)
        self.assertTrue(info["has_audio"])
        self.assertGreater(info["duration"], 3.0)
        self.assertGreater(info["size"], 0)

    def test_02_extract_audio(self):
        """视频抽音频 → wav"""
        wav = extract_audio(self.sample_mp4, self.tmp, "wav")
        self.assertTrue(os.path.isfile(wav))
        info = probe_media(wav)
        self.assertGreater(info["duration"], 3.0)

    def test_03_srt_roundtrip(self):
        """SRT 写出→解析 往返一致；中文 UTF-8 不乱码；时间戳连续"""
        segs = [
            Segment(start=0.0, end=1.5, text="你好，世界"),
            Segment(start=1.5, end=3.2, text="Hello world 中英混合 test"),
            Segment(start=3.2, end=5.0, text="第三段字幕"),
        ]
        srt = os.path.join(self.tmp, "roundtrip.srt")
        write_srt(segs, srt)

        # 强制 UTF-8 写入校验
        with open(srt, "rb") as f:
            raw = f.read()
        self.assertIn("你好".encode("utf-8"), raw)

        parsed = parse_srt(srt)
        self.assertEqual(len(parsed), 3)
        self.assertEqual(parsed[0].text, "你好，世界")
        self.assertEqual(parsed[1].text, "Hello world 中英混合 test")
        # 时间戳连续不跳轴
        for a, b in zip(parsed, parsed[1:]):
            self.assertAlmostEqual(a.end, b.start, places=2)

    def test_04_broken_file_no_crash(self):
        """损坏文件：仅抛 FFmpegError 业务异常，不崩溃"""
        broken = os.path.join(self.tmp, "broken.mp4")
        with open(broken, "wb") as f:
            f.write(b"this is not a video file")
        with self.assertRaises(FFmpegError):
            probe_media(broken)

    def test_05_asr_end_to_end(self):
        """端到端：抽音频 + large-v3 识别 + 输出 srt（模型未下载则跳过）"""
        if not is_model_downloaded("large-v3"):
            self.skipTest("模型 large-v3 未下载，跳过端到端识别测试")
        wav = extract_audio(self.sample_mp4, self.tmp, "wav")
        backend = create_backend("faster-whisper", "large-v3", "int8")
        backend.load()
        segments = backend.transcribe(wav, TranscribeParams())
        srt = os.path.join(self.tmp, "sample.srt")
        write_srt(segments, srt)
        self.assertTrue(os.path.isfile(srt))
        backend.unload()


if __name__ == "__main__":
    unittest.main(verbosity=2)
