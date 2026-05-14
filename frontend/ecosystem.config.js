module.exports = {
  apps: [
    {
      name: 'spir-frontend',
      script: 'node_modules/.bin/next',
      args: 'start',
      cwd: '/opt/spir_dynamic/frontend',

      instances: 1,
      exec_mode: 'fork',

      // Restart the process if it exceeds 512 MB RSS — Next.js can leak memory
      // on long-running servers when ISR or image caches grow unchecked.
      max_memory_restart: '512M',

      // Wait 3s before each automatic restart to avoid a crash-restart loop
      // hammering CPU on a 2-vCPU VPS.
      restart_delay: 3000,

      // Give up auto-restarting after 10 consecutive failures (prevents runaway
      // crash loops). PM2 marks the process as "errored" — requires manual restart.
      max_restarts: 10,

      // A process that crashes in under 10s counts as an unstable restart.
      min_uptime: '10s',

      watch: false,

      // Log paths — logrotate-spir.conf rotates these via copytruncate.
      // Run: sudo mkdir -p /var/log/pm2 && sudo chown spir:spir /var/log/pm2
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      error_file: '/var/log/pm2/spir-frontend-error.log',
      out_file: '/var/log/pm2/spir-frontend-out.log',
      merge_logs: true,

      env: {
        NODE_ENV: 'production',
        PORT: 3000,
      },
    },
  ],
};

// DEPLOY:
//   cd /opt/spir_dynamic/frontend
//   pm2 start /opt/spir_dynamic/frontend/ecosystem.config.js
//   pm2 save                              # persist across reboots
//   pm2 startup                           # generate systemd/init startup hook
//
// RESTART AFTER CODE CHANGE:
//   pm2 reload spir-frontend              # zero-downtime reload
//
// VIEW LOGS:
//   pm2 logs spir-frontend --lines 50
//   tail -f /var/log/pm2/spir-frontend-error.log
