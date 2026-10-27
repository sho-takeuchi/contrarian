# 自動記録・分析の導入と運用

第1段階の月足と第2段階の週足を両方通過した銘柄を、自動で保存・追跡します。ユーザーは画面で結果を確認するだけで利用できます。

アプリ名は「銘柄の記録と分析（データを蓄積して分析する）」です。全体の目的は [README](../README.md)、手動のスクリーニングと詳細な判定式は [スクリーニングCLI・判定仕様](screening.md) を参照してください。

## 画面の見方

画面は `http://127.0.0.1:8501` で開きます。1分ごとに保存済みの結果を読み直します。

| 表示 | 内容 |
|---|---|
| 通過中 | 第2段階まで通過している銘柄と、通過開始日の終値からの騰落率 |
| 上昇して通過終了 | 月足または週足が不通過になった時点で、開始日より終値が上昇していた記録 |
| 下落して通過終了 | 同じ基準で、開始日より終値が下落していた記録 |
| 変化なし・要確認 | 騰落率0％、判定不能、未観測期間、必要株価の不足など |

「銘柄の記録を見る」で銘柄・通過期間を選ぶと、価格推移、終了理由、自動分析、根拠資料と取得状況を確認できます。銘柄の選択は保存済み情報の閲覧です。画面から分析・収集・DB更新を開始する操作はありません。

終了時の上昇・下落は、その後の株価で分類し直しません。価格訂正による再評価は変更前の記録と一緒に保存します。通過終了後も価格の取得を継続します。

## 自動処理

MacのLaunchAgent、Windowsのタスクスケジューラから15分ごとに確認します。営業日の18時以降、当日株価の公開を確認して全銘柄を第2段階まで判定します。未実行日のスクリーニング記録を後から作ることはありません。

1. 完成済みCSV・run.jsonを照合して取り込む。
2. 日ごとの最新の正常な全銘柄実行（第2段階以上）から、通過開始・継続・終了・再通過を再構築する。
3. 第2段階を一度でも通過した銘柄の株価を更新する。
4. 分割・併合を調整し、開始日から終了日・最新取得対象日までの騰落率を計算する。
5. 無料の企業開示・ニュースを収集して索引を作る。
6. 個別分析と全体比較を自動保存する。

未実行、銘柄の未観測、対象外、判定不能は明確な不通過ではありません。連続性が不明になった通過期間は「要確認」と表示し、上昇・下落の全体比較から除きます。月足が不通過になり週足が未対象となった場合は、月足の不成立条件を終了理由として記録します。

価格の基準は通過開始日の終値です。比較する価格は同じ調整基準にそろえます。配当・売買費用は含めません。始点・終点や途中の営業日レコードが不足する場合、前の日の株価や0％で代用しません。

## 自動分析と情報源

| 情報源 | 用途・準備 |
|---|---|
| J-Quants | 株価、出来高、契約範囲内の財務サマリー。既存のAPIキーを使用 |
| EDINET API v2 | 有価証券報告書・半期報告書・臨時報告書等。無料の利用登録とAPIキー発行が必要 |
| GDELT DOC API | 会社の英語名を使ったニュース検索。日本で発行された記事を対象とし、会社名・コードも照合 |
| Ollama / pgvector | PC内で資料を検索し、根拠を検証した日本語の説明を保存 |

情報源の公式案内：

- [J-Quants財務サマリー](https://jpx-jquants.com/ja/spec/fin-summary)
- [EDINET API資料](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0110.html)
- [EDINET API利用規約](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140191.pdf)
- [GDELT](https://gdeltproject.org/data.html)
- [GDELT DOC API仕様](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)

通過開始の直前30日から情報を収集し、毎日追加します。通過終了後7日間は遅れて取得される情報も確認します。GDELTで遡れる期間や取得件数には制限があり、上限に達した場合は一部取得として記録します。記事本文の収集は1回の定期処理あたり10件まで進め、残りは次回へ引き継ぎます。ニュースAPIが制限中・接続不可の場合は1時間空けて再試行します。

公開記事は取得許可を確認して本文を取得します。アクセス制限、本文の取得失敗、会社の同定不能、発表日時不明を区別します。GDELTの初回検出日時は発表日時として代用しません。EDINETは提出者の証券コードと書類種別を照合し、大量保有者の書類を提出者自身の銘柄材料として混同しません。

「通過終了の理由」は判定値・条件から計算します。「値動きの要因」は別に扱い、期間内に公表された本文・決算情報を根拠として示します。終了日の引け後の発表を、それ以前の値動きの原因として扱いません。

LLMの出力は資料ID、本文に完全一致する引用、材料の分類に制限します。引用元・引用文を検証し、株価や騰落率はPythonの計算値を使います。資料内の指示には従いません。根拠が足りない場合も「要因を特定できない」という結論を保存します。

全体比較は判定ルールごとに上昇終了群・下落終了群を集計し、通過開始時の6つの月足・週足条件、期間、終了理由、資料の分類を保存します。各方向10銘柄未満は「蓄積不足」です。同じ銘柄の再通過も区別し、件数と銘柄数を併記します。これは観測された関連であり、因果関係の確定や予測モデルの学習ではありません。

## 初回設定・保守

Python 3.11以上、Git、Docker Compose、Ollamaを用意します。Dockerの実行環境とOllamaを起動してください。J-QuantsのAPI v2キーと、過去10年の日足・一括取得を利用できる契約を準備します。本実装はStandardプランを前提としています。

### 1. リポジトリとPython環境を用意する

既にリポジトリがある場合は、そのフォルダーを開き、cloneを省略します。既存の`.env`・DB・結果を作り直す必要はありません。

macOS（ターミナル）：

```sh
git clone https://github.com/sho-takeuchi/contrarian.git
cd contrarian
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements-lock.txt
./.venv/bin/python -m pip install -e ".[analysis]"
```

Windows（PowerShell）：

```powershell
git clone https://github.com/sho-takeuchi/contrarian.git
Set-Location contrarian
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install -e ".[analysis]"
```

仮想環境の有効化やPowerShellの実行ポリシー変更は不要です。以降も、その仮想環境のPythonを指定します。

### 2. ローカル設定を保存する

リポジトリ直下の`.env.example`をコピーして`.env`を作り、エディターで値を記入します。既存の`.env`は上書きしないでください。環境変数が`.env`より優先されます。

- `POSTGRES_PASSWORD`を自分のDB用パスワードに変更し、`ANALYSIS_DATABASE_URL`内のパスワードにも同じ値を設定します。URLに使う記号は必要に応じてパーセントエンコードします。
- `JQUANTS_API_KEY`にJ-QuantsのAPI v2キーを設定します。
- `EDINET_API_KEY`にEDINETのAPIキーを設定します。未設定でも他の処理は進みますが、企業開示は準備待ちになります。
- Ollamaの接続先・モデルは、まず見本の既定値を使用できます。

キー本体をGitへ追加したり、コマンド引数へ書いたりしないでください。設定項目の一覧はこの節の末尾にあります。

### 3. 上場日CSVを取り込む

[JPXの企業・銘柄情報](https://clientportal.jpx.co.jp/ClientPortal/s/Issue?language=ja)から「全件CSV取得」で上場日を含むCSVを保存し、`inputs/`に置きます。以下のファイル名は、実際に取得した名前へ置き換えます。

macOS：

```sh
./.venv/bin/python -m jp_screening prepare-listings --listing-file './inputs/JPXから取得したCSV.csv'
```

Windows（PowerShell）：

```powershell
.\.venv\Scripts\python.exe -m jp_screening prepare-listings --listing-file '.\inputs\JPXから取得したCSV.csv'
```

取り込み先は`inputs/listing_dates.csv`です。既に取り込み済みなら省略できます。対応する列名や日付形式は [上場日CSVの用意](screening.md#3-上場日csvの用意) を参照してください。

### 4. DBとローカルモデルを準備する

両OS共通で、リポジトリ直下から次を実行します。

```sh
docker compose up -d --wait postgres
ollama pull llama3.1:8b
ollama pull embeddinggemma:300m
```

次にDBのマイグレーションと接続診断を実行します。

macOS：

```sh
./.venv/bin/python -m jp_screening.analysis db upgrade
./.venv/bin/python -m jp_screening.analysis doctor
```

Windows（PowerShell）：

```powershell
.\.venv\Scripts\python.exe -m jp_screening.analysis db upgrade
.\.venv\Scripts\python.exe -m jp_screening.analysis doctor
```

`doctor`はDB・回答用モデル・埋め込みモデルを確認します。J-Quants・EDINETの取得成功は、定期処理の実行結果と画面の「資料の取得状況」でも確認してください。

### 5. 既存の結果を取り込み、定期実行と画面を起動する

macOS：

```sh
./.venv/bin/python -m jp_screening.analysis import-runs --from outputs
./.venv/bin/python -m jp_screening.analysis rebuild-monitoring
./.venv/bin/python -m jp_screening.analysis schedule install
./.venv/bin/python -m jp_screening.analysis schedule status
./.venv/bin/python -m jp_screening.analysis ui
```

Windows（PowerShell）：

```powershell
.\.venv\Scripts\python.exe -m jp_screening.analysis import-runs --from outputs
.\.venv\Scripts\python.exe -m jp_screening.analysis rebuild-monitoring
.\.venv\Scripts\python.exe -m jp_screening.analysis schedule install
.\.venv\Scripts\python.exe -m jp_screening.analysis schedule status
.\.venv\Scripts\python.exe -m jp_screening.analysis ui
```

履歴がない初回は、取り込み・再構築の対象が0件でも正常です。デモ・少数検証・事後の再計算は通常の通過履歴に混ぜません。古い形式で対象範囲が不明な記録も、そのまま通常集計へ採用しません。

`ui`を実行したターミナルは開いたままにして、`http://127.0.0.1:8501/`へアクセスします。初回判定は、営業日の18時以降に当日データの公開を確認した定期処理で始まります。18時前ならその日の18時以降、18時以降なら次の確認時に試行します。休日は次の営業日を待ちます。初回は過去10年分の株価を取得するため時間がかかります。表示の「最終成功」と取得状態を確認してください。

`schedule install`が登録するのは15分間隔の更新処理です。Docker・Ollama・画面をログイン時に起動する設定は別途必要です。CLIで起動した画面は、ターミナルを閉じると停止します。

### 設定項目

| 設定 | 内容 |
|---|---|
| `POSTGRES_PASSWORD` / `POSTGRES_PORT` | Compose用パスワード / localhostポート（既定5432） |
| `ANALYSIS_DATABASE_URL` | `postgresql+psycopg://contrarian:パスワード@127.0.0.1:5432/contrarian` |
| `ANALYSIS_READER_URL` | 任意の参照用接続。未設定時も画面のDB操作はREAD ONLY |
| `JQUANTS_API_KEY` | 株価・財務API用の既存キー |
| `EDINET_API_KEY` | 無料のEDINET利用登録で発行するキー。未設定は準備待ち |
| `ANALYSIS_RESEARCH_ENABLED` | 既定true。保守時だけfalseで外部資料収集を停止できる |
| `OLLAMA_BASE_URL` | ローカルHTTPのみ。既定 `http://127.0.0.1:11434` |
| `OLLAMA_CHAT_MODEL` / `OLLAMA_EMBED_MODEL` | 既定 `llama3.1:8b` / `embeddinggemma:300m` |
| `ANALYSIS_AUTO_IMPORT` | 既存スクリーニングCLI完了時のCSV取り込みフック |

APIキーはコマンド引数・Git・ログ・LLMへ渡しません。Macは`.env`を`chmod 600`、Windowsは実行ユーザーだけが読める権限にしてください。DockerのDBと画面はlocalhostだけに公開します。

保守用CLIは `db upgrade`、`import-runs`、`rebuild-monitoring`、`daily-update --if-due`、`doctor`、`schedule install/status/remove`です。既存の研究用CLIも履歴互換性のため利用できます。

以下の保守例の`python`は、macOSでは`./.venv/bin/python`、Windowsでは`.\.venv\Scripts\python.exe`に置き換えます。別のPythonで定期実行を登録しないでください。

### Mac・Windowsの定期実行

MacはログインユーザーのLaunchAgent、Windowsは現在のユーザーのタスクに登録します。

```sh
python -m jp_screening.analysis schedule install
python -m jp_screening.analysis schedule status
# 停止するとき
python -m jp_screening.analysis schedule remove
```

PCを起動・ログインした状態にします。WindowsはPowerShellでも同じPythonコマンドを使用できます。DockerとOllamaをログイン時に起動する設定にしてください。登録されたコマンドはプロジェクトのPythonと絶対パスのrootを使い、15分間隔で確認します。

### バックアップ・復元と障害対応

更新前にDBと`.env`、`outputs/`、`data/knowledge/`、その他ローカル原文資料をバックアップします。バックアップはGitへ追加しません。

先にリポジトリ内に`backups/`を作成します。macOSは`mkdir -p backups`、PowerShellは`New-Item -ItemType Directory -Force backups`を使用できます。

```sh
# DBの保存（パスワードはコマンドに書かない）
docker compose exec -T postgres pg_dump -U contrarian -d contrarian -Fc -f /tmp/contrarian.dump
docker compose cp postgres:/tmp/contrarian.dump backups/contrarian.dump
# 別DBへの復元確認
docker compose exec -T postgres createdb -U contrarian contrarian_restore
docker compose cp backups/contrarian.dump postgres:/tmp/restore.dump
docker compose exec -T postgres pg_restore -U contrarian -d contrarian_restore --no-owner /tmp/restore.dump
```

復元先を`.env`の接続先へ指定してから`db upgrade`、`doctor`、`rebuild-monitoring`で確認します。Mac・WindowsともComposeコマンドは同じです。移行は追加方式で、既存の判定・価格・資料・第3段階の履歴を削除しません。

DB停止時は完成済みCSVを保持します。復旧後は`db upgrade`、`import-runs --from outputs`、`daily-update --if-due`で再開します。API停止時は価格欠損を0で埋めず取得待ちにし、モデル停止時は分析待ちを表示します。数値記録と資料・LLMの処理は別に進み、定期処理が再試行します。

## 検証

`python -m unittest discover -s tests -v`で単体検証を実行できます。PostgreSQL検証は、名前が`_test`で終わる破棄可能DBだけを指定します。検証DB内の分析テーブルは初期化されます。

```sh
# 実データのあるDBは絶対に指定しない
ANALYSIS_TEST_DATABASE_URL=postgresql+psycopg://USER:PASS@127.0.0.1:55432/contrarian_test python -m unittest discover -s tests -v
# ローカルモデルの明示的な合成データ検証
python tests/smoke_monitoring_models.py
```
