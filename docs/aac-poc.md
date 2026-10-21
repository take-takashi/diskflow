# Apple AAC変換の検証

## 初回の音量未調整AAC

この記録は初回の未調整AAC変換について述べる。
その後に追加した正規化とAAC化後のTrue Peak検証は[音量正規化の検証](loudness-poc.md)を参照。

2026-10-21、保存済みの全13曲のWAVを、macOS標準の`afconvert`でAACのM4Aへ変換した。
入力WAVと元のCDイメージは変更していない。追加のPythonパッケージ・Homebrewパッケージや
FFmpegは使用していない。

出力がAAC/M4A形式であること、有効フレーム数が元WAVと同じであること、
全曲を最後までデコードできて元と同じフレーム数になることを確認した。
この初回変換では、音質評価、LUFS正規化、True Peakの確認、曲名やジャケットの埋め込みは行っていない。
`loudness_normalized`、`metadata_tagged`、`archival_verified`はすべて`false`。

## 実行方法と設定

```sh
mise run encode -- artifacts/wav-20261021-072913 --output artifacts/aac-baseline-20261021-073952
```

上の出力先は取得済みのため、再実行時は新しい名前を指定する。
初回の入力は`split`が正常終了したWAV出力ディレクトリで、`report.json`と各WAVの
ファイルハッシュ・PCMハッシュ・形式・フレーム数を確認した。
現在のCLIは正規化済みWAVも受け付け、入力の処理履歴を区別する。

使用したエンコード条件は次のとおり。

```sh
/usr/bin/afconvert -f m4af -d aac -b 256000 -s 2 -q 127 -l Stereo INPUT.wav OUTPUT.m4a.part
```

256,000 bpsを指定したconstrained VBRであり、平均ビットレートを256,000 bpsに固定する設定ではない。
[Apple Digital Masters公式資料](https://www.apple.com/apple-music/apple-digital-masters/docs/apple-digital-masters.pdf)の
AAC変換例も`-b 256000 -q 127 -s 2`を使うため、iTunes Plus相当の主要設定と考えてよい。
ただしMusic.appプリセットとのバイト単位の一致は未検証で、
その厳密な等価性を示す`itunes_plus_equivalence_verified`は`false`のままとする。

## 出力の確認

各曲について`afinfo -x`のXMLを解析し、M4Aコンテナ・AAC・44.1 kHz・2チャンネルを確認する。
packet tableのvalid framesは元WAVと一致することを要求する。
primingとremainderは有効音声に加えず、各値とtotal framesの整合性を確認する。

その後、各曲を`afconvert -f WAVE -d LEI16`で一時WAVへデコードする。
一時WAVを最後まで読み、形式とフレーム数が元と一致するか確認する。
AACは非可逆圧縮なので、元PCMとのバイト一致は判定条件にしない。
正常終了時に一時WAVを削除し、M4Aを`.part`から確定名へ変更する。
エラー・中断時には失敗レポートと取得済みの途中ファイルを残す。

| 項目 | 結果 |
| --- | --- |
| 環境 | macOS 27.0.1 / arm64、mise管理のPython 3.14.5 / uv 0.13.1 |
| 曲数 | 13曲 |
| 出力サイズ合計 | 118,176,888バイト、約118 MB |
| 処理時間 | 約22.6秒。入力検証・変換・デコード確認を含む |
| 有効フレーム数合計 | 156,201,612。元WAV・packet table・デコード結果で一致 |
| afinfoの報告ビットレート | 曲ごとの平均で259,208〜276,681 bps |
| ツール呼び出し | エンコード13回・afinfo 13回・デコード13回、全39回が終了コード0 |
| 保存後のM4A | 全13ファイルのSHA-256を再計算し、レポートと一致 |
| 入力WAV | 全13ファイルのSHA-256が変換前と一致 |
| 元のCDイメージ | SHA-256は読み取り時から変化なし |
| 途中ファイル | 正常終了後の`.part`・一時デコードWAVは0件 |

`afinfo`は各曲で`cmap`、`cnpf`、`loud`、`ctlc`、`rgls`、`mkls`のWARNINGを出した。
内容はチャンネルレイアウトやラウドネス、マーカーなどのプロパティ取得失敗。
警告を含む実行結果を、XMLは`commands.jsonl`、解析した警告内容は`report.json`に記録した。
CLIはWARNINGを記録して処理を続け、ERRORは失敗として扱う。
今回確認した形式・フレーム数・全体デコードの条件は満たしたが、
これらの付加プロパティを含むMusic.appとの互換性比較はまだ行っていない。

## ローカルの取得物

`artifacts/aac-baseline-20261021-073952/`に、`track-01.m4a`〜`track-13.m4a`、
`report.json`、`commands.jsonl`がある。音声とログはGit管理対象外。
入力WAVは`artifacts/wav-20261021-072913/`にそのまま残している。

## 今後の処理

今回は無加工WAVから作った、音量未調整・曲情報未設定の検証用AAC。
pre-emphasisが有効または不明の曲は補正を実装するまで拒否し、
`pregap.wav`は原本側に保持してAAC化の対象外として記録する。

曲単位のLUFS計測・ゲイン適用と、AACをfloat PCMへ戻したTrue Peak検証は追加済み。
MusicBrainz/Appleからの曲情報・ジャケット取得と、再エンコードなしのタグ付けも追加済み。
今回の音量未調整AACは変更せず、[メタデータ設定の検証](metadata-poc.md)で正規化済みAACへ適用した。
正規化後のAACは元のイメージまたはWAVから再生成し、このAACを再エンコードしない。
タグの埋め込みはAACの再エンコードを避けて実装する。
CDとの全曲PCM比較・別条件での再読み取りは、引き続き実CDを使う別の検証として残っている。
