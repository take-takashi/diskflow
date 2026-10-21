# diskflow

macOSで音楽CDを読み取り、原本イメージ・検証記録・鑑賞用音声をローカル保存するCLIツール。
CD読み取りの検証、曲別WAV出力、曲単位の音量正規化、Apple AAC変換を対象とする。
MusicBrainz/Apple配信情報からの曲名・ジャケット取得と、再エンコードなしのM4Aタグ付けにも対応する。

PIONEER BDR-TD05で13曲のCD全体を2回読み取り、全バイトの一致を確認済み。
読み取り処理はPython標準ライブラリだけで動作した。ただし、原本保存品質は未検証。
環境・実行結果・制約は[実機検証結果](docs/cd-reading-poc.md)を参照。
全13曲のTrack GainとAAC化後の音量・True Peakも確認済み。
詳しくは[音量正規化の検証](docs/loudness-poc.md)を参照。

## 責務の範囲

`diskflow`はCDの読み取りと、ローカルでの検証・音声処理・タグ付けまでを担当する。
Notionへのアップロードやページの作成・更新は`shortcuts_app`側で扱い、このリポジトリには実装しない。
呼び出し側は出力された音声・画像と`report.json`・`album.json`を利用する。
`diskflow`はNotionの認証情報やAPIに依存しない。

## 環境管理

- `mise.toml`でPython 3.14.5とuv 0.13.1を固定する。
- uvで`.venv`とPythonパッケージを管理し、`uv.lock`をコミットする。
- uvはmiseのPythonを使う。uvによる別のPythonのダウンロードは無効にする。
- CD読み取りの実装はPython標準ライブラリのみ。PyAVは音量解析・ゲイン適用に使う。
- PyAVもuvで管理する。wheel同梱のFFmpegライブラリを使い、FFmpeg CLIは起動しない。
- タグ編集はmutagen、画像検証はPillowを使う。どちらもuvで管理する。
- pytest・Ruffは開発用。

64-bit macOSとmiseが必要。Homebrewパッケージ、FFmpeg、cdrdao、コンパイラは
読み取りコマンドの実行に不要。
デバイスの調査にはmacOS標準の`diskutil`と`ioreg`を使う。
AAC変換と出力確認には、同じくOS標準の`afconvert`と`afinfo`を使う。
音声データは外部コマンドではなく、Pythonの`ctypes`と`fcntl.ioctl`で読み取る。

```sh
mise trust
mise install
mise run setup
mise run check
```

以降のコマンドも`mise run`で実行する。直接uvを呼ぶ場合は`mise exec -- uv ...`を使う。

## コマンド構成

| 用途 | コマンド |
| --- | --- |
| 一括取り込み・ローカル出力（mise専用） | `import` |
| デバイス・CD構造の確認 | `inspect` |
| 読み取り試験・原本イメージ保存 | `probe` / `rip` |
| 保存イメージと実CDのPCM比較 | `compare` |
| ローカルの音声出力・加工 | `split` / `normalize` / `encode` |
| 曲情報・画像の取得とタグ設定 | `metadata` / `tag` |

全体のヘルプは次のコマンドで確認できる。各サブコマンドも`--help`に対応する。

```sh
mise exec -- uv run --locked diskflow --help
```

一括処理は`mise run import`で実行する。工程別のコマンドも、検証や条件変更のために引き続き利用できる。
途中再開は未対応。失敗した出力を上書きせず、再試行には新しい出力先を指定する。

## 一括取り込み

### CDから取り込む

先に`mise run inspect`で音楽CDのデバイスを確認する。以下の`rdisk8`は例であり、番号を推測しない。
カバー画像を用意し、CDごとに新しい出力先を指定する。

```sh
mise run import -- --device /dev/rdisk8 \
  --output "$HOME/Music/CD Archive/album-01" \
  --artwork ./covers/limited.jpg
```

`--output`と`--artwork`は必須。出力先は任意の場所を選べるが、既存のファイルやフォルダは拒否する。
相対パスはmiseの作業ディレクトリ（通常はプロジェクトルート）を基準に解釈する。
別のアプリから呼び出す場合は絶対パスを推奨する。

曲情報は既定でMusicBrainzを使う。候補の選択は`--release-id`、Appleへの明示的な切り替えは
`--apple-album-id`を使う。`--edition`は任意の記録用ラベルで、カバーの自動判定には使わない。
オプション一覧はUsageの定義から生成される。

```sh
mise run import --help
```

### 保存済みイメージを使う

`--device`の代わりに`--capture`を指定すると、CDを再読み取りせず、完成済みripの内容をコピーして処理する。
以下のApple IDは今回のaikoのアルバム用であり、別CDには流用しない。

```sh
mise run import -- --capture artifacts/rip-20261020-231010 \
  --output "$HOME/Music/CD Archive/album-02" \
  --artwork ./covers/limited.jpg \
  --edition "初回限定盤" --apple-album-id 6813318544
```

`--device`と`--capture`はどちらか一方だけ指定する。
コピー対象は`audio.bin`・`toc.bin`・`report.json`と、存在する場合の`toc.json`・`disc.cue`・`reads.jsonl`。
入力フォルダの無関係なファイルはコピーしない。元のイメージと画像は変更しない。

### 出力先の構成

```text
<output>/
  report.json          全体の成否・工程別の結果・成果物一覧
  input-cover.jpg      開始時に保存した指定画像（PNGの場合は.png）
  capture/             原本イメージ・TOC・CUE・読み取りログ
  metadata/            曲情報・カバー・取得ログ
  wav/                 無加工の曲別WAV
  normalized/          音量調整済みWAV
  aac/                 タグ未設定のApple AAC
  tagged/              曲名・カバー付きの最終M4A、album.json、カバー
```

指定画像はCD読み取り前に検証してコピーし、長い取り込みの途中で元画像が変更されても同じ画像を使う。
処理順は原本保存またはコピー → 曲情報取得 → WAV切り出し → 音量調整 → AAC化 → タグ付け。
原本・中間ファイルを残すため、CD1枚につき数GB程度の空きを見込む。
`--passes`・`--retries`・`--chunk-sectors`はCDから直接取り込む場合だけ適用する。
音量条件は`--target-lufs`・`--true-peak`・`--peak-headroom`で指定できる。

### 全体レポートと外部ツールからの利用

進捗は標準エラーへ、全体レポートのJSONは標準出力と`<output>/report.json`へ出力する。
引数や入力を事前確認して失敗した場合は出力フォルダを作らず、標準出力にエラーのJSONを返す。
ただし、必須フラグ不足などUsageの解析エラーはmiseが処理するため、このJSONは返らない。

`schema_version: 1`、`operation: import`を持ち、`status`は`running`・`complete`・`failed`のいずれか。
`stages`には各工程の状態・レポートへの相対パス・レポートのSHA-256を保存する。
成功時の`result`には最終音声・画像・アルバム情報・原本の相対パス、サイズ、SHA-256をまとめる。
外部ツールは`status: complete`を確認し、`result`内のパスを出力フォルダと結合して成果物を取得できる。
工程別レポートの`source_directory`などには、従来通り実行時の絶対パスも残る。

失敗やCtrl-C時は全体を`failed`にし、未完了の成果物を`result`へ載せず、完了済み工程と途中の記録を残す。
終了コードは成功0、失敗1、Ctrl-Cによる中断130。強制終了時には`running`の記録が残る場合がある。
`complete`は全工程の完了を示し、LUFS目標の達成は`result.all_targets_reached`で別に確認する。
実CDとの全曲PCM比較は自動実行せず、`archival_verified`は常に`false`。
検証結果は[一括処理の検証](docs/workflow-poc.md)を参照。

## 工程別の実機検証手順

### 1. CDの構造を調べる

音楽CDを挿入し、Music.appなどによる再生・取り込みは止めておく。

```sh
mise run inspect -- --output artifacts/inspect-01.json
```

`/Volumes/*/.TOC.plist`、トラックファイルの名前とサイズ、IORegistry上のCD情報、
ディスク一覧をJSONに記録する。音声データはまだ読まない。
マウント先を指定して調べることもできる。

```sh
mise run inspect -- "/Volumes/Audio CD" --output artifacts/inspect-02.json
```

マウントが0件でも、未マウントのCDがあれば`cd_media`に現れる場合がある。
`cd_media`内の`BSD Name`やマウントの`disk_info`を確認し、CDのwhole diskを特定する。
`DeviceIdentifier`がパーティション（例：`disk8s1`）なら、
`ParentWholeDisk`などから親の`disk8`を確認する。番号を推測しない。

### 2. 少量のCDDAセクターを読む

以下の`rdisk8`は例。必ず調査結果で確認したCDデバイスに置き換える。

```sh
mise run probe -- /dev/rdisk8 --output artifacts/probe-01
```

TOCで分かる各曲の先頭・中央・末尾付近を1セクターずつ、最低2パス読み取る。
2パス目以降を最初のデータとバイト単位で比較し、短い読み取りも失敗として扱う。
probeの一致は、サンプル位置での読み取りが一致したことだけを示す。

対象は単一セッションの全曲オーディオCDのみ。
データトラックを含むCD、CD Extraなどの複数セッション、4チャンネルCDは拒否し、
読み取れた部分だけで取り込み成功とはしない。

### 3. 音声イメージを読む

probeが通ったら全体を読み取る。最低2パスなので、通常の1回の取り込みより時間がかかる。
出力用に約800 MB以上の空きを用意する。

```sh
mise run rip -- /dev/rdisk8 --output artifacts/rip-01
```

LBA 0からリードアウト直前までの2352バイトCDDAセクターを保存する。
全パスの全バイトが一致し、各パス終了時のTOCが変わっていない場合に限り、
`audio.bin.part`を`audio.bin`へ変更する。

読み取り単位がドライブに合わない場合などは、新しい出力先で条件を変えて試す。

```sh
mise run probe -- /dev/rdisk8 --output artifacts/probe-02 --passes 3
mise run rip -- /dev/rdisk8 --output artifacts/rip-02 --chunk-sectors 1 --passes 3
```

probeは常に1セクター単位。ripは`--chunk-sectors`で1〜75セクターに変更できる。
`--retries 2`はI/Oエラー・短い読み取りに対する追加の試行回数で、初回と合わせて最大3回。
パス間でデータが異なる場合は、多数決やゼロ埋めで補わず停止する。

デバイスは読み取り専用で開く。自動の権限昇格・アンマウント・排出はしない。
`Permission denied`はデバイスの読み取り権限の問題であり、それだけで方式が非対応とは判断しない。
権限エラー、非対応のioctl、I/Oエラーは区別して調べる。

### 4. cddafsと全曲のPCMを比較する

同じCDを挿入した状態で、保存済みイメージとcddafsの全曲PCMを比較する。
デバイス番号は使わず、現在のCDマウント先を明示する。

```sh
mise run compare -- artifacts/rip-01 --mount "/Volumes/Audio CD" --output artifacts/compare-01
```

保存イメージ・TOCのハッシュとサイズ、マウントされたCDのTOC、全トラックの番号・形式・PCM長を
確認してから、各曲の全バイトを比較する。AIFF/AIFF-CのCOMM・SSNDを解析し、
big-endianの無圧縮PCMはlittle-endianへ並べ替えて比較する。
不一致時は曲番号・最初の相違LBA・バイト位置をレポートに記録し、終了コード1を返す。
最初のINDEX 01より前の音声は比較対象外とし、対象外のセクター数をレポートに記録する。
これは同じドライブとOSを使う比較であり、独立したセキュアリッピングの検証ではない。

### 5. 保存済みイメージから曲別WAVを作る

CDを再読み取りせず、`audio.bin`とTOCから曲別WAVを生成する。
実行前に保存イメージのハッシュ・サイズを検証する。

```sh
mise run split -- artifacts/rip-01 --output artifacts/wav-01
```

`track-01.wav`から曲順に出力する。44.1 kHz・16-bit・ステレオのPCMをそのままコピーし、
音量変更・リサンプリング・ディエンファシス処理はしない。
出力後は各WAVを読み直し、形式・PCM長・PCMのSHA-256が元のイメージと一致するか確かめる。
確認が済むまでは`.part`で保持し、元のイメージと読み取り時のレポートは変更しない。

区切りはINDEX 01から次のINDEX 01またはリードアウトまで。
後続曲のプリギャップが前のファイル末尾に含まれる場合があり、INDEX 00単位の分割ではない。
最初のINDEX 01がLBA 0より後にある場合は、その手前を`pregap.wav`として残す。
負のLBAの音声は元のイメージに含まれず、復元しない。
pre-emphasisはレポートに記録するだけなので、該当するCDのWAVは補正済みの鑑賞用音声ではない。

WAV生成の成功は保存済みPCMを正確に切り出せたことを示し、CD読み取り品質の追加保証ではない。
全曲比較・別条件での読み取りなどの検証は別途必要。

### 6. 曲ごとに音量を正規化する

```sh
mise run normalize -- artifacts/wav-01 --output artifacts/normalized-01
```

既定値はIntegrated Loudness −14 LUFS、True Peak上限−1 dBTP。
各曲をPyAVの`ebur128`で測定し、タグではなくPCM自体に曲ごとの固定ゲインを適用する。
原本・無加工WAVは変更せず、新しい16-bit WAVを生成する。
ゲイン変更時の再量子化にはTPDFディザを使う。曲内のコンプレッションやリミッターは使わない。

ゲインはLUFS目標とピーク上限のうち厳しい方で決め、ピーク側には既定で0.05 dBの余裕を取る。
**ピーク制約で−14 LUFSに届かない場合は、そのまま未達として記録する**。
無音・短すぎる音声・ゲート以下の音声は無理に持ち上げない。
出力を再計測してピーク上限を確認し、LUFSは±0.1 LUを目標到達の判定に使う。
調整済みWAVの再正規化は拒否するので、条件を変える場合も無加工WAVから作り直す。

`--target-lufs`、`--true-peak`、`--peak-headroom`で条件を変えられる。
ピーク計測では100 ms未満の末尾が抜けないよう解析時だけ無音を補い、保存音声の長さは変えない。
測定方法・丸め精度・適用ゲイン・到達状況は`report.json`に残す。

### 7. 曲別WAVからAACを作る

```sh
mise run encode -- artifacts/normalized-01 --output artifacts/aac-normalized-01
```

正常終了したsplitまたはnormalizeの出力を入力にし、`afconvert`でApple AACのM4Aへ変換する。
設定は256,000 bps・constrained VBR（strategy 2）・quality 127。
[Apple公式のAAC変換例](https://www.apple.com/apple-music/apple-digital-masters/docs/apple-digital-masters.pdf)と
主要設定が同じで、iTunes Plus相当の設定。ただしMusic.app出力とのバイト一致は未検証。
CVBRなので平均ビットレートは256,000 bps固定ではない。

入力WAVの形式・ハッシュを確認し、変換後は`afinfo`でAAC/M4A・44.1 kHz・2チャンネル・
有効フレーム数を調べる。正規化済みWAVが入力なら、AACを最後までfloat32 CAFへデコードし、
LUFS・True Peakを再計測する。整数デコードでピーク超過を隠さず、−1 dBTPなどの指定上限を
超える場合は失敗とする。その場合は無加工WAVから`--peak-headroom`を増やしてやり直す。

無加工のsplit出力を直接encodeすることもできるが、それは音量未調整の検証用AAC。
この場合は整数WAVへの全体デコードとフレーム数の確認までで、True Peakは検証しない。
AACは非可逆圧縮なので、デコードしたPCMと元PCMのハッシュ一致は要求しない。
ツールの実行条件・結果は`commands.jsonl`に残し、正常時に一時デコードファイルを削除して
確認済みの`.m4a.part`を`.m4a`へ変更する。

pre-emphasisが有効または不明の曲は、補正処理ができるまでnormalize/encodeを拒否する。`pregap.wav`は処理対象外として記録し、元WAV側に保持する。
条件変更時もイメージ・無加工WAVから再生成し、既存AACを再エンコードしない。
実行結果は[AAC変換の検証](docs/aac-poc.md)と[音量正規化の検証](docs/loudness-poc.md)を参照。

### 8. 曲情報とジャケットを取得する

```sh
mise run metadata -- artifacts/rip-01 --output artifacts/metadata-01
```

TOCから標準ライブラリでMusicBrainz Disc IDを計算し、対応するリリースを照合する。
候補が複数あるときは勝手に選ばず、`candidates.json`を確認して`--release-id`で選択する。
ジャケットは選択したリリースのCover Art Archiveから取得する。
MusicBrainzへの問い合わせはUser-Agentを設定し、同じクライアント内で1.1秒以上の間隔を取る。

未登録CDでは、確認済みのAppleアルバムIDを明示するフォールバックがある。

```sh
mise run metadata -- artifacts/rip-01 --apple-album-id 6813318544 --output artifacts/metadata-02
```

このIDは今回のaikoのアルバム用であり、別CDに流用しない。
曲数・曲順・各曲の時間をTOCと比較し、時間差が2秒を超える場合は拒否する。
Appleの情報は配信版として記録し、物理CDの通常盤/初回盤やプレスを特定したとは扱わない。
出典URL・生のAPI応答・取得日時・画像ハッシュ・寸法・時間差を出力先に残す。
画像はJPEG/PNG、8 MiB以下、1600万画素以下に制限して検証する。

#### 初回盤などのジャケットを指定する

曲情報の取得元とは別に、使いたい画像と版のラベルを指定できる。
用意した画像を使う場合は、次のように実行する。

```sh
mise run metadata -- artifacts/rip-01 --apple-album-id 6813318544 \
  --edition "初回限定盤" --artwork ./covers/limited.jpg \
  --output artifacts/metadata-limited-01
```

`--artwork`の代わりに`--artwork-url HTTPSの画像URL`も使える。両方の同時指定はできない。
`--edition`は任意のラベルで、画像の自動検索・版の自動判定をするフラグではない。
`--edition`だけの指定は拒否し、画像の取得や検証に失敗しても配信版などへ切り替えない。
TOC/Disc IDでは、同じ音楽CDを同梱した通常盤・初回盤のパッケージを区別できないためである。

曲情報の出典はそのまま保持し、版のラベルはユーザー指定として別に記録する。
画像の出典・選択方法・ハッシュも`album.json`とM4Aの独自タグに保存する。
ローカル画像の絶対パスは`report.json`だけに残し、M4Aには埋め込まない。
次のtagには、ここで作った`metadata-limited-01`を指定する。

### 9. 曲名とジャケットをAACへ埋め込む

```sh
mise run tag -- artifacts/aac-normalized-01 --metadata artifacts/metadata-02 --output artifacts/tagged-01
```

元AACをコピーして、mutagenで曲名・アーティスト・アルバム名・アルバムアーティスト・日付・
曲番号・ディスク番号・ジャケットなどを設定する。ファイル名も`01 曲名.m4a`の形にする。
元AACは変更せず、元のエンコーダー情報やgapless情報を含む未知のタグは消去しない。

タグ付け前後でAACの全パケット、コーデック設定、PTS/DTS・duration・time baseを比較する。
それらが同じであることを確かめてから確定名にするので、音声の再エンコードは行わない。
保存時のレポートのハッシュ連鎖をたどり、メタデータのTOCハッシュ・曲ごとのフレーム数が
このAACの取得元CDと一致することも確認する。参照先の`report.json`は削除せず残しておく。
タグとジャケットは保存後に再読し、値と画像バイト列の一致を確認する。
音量の計測値はパケット不変の確認を根拠に親レポートから引き継ぎ、再計測とは区別して記録する。
実行結果・配信版から初回盤ジャケットへの差し替えは[メタデータ設定の検証](docs/metadata-poc.md)を参照。

## 出力と結果の読み方

出力先は毎回新しいディレクトリを指定する。既存のディレクトリや調査JSONは上書きしない。
`artifacts/`はGit管理対象外。音声データや端末固有の調査情報をコミットしない。

| ファイル | 内容 |
| --- | --- |
| `toc.bin` | macOSのformat 0x02 TOCバイナリ |
| `toc.json` | セッション、トラック、INDEX 01開始LBA、リードアウト |
| `reads.jsonl` | 各パス・LBA・試行回数・読み取り結果・SHA-256 |
| `report.json` | 実行条件、状態、パスごとのハッシュ、失敗理由 |
| `audio.bin` | ripで読み取りが一致したCDDA user data |
| `disc.cue` | ripのイメージに対するINDEX 01中心の暫定CUE |
| `samples.bin` / `sample-map.json` | probeのサンプルと元のLBA・保存位置の対応 |
| `track-01.wav`など | splitの無加工WAV、またはnormalizeの音量調整済みWAV。出力先は別々 |
| `pregap.wav` | 最初のINDEX 01より前にある非負LBAの音声。該当する場合のみ |
| `track-01.m4a`など | encodeのAAC。音量調整の有無は入力とレポートで区別する |
| `album.json` / `cover.jpg`など | 取得したアルバム・曲情報、出典、検証済みジャケット |
| `requests.jsonl` / `candidates.json` | メタデータ問い合わせの結果、MusicBrainzの候補 |
| `01 曲名.m4a`など | tagで曲情報・ジャケットを埋め込んだAAC |
| `commands.jsonl` | encodeで使用したOS標準ツールの実行条件と結果 |

rip/probeの`report.json`の`status: reads_matched`は反復読み取りの一致を示す。
compareは`pcm_matched`、splitは`wav_written`、normalizeは`normalized`、encodeは`aac_encoded`を記録する。
正規化は`loudness_normalized`、全曲のLUFS目標到達は`all_targets_reached`で区別する。
`normalized`はピーク優先ポリシーを適用できたことを示し、全曲のLUFS到達までは意味しない。
メタデータ取得は`metadata_ready`、タグ付けは`tagged`で正常終了する。
`aac_encoded`だけで、正規化や音質評価まで完了したと判断しない。
それぞれ新しい出力先の`report.json`に、比較範囲や曲ごとのハッシュなどを保存する。
`archival_verified`は常に`false`で、ビット完全な原本保存を保証する結果ではない。
失敗・Ctrl-C時は`status: failed`として、途中の`.part`と取得済みの記録を残す。
強制終了などで記録を更新できなかった場合は`running`のまま残る。
終了コードは正常終了時が0、エラー時が1、中断時が130。
この段階では途中再開に未対応なので、再試行には別の出力先を使う。

## 読み取り方式

通常の`open('/dev/rdiskN').read()`がCD音声を返すとは仮定しない。
macOSのCD専用ioctlをPythonから呼び出す。

- `DKIOCCDREADTOC`でfull TOCを取得し、`plistlib`によるマウント情報の調査と切り分ける。
- `DKIOCCDREAD`で`kCDSectorTypeCDDA`＋`kCDSectorAreaUser`を指定する。
- オフセットは`LBA × 2352`バイト。2048バイトのデータCDとは区別する。
- バッファの実転送量を確認し、短い読み取りを正常終了として扱わない。

`.TOC.plist`の`Start Block`と`Leadout Block`は絶対MSFをフレーム数にした値で、
LBAへの変換には150を引く。例えば`Start Block = 150`がLBA 0。
一方、binary full TOCはMSFからLBAへ直接変換する。CUEの位置はイメージ先頭からの相対位置。

macOSのcddafs実装に合わせ、音声を44.1 kHz・16-bit・ステレオのlittle-endian PCMとして扱う。
cddafsの拡張子`.aiff`はAIFF-Cの`sowt`の場合があり、
ヘッダーがbig-endianだから音声もbig-endianとは限らない。
compareは固定バイト数のヘッダー除去ではなく、FORM・COMM・SSNDを解析する。

ABIはApple SDKの`IOCDMediaBSDClient.h`・`IOCDTypes.h`・`sys/ioccom.h`を参照した。
開発環境ではSDKから構造体サイズ・フィールド位置・ioctl定数を確認済み。
実行時にはSDKを使わず、定義済みの64-bit ABIを利用する。

## 原本保存に向けた未検証項目

- ドライブキャッシュを無効化していないため、複数パスが独立した物理読み取りとは限らない。
- C2エラーフラグ、ジッター補正、読み取りオフセット補正、AccurateRip照合は未実装。
- 保存するのは2352バイトの音声部分のみ。サブチャンネル・CD-TEXTなどは含まない。
- TOCだけでは各曲のINDEX 00や全INDEXを復元できず、CUEは完全ではない。
- LBA 0より前のプリギャップ、リードイン、リードアウト本体は保存しない。
- pre-emphasisフラグはCUEに残すが、音声にはディエンファシス処理をしない。
- 同じTOCの別ディスクへの交換までは検知できない。実行中はCDを交換しない。

今回の実機検証では、cddafsのAIFF-C形式・PCM長を全13曲で確認し、
各曲の先頭・中央・末尾付近のPCMが保存イメージと一致した。
全曲PCM比較のCLIを追加済みだが、実CDでの全曲比較はまだ未確認。
保存済みイメージからの全13曲のWAV生成と、出力PCMの一致は確認済み。
次はCDを再挿入しての全曲PCM比較、別チャンクサイズでの比較、
傷のあるCDでの失敗検知を行う。
必要ならcdrdaoの取り込み結果とも比較する。
読み取りが成立しても検証品質が足りなければ、Pythonのみの実装には固執しない。
このPoCだけを根拠にCD原本を処分しない。

## 開発

```sh
mise run check     # 短い合成音声/モックによるテスト、lint、フォーマット確認
mise run format    # フォーマットとlintの自動修正
```

テストは実デバイスを開かない。
TOC解析、ABI、2352バイト単位のオフセット、短い読み取り、再試行、パス間不一致、
TOC変更、中断、上書き拒否を確認する。
比較・WAV出力についても、AIFF-Cのヘッダー・オフセット・バイトオーダー、全バイト比較、
入力のハッシュ検証、WAVの読み直し、プリギャップの保存、途中失敗をテストする。
AAC変換のテストではOSツールをモックし、コマンド条件・エラー・タイムアウト、
コーデック・有効フレーム数・priming/padding、元WAVの保護を確認する。
音量処理は実際のPyAVで合成音を処理し、固定ゲイン、ピーク制約、末尾ピーク、無音、
フレーム数の維持、入力保護、AAC化後のピーク超過の拒否を確認する。
メタデータのネットワークはモックし、Disc ID、候補選択、時間照合、画像の検証を行う。
タグ付けは短いAACをPython内で生成し、タグ・画像の再読、パケット/時刻情報の不変、入力保護を確認する。

## 参照

- [Apple IOCDMediaBSDClient.h](https://github.com/apple-oss-distributions/IOCDStorageFamily/blob/main/IOCDMediaBSDClient.h)
- [Apple IOCDTypes.h](https://github.com/apple-oss-distributions/IOCDStorageFamily/blob/main/IOCDTypes.h)
- [Apple cddafsのTOC生成処理](https://github.com/apple-oss-distributions/cddafs/blob/main/FilesystemProber/cddafs_util.cpp)
- [Apple cddafsのAIFF-Cヘッダー生成処理](https://github.com/apple-oss-distributions/cddafs/blob/main/AIFFSupport.c)
