# 开发与验收说明

## Host 环境

项目运行基线为 Python 3.11：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
```

需要本地 VAD/唤醒/ASR/身份/视觉/TTS 后端时安装可选组：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[media-local]"
```

`media-local` 包含 faster-whisper、openwakeword、opencv-contrib-python-headless、MediaPipe、sherpa-onnx 和 onnxruntime。只安装可选依赖不会自动提供模型文件，模型路径仍需显式配置。

启动与模拟：

```powershell
.\.venv\Scripts\naobot.exe serve
.\.venv\Scripts\naobot.exe simulate --event touch_head
.\.venv\Scripts\naobot.exe send-event --event battery_low
```

Dashboard 默认地址为 `http://127.0.0.1:8765`，健康检查为 `/health`。

## 全部 `NAOBOT_*` 配置

### 服务、链路与 Runtime

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NAOBOT_HOST` | `127.0.0.1` | FastAPI 监听地址 |
| `NAOBOT_PORT` | `8765` | FastAPI 端口 |
| `NAOBOT_ROBOT_ID` | `naobot` | 数据库与媒体使用的机器人 ID |
| `NAOBOT_DEVICE_TOKEN` | 未配置 | 媒体 hello 与 People API token；未配置时 People API 仅本机可用 |
| `NAOBOT_RUNTIME_DIR` | `runtime` | `naobot.db`、Soul、Memory、Routine 根目录 |
| `NAOBOT_ROBOT_HEARTBEAT_TIMEOUT_MS` | `7000` | Host 判断固件链路超时 |
| `NAOBOT_HOST_HEARTBEAT_INTERVAL_MS` | `2000` | Host heartbeat 间隔 |
| `NAOBOT_EVENT_QUEUE_CAPACITY` | `32` | Host 有界优先级事件队列容量 |
| `NAOBOT_SESSION_IDLE_MS` | `30000` | 自然交互会话空闲超时 |
| `NAOBOT_TTS_RESUME_DELAY_MS` | `200` | Host 在 TTS 完成后恢复监听的延迟 |
| `NAOBOT_DATA_KEY` | 未配置 | Fernet key；仅用于 embedding 与 5 张注册样本，无 key 拒绝注册 |

生成开发 key：

```powershell
$env:NAOBOT_DATA_KEY = .\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### AgentScope Brain

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NAOBOT_LLM_BASE_URL` | 未配置 | OpenAI-compatible base URL |
| `NAOBOT_LLM_MODEL` | 未配置 | 模型名；base URL 与模型都存在才启用 LLM |
| `NAOBOT_LLM_API_KEY` | 未配置 | 可选 API key |
| `NAOBOT_BRAIN_SINGLE_TIMEOUT_SECONDS` | `6.0` | 单 Agent 总超时 |
| `NAOBOT_BRAIN_TEAM_TIMEOUT_SECONDS` | `15.0` | 三专家到负责人收敛的团队总超时 |
| `NAOBOT_BRAIN_TIMEOUT_SECONDS` | `6.0` | 兼容别名，仅在未设置 single 变量时生效 |
| `NAOBOT_BRAIN_MAX_ITERS` | `4` | ReAct 轮数；代码仍封顶为 4 |
| `NAOBOT_BRAIN_TEAM_ENABLED` | `true` | 是否允许非安全请求组队 |
| `NAOBOT_BRAIN_DEBUG_FORCE_TEAM_OVERRIDE` | `false` | 仅调试时允许旧 `requires_team/complexity` 强制组队 |

生产自动路由不信任外部 `requires_team` 或 `complexity`：评分 `>=4` 自动组队，单 Agent `needs_team=true` 或 `confidence<0.65` 自升级。安全事件始终确定性 fallback。

### 媒体窗口、帧率、VAD 与短语

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NAOBOT_VIDEO_FPS` | `10` | Host/固件常态视频目标 |
| `NAOBOT_VIDEO_EVENT_FPS` | `15` | 本地事件窗口视频目标 |
| `NAOBOT_MEDIA_VIDEO_WINDOW_MS` | `10000` | RAM 视频窗口 |
| `NAOBOT_MEDIA_AUDIO_WINDOW_MS` | `15000` | RAM 音频窗口 |
| `NAOBOT_MEDIA_VIDEO_QUEUE_LIMIT` | `20` | Host 视频队列上限 |
| `NAOBOT_MEDIA_AUDIO_QUEUE_LIMIT` | `100` | Host 音频队列上限 |
| `NAOBOT_LOCAL_VAD_ENABLED` | `true` | 固件无 VAD flags 时启用 Host PCM16 能量 VAD |
| `NAOBOT_VAD_RMS_THRESHOLD` | `500` | Host VAD RMS 阈值 |
| `NAOBOT_VAD_END_SILENCE_MS` | `400` | Host VAD 结束静音窗口 |
| `NAOBOT_LOCAL_PHRASE_MODEL` | 未配置 | faster-whisper 模型名，用于本地短问候/唤醒短语 |
| `NAOBOT_WAKE_MODEL_PATH` | 未配置 | openWakeWord 本地模型路径 |
| `NAOBOT_TEMPORAL_SUMMARY_INTERVAL_MS` | `1000` | RAM 时序视觉摘要间隔 |

### 身份

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NAOBOT_IDENTITY_MODEL_PATH` | 未配置 | 本地 ONNX 人脸 embedding 模型路径 |
| `NAOBOT_IDENTITY_MATCH_THRESHOLD` | `0.78` | 余弦匹配阈值 |
| `NAOBOT_IDENTITY_MATCH_INTERVAL_MS` | `1000` | 身份匹配最小间隔 |
| `NAOBOT_IDENTITY_ENROLLMENT_SIMILARITY_THRESHOLD` | `0.8` | 5 张注册样本一致性阈值 |

### ASR、TTS、Vision 与 Sherpa

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NAOBOT_ASR_ENDPOINT` | 未配置 | OpenAI-compatible ASR endpoint |
| `NAOBOT_ASR_MODEL` | 未配置 | ASR 模型 |
| `NAOBOT_ASR_API_KEY` | 未配置 | ASR key |
| `NAOBOT_TTS_ENDPOINT` | 未配置 | OpenAI-compatible TTS endpoint |
| `NAOBOT_TTS_MODEL` | 未配置 | TTS 模型 |
| `NAOBOT_TTS_API_KEY` | 未配置 | TTS key |
| `NAOBOT_TTS_VOICE` | `alloy` | TTS voice |
| `NAOBOT_VISION_ENDPOINT` | 未配置 | OpenAI-compatible vision endpoint |
| `NAOBOT_VISION_MODEL` | 未配置 | vision 模型 |
| `NAOBOT_VISION_API_KEY` | 未配置 | vision key |
| `NAOBOT_SHERPA_ONNX_MODEL_PATH` | 未配置 | Sherpa ONNX TTS 模型路径 |
| `NAOBOT_SHERPA_ONNX_TOKENS_PATH` | 未配置 | Sherpa tokens 路径 |
| `NAOBOT_SHERPA_ONNX_LEXICON_PATH` | 未配置 | Sherpa lexicon 路径 |
| `NAOBOT_SHERPA_ONNX_DATA_DIR` | 未配置 | Sherpa data dir |
| `NAOBOT_SHERPA_ONNX_RULE_FSTS` | 未配置 | Sherpa rule FST 路径 |
| `NAOBOT_SHERPA_ONNX_NUM_THREADS` | `2` | Sherpa 推理线程数 |

## SQLite 与隐私调试

`RuntimePersistence` 创建 `runtime/naobot.db` 并启用 SQLite WAL，表包含 people、conversation sessions、agent runtimes、face embeddings 和 face samples。

- 已识别人员 runtime 持久化，visitor/guest runtime 只在内存。
- 原始 RAM 媒体窗口不落盘；Agent state 中的 base64/URL 媒体写库前替换为摘要和 SHA-256。
- Fernet 只加密 `face_embeddings.embedding_ciphertext` 与 `face_samples.sample_ciphertext`；数据库文件整体不是密文。

可查看数据库模式，不要输出密文或敏感 state 到日志：

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('runtime/naobot.db'); print(c.execute('pragma journal_mode').fetchone()); print([r[0] for r in c.execute(\"select name from sqlite_master where type='table' order by name\")])"
```

## People 启动与调试

设置 token 后启动：

```powershell
$env:NAOBOT_DEVICE_TOKEN="replace-with-a-long-random-token"
$env:NAOBOT_DATA_KEY="replace-with-a-valid-fernet-key"
.\.venv\Scripts\naobot.exe serve
$headers = @{ Authorization = "Bearer $env:NAOBOT_DEVICE_TOKEN" }
Invoke-RestMethod http://127.0.0.1:8765/api/people -Headers $headers
```

管理接口：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8765/api/people/person_xxx/runtime/reset -Headers $headers
Invoke-RestMethod -Method Delete http://127.0.0.1:8765/api/people/person_xxx -Headers $headers
Invoke-RestMethod -Method Post http://127.0.0.1:8765/api/people/enrollment/cancel -Headers $headers
```

注册流程不是 People API 直接写库：未知单人说“记住我/认识我”，系统准备最近 5 张人脸；用户在 10 秒窗口内口头说“确认”，再摸头才原子写入。无 key、已知身份、多人、帧不足或样本不一致都会拒绝。

## Git hooks 与验证

```powershell
git config core.hooksPath .githooks
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

`tools/check_prd_sync.py` 读取暂存区。代码改动时先暂存代码与 `docs/product/prd.md`，再运行：

```powershell
.\.venv\Scripts\python.exe tools/check_prd_sync.py
```

## MicroPython 构建与烧录

仓库 generic 镜像 `data/ESP32_GENERIC_S3-20260406-v1.28.0.bin` 可用于基础 MicroPython，但不含项目定制 `camera` 模块，不能验证 OV2640 路径。

定制配方固定 MicroPython `v1.28.0` commit `2b0015629f67fd186f980079b2e696ad0bc7343c` 和 `esp32-camera v2.1.6` commit `2ac69a6f1749694804f5196e63fa1f79800b74bf`。需要 Git、GNU Make、ESP-IDF 与工具链：

```powershell
firmware/esp32/build/build.ps1 -Clean
```

预期输出目录为 `firmware/esp32/build/_work/micropython/ports/esp32/build-XIAO_ESP32S3_SENSE-SPIRAM_OCT/`。只有目录中真实存在 `firmware.bin` 后才能执行：

```powershell
esptool.py --chip esp32s3 --port COM3 erase_flash
esptool.py --chip esp32s3 --port COM3 --baud 460800 write_flash -z 0x0 firmware/esp32/build/_work/micropython/ports/esp32/build-XIAO_ESP32S3_SENSE-SPIRAM_OCT/firmware.bin
```

源码上传命令见 `firmware/esp32/README.md`。2026-07-24 已完成项目定制镜像的真实 C 编译、链接和分区尺寸检查；这仅是构建证据，不代表定制 bin 已在目标硬件验收。XIAO ESP32S3 Sense 的真实烧录、签名 OTA、失败回滚、OV2640、PDM/I2S、PSRAM、USB CDC 和 30 分钟指标仍未验收。
