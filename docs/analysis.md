# 履歴の蓄積・株価追跡・条件別比較・ローカルRAG

第3段階の通過時点の条件と、その後の値動きを分けて保存します。比較するのは観測された関連です。因果効果の推定、将来の上昇予測、売買条件の自動最適化は行いません。

```mermaid
flowchart LR
    API[J-Quants] --> Screen[既存のスクリーニング]
    Screen --> CSV[outputs/実行ID のCSV・run.json]
    CSV --> DB[(PostgreSQL)]
    API --> Prices[通過銘柄の株価追跡]
    Prices --> DB
    DB --> Calc[Python・SQLによる評価・条件比較]
    Calc --> DB
    Docs[Markdown・テキスト・DOCX] --> Embed[ローカル埋め込みモデル]
    Embed --> Vector[(同じDBのpgvector)]
    DB --> UI[Streamlit]
    Vector --> Chat[Ollamaで説明]
    Calc --> Chat
    Chat --> UI
```

## 1. 初回設定

Python 3.11以上、Docker Compose、Ollamaが必要です。既存のREADMEに従って上場日CSVも準備してください。全銘柄スクリーニングには既存処理で必要な履歴を取得できるJ-Quants契約が必要です。現在の価格が遅延提供される契約では、当日の定期処理は「更新待ち」になります。

リポジトリ直下で、既存の仮想環境を有効にして実行します。

```sh
python -m pip install -e ".[analysis]"
```

`.env.example` を `.env` にコピーします。Macは `cp .env.example .env`、PowerShellは `Copy-Item .env.example .env` を使います。既存の `.env` がある場合はコピーで上書きせず、必要な設定を追加してください。

| 設定 | 内容 |
|---|---|
| `POSTGRES_PASSWORD` | 自分で生成した長いDBパスワード |
| `POSTGRES_PORT` | 既定5432。既存PostgreSQLと重なる場合は変更 |
| `ANALYSIS_DATABASE_URL` | `postgresql+psycopg://contrarian:パスワード@127.0.0.1:5432/contrarian` |
| `ANALYSIS_READER_URL` | 任意。画面・チャット用の読み取り専用アカウント |
| `ANALYSIS_AUTO_IMPORT` | trueで通常CLIのスクリーニング完了後にも取り込む |
| `JQUANTS_API_KEY` | 定期実行ユーザーが読めるローカルAPIキー |
| `OLLAMA_BASE_URL` | 既定 `http://127.0.0.1:11434`。ループバックのHTTPだけを許可 |
| `OLLAMA_CHAT_MODEL` | 既定 `llama3.1:8b` |
| `OLLAMA_EMBED_MODEL` | 既定 `embeddinggemma:300m` |

URL中のパスワードに `@`、`:`、`/` 等がある場合はURLエンコードしてください。Compose用の `POSTGRES_PASSWORD` は元の文字列です。OSの環境変数が `.env` より優先されます。Macでは `chmod 600 .env`、Windowsではファイルのアクセス権を実行ユーザーに限定してください。APIキーをコマンド引数に渡す必要はありません。

```sh
docker compose up -d postgres
python -m jp_screening.analysis db upgrade
ollama pull llama3.1:8b
ollama pull embeddinggemma:300m
python -m jp_screening.analysis doctor
python -m jp_screening.analysis import-runs --from outputs
python -m jp_screening.analysis index-docs --from README.md
python -m jp_screening.analysis ui
```

Ollamaアプリ／サービスを先に起動してください。EmbeddingGemmaはOllama 0.11.10以上が必要です（[公式モデル情報](https://ollama.com/library/embeddinggemma)）。モデルはPC内に保存し、推論もPC内で実行します。クラウドモデルは接続チェックで拒否します。モデル変更後は資料を再登録し、そのモデルの索引を作成してください。

画面は `http://127.0.0.1:8501`、DBはループバックだけで待ち受けます。PostgreSQL 18・pgvectorはComposeの永続ボリュームに保存され、既存の `data/prices.sqlite3` は取得用キャッシュとして残ります。

## 2. 日常の操作

コマンドはリポジトリ直下で実行します。他のディレクトリから使う場合は、サブコマンドの前に `--root /リポジトリの絶対パス` を指定してください。

| コマンド（`python -m jp_screening.analysis` に続ける） | 動作 |
|---|---|
| `db upgrade` | 破壊的な削除をせずスキーマを更新 |
| `import-runs --from outputs` | フォルダー配下の完成済み履歴を取り込む。実行フォルダー1個も指定可能 |
| `update-prices` | 停止中以外の追跡銘柄を更新、過去の取得漏れ・価格訂正も確認 |
| `evaluate-outcomes` | 保存済みの価格から両基準・全評価期間を計算 |
| `analyze-conditions --horizon 20 --feature daily_ma_gap` | 条件別比較をDB・CSV・JSONに保存 |
| `index-docs --from data/knowledge` | Markdown・TXT・DOCXを索引化 |
| `daily-update --if-due` | 未完了の毎日処理を実行 |
| `schedule install / status / remove` | 定期実行の登録・状態確認・解除 |
| `ui / doctor` | 画面起動／DB・ローカルモデルの接続診断 |

条件比較には `--start YYYY-MM-DD --end YYYY-MM-DD --basis close` または `--basis next_open` を指定できます。ルールが複数ある場合は画面で選ぶか `--rules-key` で指定します。特徴量は `long_low_rise`、`ceiling_room`、`monthly_low_margin`、`monthly_ma_gap`、`weekly_ma_gap`、`weekly_ma_change`、`daily_ma_gap`、`daily_ma_change` です。

出力先は `outputs/analysis_reports/<内容ハッシュ>/conditions.csv` と `report.json`。JSONに対象実行のID・ハッシュ、特徴量・計算バージョン、群の境界値、観測ごとの評価根拠を保存します。同じ入力の再計算は同じレポートになります。新しい株価訂正を反映した結果は別のレポートとして保持します。

画面には「候補・履歴」「追跡一覧」「条件比較」「資料管理」「チャット」「更新状況」があります。「追跡一覧」で停止・再開でき、既存価格を削除しません。最新価格基準の表示も選択できます。「更新状況」で取込対象範囲・通常集計への採用、最終成功、失敗内容、追跡銘柄の未取得日数を確認できます。

## 3. 記録の意味と再現性

新しい `run.json` は形式バージョン2です。ルールのソースハッシュ、全銘柄／指定銘柄／少数検証の区別、CSVのSHA-256を記録します。`all_results.csv` と段階別候補の集合・件数・判定日を照合し、同じID・同じ内容はスキップ、同じID・異なる内容は競合として拒否します。

`analysis_runs`、`analysis_results`、`analysis_features` が判定時点の記録です。価格を訂正しても書き換えません。デモ、sample、指定銘柄、recompute、対象範囲不明の旧形式は取り込めますが通常集計から除外します。純粋なreplayのように新しい `run.json` がないフォルダーは取り込みません。

旧形式の全銘柄実行は、元の入力と当時のルールを人が確認した後だけ採用できます。

```sh
python -m jp_screening.analysis review-legacy-run <実行ID> --rules-version <当時のルール識別子> --note "全銘柄実行であることを確認した根拠"
```

この操作は元のCSV・manifestを保持し、確認記録を `analysis_run_reviews` に残します。少数検証・デモ・再計算を通常実績に変更する機能ではありません。古いルールを現在のルールと同じと断定できない場合は、別の識別子にしてください。

同日・同じルールでは、段階が最も深い実行を優先し、同段階なら実行日時が最新のものを代表にします。元の全実行は残します。通過回数は代表実行で通過した**観測日数**、通過後の実績分析は**通過開始イベント**です。連続通過を1イベントにまとめ、明確な不通過後の再通過だけ新イベントにします。未実行・銘柄の未観測・判定不能ではイベントを切らず `continuity_unknown` を付けます。

第1段階以上の通過銘柄を `analysis_tracking` に登録します。その後候補から消えても削除しません。価格は `analysis_prices` に銘柄・営業日ごとに共有保存し、返却された値の変更前の価格・取得日時を `analysis_price_revisions` に保持します。APIから過去の営業日が返らなくなっただけでは、提供期間外になった可能性もあるため保存済み価格を消しません。「今回未返却の保存価格あり」と表示して取得日時を維持します。未取得の日は欠損として記録し、後日の取得で復旧すれば再評価されます。

## 4. 計算仕様

判定日は0営業日です。現物市場カレンダーの区分1・2を数え、休日取引だけの区分3は除外します。5・20・60・120・250営業日後の終値を評価し、画面の初期値は20営業日です。

- `close`：判定日終値を基準にした騰落率。
- `next_open`：翌営業日始値を基準にした、同じ評価日までの騰落率。
- 最大上昇／下落率：判定翌営業日から評価日までの最高値／最安値と基準価格の比較。判定日の高安は含めません。
- 最新：実際に取得できた最新終値まで。欠損価格を現在日の価格とみなす処理はしません。

調整前のO/H/L/Cと日々の `AdjFactor` を保存します。評価日の価格基準にそろえ、ある日の価格に掛けるのは**その翌営業日から評価日まで**の調整係数の積です。その日自身の係数は掛けません。将来発生した分割は、すでに終了した評価期間の騰落率を変えません。配当・売買費用は含みません（[J-Quantsの株価仕様](https://jpx-jquants.com/ja/spec/eq-bars-daily)）。

評価日未到来は「評価待ち」。レコード・係数・基準価格・評価日終値が不足すると評価不能です。途中の営業日のレコードは存在するが四本値がNullの場合、始点・終点がそろっていれば騰落率は計算できても、期間中の最大上昇／下落は欠損になります。Nullだけから無約定・売買停止・上場廃止の理由を断定しません。停止中の追跡は評価の `tracking_paused` と別に表示します。

特徴量の算式はバージョン1です。すべて `(分子 / 分母 - 1) × 100` の百分率で、分母が欠損・0以下ならNullです。

| 特徴量 | 分子 / 分母 |
|---|---|
| `long_low_rise` | 判定時の直近終値 / 長期最安終値 |
| `ceiling_room` | 許容上限価格 / 直近終値 |
| `monthly_low_margin` | M1〜M3の最低安値 / M4安値 |
| `monthly_ma_gap` | M1終値 / 3か月平均 |
| `weekly_ma_gap` | 確定週足終値 / 5週平均 |
| `weekly_ma_change` | 5週平均 / 前週5週平均 |
| `daily_ma_gap` | 判定日終値 / 5日平均 |
| `daily_ma_change` | 5日平均 / 前営業日5日平均 |

比較は第3段階の新規通過イベントを四分位（nearest-rank）で区分します。同値は同じ群とし、空の群が生じる場合があります。条件が欠損のイベント数を別に保存します。評価待ち・欠損は平均、中央値、上昇割合の分母に入れません。上昇割合は有効な騰落率が厳密に0%超の件数／有効件数です。分布・全件数・有効件数・待ち・欠損・銘柄数も保存します。

段階比較は同じ判定日・株式／REIT区分にそろえ、「第3段階新規通過」「第2段階まで」「第1段階まで」「判定不能・未評価」を重複なく集計します。第3段階の連続通過日は新規イベント群に混ぜません。同銘柄の再登場や同日の市場環境は独立な標本とは限らないため、日付別・銘柄数を併記して解釈してください。

## 5. 文書とチャット

資料は見出し・段落ごとに分割します。1ファイル10MBまでのMarkdown、UTF-8テキスト、DOCX（段落と表）に対応します。資料管理の登録時に銘柄・資料日付・判定ルールを付けられます。原文の抽出テキスト、内容ハッシュ、埋め込みモデルのダイジェスト、版を保存します。

同じパスの資料を更新すると現在の検索対象を新しい版へ切り替え、旧版を残します。画面で除外・再採用できます。過去日を指定した質問には、その日以前の資料日付を持つ版を優先します。過去のルール資料には正確な資料日付・ルールを指定してください。日付のない文書を当時の仕様と推定しません。回答の根拠では常に判定時の保存設定を優先します。

質問例：「最新の第3段階通過銘柄は？」「銘柄コード1000の20営業日後の騰落率は？」「日足の平均との乖離で20営業日後の成績を比較して」。名前だけの銘柄や不明確な期間は確認を返します。

LLMの出力は種類・銘柄・期間などの構造化された条件として検証し、用意した読み取り専用処理だけを呼び出します。LLM生成SQLは実行しません。表の値が計算結果の正本で、説明文と分けて表示します。実行ID・判定日・評価期間・基準価格・文書の引用箇所と原文を画面で確認できます。モデルの回答に誤りがある場合は表・原資料を確認してください。

資料が増えた場合や分析期間が長い場合は対象期間・ルールを絞ってください。初期版はpgvectorの正確な距離検索で、近似索引は使いません。埋め込みモデルの版が違うベクトルを混ぜて検索しません。

## 6. Mac・Windowsの定期実行

仮想環境のPythonで次を実行してください。そのPythonの絶対パスを登録します。

```sh
python -m jp_screening.analysis schedule install --dry-run
python -m jp_screening.analysis daily-update --if-due
python -m jp_screening.analysis schedule install
python -m jp_screening.analysis schedule status
```

Macは `~/Library/LaunchAgents/jp.contrarian.analysis.plist` にユーザー用LaunchAgentを登録します。ログは `logs/analysis-schedule.log` と `analysis-schedule-error.log`。Windowsはタスクスケジューラに `jp.contrarian.analysis` を、ログオン中のユーザー・最小権限で登録します。Windowsの登録定義は `data/analysis-task.xml` に保存します。タスクの履歴・最終結果も確認できます。APIキーは登録定義に埋め込みません。

両OSともログオン時と15分間隔で起動し、日本時間18:00以降の営業日に当日分の全銘柄・第3段階スクリーニングを行います。公開前は待ち、当日完了済みなら再スクリーニングしません。18:00前や休日も、未取得の過去株価と未取込CSVの復旧は試行します。PCの起動とユーザーのログオン、Dockerの稼働が必要です。スリープからの強制復帰は設定しません。

`data/analysis_job.json` に各処理の完了キーと失敗状態を原子的に保存し、DBの `analysis_jobs` にも記録します。既存のOSファイルロック、DBのアドバイザリロック、主キーとUPSERTで同時起動・再試行の二重登録を防ぎます。スクリーニング失敗時も追跡更新は独立して試します。Ollamaは定期処理には不要です。

停止中の営業日の株価は次回に取得できる範囲で補完します。その日に行わなかったスクリーニング結果は作成せず、イベントには観測の空白を残します。初期版は過去の訂正を見逃さないよう追跡開始日以降を再取得するため、銘柄数・追跡年数に応じてAPI通信量が増えます。

解除は `python -m jp_screening.analysis schedule remove`。データは削除しません。Python・リポジトリの場所を移したら解除・再登録してください。

## 7. 障害からの復旧

| 状態 | 操作 |
|---|---|
| DB停止 | `docker compose up -d postgres` → `doctor` → `import-runs --from outputs` → `daily-update --if-due` |
| API停止・未公開 | キー・契約・通信を確認して再試行。株価やスクリーニングを偽の当日データで補わない |
| CSV取り込み失敗 | 対象フォルダーのrun.json・ハッシュ・候補集合を確認。同IDの内容は上書きされない |
| 追跡株価の欠損 | `update-prices` → `evaluate-outcomes`。理由不明のまま上場廃止と確定しない |
| Ollama停止 | アプリを起動し `doctor`。候補表・価格更新・評価はそのまま利用可能 |
| モデル変更 | 新モデルをpullし、資料を再登録。旧版は保持 |
| 中断・同時起動 | 実行中の処理が終わってから再試行。OSが終了したプロセスのロックを解放する |

通常CLIの自動取り込みだけ失敗した場合は終了コード3です。完成済みCSVは残り、後で取り込めます。分析CLIはエラーや未完了で終了コード2を返します。復旧時は「更新状況」の最終成功日時・価格の日付・未取得日数まで確認してください。

## 8. バックアップと復元

定期実行を解除し、実行中の更新が終わってからバックアップします。以下はMac・PowerShell共通です。`backups` は事前に作成してください。

```sh
docker compose exec postgres pg_dump -U contrarian -d contrarian -Fc -f /tmp/contrarian.dump
docker compose cp postgres:/tmp/contrarian.dump backups/contrarian.dump
```

DBに判定・価格の全版・レポート・原文テキスト・ベクトルが入っています。加えて `outputs`、`inputs`、`data/knowledge`（DOCX原本等）、`data/analysis_job.json`、必要ならSQLiteとスナップショットをコピーしてください。`.env` は秘密情報として別途安全に保管します。バックアップ、実データ、資料はGit管理の対象外です。

復元は既存DBを消さず、新しいDBで検証します。

```sh
docker compose exec postgres createdb -U contrarian contrarian_restore
docker compose cp backups/contrarian.dump postgres:/tmp/contrarian-restore.dump
docker compose exec postgres pg_restore -U contrarian -d contrarian_restore --no-owner /tmp/contrarian-restore.dump
```

`.env` の接続先DB名を `contrarian_restore` に変更し、原本資料も元のパスへ戻します。`db upgrade`、`doctor`、候補件数・株価日付・文書検索を確認してから定期実行を再登録します。ボリュームを消す `docker compose down -v` は通常運用・復旧には不要です。

読み取り専用ロールを使う場合は `docker compose exec postgres psql -U contrarian -d contrarian` で対話接続し、次を実行します。パスワードは `\password contrarian_reader` の対話入力で設定してください。

```sql
CREATE ROLE contrarian_reader LOGIN;
GRANT CONNECT ON DATABASE contrarian TO contrarian_reader;
GRANT USAGE ON SCHEMA public TO contrarian_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO contrarian_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE contrarian IN SCHEMA public
  GRANT SELECT ON TABLES TO contrarian_reader;
```

そのロールのDSNを `ANALYSIS_READER_URL` に設定します。設定しない場合も、チャット・表示用のトランザクションはREAD ONLYで開始します。

## 9. テスト

```sh
python -c "from pathlib import Path; Path('data').mkdir(exist_ok=True)"
python -m unittest discover -s tests -v
```

専用の空DBを用意し、名前の末尾を `_test` にして `ANALYSIS_TEST_DATABASE_URL` を設定すると、実PostgreSQL＋pgvectorの統合テストも動きます。テストはそのDBの分析テーブルを初期化します。実データのDBを指定しないでください。変数がなければ統合テストだけをスキップします。

GitHub ActionsにMac・Windows・Linux（Python 3.11／3.13）のテストと、pgvectorコンテナを用いる統合テストを用意しています。合成価格、模擬API・LLM応答で、休日・分割・併合・欠損・訂正・再取り込み・障害復旧・資料版管理を検証します。Streamlitの全画面もAppTestで確認します。実際のAPI契約、ログオン状態、ローカルモデルの日本語回答は、導入するPCで追加確認してください。
