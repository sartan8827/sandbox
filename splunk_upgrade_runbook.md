# Splunk Enterprise バージョンアップ作業手順書
## 9.1.1 → 9.4.x → 10.2.8（EC2 / Amazon Linux 2023 / スタンドアローン / RPM / 実行ユーザー splunk）

---

## 0. 前提・方針

| 項目 | 内容 |
|---|---|
| 対象 | Splunk Enterprise スタンドアローン 1台 |
| OS | Amazon Linux 2023（EC2） |
| 導入方式 | RPM（`/opt/splunk`） |
| 実行ユーザー | `splunk` |
| アップグレード経路 | **9.1.1 → 9.4.x（最新パッチ） → 10.2.8**（10.2 へは 9.4 以上から上げる必要があるため 2 段階） |
| 戻し方 | Splunk 公式のダウングレード手段はない → **EBS スナップショット / AMI** で戻す |

> 公式の「Upgrade paths to version 10.2」表を作業前に必ず確認すること。
> https://help.splunk.com/en/splunk-enterprise/administer/install-and-upgrade/10.2/upgrade-or-migrate-splunk-enterprise/how-to-upgrade-splunk-enterprise

### 作業時間の目安（KV store / データ量によって変わる）
- 事前確認・バックアップ：30〜60 分
- 9.4 へのアップグレード＋確認：30〜60 分
- 10.2.8 へのアップグレード＋確認：30〜60 分
- 停止時間（データ取り込みが止まる時間）：各段階 10〜30 分程度

---

## 1. 変数定義（以降のコマンドで使う。セッションごとに実行）

```bash
# === 環境に合わせて書き換える ===
export SPLUNK_HOME=/opt/splunk
export SPLUNK_USER=splunk
export WORK_DIR=/home/ec2-user/splunk_upgrade        # パッケージ・ログ置き場
export BACKUP_DIR=/home/ec2-user/splunk_backup       # 設定バックアップ置き場（/opt/splunk 以外）
export RPM_94="splunk-9.4.x-xxxxxxxxxxxx.x86_64.rpm"   # 9.4 系最新パッチのファイル名
export RPM_102="splunk-10.2.8-xxxxxxxxxxxx.x86_64.rpm" # 10.2.8 のファイル名
export TS=$(date +%Y%m%d_%H%M%S)

mkdir -p "$WORK_DIR" "$BACKUP_DIR"
alias spl="sudo -u $SPLUNK_USER $SPLUNK_HOME/bin/splunk"
```

---

## 2. 事前確認（作業日前に実施推奨）

### 2-1. 現状の記録
```bash
cd "$WORK_DIR"
{
  echo "=== date ===";            date
  echo "=== splunk version ===";  spl version
  echo "=== rpm ===";             rpm -qa | grep -i splunk
  echo "=== os ===";              cat /etc/os-release
  echo "=== glibc ===";           ldd --version | head -1
  echo "=== cpu flags ===";       grep -o -w -E 'avx|sse4_2|aes' /proc/cpuinfo | sort -u
  echo "=== disk ===";            df -h "$SPLUNK_HOME"
  echo "=== process user ===";    ps -o user=,pid=,cmd= -C splunkd
  echo "=== listen ports ===";    sudo ss -lntup | grep -i splunk
  echo "=== apps ===";            ls -1 "$SPLUNK_HOME/etc/apps"
} 2>&1 | tee "pre_check_${TS}.log"
```

**判定基準**

| 確認項目 | OK の条件 |
|---|---|
| CPU フラグ | `aes` `avx` `sse4_2` の 3 つが表示される（9.4 以降の KV store に必須） |
| glibc | 2.27 以上（AL2023 は 2.34 の想定） |
| ディスク | `$SPLUNK_HOME` のあるボリュームの空きが **50% 以上**（KV store アップグレード要件） |
| プロセスユーザー | `splunk`（10.2 は root では起動しない） |

### 2-2. 起動方式（systemd / init.d）の確認
```bash
systemctl list-unit-files | grep -i splunk
ls -l /etc/systemd/system/Splunkd.service /etc/init.d/splunk 2>/dev/null
grep -E '^(User|Group|ExecStart|Restart)' /etc/systemd/system/Splunkd.service 2>/dev/null
grep -E 'SPLUNK_OS_USER|SPLUNK_SERVER_NAME' "$SPLUNK_HOME/etc/splunk-launch.conf"
```
- `Splunkd.service` があり `User=splunk` → **systemd 管理（以降「パターンS」）**
- `/etc/init.d/splunk` のみ → **init.d 管理（以降「パターンI」）**
  - 注意：init.d 方式は RHEL 8 以降の系統では非推奨。AL2023 では systemd 方式への移行を推奨（付録 B）。

> **パターンS の重要事項**
> `Restart=always` が設定されている場合、`splunk stop` で止めても systemd が自動で再起動する。
> 停止は必ず `sudo systemctl stop Splunkd` で行う。

### 2-3. KV store の状態確認（最重要）
```bash
spl show kvstore-status --verbose | tee "$WORK_DIR/kvstore_pre_${TS}.log"
```

| 確認項目 | 期待値 | NG の場合 |
|---|---|---|
| `status` | `ready` | 復旧してから進む |
| `storageEngine` | `wiredTiger` | 3-2 の移行が必要 |
| `serverVersion` | `4.2.x` | 4.0 以下なら 3-2 の移行が必要 |

> 9.4 以上へ上げる前に KV store のサーバーバージョンを 4.2.x にしておく必要がある（公式要件）。
> 9.1.x の環境でも 4.0.x のまま残っている例があるため、必ず確認する。

### 2-4. 証明書（独自証明書を使っている場合のみ）
```bash
grep -rE 'serverCert|sslRootCAPath|caCertFile' "$SPLUNK_HOME/etc/system/local/server.conf"
# 独自証明書を使っている場合は、OCSP の設定を確認
spl cmd openssl x509 -in <証明書のパス> -noout -ocsp_uri
```
- URL が返る → OCSP サーバー（通常 80/tcp）への外向き通信を許可する。
  許可できない場合は `server.conf` の `[kvstore]` に `ocspValidation = false` を設定。
- 9.4 以降で KV store が起動しない事例の多くは独自証明書が原因。

### 2-5. アプリ・フォワーダーの互換性確認
- `$SPLUNK_HOME/etc/apps` の各アプリが 9.4 と 10.2 に対応しているか Splunkbase で確認。
- 10.2 では **Node.js が削除済み**、10.0 から **Python 3.7 のサポートが終了**。これらに依存するアプリがないか確認。
- データを送ってくる Universal Forwarder のバージョンが 10.2 のインデクサーと互換か確認。

### 2-6. パッケージの入手
[Previous Releases](https://www.splunk.com/en_us/download/previous-releases.html) から 9.4 系最新パッチと 10.2.8 の **Linux .rpm** を入手する（サイトに表示される wget コマンドを使うと EC2 に直接落とせる）。

```bash
cd "$WORK_DIR"
# wget -O "$RPM_94"  "<splunk.com に表示された URL>"
# wget -O "$RPM_102" "<splunk.com に表示された URL>"

# ハッシュの確認（splunk.com に掲載の SHA512 / MD5 と比較）
sha512sum "$RPM_94" "$RPM_102"

# パッケージ情報の確認
rpm -qip "$RPM_94"  | grep -E 'Name|Version|Release'
rpm -qip "$RPM_102" | grep -E 'Name|Version|Release'
```

---

## 3. バックアップ（作業当日）

### 3-1. 取り込み・検索の状態を記録（比較用）
Splunk Web で以下の検索を実行し、結果を控えておく。
```
| tstats count where index=* earliest=-24h by index
```
```
index=_internal sourcetype=splunkd log_level=ERROR earliest=-24h | stats count by component | sort -count
```

### 3-2. （必要な場合のみ）KV store を 4.2 / WiredTiger へ移行
2-3 で `serverVersion` が 4.2 未満、または `storageEngine` が `wiredTiger` でない場合のみ実施する。
```bash
# WiredTiger でなければ、先にストレージエンジンを移行
spl migrate kvstore-storage-engine --target-engine wiredTiger

# サーバーバージョンを 4.2 に上げる
spl migrate migrate-kvstore

# 結果確認：serverVersion 4.2.x / storageEngine wiredTiger
spl show kvstore-status --verbose
```

### 3-3. KV store のバックアップ（Splunk 起動中に実行）
```bash
spl backup kvstore -archiveName "pre_upgrade_911_${TS}"
# 完了確認
spl show kvstore-status --verbose | grep -iE 'backupRestoreStatus|status'
ls -lh "$SPLUNK_HOME/var/lib/splunk/kvstorebackup/"
cp -p "$SPLUNK_HOME"/var/lib/splunk/kvstorebackup/pre_upgrade_911_${TS}* "$BACKUP_DIR/"
```

### 3-4. Splunk の停止
```bash
# パターンS
sudo systemctl stop Splunkd
# パターンI
# spl stop

# 停止の確認（何も表示されないこと）
pgrep -a -u "$SPLUNK_USER" splunkd ; pgrep -a mongod
```

### 3-5. 設定ファイルのバックアップ
```bash
sudo tar czpf "$BACKUP_DIR/splunk_etc_911_${TS}.tgz" -C "$SPLUNK_HOME" etc
sudo cp -p /etc/systemd/system/Splunkd.service "$BACKUP_DIR/" 2>/dev/null
ls -lh "$BACKUP_DIR"
```

### 3-6. EBS スナップショットの取得（これが一番確実な戻し手段）
Splunk を**停止した状態**で取得する。AWS コンソールで行う場合は「EC2 → インスタンス → アクション → イメージとテンプレート → イメージを作成（AMI）」でも可。

```bash
# AWS CLI を使う場合（作業端末または権限のあるインスタンスで実行）
INSTANCE_ID=i-xxxxxxxxxxxxxxxxx
aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[].Instances[].BlockDeviceMappings[].[DeviceName,Ebs.VolumeId]' --output table

# 対象の全ボリュームのスナップショットを取得
aws ec2 create-snapshots \
  --instance-specification InstanceId="$INSTANCE_ID",ExcludeBootVolume=false \
  --description "splunk pre-upgrade 9.1.1 ${TS}" --copy-tags-from-source volume

# 完了するまで待つ（State が completed になること）
aws ec2 describe-snapshots --owner-ids self \
  --filters Name=description,Values="splunk pre-upgrade 9.1.1 ${TS}" \
  --query 'Snapshots[].[SnapshotId,VolumeId,State,Progress]' --output table
```

> **スナップショットが completed になってから次に進むこと。**

---

## 4. 第 1 段階：9.1.1 → 9.4.x

### 4-1. RPM のアップグレード（Splunk が停止していることを再確認してから）
```bash
pgrep -a splunkd && echo "!!! splunkd が動いています。停止してください !!!"

cd "$WORK_DIR"
sudo rpm -Uvh --test "$RPM_94"     # 事前テスト（エラーが出ないこと）
sudo rpm -Uvh "$RPM_94" 2>&1 | tee "rpm_94_${TS}.log"

rpm -qa | grep -i splunk           # 9.4.x になっていること
```

### 4-2. 所有者の確認
```bash
# splunk 以外が所有するファイルがないこと（何も表示されなければ OK）
sudo find "$SPLUNK_HOME" ! -user "$SPLUNK_USER" | head -20
# 表示された場合
# sudo chown -R "$SPLUNK_USER":"$SPLUNK_USER" "$SPLUNK_HOME"
```

### 4-3. 移行内容の事前確認（任意・推奨）
対話形式で起動し、プロンプトで **`n`** を選ぶと、変更予定が出力されるだけで起動しない。
```bash
sudo -u "$SPLUNK_USER" "$SPLUNK_HOME/bin/splunk" start
#   Perform migration and upgrade without previewing configuration changes? [y/n] → n
ls -lt "$SPLUNK_HOME"/var/log/splunk/migration.log.* | head -1
less "$(ls -t "$SPLUNK_HOME"/var/log/splunk/migration.log.* | head -1)"
```

### 4-4. 移行の実行と起動

**パターンS（systemd 管理）**
```bash
# 1) ライセンス同意と移行処理を splunk ユーザーで実行する
#    （systemd 管理のため、内部で systemctl が呼ばれ権限エラーになることがある。その場合は 2) へ）
sudo -u "$SPLUNK_USER" "$SPLUNK_HOME/bin/splunk" start --accept-license --answer-yes --no-prompt

# 2) 1) が "Interactive authentication required" などの権限エラーで失敗した場合
#    公式ドキュメントの systemd 環境の手順どおり sudo で実行する（splunkd 自体は unit の User=splunk で動く）
sudo "$SPLUNK_HOME/bin/splunk" start --accept-license --answer-yes --no-prompt

# 3) systemd の状態確認
sudo systemctl daemon-reload
sudo systemctl status Splunkd --no-pager
```

**パターンI（init.d 管理）**
```bash
sudo -u "$SPLUNK_USER" "$SPLUNK_HOME/bin/splunk" start --accept-license --answer-yes --no-prompt
```

### 4-5. 9.4 の動作確認
```bash
spl version                                        # Splunk 9.4.x
ps -o user=,cmd= -C splunkd | head -3              # 実行ユーザーが splunk
spl status
spl show kvstore-status --verbose | tee "$WORK_DIR/kvstore_94_${TS}.log"
#   status : ready / serverVersion : 7.0.x になっていること（9.4 で自動的に上がる）

# エラーの確認
grep -E ' (ERROR|FATAL) ' "$SPLUNK_HOME/var/log/splunk/splunkd.log" | tail -50
tail -50 "$SPLUNK_HOME/var/log/splunk/mongod.log"
ls -lt "$SPLUNK_HOME"/var/log/splunk/migration.log.* | head -1
```

Splunk Web（`https://<host>:8000`）で以下を確認する。
- [ ] ログインできる
- [ ] 3-1 の検索結果と比べて、データの取り込みが続いている
- [ ] 主要なダッシュボード、保存済み検索、アラートが動く
- [ ] 各アプリでエラーが出ていない（設定 → メッセージ）

> **KV store が起動しない場合**：`mongod.log` と `splunkd.log`（`KVStore` で grep）を確認する。
> 独自証明書が原因の例が多い。2-4 を見直し、解決するまで 10.2 へ進まないこと。

### 4-6. 中間スナップショットの取得（推奨）
```bash
sudo systemctl stop Splunkd        # パターンI は spl stop
pgrep -a splunkd ; pgrep -a mongod   # 何も表示されないこと
sudo tar czpf "$BACKUP_DIR/splunk_etc_94_${TS}.tgz" -C "$SPLUNK_HOME" etc
```
3-6 と同じ手順で EBS スナップショットを取得する（説明は `splunk post-upgrade 9.4 ...` など）。

---

## 5. 第 2 段階：9.4.x → 10.2.8

### 5-1. 10.2 固有の事前確認
```bash
# root で動いていないこと（10.2 では root で起動しない）
grep -E '^User=' /etc/systemd/system/Splunkd.service
grep SPLUNK_OS_USER "$SPLUNK_HOME/etc/splunk-launch.conf"

# 1024 未満のポートで待ち受けている入力がないこと（splunk ユーザーでは bind できない）
grep -rhE '^\[(tcp|udp|splunktcp)[^]]*:[0-9]+\]' "$SPLUNK_HOME/etc/apps"/*/local/inputs.conf "$SPLUNK_HOME/etc/system/local/inputs.conf" 2>/dev/null
```

### 5-2. RPM のアップグレード
```bash
pgrep -a splunkd && echo "!!! splunkd が動いています。停止してください !!!"

cd "$WORK_DIR"
sudo rpm -Uvh --test "$RPM_102"
sudo rpm -Uvh "$RPM_102" 2>&1 | tee "rpm_102_${TS}.log"
rpm -qa | grep -i splunk           # 10.2.8 になっていること

sudo find "$SPLUNK_HOME" ! -user "$SPLUNK_USER" | head -20   # 何も表示されなければ OK
```

### 5-3. 移行の実行と起動
4-4 と同じ手順で実行する（パターンS は 1) を試し、権限エラーなら 2)）。
```bash
sudo -u "$SPLUNK_USER" "$SPLUNK_HOME/bin/splunk" start --accept-license --answer-yes --no-prompt
# 権限エラーの場合（パターンS）
# sudo "$SPLUNK_HOME/bin/splunk" start --accept-license --answer-yes --no-prompt

sudo systemctl daemon-reload
sudo systemctl status Splunkd --no-pager
```

> 起動後、KV store のアップグレード（7.0 → 8.0）が自動で走ることがある。
> その間は **Splunk / KV store の再起動や、KV store への大量の書き込みをしない**こと。
> 失敗した場合は、直前に自動で取られたバックアップから自動で元に戻る。

### 5-4. KV store の確認
```bash
# status が ready になるまで数分待つ
watch -n 30 "sudo -u $SPLUNK_USER $SPLUNK_HOME/bin/splunk show kvstore-status --verbose | grep -E 'status|serverVersion|storageEngine'"
spl show kvstore-status --verbose | tee "$WORK_DIR/kvstore_102_${TS}.log"
```

---

## 6. 事後確認

### 6-1. コマンドでの確認
```bash
{
  spl version
  sudo systemctl is-active Splunkd; sudo systemctl is-enabled Splunkd
  ps -o user=,cmd= -C splunkd | head -3
  spl show kvstore-status --verbose | grep -E 'status|serverVersion|storageEngine'
  sudo ss -lntp | grep -E ':(8000|8089|9997)\b'
  grep -E ' (ERROR|FATAL) ' "$SPLUNK_HOME/var/log/splunk/splunkd.log" | tail -30
} 2>&1 | tee "$WORK_DIR/post_check_${TS}.log"
```

### 6-2. チェックリスト
- [ ] `splunk version` が **10.2.8**
- [ ] `Splunkd` が active / enabled、実行ユーザーが `splunk`
- [ ] KV store が `status : ready`
- [ ] 8000（Web）/ 8089（管理）/ 9997（受信）などが LISTEN している
- [ ] Splunk Web にログインできる
- [ ] 3-1 の検索で、取り込み件数が作業前と同じ傾向
- [ ] `index=_internal log_level=ERROR` の件数が作業前より急に増えていない
- [ ] フォワーダーからの受信が続いている
```
index=_internal sourcetype=splunkd group=tcpin_connections earliest=-15m | stats latest(_time) as last by hostname | convert ctime(last)
```
- [ ] 主要なダッシュボード、保存済み検索、アラート、KV store を使うルックアップが動く
- [ ] ライセンスが有効（設定 → ライセンス）
- [ ] EC2 を再起動しても Splunk が自動で起動する（可能なら確認）
```bash
sudo reboot
# 再接続後
sudo systemctl status Splunkd --no-pager
```

### 6-3. 後片付け（安定稼働を確認してから）
- 作業用の RPM、`$WORK_DIR` のログを保管・削除する
- 不要になった EBS スナップショット / AMI を削除する（一定期間残すことを推奨）

---

## 付録 A：切り戻し手順

Splunk には公式のダウングレード手段がないため、スナップショットから戻す。

1. EC2 インスタンスを停止する
2. 対象ボリュームをデタッチする
3. 3-6（または 4-6）のスナップショットからボリュームを作成する（**同じ AZ**）
4. 元と同じデバイス名でアタッチする
5. インスタンスを起動し、`splunk version` と取り込みを確認する

> 注意：戻した場合、スナップショット取得後に取り込んだデータは失われる。
> フォワーダー側のキューに残っている分は再送されるが、全量が戻る保証はない。

ルートボリュームごと戻す場合は「ルートボリュームの置き換え（Replace root volume）」の機能も使える。

---

## 付録 B：init.d から systemd 方式への移行（パターンI の場合）

アップグレードの前後どちらかで実施する（同時にやると切り分けが難しくなるため、別作業が望ましい）。

```bash
spl stop
sudo "$SPLUNK_HOME/bin/splunk" disable boot-start
sudo "$SPLUNK_HOME/bin/splunk" enable boot-start -systemd-managed 1 -user "$SPLUNK_USER" -group "$SPLUNK_USER"
# splunk ユーザーが sudo なしで start/stop/restart できるようにする場合（polkit が必要）
# sudo dnf install -y polkit
# sudo "$SPLUNK_HOME/bin/splunk" enable boot-start -systemd-managed 1 -create-polkit-rules 1 -user "$SPLUNK_USER"
sudo systemctl daemon-reload
sudo systemctl start Splunkd
sudo systemctl status Splunkd --no-pager
```

---

## 付録 C：トラブル時の確認箇所

| 症状 | 確認箇所 |
|---|---|
| 起動しない | `$SPLUNK_HOME/var/log/splunk/splunkd.log`、`sudo journalctl -u Splunkd -n 200` |
| `Running as root is not allowed` など（10.2） | 実行ユーザーを確認。unit の `User=`、`splunk-launch.conf` の `SPLUNK_OS_USER` |
| ライセンス未同意で止まる | `--accept-license` を付けて splunk コマンドで一度起動する |
| KV store が起動しない | `mongod.log`、`splunkd.log`（`KVStore` で grep）、証明書、ディスク空き容量 |
| 移行内容の確認 | `$SPLUNK_HOME/var/log/splunk/migration.log.<timestamp>` |
| アプリのエラー | Splunk Web の「メッセージ」、`splunkd.log`（`ExecProcessor` / `python` で grep） |

---

## 参考資料
- How to upgrade Splunk Enterprise（10.2）: https://help.splunk.com/en/splunk-enterprise/administer/install-and-upgrade/10.2/upgrade-or-migrate-splunk-enterprise/how-to-upgrade-splunk-enterprise
- About upgrading to 10.2 READ THIS FIRST: https://help.splunk.com/en/splunk-enterprise/administer/install-and-upgrade/10.2/upgrade-or-migrate-splunk-enterprise/about-upgrading-to-10.2-read-this-first
- Upgrade to version 10.2 on UNIX: https://help.splunk.com/en/splunk-enterprise/administer/install-and-upgrade/10.2/upgrade-or-migrate-splunk-enterprise/upgrade-to-version-10.2-on-unix
- Upgrade the KV store server version（10.2）: https://help.splunk.com/en/data-management/splunk-enterprise-admin-manual/10.2/administer-the-app-key-value-store/upgrade-the-kv-store-server-version
- Migrate the KV store storage engine（9.3）: https://help.splunk.com/en/data-management/splunk-enterprise-admin-manual/9.3/administer-the-app-key-value-store/migrate-the-kv-store-storage-engine
- Run Splunk Enterprise as a systemd service: https://help.splunk.com/en/data-management/splunk-enterprise-admin-manual/9.4/start-splunk-enterprise-and-perform-initial-tasks/run-splunk-enterprise-as-a-systemd-service
- Splunk Enterprise 10.2.8 fixed issues: https://help.splunk.com/en/splunk-enterprise/release-notes-and-updates/release-notes/10.2/fixed-issues/fixed-issues/splunk-enterprise-10.2.8-fixed-issues
