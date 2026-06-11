# VPS デプロイ手順（PM2 + Nginx + Let's Encrypt）

Job Hunter は 2 コンポーネント構成です。

| コンポーネント | 中身 | VPS での実行 | ポート |
| --- | --- | --- | --- |
| `dashboard/` | Next.js 15 ダッシュボード | PM2 `npm start`（`next start`） | **3010**（:3000 は既存 `carbey` が使用中のため回避） |
| `Hunter/` | Python 3.12 スクレイパ常駐（`monitor.py`） | PM2（venv の python で常駐ループ） | — |

DB は **Neon（クラウド Postgres）** をそのまま使うため、VPS に DB は不要です。
公開は **Nginx リバースプロキシ + Let's Encrypt（HTTPS）**。

`<...>` は自分の値に置き換えてください（ドメイン・リポジトリURL）。

---

## 0. ランタイム導入（初回のみ）

```bash
# Node 20
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# Python 3.12 / Nginx / certbot / git
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv git nginx
sudo snap install --classic certbot && sudo ln -sf /snap/bin/certbot /usr/bin/certbot

# PM2
sudo npm install -g pm2
```

## 1. コード配置

```bash
sudo mkdir -p /var/www/jobhunter && sudo chown $USER:$USER /var/www/jobhunter
cd /var/www/jobhunter
git clone <リポジトリURL> .
```

> `.env`（dashboard・Hunter 両方）と `Hunter/lancers_seen.json` は git 管理外です。
> `scp` 等で安全に転送してください（Discord Webhook・DB認証・ingestシークレットを含む）。

## 2. dashboard ビルド

```bash
cd /var/www/jobhunter/dashboard
# .env を配置（下記キー参照）
npm ci                 # postinstall で prisma generate も実行
npx prisma db push     # Neon にスキーマ反映（既存なら no-op）
npm run build
```

`dashboard/.env`（最低限）:

```
DATABASE_URL="postgresql://...neon.../neondb?sslmode=require&channel_binding=require"
NEXT_PUBLIC_APP_URL="https://kaguyatop.dev"
DASHBOARD_INGEST=1
DASHBOARD_INGEST_SECRET=<強いランダム文字列。change-me-... のままにしない>
```

## 3. Hunter セットアップ

```bash
cd /var/www/jobhunter/Hunter
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
# .env を配置（下記キー参照）
```

`Hunter/.env`（要点）:

```
DASHBOARD_INGEST=1
DASHBOARD_INGEST_URL=https://kaguyatop.dev        # ← VPS のダッシュボードURL（末尾pathなし）
DASHBOARD_INGEST_SECRET=<dashboard と同一値>
COPY_TO_CLIPBOARD=0                            # サーバーは GUI なし。必須
# CROWDWORKS_URLS / *_DISCORD_WEBHOOK_URLS / LANCERS_* は既存値を維持
```

> 同一 VPS で Nginx 越しを使わず直接叩くなら
> `DASHBOARD_INGEST_URL=http://127.0.0.1:3010` でも可。

## 4. PM2 で常駐（dashboard:3010 + monitor）

リポジトリ直下の [`ecosystem.config.js`](ecosystem.config.js) を使用:

```bash
cd /var/www/jobhunter
pm2 start ecosystem.config.js
pm2 save
pm2 startup systemd     # 表示コマンドを sudo 実行 → 再起動後も自動復帰
```

確認:

```bash
pm2 status              # jobhunter-dashboard / jobhunter-monitor が online
pm2 logs jobhunter-monitor --lines 50
```

## 5. Nginx + HTTPS

```bash
# nginx-jobhunter.conf は server_name kaguyatop.dev で設定済み
sudo cp deploy/nginx-jobhunter.conf /etc/nginx/sites-available/jobhunter
sudo ln -s /etc/nginx/sites-available/jobhunter /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d kaguyatop.dev      # 443 + 自動更新を設定
```

ファイアウォール:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

## 6. 動作確認

```bash
curl -I https://kaguyatop.dev                       # 200
pm2 logs jobhunter-monitor | grep -i ingest      # 「Dashboard ingest OK」
```

ブラウザで `https://kaguyatop.dev` → Overview / Jobs / Clients / Settings が表示されれば完了。

---

## 更新（再デプロイ）

```bash
cd /var/www/jobhunter && git pull
cd dashboard && npm ci && npx prisma db push && npm run build
pm2 restart jobhunter-dashboard
# Hunter 側を変更したら:
pm2 restart jobhunter-monitor
```

## トラブルシュート

- **ポート衝突**: `ss -tlnp | grep ':3010'` で 3010 を使うのが jobhunter-dashboard だけか確認。:3000 は `carbey` 専用のまま。
- **502 Bad Gateway**: dashboard が起動しているか `pm2 logs jobhunter-dashboard`。`PORT=3010` と Nginx の `proxy_pass` が一致しているか。
- **ingest 401/404**: dashboard と Hunter の `DASHBOARD_INGEST_SECRET` 一致、`DASHBOARD_INGEST_URL` が末尾 path なしの正しい origin か。
- **monitor が再起動ループ**: `COPY_TO_CLIPBOARD=0` 未設定の可能性。`.env` を確認。
