/**
 * PM2 process config for the Job Hunter stack on a VPS.
 *
 *   pm2 start ecosystem.config.js
 *   pm2 save
 *   pm2 startup systemd   # run the printed command with sudo (survives reboot)
 *
 * Notes:
 *  - dashboard runs on port 3010 to avoid clashing with the existing
 *    `carbey` PM2 app (which uses :3000). Nginx proxies the public domain here.
 *  - monitor.py runs as a long-lived loop (POLL_INTERVAL_SECONDS in Hunter/.env);
 *    do NOT pass --once or it would exit and PM2 would restart-loop it.
 *  - Set COPY_TO_CLIPBOARD=0 in Hunter/.env (no GUI/clipboard on a server).
 *  - Secrets (DATABASE_URL, DISCORD webhooks, DASHBOARD_INGEST_SECRET) live in
 *    each component's .env, never here.
 */
module.exports = {
  apps: [
    {
      name: "jobhunter-dashboard",
      cwd: "/var/www/jobhunter/dashboard",
      script: "npm",
      args: "start", // -> next start
      env: {
        PORT: "3010",
        NODE_ENV: "production",
      },
      autorestart: true,
      max_restarts: 10,
      time: true, // timestamped logs
    },
    {
      name: "jobhunter-monitor",
      cwd: "/var/www/jobhunter/Hunter",
      // Use the venv interpreter directly so PM2 doesn't depend on shell activation.
      script: "/var/www/jobhunter/Hunter/.venv/bin/python",
      args: "monitor.py",
      interpreter: "none", // script is already an executable interpreter
      autorestart: true,
      max_restarts: 10,
      time: true,
    },
  ],
};
