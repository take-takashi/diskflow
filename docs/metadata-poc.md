# 曲名・ジャケット設定の検証

## 対象と採用した情報

2026-10-21、音量正規化済みの全13曲に曲名・ジャケットを設定した。
対象はaiko『最後に熱い日々を狂わしき想いで』。
元のCDイメージ、無加工WAV、正規化WAV、タグ未設定AACはいずれも変更していない。
Music.appのライブラリへの登録やNotionへのアップロードは行っていない。
最初の出力は配信版ジャケットを使った。ユーザーから初回限定盤との指定があり、
後述の「初回限定盤ジャケットへの差し替え」で別の出力を作成した。

保存済みTOCから計算したMusicBrainz Disc IDは次の値だった。

```text
Y9s8NdRjjb.sQovlAPRfnec7gS0-
```

このDisc IDへのMusicBrainz問い合わせはHTTP 404で、未登録だった。
そこで、アーティスト公式の曲目とApple公式配信情報を照合し、
AppleのアルバムID`6813318544`を明示して取得した。
全13曲の順番と曲名は一致し、再生時間はTOCからの計算値に対して最大約0.333 msの差だった。
Appleの時間はミリ秒整数なので、この差は丸めの範囲内。
今回使用した情報は配信版であり、物理CDの通常盤/初回盤やプレスを判別した結果ではない。

- [Appleのアルバムページ](https://music.apple.com/jp/album/6813318544)
- [Apple lookup API](https://itunes.apple.com/lookup?id=6813318544&entity=song&country=JP&limit=200)
- [アーティスト公式の収録内容](https://www.aiko.com/topics/?id=8027)

## 再現コマンド

```sh
mise run metadata -- artifacts/rip-20261020-231010 --apple-album-id 6813318544 \
  --output artifacts/metadata-20261021-142303
mise run tag -- artifacts/aac-normalized-20261021-135108 \
  --metadata artifacts/metadata-20261021-142303 --output artifacts/tagged-20261021-142308
```

これらの出力先は取得済みなので、再実行時は新しい名前を使う。
MusicBrainz登録済みのCDでは、`--apple-album-id`を省いてDisc IDから照合できる。
複数のリリース候補がある場合は自動で決めず、`--release-id`で選択する。
登録済みMusicBrainz経路はモックでテストしているが、今回の実データはApple経路で取得した。

## 設定したタグ

| 項目 | 設定値 |
| --- | --- |
| アーティスト / アルバムアーティスト | aiko |
| アルバム | 最後に熱い日々を狂わしき想いで |
| 日付 | 2026-10-21 |
| ジャンル | J-Pop |
| 曲番号 | 1/13〜13/13 |
| ディスク番号 | 1/1 |
| Copyright | ℗ 2026 PONY CANYON |
| ジャケット | Apple配信版、JPEG、1200×1200、543,242バイト |

曲名は「君の言う通り」「淡い」「Cry High Fly」「Darling」「カプセル」「あなたの花の色」
「グミ」「あせとなみだ」「桃水色」「忘れ荘」「シネマ」「青春朱夏」「マイスイートピアノ」。
ファイル名も`01 君の言う通り.m4a`〜`13 マイスイートピアノ.m4a`にした。

ジャケットの出典は次のApple CDN URLで、受信したバイト列を変換せず埋め込んだ。

```text
https://is1-ssl.mzstatic.com/image/thumb/Music211/v4/cc/bb/d2/ccbbd243-dd6f-4913-cb2d-ff86f7377e70/28897.jpg/1200x1200bb.jpg
```

画像のSHA-256は次の値で、全13ファイルの`covr`を再読して同じ画像が入っていることを確認した。

```text
54623522cee7847aa859b8bd71e4e0bc90e06b1cec749715d5ecea51fc99046c
```

## 再エンコードしていないことの確認

タグ編集にはuv管理のmutagen 1.48.1を使い、画像検証にはPillow 12.3.0を使った。
タグ編集はコピー先だけに行い、既存のエンコーダー情報など、変更対象外のタグは保持する。

PyAVで全AACパケットを取り出し、書き込み前後で次の値が全13曲とも一致した。

- パケット数と、長さ付きのパケットバイト列のSHA-256
- PTS・DTS・duration・time baseのSHA-256
- コーデック設定のSHA-256、サンプルレート、チャンネル数、ストリームの長さ

全タグ・ジャケットをmutagenで再読し、設定値が保存されていることも確認した。
タグ付け後のファイル自体のSHA-256もレポートと照合し、元AAC全13ファイルのSHA-256は不変だった。
出力M4Aの合計サイズは124,853,010バイトで、増加分には各曲へのジャケット埋め込みが含まれる。
音声・時刻情報が同じなので、親レポートのLUFS/True Peak測定値を引き継いだ。
これはタグ付け後に再計測したという意味ではなく、`loudness_metrics_inherited`で区別する。

## 検証と取得物

メタデータは曲数・曲順・各曲の時間をTOCと照合する。時間差の許容値は2秒で、今回は全曲が0.334 ms未満。
AAC側は入力ファイルのハッシュ、元のレポートのハッシュ連鎖、取得元TOCのハッシュ、
各曲のフレーム数を検証し、別CDのメタデータを誤って適用しないようにしている。
画像はJPEG/PNG・8 MiB以下・1600万画素以下を条件に検証する。

- `artifacts/metadata-20261021-142303/`：`album.json`、`cover.jpg`、Apple API応答、取得ログ
- `artifacts/tagged-20261021-142308/`：曲名付きM4A、`album.json`、`cover.jpg`、検証レポート
- `artifacts/tagged-20261021-142308/validation-summary.json`：保存後のタグ・画像・音声不変の確認結果

音声・画像・端末固有ログはGit管理対象外。出典・版の選択と検証結果の要点だけをこの文書に残した。
MusicBrainz未登録CDの自動推定は行わず、Appleフォールバックには確認済みIDの指定を要求する。
CD全曲PCM比較、別条件での読み取りなどは引き続き残っている。
Notion連携は`shortcuts_app`側で扱い、`diskflow`はローカル出力までを担当する。

## 初回限定盤ジャケットへの差し替え

公式の掲載画像では初回限定仕様盤A・Bが共通のジャケットで、通常仕様盤は別の画像だった。
[公式の形態別掲載](https://www.aiko.com/topics/?id=8027)を参照し、
アーティスト公式サイトの初回盤画像を指定した。
A/Bのどちらを所持しているかや品番・プレスまでは確定していない。

```sh
mise run metadata -- artifacts/rip-20261020-231010 --apple-album-id 6813318544 \
  --edition "初回限定盤（A/B共通ジャケット）" \
  --artwork-url https://www.aiko.com/img/news/images_pc/20260823/6a8b0122f18f1.jpg \
  --output artifacts/metadata-limited-20261021-144903
mise run tag -- artifacts/aac-normalized-20261021-135108 \
  --metadata artifacts/metadata-limited-20261021-144903 \
  --output artifacts/tagged-limited-20261021-144903
```

`--artwork-url`の代わりに`--artwork /path/to/cover.jpg`で手元のJPEG/PNGも指定できる。
画像のサイズ・形式・画素数の制限は自動取得と同じ。
`--edition`はユーザーが指定するラベルで、画像の検索条件ではない。
画像指定を必須にし、画像の取得・検証に失敗した場合も既定のカバーへ切り替えず停止する。
画像だけを指定し、版のラベルを省くことはできる。

曲情報はApple配信情報のまま保持し、ジャケットだけ公式の初回盤画像を使った。
`album.json`では曲情報の`source`と、ユーザー指定の`edition`、画像の`artwork`を分けて記録する。
`edition.basis: user-specified`は、TOCから版を特定した結果ではないことを示す。
M4Aにも`diskflow_edition`と`diskflow_artwork_source`の独自タグを追加し、
曲情報の取得元を示す既存の`diskflow_metadata_source`とは区別した。
アルバム名には版のラベルを付け足していない。

初回盤画像はJPEG・1000×1000・176,540バイト。拡大・再圧縮せず、そのまま埋め込んだ。
SHA-256は次の値。

```text
ffaf5e9a5a575c5812d844efbf8e5bf28ccb38299b8abf821cedf9b7095ce5d4
```

全13曲のタグ・版のラベル・出典・画像を保存後に再読し、指定値との一致を確認した。
AACの全パケット・コーデック設定・時刻情報も元AACと一致し、再エンコードは行っていない。
合計サイズは120,094,386バイト。LUFS/True Peakは音声不変を根拠に元の値を引き継いだ。
元AAC、以前の配信版ジャケット付きM4A全13曲、元CDイメージのハッシュが変わっていないことも確認した。
結果は`artifacts/tagged-limited-20261021-144903/validation-summary.json`に保存した。

単体テストではApple/MusicBrainz双方の画像上書き、ローカルJPEG/PNG、独自タグの再読、
画像不良やダウンロード失敗時に既定カバーへ切り替えないこと、旧形式メタデータとの互換性を確認した。
全222テスト、Ruff、miseのタスク定義検証、uvのロック確認が通った。
