# LocalOfflineSubGen 本地离线批量音视频字幕生成器

面向本地音视频素材的批量字幕工具：批量抽取音频 → 本地 ASR 识别（中英混合）→ 输出标准 SRT，
并可对已有 SRT 做**本地离线批量翻译**（NLLB-200）。
识别与翻译均在本地运行，仅首次下载模型权重时联网。

## 1. 本地离线设计

- 识别与翻译在本地完成；代码不包含音视频/文本上传、埋点、遥测、崩溃上报与日志回传功能
- 联网场景：首次下载 ASR / 翻译模型权重（HuggingFace 或镜像站）。下载完成后可在断网环境运行
- 【设置】页提供**离线锁定开关**：开启后在进程内阻断网络连接（含模型下载）
- 日志记录文件名/状态/报错，不包含音频内容与识别文本
- 临时音频默认在处理完成后删除；程序读取用户手动选择的文件，不扫描磁盘其他位置

## 2. 运行方式

### 方式 A：绿色版（推荐给终端用户）
解压 `LocalOfflineSubGen.zip` → 双击 `LocalOfflineSubGen.exe`。
无需安装 Python / CUDA / FFmpeg（已内置 `bin/ffmpeg.exe`、`bin/ffprobe.exe`）。

### 方式 B：源码运行（开发）
```bat
pip install -r requirements.txt
python main.py
```
需要本机已有 FFmpeg（PATH 中），或将 `ffmpeg.exe/ffprobe.exe` 放入程序目录 `bin/` 下。

## 3. 硬件与显存建议

| 配置 | 模型 | 精度 | 说明 |
|---|---|---|---|
| NVIDIA 显卡 ≥ 8GB 显存 | large-v3 | float16 | 推荐，速度与精度最佳 |
| NVIDIA 显卡 4~6GB | large-v3-turbo | int8 | 速度优先 |
| 无 N 卡 / 核显 | large-v3 / turbo | int8 (CPU) | 自动降级，速度约为 GPU 的 1/5~1/10 |

- 程序自动探测 CUDA；显存溢出时给出提示并自动降级 CPU int8 继续跑
- 并发数默认 1（串行）。每增加一路并发会多加载一份模型，显存占用成倍增加
- 大文件（几十 GB）为流式处理，内存占用与文件大小无关

## 4. 离线模式使用说明

1. 首次启动按向导下载 large-v3 模型（保持联网）
2. 【模型管理】页确认模型状态为"已下载"
3. 【设置】页开启**离线锁定** → 此后可断开网络使用
4. 需要下载其他模型时，临时关闭离线锁定，下载完再开启

## 5. 字幕翻译（本地离线）

【字幕翻译】页签：批量选择 `.srt` 文件或整个文件夹（含全部子文件夹），用本地 NLLB-200 模型翻译。

- **翻译方向**：自动识别（默认，逐句判定：英文句及英文为主的混排句译成中文，中文句原样保留）/ 中→英 / 英→中
- **输出方式**：双语双行（原文+译文，默认）/ 仅译文。输出文件名为 `<原文件名>_translated.srt`，**不覆盖原字幕**
  - 自动识别时：英文段为「英文原文 + 中文译文」两行；中文段只有一行原文（不重复、不翻译）
- **时间轴**：沿用原 SRT；仅在排版需要时按字数比例拆分时段，总起止时间不变
- **输出目录**：同原文件目录 / 统一输出文件夹（选后者时保留相对子目录结构，避免同名覆盖）
- 模型：`NLLB-200 distilled 600M int8`（约 0.7GB），在【模型管理】下载；GPU 可用时自动走 CUDA，否则 CPU int8
- 已带译文的双语字幕会被识别，只翻译第一行，避免重复翻译

### 字幕排版（识别与翻译统一生效）

- 每段字幕最多 **2 行**：约 20 个汉字/行，或 40 个英文字符/行（按显示宽度折算）
- 一段装不下时，按标点优先自动拆成多个**连续时段**，时间按字数比例分配，总起止时间不变
- **中英混排段按语种拆条**：同一段里的英文语句和中文语句各自成条（中文句里的英文术语如 REFINE 保留不动），时间按宽度比例分配
- 宽度在 1~2 行之间时按标点**均衡分成两行**，避免「行满 + 孤零零短行」
- 双语输出的每段固定为「原文 1 行 + 译文 1 行」；纯中文段保持单行原文
- 可在 `config/settings.json` 调整：`subtitle_max_lines`（2 为默认，1 更简洁，0 关闭排版）、`subtitle_max_width`（默认 40）、`subtitle_split_lang`（默认开启语种拆条，false 关闭）

## 6. 模型路径与内网手动部署

模型缓存目录（默认）：`程序目录/cache/models/`

```
cache/models/
├── models--Systran--faster-whisper-large-v3/        # large-v3
├── models--mobiuslabsgmbh--faster-whisper-large-v3-turbo/  # large-v3-turbo
└── nllb-200-distilled-600M-int8/                    # 字幕翻译模型（普通目录）
    ├── model.bin
    ├── config.json
    └── sentencepiece.bpe.model
```

翻译模型为普通目录结构：在其他机器下载后，把整个 `nllb-200-distilled-600M-int8` 文件夹
拷贝到内网机器的 `cache/models/` 下即可离线使用。

**内网机器部署（不联网）**：在能联网的机器上运行本软件下载模型，或执行
`pip install faster-whisper` 后用 Python 触发一次 `WhisperModel("large-v3", download_root="<目录>")`，
将生成的 `models--Systran--faster-whisper-large-v3` 整个文件夹拷贝到内网机器的
`cache/models/` 下即可；或在【模型管理】中点"选择本地模型文件夹"直接指向模型目录。

**sherpa-onnx FireRedASR2（可选）**：`pip install sherpa-onnx soundfile`，手动下载
FireRedASR2 模型文件（encoder.onnx / decoder.onnx / tokens.txt）放到本地目录，
在【模型管理】中选择该目录，并在任务页将后端切换为 sherpa-onnx。

## 7. 打包（构建机操作）

```bat
REM 1. 准备 FFmpeg 到 bin/
powershell -ExecutionPolicy Bypass -File scripts\fetch_ffmpeg.ps1
REM 2. 安装依赖与打包工具
pip install -r requirements.txt
pip install nuitka ordered-set zstandard
REM 3. 打包绿色版
build_nuitka.bat
```
产物：`dist\LocalOfflineSubGen\`，整体压缩为 zip 即可分发。

## 8. 测试

```bat
pip install pytest
python -m pytest tests/ -v
```
测试覆盖：FFmpeg 探测、抽音频、SRT 往返一致性（UTF-8 中文/时间戳连续）、损坏文件容错、
ASR 端到端（模型未下载时自动跳过）。
建议再用一段真实**中英混合 mp4 样片**走一遍：导入 → 识别 → 双击预览校对 → 检查 SRT 导入 PR/剪映。

## 9. 目录结构

```
LocalOfflineSubGen/
├── main.py                  # 程序入口
├── requirements.txt
├── build_nuitka.bat         # Windows 绿色版打包脚本
├── bin/                     # 内置 ffmpeg.exe / ffprobe.exe（打包前准备）
├── core/
│   ├── config.py            # 配置持久化（程序目录 config/settings.json）
│   ├── logger.py            # 本地日志（仅文件名/状态/报错）
│   ├── offline_guard.py     # 离线锁定（socket 层封堵 + HF 离线变量）
│   ├── ffmpeg_utils.py      # FFmpeg 定位/探测/抽音频
│   ├── model_manager.py     # 模型查询/下载/删除
│   ├── srt_utils.py         # SRT/VTT/TXT/JSON 输出与 SRT 解析
│   ├── task_runner.py       # 批量调度（串行/并行/暂停/停止/重试）
│   ├── asr/                 # ASR 后端（faster-whisper / sherpa-onnx）
│   └── translate/           # 本地翻译（NLLB-200 后端 + SRT 批量翻译）
├── ui/                      # PyQt6 界面（任务/模型管理/字幕翻译/字幕预览/设置）
├── scripts/fetch_ffmpeg.ps1 # 构建机下载 FFmpeg
└── tests/test_pipeline.py   # 流水线测试
```

## 10. 常见问题

- **提示"模型未下载且当前处于离线状态"**：关闭离线锁定 → 【模型管理】下载模型 → 再开启锁定。
- **CUDA 显存不足**：程序会自动降级 CPU int8；也可在任务页把计算精度改为 int8、或改用 turbo 模型。
- **SRT 中文乱码**：本工具以 UTF-8 输出；PR/剪映导入时选择 UTF-8 编码即可。
- **某个文件失败**：不影响队列其他任务，处理完后可点"重试失败项"。
- **翻译速度慢**：CPU 上每条约 0.1~0.5 秒；有 N 卡时程序自动用 CUDA。也可在翻译设置里改用"仅译文"减少写盘量。
- **翻译模型下载失败**：程序会自动尝试 `hf-mirror.com` 镜像；仍失败时可在能联网的机器下载
  `JustFrederik/nllb-200-distilled-600M-ct2-int8` 仓库，把 `model.bin / config.json /
  sentencepiece.bpe.model` 放入 `cache/models/nllb-200-distilled-600M-int8/`。
