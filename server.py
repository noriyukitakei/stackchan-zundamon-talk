"""
stackchan-zundamon-talk: スタックチャンと、ずんだもんの声で会話するサーバー

スタックチャン（XiaoZhi プロトコル）とのやり取り:
  1. スタックチャン → POST /xiaozhi/ota/       ... WebSocket の接続先を JSON で返す
  2. スタックチャン → WebSocket /xiaozhi/v1/   ... hello を交換して接続完了
  3. あなた        → POST /emotion, /speech, /say, /zundamon ... 接続中のスタックチャンへ指示を送る
  4. スタックチャン → マイクの音声（Opus）     ... 話し終わったら、次の順に処理して返事をする
       ① 音声認識（Azure OpenAI の gpt-4o-transcribe など）
       ② 返事を作る（Azure OpenAI の Chat。ストリームで受け取る）
       ③ 文ごとに VOICEVOX でずんだもんの声にして、すぐスタックチャンへ送る
"""

import array
import asyncio
import collections
import io
import json
import logging
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import av
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stackchan-zundamon-talk")

app = FastAPI(title="stackchan-zundamon-talk")

# OTA の返事に入れるトークン。スタックチャンは WebSocket 接続時に
# "Authorization: Bearer <token>" として送ってくる
WS_TOKEN = "hello-stackchan"

# スタックチャンが解釈できる表情（firmware/main/hal/board/stackchan_display.cc）
EMOTIONS = {"neutral", "happy", "laughing", "angry", "sad", "crying", "sleepy", "doubtful"}

# スタックチャンのスピーカーは 24kHz（firmware/main/hal/board/config.h の AUDIO_OUTPUT_SAMPLE_RATE）
SAMPLE_RATE = 24000
FRAME_DURATION_MS = 60
# 喋らせるときの声（`say -v '?'` で一覧。日本語は Kyoko など）
DEFAULT_VOICE = "Kyoko"


@dataclass
class Device:
    device_id: str
    ws: WebSocket
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ready: bool = False  # hello の交換が終わったら True
    speaking: asyncio.Lock = field(default_factory=asyncio.Lock)  # 同時に喋らせないため

    # --- 会話用 ---
    # マイクの音声の形式（スタックチャンの hello の audio_params で上書きする）
    in_rate: int = 16000
    in_frame_ms: int = 60
    listen_mode: str = "auto"  # auto: サーバーが話し終わりを判定 / manual: listen stop で終わり
    state: str = "idle"  # idle / listening（聞いている）/ thinking（返事を作って喋っている）
    listener: "Listener | None" = None
    task: asyncio.Task | None = None  # 返事を作って喋っているタスク
    history: list[dict] = field(default_factory=list)  # 会話の履歴（Chat に渡す）


# 接続中のスタックチャン（Device-Id → Device）
devices: dict[str, Device] = {}


# ---------------------------------------------------------------------------
# 1. OTA: スタックチャンが起動時（AI エージェント開始時）に問い合わせてくる
# ---------------------------------------------------------------------------
@app.api_route("/xiaozhi/ota/", methods=["GET", "POST"])
async def ota(request: Request):
    device_id = request.headers.get("device-id", "unknown")
    log.info("OTA check from %s (%s)", device_id, request.headers.get("user-agent", ""))

    # スタックチャンが OTA URL に使ったホスト名をそのまま WebSocket の接続先にする
    host = request.headers.get("host", "localhost:8000")
    return {
        "server_time": {
            "timestamp": int(time.time() * 1000),
            "timezone_offset": 540,  # JST（分）
        },
        # mqtt を返さず websocket だけ返すと、スタックチャンは WebSocket で繋ぎに来る
        "websocket": {
            "url": f"ws://{host}/xiaozhi/v1/",
            "token": WS_TOKEN,
        },
        # firmware を返さないので、ファームの更新は行われない
    }


# ---------------------------------------------------------------------------
# 2. WebSocket: 会話を始めるとスタックチャンが繋ぎに来る
# ---------------------------------------------------------------------------
@app.websocket("/xiaozhi/v1/")
async def xiaozhi_ws(ws: WebSocket):
    device_id = ws.headers.get("device-id", "unknown")
    await ws.accept()
    device = Device(device_id=device_id, ws=ws)
    devices[device_id] = device
    log.info("WebSocket connected: %s", device_id)

    audio_frames = 0
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break

            # バイナリはマイクの音声（Opus）。聞いている間だけ溜めて、話し終わりを判定する
            if msg.get("bytes") is not None:
                audio_frames += 1
                _on_audio(device, msg["bytes"])
                continue

            data = json.loads(msg["text"])
            log.info("<< %s: %s", device_id, data)
            kind = data.get("type")

            if kind == "hello":
                # スタックチャンが送ってくるマイクの音声の形式を覚えておく
                params = data.get("audio_params") or {}
                device.in_rate = int(params.get("sample_rate", device.in_rate))
                device.in_frame_ms = int(params.get("frame_duration", device.in_frame_ms))

                # サーバーの hello を返す。transport が "websocket" でないと接続失敗になる
                await ws.send_json({
                    "type": "hello",
                    "transport": "websocket",
                    "session_id": device.session_id,
                    # サーバーから送る音声の形式。スピーカーに合わせて 24kHz にする
                    "audio_params": {
                        "format": "opus",
                        "sample_rate": SAMPLE_RATE,
                        "channels": 1,
                        "frame_duration": FRAME_DURATION_MS,
                    },
                })
                device.ready = True
                log.info("Handshake done: %s (session %s, mic %dHz/%dms)",
                         device_id, device.session_id, device.in_rate, device.in_frame_ms)

            elif kind == "listen":
                _on_listen(device, data)

            elif kind == "abort":
                # 喋っている途中でタッチされたときなど。返事を止める
                if device.task and not device.task.done():
                    log.info("Abort: %s (%s)", device_id, data.get("reason", ""))
                    device.task.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        if device.task and not device.task.done():
            device.task.cancel()
        if devices.get(device_id) is device:
            del devices[device_id]
        log.info("WebSocket disconnected: %s (received %d audio frames)", device_id, audio_frames)


# ---------------------------------------------------------------------------
# 3. あなたが叩く HTTP API
# ---------------------------------------------------------------------------
class EmotionRequest(BaseModel):
    emotion: str = "happy"
    device_id: str | None = None  # 省略時は接続中の全台に送る


class SpeechRequest(BaseModel):
    text: str = "Hello, world!"
    device_id: str | None = None


class SayRequest(BaseModel):
    text: str = "こんにちは、スタックチャンです"
    voice: str = DEFAULT_VOICE
    emotion: str | None = None  # 指定すると、喋る前に表情を変える
    device_id: str | None = None


def _targets(device_id: str | None) -> list[Device]:
    ready = [d for d in devices.values() if d.ready]
    if device_id:
        ready = [d for d in ready if d.device_id == device_id]
    if not ready:
        raise HTTPException(
            status_code=409,
            detail="接続中のスタックチャンがいません。AI エージェントで会話を始めてから叩いてください",
        )
    return ready


async def _send(targets: list[Device], payload: dict) -> list[str]:
    sent = []
    for d in targets:
        message = {"session_id": d.session_id, **payload}
        await d.ws.send_json(message)
        log.info(">> %s: %s", d.device_id, message)
        sent.append(d.device_id)
    return sent


@app.get("/devices")
async def list_devices():
    return [{"device_id": d.device_id, "ready": d.ready, "state": d.state} for d in devices.values()]


@app.post("/emotion")
async def set_emotion(req: EmotionRequest):
    """表情を変える（例: happy で笑顔）"""
    if req.emotion not in EMOTIONS:
        raise HTTPException(status_code=400, detail=f"emotion は {sorted(EMOTIONS)} のいずれか")
    sent = await _send(_targets(req.device_id), {"type": "llm", "emotion": req.emotion})
    return {"sent_to": sent, "emotion": req.emotion}


@app.post("/speech")
async def show_speech(req: SpeechRequest):
    """吹き出しにテキストを表示する（音声はまだ出ない）"""
    sent = await _send(_targets(req.device_id), {"type": "tts", "state": "sentence_start", "text": req.text})
    return {"sent_to": sent, "text": req.text}


# ---------------------------------------------------------------------------
# 喋らせる: テキスト → 音声（macOS の say）→ Opus → スタックチャン
# ---------------------------------------------------------------------------
async def _synthesize(text: str, voice: str) -> bytes:
    """macOS の say コマンドで、24kHz・16bit・モノラルの PCM を作る"""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "tts.wav")
        proc = await asyncio.create_subprocess_exec(
            "say", "-v", voice, "-o", path,
            "--file-format=WAVE", f"--data-format=LEI16@{SAMPLE_RATE}", text,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise HTTPException(status_code=400, detail=f"say に失敗しました: {err.decode().strip()}")
        with wave.open(path) as w:
            return w.readframes(w.getnframes())


def _encode_opus(pcm: bytes) -> list[bytes]:
    """PCM を 60ms ごとの Opus パケットに分ける"""
    ctx = av.CodecContext.create("libopus", "w")
    ctx.sample_rate = SAMPLE_RATE
    ctx.layout = "mono"
    ctx.format = "s16"
    ctx.options = {"frame_duration": str(FRAME_DURATION_MS), "application": "voip"}
    ctx.open()

    samples = ctx.frame_size  # 60ms 分 = 1440 サンプル
    step = samples * 2  # 16bit なので 1 サンプル 2 バイト
    pcm += b"\0" * ((-len(pcm)) % step)  # 端数は無音で埋める

    packets = []
    for i in range(0, len(pcm), step):
        frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
        frame.planes[0].update(pcm[i:i + step])
        frame.sample_rate = SAMPLE_RATE
        frame.pts = i // 2
        packets += [bytes(p) for p in ctx.encode(frame)]
    packets += [bytes(p) for p in ctx.encode(None)]
    return packets


async def _speak_stream(
    device: Device,
    sentences: AsyncIterator[tuple[str, list[bytes]]],
    on_first_packet: Callable[[], None] | None = None,
):
    """(文, Opus パケット) を受け取った順に喋らせる。返事の文が届くたびに続けて喋る"""
    async with device.speaking:
        started = False
        try:
            async for text, packets in sentences:
                if not started:
                    # tts start でスタックチャンが「話す」状態になる。この状態のときだけ音声を再生する
                    await _send([device], {"type": "tts", "state": "start"})
                    await asyncio.sleep(0.1)  # 状態が切り替わるのを少し待つ
                    started = True
                await _send([device], {"type": "tts", "state": "sentence_start", "text": text})

                # 再生速度に合わせて 60ms ごとに送る（最初の数個は先に送ってバッファさせる）
                start = time.monotonic()
                for i, packet in enumerate(packets):
                    await device.ws.send_bytes(packet)
                    if i == 0 and on_first_packet:
                        on_first_packet()
                        on_first_packet = None
                    wait = start + (i - 3) * FRAME_DURATION_MS / 1000 - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
        finally:
            if started:
                try:
                    await asyncio.sleep(0.3)  # 最後の音が鳴り終わるのを待つ
                    await _send([device], {"type": "tts", "state": "stop"})
                except Exception:
                    pass  # 途中で切断・中断されたときは送れなくてもよい


async def _speak(device: Device, text: str, packets: list[bytes]):
    async def one():
        yield text, packets

    await _speak_stream(device, one())


async def _play(targets: list[Device], text: str, pcm: bytes, emotion: str | None, voice_label: str) -> dict:
    """PCM を Opus にして、表情を変えてから喋らせる（/say と /zundamon で共通）"""
    packets = await asyncio.to_thread(_encode_opus, pcm)
    seconds = len(packets) * FRAME_DURATION_MS / 1000
    log.info("TTS: %r (%s, %.1fs, %d packets)", text, voice_label, seconds, len(packets))

    if emotion:
        await _send(targets, {"type": "llm", "emotion": emotion})
    await asyncio.gather(*(_speak(d, text, packets) for d in targets))
    return {"sent_to": [d.device_id for d in targets], "text": text, "voice": voice_label, "seconds": seconds}


def _check_emotion(emotion: str | None):
    if emotion and emotion not in EMOTIONS:
        raise HTTPException(status_code=400, detail=f"emotion は {sorted(EMOTIONS)} のいずれか")


@app.post("/say")
async def say(req: SayRequest):
    """テキストを喋らせる（macOS の say の声）"""
    _check_emotion(req.emotion)
    targets = _targets(req.device_id)
    pcm = await _synthesize(req.text, req.voice)
    return await _play(targets, req.text, pcm, req.emotion, req.voice)


# ---------------------------------------------------------------------------
# ずんだもんで喋らせる: テキスト → VOICEVOX → Opus → スタックチャン
# ---------------------------------------------------------------------------
VOICEVOX_URL = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021")

# ずんだもんのスタイルと VOICEVOX の話者 ID（GET /speakers で確認したもの）
ZUNDAMON_STYLES = {
    "ノーマル": 3,
    "あまあま": 1,
    "ツンツン": 7,
    "セクシー": 5,
    "ささやき": 22,
    "ヒソヒソ": 38,
    "ヘロヘロ": 75,
    "なみだめ": 76,
}


class ZundamonRequest(BaseModel):
    text: str = "ぼくはずんだもんなのだ"
    style: str = "ノーマル"
    speed: float = 1.0  # 話す速さ（0.5〜2.0 くらい）
    emotion: str | None = None
    device_id: str | None = None


def _voicevox_synthesize(text: str, speaker: int, speed: float) -> bytes:
    """VOICEVOX で 24kHz・16bit・モノラルの PCM を作る"""
    base = VOICEVOX_URL
    # ① テキストから読み・アクセントなどの合成用パラメータを作る
    q = urllib.parse.urlencode({"text": text, "speaker": speaker})
    with urllib.request.urlopen(urllib.request.Request(f"{base}/audio_query?{q}", method="POST")) as r:
        query = json.load(r)

    # スピーカーに合わせて 24kHz・モノラルで出力させる
    query["outputSamplingRate"] = SAMPLE_RATE
    query["outputStereo"] = False
    query["speedScale"] = speed

    # ② パラメータから WAV を合成する
    req = urllib.request.Request(
        f"{base}/synthesis?speaker={speaker}",
        data=json.dumps(query).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        wav = r.read()
    with wave.open(io.BytesIO(wav)) as w:
        return w.readframes(w.getnframes())


@app.post("/zundamon")
async def zundamon(req: ZundamonRequest):
    """ずんだもんの声で喋らせる（VOICEVOX）"""
    _check_emotion(req.emotion)
    if req.style not in ZUNDAMON_STYLES:
        raise HTTPException(status_code=400, detail=f"style は {list(ZUNDAMON_STYLES)} のいずれか")
    targets = _targets(req.device_id)

    try:
        pcm = await asyncio.to_thread(_voicevox_synthesize, req.text, ZUNDAMON_STYLES[req.style], req.speed)
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"VOICEVOX（{VOICEVOX_URL}）に接続できません: {e}")
    return await _play(targets, req.text, pcm, req.emotion, f"VOICEVOX:ずんだもん（{req.style}）")


# ---------------------------------------------------------------------------
# 4. 会話する: マイクの音声 → ①音声認識 → ②返事を作る → ③VOICEVOX → スタックチャン
# ---------------------------------------------------------------------------
# Azure OpenAI の設定（.env に書いて `uvicorn --env-file .env` で読み込む）
AOAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AOAI_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY", "")
# ② 返事を作る Chat モデルのデプロイ名
AOAI_CHAT_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")
AOAI_CHAT_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
# ① 音声認識モデルのデプロイ名（速さ重視で gpt-4o-mini-transcribe。精度重視なら gpt-4o-transcribe）
AOAI_STT_DEPLOYMENT = os.environ.get("AZURE_OPENAI_STT_DEPLOYMENT", "gpt-4o-mini-transcribe")
AOAI_STT_API_VERSION = os.environ.get("AZURE_OPENAI_STT_API_VERSION", "2025-03-01-preview")

# ③ 返事を喋るずんだもんのスタイルと速さ
CHAT_STYLE = os.environ.get("ZUNDAMON_STYLE", "ノーマル")
CHAT_SPEED = float(os.environ.get("ZUNDAMON_SPEED", "1.0"))

# 話し終わりの判定（auto モードのとき）。音量（RMS）がしきい値を下回った状態が続いたら話し終わり
VAD_RMS_THRESHOLD = int(os.environ.get("VAD_RMS_THRESHOLD", "600"))
VAD_SILENCE_MS = int(os.environ.get("VAD_SILENCE_MS", "800"))  # この長さ黙ったら話し終わり
VAD_MIN_SPEECH_MS = 300  # これより短い音は咳払いなどとみなして無視する
VAD_MAX_SPEECH_MS = 30_000  # 長すぎるときは、そこで区切る
VAD_PREROLL_MS = 300  # 話し始めの頭が切れないよう、直前の音も含める
HISTORY_MAX = 10  # Chat に渡す会話の履歴（発言の数）

SYSTEM_PROMPT = f"""あなたは、手のひらサイズのロボット「スタックチャン」に宿った、ずんだもんです。
ユーザーと音声で会話します。次のルールを守ってください。
- 語尾は「〜なのだ」「〜のだ」にする。一人称は「ぼく」
- 返事は1〜2文の短い話し言葉にする。読み上げるので、記号・絵文字・箇条書き・コードは使わない
- 返事の最初に、そのときの表情を [happy] のように1つだけ付ける
  使える表情: {", ".join(sorted(EMOTIONS))}
"""

CONVERSATION_ENABLED = bool(AOAI_ENDPOINT and AOAI_API_KEY and AOAI_CHAT_DEPLOYMENT)
_stt_client = None
_chat_client = None
if CONVERSATION_ENABLED:
    from openai import AsyncAzureOpenAI

    _stt_client = AsyncAzureOpenAI(
        azure_endpoint=AOAI_ENDPOINT, api_key=AOAI_API_KEY, api_version=AOAI_STT_API_VERSION)
    _chat_client = AsyncAzureOpenAI(
        azure_endpoint=AOAI_ENDPOINT, api_key=AOAI_API_KEY, api_version=AOAI_CHAT_API_VERSION)
    log.info("Conversation: enabled (stt=%s, chat=%s, style=%s)", AOAI_STT_DEPLOYMENT, AOAI_CHAT_DEPLOYMENT, CHAT_STYLE)
else:
    log.warning("Conversation: disabled（AZURE_OPENAI_ENDPOINT / API_KEY / DEPLOYMENT を設定すると会話できます）")


class Listener:
    """マイクの Opus をデコードして溜め、話し始めと話し終わりを判定する"""

    def __init__(self, rate: int, frame_ms: int):
        self.rate = rate
        self.frame_ms = frame_ms
        self.decoder = av.CodecContext.create("libopus", "r")
        self.decoder.sample_rate = rate
        self.decoder.layout = "mono"
        self.resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        self.preroll: collections.deque[bytes] = collections.deque(maxlen=max(1, VAD_PREROLL_MS // frame_ms))
        self.pcm = bytearray()
        self.speech_ms = 0  # 声が入っていたフレームの合計
        self.silence_ms = 0  # 話し始めてから、黙っている長さ
        self.started = False

    def _decode(self, packet: bytes) -> bytes:
        out = b""
        try:
            for frame in self.decoder.decode(av.Packet(packet)):
                for f in self.resampler.resample(frame):
                    out += bytes(f.planes[0])[: f.samples * 2]
        except av.error.FFmpegError as e:
            log.debug("Opus decode error: %s", e)
        return out

    def feed(self, packet: bytes) -> bool:
        """1パケット分を足す。話し終わったら True を返す（auto モードの判定）"""
        pcm = self._decode(packet)
        if not pcm:
            return False
        samples = array.array("h", pcm)
        rms = math.sqrt(sum(s * s for s in samples) / len(samples))
        voiced = rms >= VAD_RMS_THRESHOLD

        if not self.started:
            self.preroll.append(pcm)
            if voiced:
                self.speech_ms += self.frame_ms
                if self.speech_ms >= 2 * self.frame_ms:  # 2フレーム続けて声が入ったら話し始め
                    self.started = True
                    self.pcm.extend(b"".join(self.preroll))
                    log.info("VAD: speech start (rms=%d)", rms)
            else:
                self.speech_ms = 0
            return False

        self.pcm.extend(pcm)
        if voiced:
            self.speech_ms += self.frame_ms
            self.silence_ms = 0
        else:
            self.silence_ms += self.frame_ms
        duration_ms = len(self.pcm) // 2 * 1000 // self.rate
        return self.silence_ms >= VAD_SILENCE_MS or duration_ms >= VAD_MAX_SPEECH_MS

    def has_speech(self) -> bool:
        return self.started and self.speech_ms >= VAD_MIN_SPEECH_MS


def _start_listening(device: Device):
    device.state = "listening"
    device.listener = Listener(device.in_rate, device.in_frame_ms)


def _on_listen(device: Device, data: dict):
    state = data.get("state")
    if state == "start":
        device.listen_mode = "manual" if data.get("mode") == "manual" else "auto"
        if device.state != "thinking":
            _start_listening(device)
    elif state == "stop":
        # manual モード（押して話す）では、これが話し終わりの合図
        if device.state == "listening":
            _finish_utterance(device)
        if device.state == "listening":
            device.state = "idle"


def _on_audio(device: Device, packet: bytes):
    if not CONVERSATION_ENABLED or device.state == "thinking":
        return  # 返事を作っている間・喋っている間のマイクの音は使わない
    if device.state == "idle":
        # listen start が来ないまま音声が届いたときも、聞き始める
        log.info("Audio without listen start: %s → start listening", device.device_id)
        _start_listening(device)
    if device.listener.feed(packet) and device.listen_mode == "auto":
        _finish_utterance(device)


def _finish_utterance(device: Device):
    """話し終わった音声を、返事を作るタスクに渡す"""
    listener = device.listener
    device.listener = None
    if not CONVERSATION_ENABLED or listener is None or not listener.has_speech():
        if device.state == "listening":
            _start_listening(device)  # 短すぎる音は捨てて、聞き直す
        return
    device.state = "thinking"
    device.task = asyncio.create_task(_converse(device, bytes(listener.pcm)))


async def _transcribe(pcm: bytes, rate: int) -> str:
    """① 音声認識: PCM を WAV にして Azure OpenAI に送る"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    r = await _stt_client.audio.transcriptions.create(
        model=AOAI_STT_DEPLOYMENT,
        file=("speech.wav", buf.getvalue(), "audio/wav"),
        language="ja",
    )
    return r.text


async def _chat_stream(history: list[dict]) -> AsyncIterator[str]:
    """② 返事を作る: Chat の返事を少しずつ受け取る"""
    stream = await _chat_client.chat.completions.create(
        model=AOAI_CHAT_DEPLOYMENT,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, *history],
        stream=True,
    )
    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


EMOTION_TAG = re.compile(r"^\s*\[(\w+)\]\s*")
SENTENCE_END = re.compile(r"[。！？!?\n]")


def _pop_sentence(buf: str, final: bool = False) -> tuple[str | None, str]:
    """溜まったテキストから1文を切り出す。まだ文が終わっていなければ None"""
    m = SENTENCE_END.search(buf)
    if m:
        return buf[: m.end()].strip(), buf[m.end():]
    if len(buf) > 60 and "、" in buf:  # 長い文は読点で区切って、早く喋り始める
        i = buf.rfind("、")
        return buf[: i + 1].strip(), buf[i + 1:]
    if final and buf.strip():
        return buf.strip(), ""
    return None, buf


async def _produce_reply(device: Device, queue: asyncio.Queue, t: dict) -> str:
    """② の返事を文ごとに区切り、③ VOICEVOX で声にしてキューに入れる"""
    speaker = ZUNDAMON_STYLES.get(CHAT_STYLE, ZUNDAMON_STYLES["ノーマル"])
    reply = ""
    buf = ""
    tag_checked = False

    async def emit(sentence: str):
        nonlocal reply
        if not sentence:
            return
        reply += sentence
        t.setdefault("sentence", time.monotonic())
        pcm = await asyncio.to_thread(_voicevox_synthesize, sentence, speaker, CHAT_SPEED)
        packets = await asyncio.to_thread(_encode_opus, pcm)
        t.setdefault("voicevox", time.monotonic())
        await queue.put((sentence, packets))

    try:
        async for delta in _chat_stream(device.history):
            t.setdefault("llm", time.monotonic())
            buf += delta
            if not tag_checked:
                # 返事の先頭の [happy] などを表情に使う
                head = buf.lstrip()
                if head.startswith("[") and "]" not in head and len(head) < 20:
                    continue  # タグの途中なので、もう少し待つ
                m = EMOTION_TAG.match(buf)
                if m:
                    buf = buf[m.end():]
                    if m.group(1) in EMOTIONS:
                        await _send([device], {"type": "llm", "emotion": m.group(1)})
                tag_checked = True
            while True:
                sentence, buf = _pop_sentence(buf)
                if sentence is None:
                    break
                await emit(sentence)
        sentence, buf = _pop_sentence(buf, final=True)
        if sentence:
            await emit(sentence)
        return reply
    finally:
        await queue.put(None)  # 終わりの合図


async def _converse(device: Device, pcm: bytes):
    """話し終わった音声に、ずんだもんの声で返事をする"""
    t = {"end": time.monotonic()}  # 処理ごとの時刻（話し終わりからの経過時間をログに出す）
    try:
        seconds = len(pcm) / 2 / device.in_rate
        text = (await _transcribe(pcm, device.in_rate)).strip()
        t["stt"] = time.monotonic()
        log.info("STT: %s: %r (%.1fs の音声)", device.device_id, text, seconds)
        if not text:
            return
        await _send([device], {"type": "stt", "text": text})  # 自分の発言を画面に出す
        device.history.append({"role": "user", "content": text})

        queue: asyncio.Queue = asyncio.Queue()
        producer = asyncio.create_task(_produce_reply(device, queue, t))

        async def sentences():
            while (item := await queue.get()) is not None:
                yield item

        try:
            await _speak_stream(device, sentences(), on_first_packet=lambda: t.setdefault("voice", time.monotonic()))
        finally:
            if not producer.done():
                producer.cancel()
        reply = await producer
        log.info("Reply: %s: %r", device.device_id, reply)
        if reply:
            device.history.append({"role": "assistant", "content": reply})
        del device.history[:-HISTORY_MAX]

        def since_end(key: str) -> str:
            return f"{t[key] - t['end']:.2f}s" if key in t else "-"

        log.info(
            "TIMING: 音声認識 %s / 返事の最初の文字 %s / 最初の1文 %s / VOICEVOX %s / 声が出るまで %s",
            since_end("stt"), since_end("llm"), since_end("sentence"), since_end("voicevox"), since_end("voice"),
        )
    except asyncio.CancelledError:
        log.info("Conversation cancelled: %s", device.device_id)
        raise
    except Exception:
        log.exception("Conversation failed: %s", device.device_id)
    finally:
        # 喋り終わったら聞き直す（スタックチャンからの listen start でも聞き直す）
        if device.state == "thinking":
            if device.listen_mode == "auto":
                _start_listening(device)
            else:
                device.state = "idle"
