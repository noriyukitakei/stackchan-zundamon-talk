# stackchan-zundamon-talk

[スタックチャン](https://github.com/m5stack/StackChan)に話しかけると、**ずんだもんの声で返事をする**サーバーです。

スタックチャン（M5Stack 版。AI 会話に [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) を使うファーム）の `OTA_URL` をこのサーバーに向けて動かします。
ファームのコードは変えず、`OTA_URL` の設定だけを変えて焼き直せば使えます。会話の処理はすべてサーバー側で行います。

- 話しかけた声を **Azure OpenAI** で文字にして、ずんだもんとして返事を作る
- 返事を **VOICEVOX** でずんだもんの声にして、スタックチャンから喋らせる
- 返事に合わせて、スタックチャンの**表情**も変わる
- HTTP API から、表情を変えたり、好きなテキストを喋らせたりもできる

> 「世界一わかりみの深いクラウドネイティブ on Azure」第39回
> [スタックチャンとずんだもんの声で会話する 〜Realtime API で作る音声対話〜](https://tech-lab.connpass.com/event/405532/) で紹介したコードです。

## 仕組み

```
              ① OTA_URL に問い合わせ → WebSocket の接続先を返す
スタックチャン ────────────────────────────────────────▶ server.py（このリポジトリ）
              ◀──────────── ② WebSocket でつながる ─────────
                                                          │
  ③ マイクの音声（Opus） ─────────────────────────────▶  │ ④ 音声認識   Azure OpenAI（gpt-4o-mini-transcribe など）
                                                          │ ⑤ 返事を作る Azure OpenAI（gpt-4o-mini など、ストリームで受け取る）
                                                          │ ⑥ 1文ずつ声にする VOICEVOX（ずんだもん）
  ⑧ ずんだもんの声（Opus、60ms ごと） ◀────────────── ⑦ │
```

| 段階 | 中身 |
|---|---|
| 接続 | スタックチャンは会話を始めるとき、まず `OTA_URL` に問い合わせます。返事に入っている WebSocket の接続先に、スタックチャンのほうからつないできます |
| 話し終わりの判定 | `listen` の `mode: auto` のときは、サーバーがマイクの音量（RMS）を見て判定します。`mode: manual`（押して話す）のときは、スタックチャンから届く `listen stop` を話し終わりとして扱います |
| 1文ずつ喋る | 返事をストリームで受け取り、「。」などで1文ずつ区切って VOICEVOX にかけます。1文目を喋っている間に、2文目を合成します |
| 表情 | 返事の先頭にモデルが `[happy]` のような表情のタグを付けます。それを `llm` メッセージとしてスタックチャンに送ります |
| 会話の履歴 | 直近 10 発言を覚えていて、話の流れを踏まえて返事をします（接続が切れるとリセットされます） |
| 途中で止める | 喋っている途中にスタックチャンから `abort` が届いたら、返事を止めます |

### Realtime API を使わない理由

Realtime API（Speech-to-Speech）の声はプリセットから選ぶ方式で、ずんだもんの声にはできません。
テキストで返事をさせて VOICEVOX に渡すこともできますが、そうすると Realtime API のいちばんの利点（声を直接返すことによる速さ）がほとんどなくなります。

そこで、**音声認識 → Chat → VOICEVOX** と普通の API を順番に呼ぶ構成にしました。
待ち時間は、返事をストリームで受け取り、1文ずつ声にすることで短くしています。

## 必要なもの

- スタックチャン（M5Stack 版）と、`OTA_URL` を書き換えてファームを焼ける環境（ESP-IDF）
- Python 3.10 以上
- [VOICEVOX](https://voicevox.hiroshiba.jp/)（アプリ、または Docker のエンジン）
- Azure OpenAI のリソースと、次の2つのモデルのデプロイ

| 用途 | 環境変数 | 例 |
|---|---|---|
| 音声認識 | `AZURE_OPENAI_STT_DEPLOYMENT` | `gpt-4o-mini-transcribe`（精度重視なら `gpt-4o-transcribe`） |
| 返事を作る | `AZURE_OPENAI_DEPLOYMENT` | `gpt-4o-mini` / `gpt-4.1-mini` など。会話のテンポを重視するなら、推論しない速いモデルがおすすめ |

スタックチャンとサーバーを動かすマシンは、同じ Wi-Fi（LAN）につないでください。

## セットアップ

```bash
git clone https://github.com/noriyukitakei/stackchan-zundamon-talk.git
cd stackchan-zundamon-talk
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Azure OpenAI の設定を `.env` に書きます。**`.env` には API キーが入るので、git に入れないでください**（`.gitignore` で除外してあります）。

```bash
cp .env.example .env
# AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY / AZURE_OPENAI_DEPLOYMENT などを書き換える
```

VOICEVOX を起動しておきます。

```bash
# VOICEVOX アプリを起動するか、Docker でエンジンだけ動かす
docker run --rm -p 50021:50021 voicevox/voicevox_engine:cpu-latest

# 動いているか確認
curl http://127.0.0.1:50021/version
```

## サーバーを起動する

スタックチャンからつなげるように、`0.0.0.0` で起動します。

```bash
.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000 --env-file .env
```

起動時のログに `Conversation: enabled` と出れば、会話ができる状態です。
`.env` が無い（または Azure OpenAI の設定が足りない）ときもサーバーは起動しますが、会話はできず、後述の HTTP API だけが使えます。

サーバーの IP アドレスを確認しておきます（macOS・Wi-Fi の場合）。

```bash
ipconfig getifaddr en0
# → 例: 192.168.1.23
```

ブラウザで `http://<サーバーのIP>:8000/docs` を開くと、HTTP API を画面から試せます。

## スタックチャンの OTA_URL を向ける

出荷時のファームの `OTA_URL` は、XiaoZhi の公式サーバー（`https://api.tenclass.net/xiaozhi/ota/`）を向いています。
これを、このサーバーに向けたファームをビルドして焼きます。ファームのビルド設定（`sdkconfig.defaults.local` など）に次のように書きます。

```
CONFIG_OTA_URL="http://<サーバーのIP>:8000/xiaozhi/ota/"
```

- サーバーの IP が変わるとつながらなくなります。ルーターで IP を固定しておくと安心です
- OTA の返事で受け取った WebSocket の接続先は、スタックチャンの中（NVS）に保存されます。公式の状態に戻すときは、焼く前にバックアップを取っておき、そこから書き戻してください

## 会話する

1. VOICEVOX とサーバーを起動する（ログに `Conversation: enabled`）
2. スタックチャンで AI エージェントを開始する。サーバーのログに `OTA check from ...` → `Handshake done: ...` と出れば接続完了
3. 話しかけると、ずんだもんの声で返事が返ってくる

サーバーのログには、話し終わってから声が出るまでの時間が、処理ごとに出ます。どこが遅いかを確かめるのに使えます。

```
STT: <device-id>: 'カレーにパイナップル入れるの、どう思う？' (2.1s の音声)
Reply: <device-id>: 'カレーにパイナップルはちょっと変わってるけど、好きな人もいるのだね'
TIMING: 音声認識 0.82s / 返事の最初の文字 1.31s / 最初の1文 1.45s / VOICEVOX 1.78s / 声が出るまで 1.90s
```

（数値は例です）

### 話し終わりがうまく判定されないとき

`.env` で調整して、サーバーを起動し直します。

| 環境変数 | 既定値 | 症状と調整 |
|---|---|---|
| `VAD_RMS_THRESHOLD` | 600 | 話していないのに反応する → 上げる。話しても反応しない → 下げる（ログの `VAD: speech start (rms=...)` が目安） |
| `VAD_SILENCE_MS` | 800 | 話の途中で区切られる → 長くする。返事が遅い → 短くする |

ちょうどいいしきい値は、部屋の静かさやマイクの位置で変わります。

## HTTP API

会話とは別に、接続中のスタックチャンを HTTP から操作できます。
WebSocket はスタックチャンのほうからつないでくるので、**操作できるのは会話を始めてから接続が切れるまでの間だけ**です。接続中のスタックチャンがいないときは `409` が返ります。

| API | 動き |
|---|---|
| `GET /devices` | 接続中のスタックチャンの一覧 |
| `POST /emotion` | 表情を変える |
| `POST /speech` | 吹き出しに文字を出す（音は出ない） |
| `POST /zundamon` | ずんだもんの声で喋らせる（VOICEVOX） |
| `POST /say` | macOS の `say` コマンドの声で喋らせる（macOS のみ。API キー不要） |

```bash
# 接続中のスタックチャンを確認
curl http://localhost:8000/devices

# 笑顔にする
curl -X POST http://localhost:8000/emotion \
  -H 'Content-Type: application/json' -d '{"emotion":"happy"}'

# ずんだもんで喋らせる
curl -X POST http://localhost:8000/zundamon \
  -H 'Content-Type: application/json' -d '{"text":"ぼくはずんだもんなのだ"}'

# スタイル・速さ・表情を指定する
curl -X POST http://localhost:8000/zundamon \
  -H 'Content-Type: application/json' \
  -d '{"text":"泣いてないのだ","style":"なみだめ","speed":1.2,"emotion":"sad"}'
```

`/zundamon` のパラメータ:

| パラメータ | 既定値 | 内容 |
|---|---|---|
| `text` | ぼくはずんだもんなのだ | 喋らせるテキスト |
| `style` | ノーマル | `ノーマル` / `あまあま` / `ツンツン` / `セクシー` / `ささやき` / `ヒソヒソ` / `ヘロヘロ` / `なみだめ` |
| `speed` | 1.0 | 話す速さ（0.5〜2.0 くらい） |
| `emotion` | なし | 喋る前に変える表情 |

使える表情: `neutral` / `happy` / `laughing` / `angry` / `sad` / `crying` / `sleepy` / `doubtful`
（`laughing` は `happy`、`crying` は `sad` と同じ顔です）

## 環境変数

`.env.example` に一覧があります。

| 環境変数 | 既定値 | 内容 |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | （必須） | Azure OpenAI のエンドポイント |
| `AZURE_OPENAI_API_KEY` | （必須） | Azure OpenAI の API キー |
| `AZURE_OPENAI_DEPLOYMENT` | （必須） | 返事を作る Chat モデルのデプロイ名 |
| `AZURE_OPENAI_API_VERSION` | `2024-10-21` | Chat の API バージョン |
| `AZURE_OPENAI_STT_DEPLOYMENT` | `gpt-4o-mini-transcribe` | 音声認識モデルのデプロイ名 |
| `AZURE_OPENAI_STT_API_VERSION` | `2025-03-01-preview` | 音声認識の API バージョン |
| `VOICEVOX_URL` | `http://127.0.0.1:50021` | VOICEVOX エンジンの URL |
| `ZUNDAMON_STYLE` | `ノーマル` | 会話の返事に使うずんだもんのスタイル |
| `ZUNDAMON_SPEED` | `1.0` | 会話の返事の話す速さ |
| `VAD_RMS_THRESHOLD` | `600` | 話し始め・話し終わりを判定する音量のしきい値 |
| `VAD_SILENCE_MS` | `800` | この長さ黙ったら話し終わりとみなす（ミリ秒） |

## ハマりどころ

- **ずんだもんが英単語を読み間違える**: 返事を作るのは AI なので、英単語が混ざると VOICEVOX が読み間違えることがあります（例: `Azure` → アジュア）。よく出る言葉は、VOICEVOX のユーザー辞書に読みを登録しておくと安心です。漢字1文字だけを登録すると、ほかの言葉まで読みが変わってしまうので、言い回しごと登録してください
- **英語と日本語の間の空白で、不自然な間が空く**: VOICEVOX は、英単語の前後の半角スペースや `-` `_` のところで区切って間を空けます。読ませる文章では、英語と日本語の間にスペースを入れないほうが自然に聞こえます
- **サーバーを再起動したら、会話を始め直す**: 音声の形式は、つないだときの `hello` で決まります
- **音声認識も聞き間違える**: 短い言葉（「じゃあね」など）は、別の言語として認識されることがあります

## クレジット

ずんだもんの音声を使うときは、**「VOICEVOX:ずんだもん」のクレジット表記が必要**です（イベントでのデモや、動画・SNS への投稿など）。
詳しくは [VOICEVOX の利用規約](https://voicevox.hiroshiba.jp/term/) と、ずんだもんの利用規約を確認してください。

## ライセンス

[MIT License](LICENSE)
