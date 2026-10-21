# 曲単位の音量正規化の検証

## 方針と参考実装

`~/github/shortcuts_app/gemini_music_to_audio_pyav.py`を参考にした。
同ファイルはPyAVの`ebur128`でIntegrated LUFS・True Peakを計測し、
−14 LUFSを目標、True Peak上限−1 dBTPとして、両方を満たす範囲の固定ゲインを選んでいた。
FFmpeg CLI版の`gemini_music_to_audio.py`も同じゲイン方針だった。
参考リポジトリのファイルは変更していない。

今回も曲ごとに固定ゲインをPCM自体へ適用する。ReplayGainやSound Checkタグだけの処理ではない。
曲内のダイナミクスを変えるコンプレッサーやリミッターは使わず、
ピーク制約でLUFS目標に届かない場合は未達として記録する。
既定値は−14 LUFS・True Peak上限−1 dBTP、LUFS目標到達の許容差は±0.1 LU。
ゲイン変更後の16-bit化にはTPDFディザを使う。0 dBの処理はコピーし、無音にノイズを足さない。

## 実装と依存

- `src/diskflow/loudness.py`：ストリーミングLUFS・True Peak・sample peak計測
- `src/diskflow/normalize.py`：固定ゲインの計算、適用、WAV再計測、レポート
- `src/diskflow/wavs.py`：無加工/正規化済みWAVの形式・ハッシュ・処理履歴の検証
- `src/diskflow/aac.py`：正規化済みWAVのAAC化と、デコード後の音量・ピーク確認

PyAV 18.1.0をuvで管理する。計測は`ebur128`と`astats`、ゲイン適用はdouble精度の`volume`、
16-bit化はlibswresampleの三角分布ディザを使う。
PyAVのwheel同梱FFmpegライブラリを使用し、FFmpeg/ffprobe CLIやHomebrewパッケージは追加しない。
CD読み取り・未調整AACの処理ではPyAVを読み込まない。

## 末尾と計測精度

参考実装と同じメタデータ取得を試したところ、100 ms未満の末尾にあるピークが抜けるケースを確認した。
1.013秒の音声で最後の8フレームだけ振幅を大きくすると、通常の最終メタデータではピーク0だった。
解析時だけ100 ms境界までの無音と追加の100 msを補うと、True Peak約+0.407 dBTPを検出できた。

この対策を実装し、保存するWAVには無音を追加しない。
Integrated LUFSには、無音追加前に得られた最後の100 ms境界の値を使う。
400 ms未満・絶対ゲート以下など、LUFSを有効に計測できない音声は持ち上げない。
その場合でも末尾を含むピークを計測し、必要なら減衰させる。

`ebur128`のTrue Peakメタデータは線形振幅で小数3桁に丸められるため、
丸め値へ0.001を加えた上側推定とsample peakを用いて制約を確認する。
さらに既定で0.05 dBの余裕を取り、出力WAVを再計測する。
これは採用したメーターに基づく保守的な推定であり、任意の再生機器でのアナログピーク保証ではない。
計測不能値はJSONで`null`にし、NaN/Infinityを成功レポートに出さない。

## 再現コマンド

```sh
mise run normalize -- artifacts/wav-20261021-072913 --output artifacts/normalized-20261021-135024
mise run encode -- artifacts/normalized-20261021-135024 --output artifacts/aac-normalized-20261021-135108
```

上の出力先は取得済みのため、再実行時には別の出力先を指定する。
既存の音声イメージ、無加工WAV、音量未調整のAACはすべて残している。

## 全13曲での結果

2026-10-21、macOS 27.0.1 / arm64、Python 3.14.5、PyAV 18.1.0で実行した。

| 項目 | 結果 |
| --- | --- |
| 元WAVのIntegrated LUFS | −9.968〜−6.603 LUFS |
| 適用した固定ゲイン | −7.397〜−4.032 dB。今回は全曲が減衰 |
| 正規化WAVのIntegrated LUFS | −14.000〜−13.998 LUFS |
| AACデコード後のIntegrated LUFS | −14.031〜−14.009 LUFS |
| WAVのTrue Peak上側推定の最大値 | 約−3.466 dBTP |
| AACデコード後のTrue Peak上側推定の最大値 | 約−2.963 dBTP |
| LUFS目標到達 | WAV・AACとも全13曲が±0.1 LU以内 |
| ピーク制約 | WAV・AACとも全13曲が上限−1 dBTP以下 |
| フレーム数合計 | 156,201,612。無加工WAV・正規化WAV・AACデコード結果で一致 |
| 正規化処理 | 約38.8秒 |
| AAC化と検証 | 約42.0秒 |
| AACサイズ合計 | 117,647,577バイト、約118 MB |
| 入力保護 | 元イメージ・全13曲の無加工WAVのSHA-256が処理前と一致 |

AACは前と同じApple AAC 256,000 bps指定・CVBR・quality 127で、曲名やジャケットはまだ未設定。
音質の主観評価やMusic.appとの厳密な出力比較は行っていない。
音量を揃えても、元のCDの読み取り品質を新たに保証するものではない。

## AAC化後のピーク確認

AAC化によるピークの増加もあるため、正規化WAVの計測だけで処理を終えない。
Appleの`afconvert`でAACをfloat32 CAFへ全体デコードし、その波形をPyAVで再計測する。
整数PCMへデコードするとオーバーシュートがクリップされ得るため、この確認には使わない。

デコード後が設定のピーク上限を超えた場合は、AACの確定名への変更をせず失敗を記録する。
その場合は元の無加工WAVから、例えば`--peak-headroom 1.0`で正規化をやり直す。
この余裕はWAV側のゲインを保守的にする値で、AACのピーク上限そのものは−1 dBTPのまま。
調整済みWAVの再正規化やAACの再エンコードは行わない。

無音やピーク制約のため未達の曲は、`target_reached: false`と理由を残す。
`normalized`や`loudness_normalized: true`はこのポリシーを適用したことを示し、
全曲が必ず−14 LUFSになるという意味ではない。全曲の到達状況は`all_targets_reached`で区別する。
ピーク制約なしでもゲーティングなどで再計測値が目標から外れた場合は、
`measurement_target_miss`として区別し、達成したとは記録しない。

## 取得物

- `artifacts/normalized-20261021-135024/`：音量調整済みWAV、`report.json`、`validation-summary.json`
- `artifacts/aac-normalized-20261021-135108/`：音量調整済みAAC、`report.json`、`commands.jsonl`

ファイル名は各フォルダとも`track-01`〜`track-13`。音声・端末固有ログはGit管理対象外。
pre-emphasisの補正、曲名・ジャケット取得、Notionアップロードは引き続き未実装。
実CDとの全曲PCM比較など、読み取り側の追加検証も残っている。
